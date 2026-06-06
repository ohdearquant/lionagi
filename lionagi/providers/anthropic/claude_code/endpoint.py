# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

from pydantic import BaseModel

from lionagi.providers.anthropic.claude_code.models import (
    ClaudeChunk,
    ClaudeCodeRequest,
    ClaudeSession,
    stream_claude_code_cli,
)
from lionagi.providers.anthropic.claude_code.models import log as cc_log
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from .._config import ClaudeCodeConfigs

CONTEXT_WINDOWS: dict[str, int] = {
    "opus-4-7": 1_000_000,
    "opus-4-6": 1_000_000,
    "opus": 1_000_000,
    "sonnet-4-6": 1_000_000,
    "sonnet-4-5": 200_000,
    "sonnet": 1_000_000,
    "haiku-4-5": 200_000,
    "haiku": 200_000,
}

_CLAUDE_HANDLER_PARAMS = (
    "on_thinking",
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_system",
    "on_final",
)


def _validate_handlers(handlers: dict[str, Callable | None], /) -> None:
    if not isinstance(handlers, dict):
        raise ValueError("Handlers must be a dictionary")
    for k, v in handlers.items():
        if k not in _CLAUDE_HANDLER_PARAMS:
            raise ValueError(f"Invalid handler key: {k}")
        if not (v is None or callable(v)):
            raise ValueError(f"Handler value must be callable or None, got {type(v)}")


@ClaudeCodeConfigs.CLI.register
class ClaudeCodeCLIEndpoint(AgenticEndpoint):
    transport_arg_keys = _CLAUDE_HANDLER_PARAMS

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("claude_handlers", None)
        super().__init__(config=config, **kwargs)
        config_handlers = self.config.kwargs.pop("claude_handlers", None)
        self._claude_handlers = {k: None for k in _CLAUDE_HANDLER_PARAMS}
        if config_handlers is not None:
            _validate_handlers(config_handlers)
            self._claude_handlers.update(config_handlers)
        if handlers is not None:
            _validate_handlers(handlers)
            self._claude_handlers.update(handlers)

    @property
    def claude_handlers(self):
        return self._claude_handlers

    @claude_handlers.setter
    def claude_handlers(self, value: dict):
        _validate_handlers(value)
        self._claude_handlers = {k: None for k in _CLAUDE_HANDLER_PARAMS}
        self._claude_handlers.update(value)

    def update_handlers(self, **kwargs):
        _validate_handlers(kwargs)
        handlers = {**self.claude_handlers, **kwargs}
        self.claude_handlers = handlers

    def copy_runtime_state_to(self, other):
        if isinstance(other, ClaudeCodeCLIEndpoint):
            other.claude_handlers = self.claude_handlers.copy()

    def _runtime_handlers(self, kwargs: dict) -> dict:
        handlers = self.claude_handlers.copy()
        call_handlers = {k: kwargs.pop(k) for k in list(kwargs) if k in _CLAUDE_HANDLER_PARAMS}
        if call_handlers:
            _validate_handlers(call_handlers)
            handlers.update(call_handlers)
        return {k: v for k, v in handlers.items() if v is not None}

    def create_payload(self, request: dict | BaseModel, **kwargs):
        req_dict = {**self.config.kwargs, **to_dict(request), **kwargs}
        messages = req_dict.pop("messages", [])
        req_dict = {k: v for k, v in req_dict.items() if k in ClaudeCodeRequest.model_fields}
        req_obj = ClaudeCodeRequest(messages=messages, **req_dict)
        return {"request": req_obj}, {}

    async def stream(self, request: dict | BaseModel, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_claude_code_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, ClaudeSession):
                    continue
                if isinstance(item, dict):
                    typ = item.get("type", "")
                    if typ == "system":
                        yield StreamChunk(
                            type="system",
                            metadata={
                                "session_id": item.get("session_id"),
                                "model": item.get("model"),
                                "tools": item.get("tools", []),
                            },
                        )
                    elif typ == "result":
                        yield StreamChunk(
                            type="result",
                            content=item.get("result", ""),
                            metadata={
                                k: item.get(k)
                                for k in (
                                    "usage",
                                    "total_cost_usd",
                                    "num_turns",
                                    "duration_ms",
                                    "duration_api_ms",
                                )
                                if item.get(k) is not None
                            },
                            is_error=item.get("is_error", False),
                        )
                    continue
                if isinstance(item, ClaudeChunk):
                    raw = item.raw
                    if item.type in ("assistant", "user"):
                        msg = raw.get("message", {})
                        for blk in msg.get("content", []):
                            btype = blk.get("type")
                            if btype == "thinking":
                                yield StreamChunk(
                                    type="thinking",
                                    content=blk.get("thinking", ""),
                                )
                            elif btype == "text":
                                yield StreamChunk(
                                    type="text",
                                    content=blk.get("text", ""),
                                )
                            elif btype == "tool_use":
                                yield StreamChunk(
                                    type="tool_use",
                                    tool_name=blk.get("name"),
                                    tool_id=blk.get("id"),
                                    tool_input=blk.get("input"),
                                )
                            elif btype == "tool_result":
                                yield StreamChunk(
                                    type="tool_result",
                                    tool_id=blk.get("tool_use_id"),
                                    tool_output=blk.get("content"),
                                    is_error=blk.get("is_error", False),
                                )

    async def _call(
        self,
        payload: dict,
        headers: dict,  # type: ignore[unused-argument]
        **kwargs,
    ):
        responses = []
        request: ClaudeCodeRequest = payload["request"]
        session: ClaudeSession = ClaudeSession()
        system: dict = None
        _cancelled = False
        handlers = self._runtime_handlers(kwargs)

        # 1. stream the Claude Code response
        try:
            async with contextlib.aclosing(
                stream_claude_code_cli(request, session, **handlers)
            ) as gen:
                async for chunk in gen:
                    if isinstance(chunk, dict):
                        if chunk.get("type") == "done":
                            break
                        system = chunk
                    responses.append(chunk)
        except BaseException:
            # CancelledError, KeyboardInterrupt — must not trigger auto_finish
            _cancelled = True
            raise

        if (
            not _cancelled
            and request.auto_finish
            and responses
            and not isinstance(responses[-1], ClaudeSession)
        ):
            req2 = request.model_copy(deep=True)
            req2.prompt = "Please provide a the final result message only"
            req2.max_turns = 1
            req2.continue_conversation = True
            if system:
                req2.resume = system.get("session_id") if system else None

            async with contextlib.aclosing(stream_claude_code_cli(req2, session)) as gen2:
                async for chunk in gen2:
                    responses.append(chunk)
                    if isinstance(chunk, ClaudeSession):
                        break
        cc_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")
        texts = []
        for i in session.chunks:
            if i.text is not None:
                texts.append(i.text)

        # Guard against IndexError when no text chunks arrived (early cancel,
        # tool-only sessions, empty responses under auto_finish).
        if session.result and (not texts or session.result.strip() != texts[-1].strip()):
            texts.append(session.result)

        session.result = "\n".join(texts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)
