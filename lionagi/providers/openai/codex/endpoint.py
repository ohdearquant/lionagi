# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

from pydantic import BaseModel

from lionagi.providers.openai.codex.models import (
    CodexChunk,
    CodexCodeRequest,
    CodexSession,
    stream_codex_cli,
)
from lionagi.providers.openai.codex.models import log as codex_log
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from .._config import CodexConfigs

CONTEXT_WINDOWS: dict[str, int] = {
    "codex-mini": 200_000,
    "o4-mini": 200_000,
    "o3": 200_000,
    "gpt-4.1": 1_047_576,
}

_CODEX_HANDLER_PARAMS = (
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_final",
)


def _validate_handlers(handlers: dict[str, Callable | None], /) -> None:
    if not isinstance(handlers, dict):
        raise ValueError("Handlers must be a dictionary")
    for k, v in handlers.items():
        if k not in _CODEX_HANDLER_PARAMS:
            raise ValueError(f"Invalid handler key: {k}")
        if not (v is None or callable(v)):
            raise ValueError(f"Handler value must be callable or None, got {type(v)}")


@CodexConfigs.CLI.register
class CodexCLIEndpoint(AgenticEndpoint):
    transport_arg_keys = _CODEX_HANDLER_PARAMS

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("codex_handlers", None)
        super().__init__(config=config, **kwargs)
        config_handlers = self.config.kwargs.pop("codex_handlers", None)
        self._codex_handlers = {k: None for k in _CODEX_HANDLER_PARAMS}
        if config_handlers is not None:
            _validate_handlers(config_handlers)
            self._codex_handlers.update(config_handlers)
        if handlers is not None:
            _validate_handlers(handlers)
            self._codex_handlers.update(handlers)

    @property
    def codex_handlers(self):
        return self._codex_handlers

    @codex_handlers.setter
    def codex_handlers(self, value: dict):
        _validate_handlers(value)
        self._codex_handlers = {k: None for k in _CODEX_HANDLER_PARAMS}
        self._codex_handlers.update(value)

    def update_handlers(self, **kwargs):
        _validate_handlers(kwargs)
        handlers = {**self.codex_handlers, **kwargs}
        self.codex_handlers = handlers

    def copy_runtime_state_to(self, other):
        if isinstance(other, CodexCLIEndpoint):
            other.codex_handlers = self.codex_handlers.copy()

    def _runtime_handlers(self, kwargs: dict) -> dict:
        handlers = self.codex_handlers.copy()
        call_handlers = {k: kwargs.pop(k) for k in list(kwargs) if k in _CODEX_HANDLER_PARAMS}
        if call_handlers:
            _validate_handlers(call_handlers)
            handlers.update(call_handlers)
        return {k: v for k, v in handlers.items() if v is not None}

    def create_payload(self, request: dict | BaseModel, **kwargs):
        req_dict = {**self.config.kwargs, **to_dict(request), **kwargs}
        messages = req_dict.pop("messages", [])
        req_dict = {k: v for k, v in req_dict.items() if k in CodexCodeRequest.model_fields}
        req_obj = CodexCodeRequest(messages=messages, **req_dict)
        return {"request": req_obj}, {}

    async def stream(self, request: dict | BaseModel, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_codex_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, CodexSession):
                    continue
                if isinstance(item, dict):
                    typ = item.get("type", "")
                    if typ == "result":
                        yield StreamChunk(
                            type="result",
                            content=item.get("result", ""),
                            metadata=item,
                        )
                    continue
                if isinstance(item, CodexChunk):
                    if item.text is not None:
                        yield StreamChunk(type="text", content=item.text)
                    if item.tool_use is not None:
                        tu = item.tool_use
                        yield StreamChunk(
                            type="tool_use",
                            tool_name=tu.get("name"),
                            tool_id=tu.get("id"),
                            tool_input=tu.get("input"),
                        )
                    if item.tool_result is not None:
                        tr = item.tool_result
                        yield StreamChunk(
                            type="tool_result",
                            tool_id=tr.get("tool_use_id"),
                            tool_output=tr.get("content"),
                            is_error=tr.get("is_error", False),
                        )
                    if (
                        item.text is None
                        and item.tool_use is None
                        and item.tool_result is None
                        and item.type == "result"
                    ):
                        yield StreamChunk(
                            type="result",
                            content=item.raw.get("result", ""),
                            metadata=item.raw,
                        )

    async def _call(
        self,
        payload: dict,
        headers: dict,
        **kwargs,
    ):
        responses = []
        request: CodexCodeRequest = payload["request"]
        session: CodexSession = CodexSession()
        handlers = self._runtime_handlers(kwargs)

        async with contextlib.aclosing(stream_codex_cli(request, session, **handlers)) as gen:
            async for chunk in gen:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "done":
                        break
                responses.append(chunk)

        codex_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")
        if not session.result:
            texts = [c.text for c in session.chunks if c.text is not None]
            session.result = "\n".join(texts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)
