# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

__all__ = [
    "AG2AgentRequest",
    "AgentConfig",
    "run_beta_agent",
]


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


async def run_beta_agent(
    config: AgentConfig | None,
    message: str,
    llm_config: Any,
    tool_registry: dict[str, Callable] | None = None,
    agent: Any | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Run an AG2 beta Agent and yield events from its stream.

    Subscribes to the MemoryStream to yield intermediate events
    (tool calls, model chunks) as they arrive, then yields the
    final response with optional typed result from response_schema.

    If ``agent`` is provided it is used directly and ``config`` is ignored
    (no Agent construction happens).  When both are supplied, ``agent``
    wins and a log message is emitted noting that ``config`` was skipped.
    When neither is supplied, ``config`` must be non-None and is used to
    construct a fresh Agent on every call (original behavior).
    """
    tool_registry = tool_registry or {}

    if agent is not None:
        if config is not None:
            logger.info(
                "run_beta_agent: pre-built agent provided; agent_config is ignored."
            )
        # Use the caller-supplied agent as-is.
        # AG2 stream imports still needed for the subscription machinery below.
    else:
        # Config-driven path: build a fresh Agent from AgentConfig.
        if config is None:
            raise ValueError(
                "run_beta_agent requires either a pre-built 'agent' or a non-None 'config'."
            )
        from autogen.beta.agent import Agent, KnowledgeConfig, TaskConfig
        from autogen.beta.knowledge.memory import MemoryKnowledgeStore
        from autogen.beta.tools.final import tool as ag2_tool

        agent_kwargs: dict[str, Any] = {
            "name": config.name,
            "prompt": (
                config.prompt if isinstance(config.prompt, list) else [config.prompt]
            ),
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
            content = " ".join(
                getattr(p, "content", str(p)) for p in event.result.parts
            )
        await event_queue.put(
            {
                "type": "tool_result",
                "name": event.name,
                "parent_id": event.parent_id,
                "content": content,
            }
        )

    from autogen.beta.events.conditions import TypeCondition

    sub_tools = stream.subscribe(
        _on_tool_calls, condition=TypeCondition(ToolCallsEvent)
    )
    sub_results = stream.subscribe(
        _on_tool_result, condition=TypeCondition(ToolResultEvent)
    )

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
            content = getattr(
                reply.response.message, "content", str(reply.response.message)
            )

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
            # Await the cancellation so we don't return with a dangling
            # background task — leaks the event loop close just like the
            # rate-limit replenisher did pre-R4. Suppress both the
            # intentional CancelledError and any error from ``agent.ask``
            # that the consumer no longer wants to see.
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001 — intentional teardown reap
                pass
