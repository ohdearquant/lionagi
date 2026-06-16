# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AG2 GroupChat endpoint: delegates to build_group_chat/stream_group_chat and converts events to StreamChunk."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from lionagi.service.connections import AgenticEndpoint, EndpointConfig
from lionagi.service.types import StreamChunk
from lionagi.utils import to_dict

from .._config import AG2Configs

logger = logging.getLogger(__name__)


@AG2Configs.GROUP_CHAT.register
class AG2GroupChatEndpoint(AgenticEndpoint):
    """Wraps AG2 v0.12 GroupChat as a lionagi agentic endpoint; stream-only."""

    DEFAULT_CONCURRENCY_LIMIT = 1
    DEFAULT_QUEUE_CAPACITY = 3

    def __init__(self, config: EndpointConfig | None = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self._agent_configs: list[dict[str, Any]] = kwargs.get("agent_configs", [])
        self._llm_config: dict[str, Any] = kwargs.get("llm_config", {})
        self._tool_registry: dict[str, Any] = kwargs.get("tool_registry", {})

    async def _call(self, payload, headers, **kwargs):
        raise NotImplementedError("AG2 GroupChat is stream-only. Use stream() to iterate events.")

    def create_payload(self, request: dict | BaseModel, **kwargs):
        from .models import AG2GroupChatRequest

        req_dict = {**self.config.kwargs, **to_dict(request), **kwargs}
        messages = req_dict.pop("messages", [])
        prompt = req_dict.pop("prompt", "")
        max_round = req_dict.pop("max_round", 15)
        ctx = req_dict.pop("context_variables", {})
        return {
            "request": AG2GroupChatRequest(
                messages=messages,
                prompt=prompt,
                max_round=max_round,
                context_variables=ctx,
            )
        }, {}

    async def stream(self, request: dict | BaseModel, **kwargs) -> AsyncIterator[StreamChunk]:
        from .models import GroupChatSpec, build_group_chat, stream_group_chat

        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]

        prompt = request_obj.prompt or (
            request_obj.messages[-1]["content"] if request_obj.messages else ""
        )
        if not prompt:
            raise ValueError(
                "AG2GroupChatEndpoint requires a non-empty prompt or at least one message."
            )

        agent_configs = kwargs.get("agent_configs", self._agent_configs)
        llm_config = kwargs.get("llm_config", self._llm_config)
        tool_registry = kwargs.get("tool_registry", self._tool_registry)
        code_executor = kwargs.get("code_executor")

        spec = GroupChatSpec(
            name="endpoint_chat",
            objective=prompt,
            agents=[
                {
                    "name": c["name"],
                    "role": c.get("role", ""),
                    "system_message": c.get("system_message", ""),
                    "tools": c.get("tools", []),
                    "handoffs": [
                        {"target": h["target"], "condition": h["condition"]}
                        for h in c.get("handoff_conditions", c.get("handoffs", []))
                    ],
                    "nlip_url": c.get("nlip_url"),
                    "state_template": c.get("state_template"),
                }
                for c in agent_configs
            ],
            context=request_obj.context_variables,
            max_round=request_obj.max_round,
        )

        user, pattern, agents_by_name = build_group_chat(
            spec, llm_config, tool_registry, code_executor
        )

        yield StreamChunk(
            type="system",
            metadata={
                "provider": "ag2",
                "api": "v0.12",
                "pattern": "DefaultPattern",
                "agent_count": len(agent_configs),
                "max_round": request_obj.max_round,
            },
        )

        async for event in stream_group_chat(
            pattern=pattern,
            prompt=prompt,
            max_rounds=request_obj.max_round,
        ):
            chunk = _event_to_chunk(event)
            if chunk:
                yield chunk

        yield StreamChunk(
            type="result",
            content="GroupChat complete",
            metadata={"agents": list(agents_by_name.keys())},
        )


def _event_to_chunk(event) -> StreamChunk | None:
    """Convert an AG2 wrapped event to a StreamChunk; returns None for unrecognized event types."""
    from autogen.events.agent_events import (
        GroupChatRunChatEvent,
        SelectSpeakerEvent,
        TextEvent,
        ToolCallEvent,
        ToolResponseEvent,
    )

    inner = getattr(event, "content", None)

    if isinstance(event, TextEvent):
        text = getattr(inner, "content", str(event)) if inner is not None else str(event)
        sender = getattr(inner, "sender", "unknown") if inner is not None else "unknown"
        return StreamChunk(
            type="text",
            content=text,
            metadata={"agent": sender},
        )
    if isinstance(event, GroupChatRunChatEvent):
        speaker = getattr(inner, "speaker", "unknown") if inner is not None else "unknown"
        return StreamChunk(
            type="system",
            content=f"Speaker: {speaker}",
            metadata={"event": "speaker_turn", "agent": speaker},
        )
    if isinstance(event, SelectSpeakerEvent):
        agents = getattr(inner, "agents", []) if inner is not None else []
        if agents:
            names = ", ".join(getattr(a, "name", str(a)) for a in agents)
        else:
            names = "?"
        return StreamChunk(
            type="system",
            content=f"Speaker candidates: {names}",
            metadata={"event": "speaker_candidates"},
        )
    if isinstance(event, ToolCallEvent):
        tool_calls = getattr(inner, "tool_calls", []) if inner is not None else []
        first = tool_calls[0] if tool_calls else None
        tool_name = getattr(getattr(first, "function", None), "name", None) if first else None
        tool_args = getattr(getattr(first, "function", None), "arguments", None) if first else None
        sender = getattr(inner, "sender", "unknown") if inner is not None else "unknown"
        return StreamChunk(
            type="tool_use",
            tool_name=tool_name,
            tool_id=None,
            tool_input=tool_args,
            metadata={"agent": sender},
        )
    if isinstance(event, ToolResponseEvent):
        tool_responses = getattr(inner, "tool_responses", []) if inner is not None else []
        first = tool_responses[0] if tool_responses else None
        tool_output = getattr(first, "content", None) if first else None
        tool_id = getattr(first, "tool_call_id", None) if first else None
        sender = getattr(inner, "sender", "unknown") if inner is not None else "unknown"
        return StreamChunk(
            type="tool_result",
            tool_output=tool_output,
            metadata={"agent": sender, "tool_call_id": tool_id},
        )
    return None
