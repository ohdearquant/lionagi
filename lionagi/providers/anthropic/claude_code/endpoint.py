# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest, stream_claude_code_cli
from lionagi.providers.anthropic.claude_code.models import log as cc_log
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.cli_session import CLISession
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


@ClaudeCodeConfigs.CLI.register
class ClaudeCodeCLIEndpoint(AgenticHandlersMixin, AgenticEndpoint):
    transport_arg_keys = _CLAUDE_HANDLER_PARAMS
    _handler_params = _CLAUDE_HANDLER_PARAMS
    _handler_kwarg = "claude_handlers"
    _request_model = ClaudeCodeRequest

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("claude_handlers", None)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers)

    @property
    def claude_handlers(self):
        return self._handlers

    @claude_handlers.setter
    def claude_handlers(self, value: dict):
        self._set_handlers(value)

    async def stream(self, request, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_claude_code_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, CLISession):
                    continue
                yield item

    async def _call(
        self,
        payload: dict,
        headers: dict,  # type: ignore[unused-argument]
        **kwargs,
    ):
        responses = []
        request: ClaudeCodeRequest = payload["request"]
        session: CLISession = CLISession()
        system_meta: dict | None = None
        _cancelled = False
        handlers = self._runtime_handlers(kwargs)

        try:
            async with contextlib.aclosing(
                stream_claude_code_cli(request, session, **handlers)
            ) as gen:
                async for chunk in gen:
                    if isinstance(chunk, StreamChunk) and chunk.type == "system":
                        system_meta = chunk.metadata
                    responses.append(chunk)
        except BaseException:
            _cancelled = True
            raise

        if (
            not _cancelled
            and request.auto_finish
            and responses
            and not isinstance(responses[-1], CLISession)
        ):
            req2 = request.model_copy(deep=True)
            req2.prompt = "Please provide a the final result message only"
            req2.max_turns = 1
            req2.continue_conversation = True
            if system_meta:
                req2.resume = system_meta.get("session_id")

            async with contextlib.aclosing(stream_claude_code_cli(req2, session)) as gen2:
                async for chunk in gen2:
                    responses.append(chunk)
                    if isinstance(chunk, CLISession):
                        break
        cc_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")
        texts = []
        for sc in session.chunks:
            if sc.type == "text" and sc.content is not None:
                texts.append(sc.content)

        if session.result and (not texts or session.result.strip() != texts[-1].strip()):
            texts.append(session.result)

        session.result = "\n".join(texts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)
