# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers.openai.codex.models import CodexCodeRequest, stream_codex_cli
from lionagi.providers.openai.codex.models import log as codex_log
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from .._config import CodexConfigs

CONTEXT_WINDOWS: dict[str, int] = {
    "codex-mini": 200_000,
    "o4-mini": 200_000,
    "o3": 200_000,
    "gpt-4.1": 1_047_576,
    "gpt-5.5": 1_047_576,
}

_CODEX_HANDLER_PARAMS = (
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_final",
)


@CodexConfigs.CLI.register
class CodexCLIEndpoint(AgenticHandlersMixin, AgenticEndpoint):
    transport_arg_keys = _CODEX_HANDLER_PARAMS
    _handler_params = _CODEX_HANDLER_PARAMS
    _handler_kwarg = "codex_handlers"
    _request_model = CodexCodeRequest

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("codex_handlers", None)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers)

    @property
    def codex_handlers(self):
        return self._handlers

    @codex_handlers.setter
    def codex_handlers(self, value: dict):
        self._set_handlers(value)

    async def stream(self, request, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_codex_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, CLISession):
                    if item.is_error:
                        yield StreamChunk(
                            type="error",
                            content=item.result or "Codex session failed",
                        )
                    continue
                yield item

    async def _call(
        self,
        payload: dict,
        headers: dict,
        **kwargs,
    ):
        responses = []
        request: CodexCodeRequest = payload["request"]
        session: CLISession = CLISession()
        handlers = self._runtime_handlers(kwargs)

        async with contextlib.aclosing(stream_codex_cli(request, session, **handlers)) as gen:
            async for chunk in gen:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "done":
                        break
                responses.append(chunk)

        codex_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")
        if not session.result:
            texts = [c.content for c in session.chunks if c.type == "text" and c.content]
            session.result = "\n".join(texts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)
