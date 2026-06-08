# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from .._config import PiConfigs
from .models import PiChunk, PiCodeRequest, PiSession, stream_pi_cli
from .models import log as pi_log

CONTEXT_WINDOWS: dict[str, int] = {
    "pi": 128_000,
}

_PI_HANDLER_PARAMS = (
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_final",
)


@PiConfigs.CLI.register
class PiCLIEndpoint(AgenticHandlersMixin, AgenticEndpoint):
    transport_arg_keys = _PI_HANDLER_PARAMS
    _handler_params = _PI_HANDLER_PARAMS
    _handler_kwarg = "pi_handlers"
    _request_model = PiCodeRequest

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("pi_handlers", None)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers)

    @property
    def pi_handlers(self):
        return self._handlers

    @pi_handlers.setter
    def pi_handlers(self, value: dict):
        self._set_handlers(value)

    async def stream(self, request, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        session = PiSession()
        async with contextlib.aclosing(stream_pi_cli(request_obj, session, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, PiSession):
                    yield StreamChunk(
                        type="result",
                        content=item.result or "",
                        metadata={"session_id": item.session_id},
                    )
                    continue
                if isinstance(item, dict):
                    continue
                if isinstance(item, PiChunk):
                    if item.text is not None:
                        yield StreamChunk(type="text", content=item.text)
                    if item.thinking is not None:
                        yield StreamChunk(type="thinking", content=item.thinking)
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

    async def _call(
        self,
        payload: dict,
        headers: dict,
        **kwargs,
    ):
        responses = []
        request: PiCodeRequest = payload["request"]
        session: PiSession = PiSession()
        handlers = self._runtime_handlers(kwargs)

        async with contextlib.aclosing(stream_pi_cli(request, session, **handlers)) as gen:
            async for chunk in gen:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "done":
                        break
                responses.append(chunk)

        pi_log.info(f"Session finished with {len(responses)} chunks")
        if not session.result:
            texts = [c.text for c in session.chunks if c.text is not None]
            session.result = "\n".join(texts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)
