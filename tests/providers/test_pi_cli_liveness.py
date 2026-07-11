import pytest

from lionagi.providers.anthropic.claude_code import ClaudeCodeCLIEndpoint
from lionagi.providers.google.gemini_code import GeminiCLIEndpoint
from lionagi.providers.openai.codex import CodexCLIEndpoint
from lionagi.providers.pi import cli as pi_cli
from lionagi.providers.pi.cli import PiChunk, PiCLIEndpoint


def test_early_output_capability_matches_projected_streams():
    assert ClaudeCodeCLIEndpoint.streams_first_output_early is True
    assert CodexCLIEndpoint.streams_first_output_early is True
    assert GeminiCLIEndpoint.streams_first_output_early is False
    assert PiCLIEndpoint.streams_first_output_early is False


@pytest.mark.anyio
async def test_pi_stream_discards_pre_payload_events(monkeypatch):
    """The transport's early dict events never reach the caller as chunks.

    Pi emits agent_start/turn_start/message_start dicts right after spawn, but
    stream() drops raw dicts — the first observable StreamChunk is the first
    PiChunk payload. This is why the endpoint must not declare
    streams_first_output_early: the first-output liveness window would measure
    model think time, not subprocess health.
    """

    async def fake_stream(request_obj, session, **handlers):
        yield {"type": "agent_start"}
        yield {"type": "turn_start"}
        yield {"type": "message_start"}
        yield PiChunk(raw={}, type="message_delta", text="hello")

    monkeypatch.setattr(pi_cli, "stream_pi_cli", fake_stream)
    endpoint = PiCLIEndpoint()

    chunks = [chunk async for chunk in endpoint.stream({"request": object()})]

    assert chunks, "payload chunk should be projected"
    assert chunks[0].type == "text"
    assert chunks[0].content == "hello"
