# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AG2 beta Agent endpoint: wraps autogen.beta.Agent and streams events as StreamChunks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections import AgenticEndpoint, EndpointConfig
from lionagi.service.types import StreamChunk
from lionagi.utils import to_dict

from ._config import AG2Configs

logger = logging.getLogger(__name__)

__all__ = ["AG2AgentRequest", "AgentConfig", "run_beta_agent", "AG2BetaEndpoint"]


class AgentConfig(BaseModel):
    """Declarative config for an AG2 beta Agent."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(default="agent", description="Agent name")
    prompt: str | list[str] = Field(
        default="You are a helpful assistant.",
        description="System prompt(s)",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Tool names from the registry",
    )
    enable_subtasks: bool = Field(
        default=False,
        description="Enable run_subtask / run_subtasks tools",
    )
    knowledge: bool = Field(
        default=False,
        description="Enable MemoryKnowledgeStore",
    )
    observers: list[str] = Field(
        default_factory=list,
        description="Observer names: 'loop_detector', 'token_monitor'",
    )
    policies: list[str] = Field(
        default_factory=list,
        description="Policy names: 'sliding_window', 'token_budget', "
        "'working_memory', 'episodic_memory', 'alert', 'conversation'",
    )
    response_schema: type[BaseModel] | None = Field(
        default=None,
        description="Pydantic model for structured output",
    )


class AG2AgentRequest(BaseModel):
    """Request for AG2 beta Agent endpoint."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[dict[str, Any]] = Field(default_factory=list)
    prompt: str = ""
    agent_config: AgentConfig | None = None
    agent: Any | None = Field(
        default=None,
        description=(
            "Pre-built autogen.beta.Agent instance. "
            "If provided, overrides agent_config — agent_config is ignored. "
            "Use this to reuse an agent with expensive init (tools, knowledge, "
            "observers) across multiple stream() calls."
        ),
    )


def _build_observers(names: list[str]) -> list:
    observers = []
    for name in names:
        if name == "loop_detector":
            from autogen.beta.observer.loop_detector import LoopDetector

            observers.append(LoopDetector())
        elif name == "token_monitor":
            from autogen.beta.observer.token_monitor import TokenMonitor

            observers.append(TokenMonitor())
        else:
            logger.warning("Unknown observer: %r — skipped", name)
    return observers


def _build_policies(names: list[str]) -> list:
    policies = []
    for name in names:
        if name == "sliding_window":
            from autogen.beta.policies.sliding_window import SlidingWindowPolicy

            policies.append(SlidingWindowPolicy(max_events=20))
        elif name == "token_budget":
            from autogen.beta.policies.token_budget import TokenBudgetPolicy

            policies.append(TokenBudgetPolicy(max_tokens=8000))
        elif name == "working_memory":
            from autogen.beta.policies.working_memory import WorkingMemoryPolicy

            policies.append(WorkingMemoryPolicy())
        elif name == "episodic_memory":
            from autogen.beta.policies.episodic_memory import EpisodicMemoryPolicy

            policies.append(EpisodicMemoryPolicy())
        elif name == "alert":
            from autogen.beta.policies.alert import AlertPolicy

            policies.append(AlertPolicy())
        elif name == "conversation":
            from autogen.beta.policies.conversation import ConversationPolicy

            policies.append(ConversationPolicy())
        else:
            logger.warning("Unknown policy: %r — skipped", name)
    return policies


# TODO(#1043 Phase 2): migrate create_task + Queue + wait_for to anyio primitives
async def run_beta_agent(
    config: AgentConfig | None,
    message: str,
    llm_config: Any,
    tool_registry: dict[str, Callable] | None = None,
    agent: Any | None = None,
) -> AsyncGenerator[dict[str, Any]]:
    """Run an AG2 beta Agent and yield tool/response events; pre-built agent takes precedence over config."""
    tool_registry = tool_registry or {}

    if agent is not None:
        if config is not None:
            logger.info("run_beta_agent: pre-built agent provided; agent_config is ignored.")
    else:
        if config is None:
            raise ValueError(
                "run_beta_agent requires either a pre-built 'agent' or a non-None 'config'."
            )
        from autogen.beta.agent import Agent, KnowledgeConfig, TaskConfig
        from autogen.beta.knowledge.memory import MemoryKnowledgeStore
        from autogen.beta.tools.final import tool as ag2_tool

        agent_kwargs: dict[str, Any] = {
            "name": config.name,
            "prompt": (config.prompt if isinstance(config.prompt, list) else [config.prompt]),
            "config": llm_config,
        }

        ag2_tools = []
        for tool_name in config.tools:
            if tool_name in tool_registry:
                fn = tool_registry[tool_name]
                wrapped = ag2_tool(
                    fn,
                    name=tool_name,
                    description=getattr(fn, "__doc__", "") or tool_name,
                )
                ag2_tools.append(wrapped)
        if ag2_tools:
            agent_kwargs["tools"] = ag2_tools

        observers = _build_observers(config.observers)
        if observers:
            agent_kwargs["observers"] = observers

        policies = _build_policies(config.policies)
        if policies:
            agent_kwargs["assembly"] = policies

        if config.knowledge:
            from autogen.beta.knowledge.memory import MemoryKnowledgeStore

            agent_kwargs["knowledge"] = KnowledgeConfig(store=MemoryKnowledgeStore())

        if config.enable_subtasks:
            agent_kwargs["tasks"] = TaskConfig()

        if config.response_schema:
            agent_kwargs["response_schema"] = config.response_schema

        agent = Agent(**agent_kwargs)

    # AG2 stream/event imports are deferred until after the ValueError guard
    # so that tests can catch the ValueError without needing autogen installed.
    from autogen.beta.events.tool_events import ToolCallsEvent, ToolResultEvent
    from autogen.beta.stream import MemoryStream

    stream = MemoryStream()
    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _on_tool_calls(event: ToolCallsEvent) -> None:
        for call in event.calls:
            await event_queue.put(
                {
                    "type": "tool_use",
                    "name": call.name,
                    "id": call.id,
                    "arguments": call.arguments,
                }
            )

    async def _on_tool_result(event: ToolResultEvent) -> None:
        content = ""
        if event.result and event.result.parts:
            content = " ".join(getattr(p, "content", str(p)) for p in event.result.parts)
        await event_queue.put(
            {
                "type": "tool_result",
                "name": event.name,
                "parent_id": event.parent_id,
                "content": content,
            }
        )

    from autogen.beta.events.conditions import TypeCondition

    sub_tools = stream.subscribe(_on_tool_calls, condition=TypeCondition(ToolCallsEvent))
    sub_results = stream.subscribe(_on_tool_result, condition=TypeCondition(ToolResultEvent))

    async def _run_agent():
        return await agent.ask(message, stream=stream)

    task = asyncio.create_task(_run_agent())

    try:
        while not task.done():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                continue

        while not event_queue.empty():
            yield event_queue.get_nowait()

        reply = task.result()

        typed_result = None
        response_schema = config.response_schema if config is not None else None
        if response_schema:
            try:
                typed_result = await reply.content(retries=1)
            except Exception:
                logger.warning("Schema validation failed, falling back to raw content")

        content = ""
        if reply.response and reply.response.message:
            content = getattr(reply.response.message, "content", str(reply.response.message))

        yield {
            "type": "response",
            "content": reply.response,
            "text": content,
            "typed_result": typed_result,
            "usage": reply.response.usage if reply.response else None,
            "stream": stream,
        }

    finally:
        stream.unsubscribe(sub_tools)
        stream.unsubscribe(sub_results)
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001 — intentional teardown reap
                pass


logger = logging.getLogger(__name__)


@AG2Configs.AGENT.register
class AG2BetaEndpoint(AgenticEndpoint):
    """Wraps AG2 beta Agent as a lionagi agentic endpoint; stream-only."""

    DEFAULT_CONCURRENCY_LIMIT = 1
    DEFAULT_QUEUE_CAPACITY = 3

    def __init__(self, config: EndpointConfig | None = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self._agent_config: dict[str, Any] = kwargs.get("agent_config", {})
        self._llm_config: Any = kwargs.get("llm_config", None)
        self._tool_registry: dict[str, Any] = kwargs.get("tool_registry", {})

    async def _call(self, payload, headers, **kwargs):
        raise NotImplementedError("AG2 beta Agent is stream-only. Use stream() to iterate events.")

    def create_payload(self, request: dict | BaseModel, **kwargs):

        req_dict = {**self.config.kwargs, **to_dict(request), **kwargs}
        messages = req_dict.pop("messages", [])
        prompt = req_dict.pop("prompt", "")
        agent_config = req_dict.pop("agent_config", None)
        agent = req_dict.pop("agent", None)
        return {
            "request": AG2AgentRequest(
                messages=messages,
                prompt=prompt,
                agent_config=agent_config,
                agent=agent,
            )
        }, {}

    async def stream(self, request: dict | BaseModel, **kwargs) -> AsyncIterator[StreamChunk]:

        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]

        prompt = request_obj.prompt or (
            request_obj.messages[-1]["content"] if request_obj.messages else ""
        )
        if not prompt:
            raise ValueError("AG2BetaEndpoint requires a non-empty prompt or at least one message.")

        # Pre-built agent takes precedence over agent_config.
        prebuilt_agent = request_obj.agent or kwargs.get("agent")

        if prebuilt_agent is None:
            agent_config = request_obj.agent_config
            if agent_config is None:
                agent_config = AgentConfig(**kwargs.get("agent_config", self._agent_config))
        else:
            agent_config = request_obj.agent_config  # pre-built path: may be None, metadata only

        llm_config = kwargs.get("llm_config", self._llm_config)
        tool_registry = kwargs.get("tool_registry", self._tool_registry)

        if llm_config is None and prebuilt_agent is None:
            raise ValueError("AG2BetaEndpoint requires llm_config")

        model_config = _resolve_model_config(llm_config) if llm_config is not None else None

        # Pre-built agent: config fields aren't authoritative; surface what we know.
        if prebuilt_agent is not None:
            agent_name = getattr(prebuilt_agent, "name", "pre-built")
            system_meta: dict = {
                "provider": "ag2",
                "api": "beta",
                "agent": agent_name,
                "pre_built": True,
            }
        else:
            system_meta = {
                "provider": "ag2",
                "api": "beta",
                "agent": agent_config.name,
                "tools": agent_config.tools,
                "observers": agent_config.observers,
                "policies": agent_config.policies,
            }

        yield StreamChunk(type="system", metadata=system_meta)

        _agent_name = system_meta["agent"]

        try:
            async for event in run_beta_agent(
                config=agent_config,
                message=prompt,
                llm_config=model_config,
                tool_registry=tool_registry,
                agent=prebuilt_agent,
            ):
                etype = event.get("type")

                if etype == "tool_use":
                    yield StreamChunk(
                        type="tool_use",
                        tool_name=event.get("name"),
                        tool_id=event.get("id"),
                        tool_input=event.get("arguments"),
                        metadata={"agent": _agent_name},
                    )

                elif etype == "tool_result":
                    yield StreamChunk(
                        type="tool_result",
                        tool_output=event.get("content"),
                        metadata={
                            "agent": _agent_name,
                            "tool_name": event.get("name"),
                        },
                    )

                elif etype == "response":
                    content = event.get("text", "")
                    typed_result = event.get("typed_result")

                    yield StreamChunk(
                        type="text",
                        content=content,
                        metadata={
                            "agent": _agent_name,
                            "typed_result": typed_result,
                        },
                    )

        except Exception:
            logger.exception("AG2 beta Agent execution failed")
            raise

        yield StreamChunk(
            type="result",
            content="Agent complete",
            metadata={"agent": _agent_name},
        )


def _resolve_model_config(llm_config: Any) -> Any:
    """Convert a dict llm_config to an AG2 beta ModelConfig."""
    if not isinstance(llm_config, dict):
        return llm_config

    api_type = llm_config.get("api_type", "openai")
    model = llm_config.get("model", "gpt-4o-mini")
    api_key = llm_config.get("api_key")
    temperature = llm_config.get("temperature")

    kwargs = {"model": model}
    if api_key:
        kwargs["api_key"] = api_key
    if temperature is not None:
        kwargs["temperature"] = temperature

    if api_type == "openai":
        from autogen.beta.config.openai.config import OpenAIConfig

        return OpenAIConfig(**kwargs)

    elif api_type == "anthropic":
        from autogen.beta.config.anthropic.config import AnthropicConfig

        return AnthropicConfig(**kwargs)

    elif api_type == "gemini":
        from autogen.beta.config.gemini.config import GeminiConfig

        kwargs.pop("temperature", None)
        return GeminiConfig(**kwargs)

    elif api_type == "ollama":
        from autogen.beta.config.ollama.config import OllamaConfig

        kwargs.pop("api_key", None)
        kwargs.pop("temperature", None)
        return OllamaConfig(**kwargs)

    raise ValueError(f"Unknown api_type: {api_type}")
