# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import heapq
import logging
import os
import random
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Type

import numpy as np
import ray
import torch
from cachetools import LRUCache
from omegaconf import DictConfig
from pydantic import BaseModel
from tensordict import TensorDict
from transformers import AutoTokenizer

from siirl import DataProto
from siirl.models.loader import load_tokenizer
from siirl.utils.extras.fs import copy_to_local
from siirl.workers.rollout.async_server import async_server_class
from loguru import logger
logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("siirl_LOGGING_LEVEL", "WARN"))

async def get_ddp_world_size_rank(local_world_size, local_rank, local_parallel_size):
    
    ddp_world_size = local_world_size // local_parallel_size
    ddp_rank = local_rank // local_parallel_size
    return ddp_world_size, ddp_rank



class AsyncLLMServerManager:
    """
    A class to manage multiple OpenAI compatible LLM servers. This class provides
    - Load balance: least requests load balancing
    - Sticky session: send multi-turn chat completions to same server for automatic prefix caching
    """

    def __init__(self, config: DictConfig, server_handles: List[ray.actor.ActorHandle], max_cache_size: int = 10000):
        """Initialize the AsyncLLMServerManager.

        Args:
            config (DictConfig): YAML config.
            server_handles (List[ray.actor.ActorHandle]): OpenAI compatible LLM server actor handles.
            max_cache_size (int, optional): max cache size for request_id to server mapping. Defaults to 10000.
        """
        self.config = config
        self.server_handles = server_handles
        random.shuffle(self.server_handles)

        # Least requests load balancing
        self.weighted_serveres = [[0, (hash(server), server)] for server in server_handles]
        heapq.heapify(self.weighted_serveres)

        # LRU cache to map request_id to server
        self.request_id_to_server = LRUCache(maxsize=max_cache_size)

    def _choose_server(self, request_id: str) -> ray.actor.ActorHandle:
        # TODO: implement server pressure awareness load balancing
        if request_id in self.request_id_to_server:
            return self.request_id_to_server[request_id]

        server = self.weighted_serveres[0][1][1]
        self.weighted_serveres[0][0] += 1
        heapq.heapreplace(self.weighted_serveres, self.weighted_serveres[0])
        self.request_id_to_server[request_id] = server
        return server

    async def generate(
        self,
        request_id,
        *,
        prompt_ids: List[int],
        sampling_params: Dict[str, Any],
    ) -> List[int]:
        """Generate tokens from prompt ids.

        Args:
            request_id (str): request id for sticky session.
            prompt_ids (List[int]): List of prompt token ids.
            sampling_params (Dict[str, Any]): Sampling parameters for the chat completion.

        Returns:
            List[int]: List of generated token ids.
        """
        server = self._choose_server(request_id)
        output = await server.generate.remote(
            request_id=request_id,
            prompt_ids=prompt_ids,
            sampling_params=sampling_params,
        )
        return output


class AgentLoopMetrics(BaseModel):
    """Agent loop performance metrics."""

    generate_sequences: float = 0.0
    tool_calls: float = 0.0


class AgentLoopOutput(BaseModel):
    """Agent loop output."""

    prompt_ids: List[int]
    response_ids: List[int]
    response_mask: List[int]
    num_turns: int = 0
    metrics: AgentLoopMetrics


class AgentLoopBase(ABC):
    """An agent loop takes a input message, chat with OpenAI compatible LLM server and interact with various
    environments."""

    _class_initialized = False

    def __init__(self, config: DictConfig, server_manager: AsyncLLMServerManager, tokenizer: AutoTokenizer):
        """Initialize agent loop.

        Args:
            config (DictConfig): YAML config.
            server_manager (AsyncLLMServerManager): OpenAI compatible LLM server manager.
            tokenizer (AutoTokenizer): Tokenizer for tokenize messages.
        """
        self.config = config
        self.server_manager = server_manager
        self.tokenizer = tokenizer
        self.loop = asyncio.get_running_loop()
        self.init_class(config, tokenizer)

    @classmethod
    def init_class(cls, config: DictConfig, tokenizer: AutoTokenizer):
        """Initialize class state shared across all instances."""
        if cls._class_initialized:
            return
        cls._class_initialized = True

    @abstractmethod
    async def run(self, messages: List[Dict[str, Any]], sampling_params: Dict[str, Any]) -> AgentLoopOutput:
        """Run agent loop to interact with LLM server and environment.

        Args:
            messages (List[Dict[str, Any]]): Input messages.
            sampling_params (Dict[str, Any]): LLM sampling params.

        Returns:
            AgentLoopOutput: Agent loop output.
        """
        raise NotImplementedError


@ray.remote
class AgentLoopWorker:
    """Agent loop worker takes a batch of messages and run each message in an agent loop."""

    def __init__(self, config: DictConfig, server_handles: List[ray.actor.ActorHandle]):
        """Initialize agent loop manager.

        Args:
            config (DictConfig): YAML config.
            server_handles (List[ray.actor.ActorHandle]): OpenAI compatible LLM server actor handles.
        """
        self.config = config
        self.server_manager = AsyncLLMServerManager(config, server_handles)
        model_path = config.model.path
        self.model_name = "/".join(model_path.split("/")[-2:])
        local_path = copy_to_local(config.model.path)
        tokenizer_module = load_tokenizer(model_args=self.config.model)
        self.tokenizer, self.processor = tokenizer_module["tokenizer"], tokenizer_module["processor"]

    async def generate_sequences(self, batch: DataProto) -> DataProto:
        """Generate sequences from agent loop.

        Args:
            batch (DataProto): Input batch.

        Returns:
            DataProto: Output batch.
            - prompts: [bsz, prompt_length], prompt token ids from dataset.
            - responses: [bsz, response_length], output token ids include response tokens
              from LLM generation and observation tokens from tool_calls.
            - response_mask: [bsz, response_length], 1 for LLM generated tokens, 0 for observation/padding tokens.
            - input_ids: [bsz, prompt_length + response_length], whole sequence token ids, including prompt tokens
              and response tokens.
            - attention_mask: [bsz, prompt_length + response_length], 0 for padding tokens, 1 for other tokens.
            - position_ids: [bsz, prompt_length + response_length], incremental position ids.

            For multi-turn conversations:
            responses:     |<- LLM generation ->|<- tool_calls ->|<- LLM generation ->|<- padding ->|
            response_mask: | 1, 1, 1, ..., 1, 1 | 0, 0, .., 0, 0 | 1, 1, 1, ..., 1, 1 | 0, 0, ..., 0|
        """
        config = self.config.rollout
        sampling_params = dict(
            temperature=config.temperature,
            top_p=config.top_p,
            repetition_penalty=1.0,
        )

        # override sampling params for validation
        if batch.meta_info.get("validate", False):
            sampling_params["top_p"] = config.val_kwargs.top_p
            sampling_params["temperature"] = config.val_kwargs.temperature

        n = 1 if batch.meta_info.get("validate", False) else config.n
        tasks = []
        # by default, we assume it's a single turn agent
        agent_name = self.config.rollout.agent.agent_name
        
        # agent_names = batch.non_tensor_batch["agent_name"].repeat(n, axis=0)
        raw_prompts = batch.non_tensor_batch["raw_prompt"].repeat(n, axis=0)
        target_size = raw_prompts.shape[0]
        agent_names = np.full(target_size, agent_name)
        
        for agent_name, messages in zip(agent_names, raw_prompts):
            tasks.append(asyncio.create_task(self._run_agent_loop(agent_name, messages.tolist(), sampling_params)))
        outputs = await asyncio.gather(*tasks)

        output = self._postprocess(outputs)
        return output

    async def _run_agent_loop(
        self, agent_name: str, messages: List[Dict[str, Any]], sampling_params: Dict[str, Any]
    ) -> AgentLoopOutput:
        agent_loop_class = self.get_agent_loop_class(agent_name)
        agent_loop = agent_loop_class(self.config, self.server_manager, self.tokenizer)
        output = await agent_loop.run(messages, sampling_params)
        return output

    def get_agent_loop_class(self, agent_name: str) -> Type[AgentLoopBase]:
        # TODO: add tool agent registrary
        from siirl.multiturn.agent_loop.single_turn_agent_loop import SingleTurnAgentLoop
        from siirl.multiturn.agent_loop.tool_agent_loop import ToolAgentLoop

        if agent_name == "single_turn_agent":
            return SingleTurnAgentLoop
        elif agent_name == "tool_agent":
            return ToolAgentLoop
        raise ValueError(f"Unknown agent_name: {agent_name}")

    def _postprocess(self, inputs: List[AgentLoopOutput]) -> DataProto:
        # NOTE: consistent with batch version of generate_sequences in vllm_rollout_spmd.py
        # prompts: left pad
        # responses: right pad
        # input_ids: prompt + response
        # attention_mask: [0,0,0,0,1,1,1,1, | 1,1,1,0,0,0,0,0]
        # position_ids:   [0,0,0,0,0,1,2,3, | 4,5,6,7,8,9,10,11]

        # prompts
        self.tokenizer.padding_side = "left"
        outputs = self.tokenizer.pad(
            [{"input_ids": input.prompt_ids} for input in inputs],
            padding="max_length",
            max_length=self.config.rollout.prompt_length,
            return_tensors="pt",
            return_attention_mask=True,
        )
        prompt_ids, prompt_attention_mask = outputs["input_ids"], outputs["attention_mask"]

        # responses
        self.tokenizer.padding_side = "right"
        outputs = self.tokenizer.pad(
            [{"input_ids": input.response_ids} for input in inputs],
            padding="max_length",
            max_length=self.config.rollout.response_length,
            return_tensors="pt",
            return_attention_mask=True,
        )
        response_ids, response_attention_mask = outputs["input_ids"], outputs["attention_mask"]

        # response_mask
        outputs = self.tokenizer.pad(
            [{"input_ids": input.response_mask} for input in inputs],
            padding="max_length",
            max_length=self.config.rollout.response_length,
            return_tensors="pt",
            return_attention_mask=False,
        )
        response_mask = outputs["input_ids"]
        assert response_ids.shape == response_mask.shape, (
            f"mismatch in response_ids and response_mask shape: {response_ids.shape} vs {response_mask.shape}"
        )
        response_mask = response_mask * response_attention_mask

        input_ids = torch.cat([prompt_ids, response_ids], dim=1)
        attention_mask = torch.cat([prompt_attention_mask, response_attention_mask], dim=1)
        position_ids = (attention_mask.cumsum(dim=1) - 1) * attention_mask

        batch = TensorDict(
            {
                "prompts": prompt_ids,  # [bsz, prompt_length]
                "responses": response_ids,  # [bsz, response_length]
                "response_mask": response_mask,  # [bsz, response_length]
                "input_ids": input_ids,  # [bsz, prompt_length + response_length]
                "attention_mask": attention_mask,  # [bsz, prompt_length + response_length]
                "position_ids": position_ids,  # [bsz, prompt_length + response_length]
            },
            batch_size=len(input_ids),
        )

        num_turns = np.array([input.num_turns for input in inputs], dtype=np.int32)
        metrics = [input.metrics.model_dump() for input in inputs]
        return DataProto(batch=batch, non_tensor_batch={"__num_turns__": num_turns}, meta_info={"metrics": metrics})


class AgentLoopManager:
    """Agent loop manager that manages a group of agent loop workers."""

    def __init__(self, config: DictConfig, cur_dp_size, cur_dp_rank, name_prefix):
        """Initialize agent loop manager.

        Args:
            config (DictConfig): trainer config.
            worker_group (RayWorkerGroup): ActorRolloutRef worker group.
        """
        self.config = config
        self.cur_dp_size = cur_dp_size
        self.cur_dp_rank = cur_dp_rank
        self.name_prefix = name_prefix
        
    async def init_model(self):
        await self._initialize_llm_servers()
        await self._init_agent_loop_workers()

    async def _initialize_llm_servers(self):
        self.rollout_tp_size = self.config.rollout.tensor_model_parallel_size
        # self.rollout_dp_size = self.worker_group.world_size // self.rollout_tp_size
        # in siirl, every dp has private rollout engine
        self.rollout_dp_size = 1
        register_center = ray.get_actor(f"{self.name_prefix}_register_center")
        workers_info = ray.get(register_center.get_worker_info.remote())
        rank = self.cur_dp_rank * self.rollout_tp_size 
        ddp_rank = self.cur_dp_rank
        self.async_llm_servers = []


        if self.config.rollout.agent.custom_async_server:
            server_class = async_server_class(
                rollout_backend=self.config.rollout.name,
                rollout_backend_module=self.config.rollout.agent.custom_async_server.path,
                rollout_backend_class=self.config.rollout.agent.custom_async_server.name,
            )
        else:
            server_class = async_server_class(rollout_backend=self.config.rollout.name)

        # Start all server instances, restart if address already in use.
        unready_dp_ranks = set([ddp_rank])
        while len(unready_dp_ranks) > 0:
            servers = {
                rollout_dp_rank: server_class.options(
                    # make sure AsyncvLLMServer colocates with its corresponding workers
                    scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                        node_id=workers_info[rollout_dp_rank * self.rollout_tp_size],
                        soft=False,
                    ),
                    name=f"async_llm_server_{rollout_dp_rank}",
                ).remote(self.config, rank, self.name_prefix)
                for rollout_dp_rank in unready_dp_ranks
            }
            
            for rollout_dp_rank, server in servers.items():
                self.async_llm_servers.append(server)
                unready_dp_ranks.remove(rollout_dp_rank)
        # All server instances are ready, init AsyncLLM engine.
        futures = [server.init_engine.remote() for server in self.async_llm_servers]
        await asyncio.gather(*futures)


    async def _init_agent_loop_workers(self):
        if self.config.rollout.agent.num_workers != 1:
            self.config.rollout.agent.num_workers = 1
            logger.warning("only support agent has one num_workers")
            
        self.agent_loop_workers = AgentLoopWorker.options(
            name=f"agent_loop_worker_{self.cur_dp_rank}",
        ).remote(self.config, self.async_llm_servers)
            

    def generate_sequences(self, prompts: DataProto) -> DataProto:
        """Split input batch and dispatch to agent loop workers.

        Args:
            prompts (DataProto): Input batch.

        Returns:
            DataProto: Output batch.
        """
        if self.config.rollout.free_cache_engine:
            self.wake_up()
        outputs = ray.get(
            [self.agent_loop_workers.generate_sequences.remote(prompts)]
        )
        output = DataProto.concat(outputs)
        if self.config.rollout.free_cache_engine:
            self.sleep()

        # calculate performance metrics
        metrics = [output.meta_info["metrics"] for output in outputs]  # List[List[Dict[str, str]]]
        timing = self._performance_metrics(metrics, output)

        output.meta_info = {"timing": timing}
        return output

    def _performance_metrics(self, metrics: List[List[Dict[str, str]]], output: DataProto) -> Dict[str, float]:
        timing = {}
        t_generate_sequences = np.array([metric["generate_sequences"] for chunk in metrics for metric in chunk])
        t_tool_calls = np.array([metric["tool_calls"] for chunk in metrics for metric in chunk])
        timing["agent_loop/generate_sequences/min"] = t_generate_sequences.min()
        timing["agent_loop/generate_sequences/max"] = t_generate_sequences.max()
        timing["agent_loop/generate_sequences/mean"] = t_generate_sequences.mean()
        timing["agent_loop/tool_calls/min"] = t_tool_calls.min()
        timing["agent_loop/tool_calls/max"] = t_tool_calls.max()
        timing["agent_loop/tool_calls/mean"] = t_tool_calls.mean()

        # batch sequence generation is bounded by the slowest sample
        slowest = np.argmax(t_generate_sequences + t_tool_calls)
        attention_mask = output.batch["attention_mask"][slowest]
        prompt_length = output.batch["prompts"].shape[1]
        timing["agent_loop/slowest/generate_sequences"] = t_generate_sequences[slowest]
        timing["agent_loop/slowest/tool_calls"] = t_tool_calls[slowest]
        timing["agent_loop/slowest/prompt_length"] = attention_mask[:prompt_length].sum().item()
        timing["agent_loop/slowest/response_length"] = attention_mask[prompt_length:].sum().item()

        return timing

    def wake_up(self):
        """Wake up all rollout server instances."""
        ray.get([server.wake_up.remote() for server in self.async_llm_servers])

    def sleep(self):
        """Sleep all rollout server instances."""
        ray.get([server.sleep.remote() for server in self.async_llm_servers])
