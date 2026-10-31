# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI result parsers measure elapsed time on the active async backend."""

from __future__ import annotations

from importlib import import_module

import anyio
import pytest
import sniffio
from trio.testing import MockClock

from lionagi.service.types.cli_session import CLISession


@pytest.mark.parametrize("provider", ["gemini", "codex", "pi"])
def test_cli_parser_reports_elapsed_duration_on_trio(monkeypatch, provider):
    clock = MockClock()

    async def scenario():
        assert sniffio.current_async_library() == "trio"
        if provider == "gemini":
            module = import_module("lionhub.providers.google.gemini_code")
            request = module.GeminiCodeRequest(prompt="Hello", verbose_output=False)
            stream = module.stream_gemini_cli
            transport = "stream_gemini_cli_events"
            events = [{"status": "SUCCESS", "response": "Hello"}]
        elif provider == "codex":
            module = import_module("lionhub.providers.openai.codex")
            request = module.CodexCodeRequest(prompt="Hello", verbose_output=False)
            stream = module.stream_codex_cli
            transport = "stream_codex_cli_events"
            events = [
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello"}},
                {"type": "done"},
            ]
        else:
            module = import_module("lionhub.providers.pi.cli")
            request = module.PiCodeRequest(prompt="Hello", verbose_output=False)
            stream = module.stream_pi_cli
            transport = "stream_pi_cli_events"
            events = [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
                },
                {"type": "done"},
            ]

        closed = []

        async def event_transport(actual_request):
            assert actual_request is request
            try:
                clock.jump(0.125)
                for event in events:
                    yield event
            finally:
                closed.append(True)

        monkeypatch.setattr(module, transport, event_transport)
        finalized = []
        items = [item async for item in stream(request, on_final=finalized.append)]
        session = items[-1]
        assert isinstance(session, CLISession)
        assert session.result == "Hello"
        assert session.duration_ms == 125
        assert not session.is_error
        assert finalized == [session]
        assert closed == [True]

    # A synchronous entry point keeps pytest-asyncio auto mode from taking
    # this coroutine away from the explicitly selected Trio backend.
    anyio.run(scenario, backend="trio", backend_options={"clock": clock})
