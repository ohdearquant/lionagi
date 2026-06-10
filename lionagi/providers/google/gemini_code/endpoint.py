# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers.google.gemini_code.models import (
    GeminiChunk,
    GeminiCodeRequest,
    GeminiSession,
    stream_gemini_cli,
)
from lionagi.providers.google.gemini_code.models import log as gemini_log
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from .._config import GeminiCodeConfigs

CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-2.5": 1_048_576,
    "gemini-2.0": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
}

_GEMINI_HANDLER_PARAMS = (
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_final",
)


@GeminiCodeConfigs.CLI.register
class GeminiCLIEndpoint(AgenticHandlersMixin, AgenticEndpoint):
    transport_arg_keys = _GEMINI_HANDLER_PARAMS
    _handler_params = _GEMINI_HANDLER_PARAMS
    _handler_kwarg = "gemini_handlers"
    _request_model = GeminiCodeRequest
    _filter_model_fields = False

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("gemini_handlers", None)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers)

    @property
    def gemini_handlers(self):
        return self._handlers

    @gemini_handlers.setter
    def gemini_handlers(self, value: dict):
        self._set_handlers(value)

    async def stream(self, request, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_gemini_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, GeminiSession):
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
                if isinstance(item, GeminiChunk):
                    if item.text is not None:
                        yield StreamChunk(
                            type="text",
                            content=item.text,
                            is_delta=item.is_delta,
                        )
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
        request: GeminiCodeRequest = payload["request"]
        session: GeminiSession = GeminiSession()
        handlers = self._runtime_handlers(kwargs)

        async with contextlib.aclosing(stream_gemini_cli(request, session, **handlers)) as gen:
            async for chunk in gen:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "done":
                        break
                responses.append(chunk)

        gemini_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")

        parts = []
        current_delta: list[str] = []
        for i in session.chunks:
            if i.text is not None:
                if i.is_delta:
                    current_delta.append(i.text)
                else:
                    if current_delta:
                        parts.append("".join(current_delta))
                        current_delta = []
                    parts.append(i.text)
        if current_delta:
            parts.append("".join(current_delta))

        if parts:
            session.result = "\n".join(parts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)
