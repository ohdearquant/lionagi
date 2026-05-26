import json
from pathlib import Path

import pytest

from lionagi.providers.pi.cli import models as pi_models
from lionagi.providers.pi.cli.endpoint import PiCLIEndpoint
from lionagi.providers.pi.cli.models import PiChunk, PiCodeRequest, PiSession


def _fixture_events(name: str) -> list[dict]:
    # Fixtures live in the library at lionagi/testing/data/ — kept there so
    # external test consumers can use them via lionagi.testing.TestDataLoader.
    from lionagi import testing as _lt

    path = Path(_lt.__file__).resolve().parent / "data" / name
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class TestPiCodeRequest:
    def test_as_cmd_args_orders_flags_files_and_prompt(self):
        req = PiCodeRequest(
            prompt="fix the tests",
            model="openrouter/anthropic/claude-sonnet-4-5",
            thinking="high",
            tools=["read", "edit"],
            file_args=["README.md", "@pyproject.toml"],
            system_prompt="be concise",
        )

        args = req.as_cmd_args()

        assert args[:3] == ["-p", "--mode", "json"]
        assert args[args.index("--provider") + 1] == "openrouter"
        assert args[args.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
        assert args.index("--provider") < args.index("--model")
        assert args.index("--model") < args.index("--thinking")
        assert args.index("--thinking") < args.index("--no-session")
        assert args.index("--tools") < args.index("--system-prompt")
        assert args[-3:] == ["@README.md", "@pyproject.toml", "fix the tests"]
        assert "--" not in args, "Pi CLI does not support -- terminator"

    @pytest.mark.parametrize(
        ("model", "provider", "normalized_model"),
        [
            ("deepseek-chat", "deepseek", "deepseek-chat"),
            ("claude-sonnet-4-5", "anthropic", "claude-sonnet-4-5"),
            ("gpt-5", "openai", "gpt-5"),
            ("o4-mini", "openai", "o4-mini"),
            ("openrouter/openai/gpt-5", "openrouter", "openai/gpt-5"),
        ],
    )
    def test_infers_provider_from_unambiguous_model_prefixes(
        self,
        model,
        provider,
        normalized_model,
    ):
        req = PiCodeRequest(prompt="hello", model=model)

        assert req.provider == provider
        assert req.model == normalized_model

    def test_explicit_provider_is_not_overridden(self):
        req = PiCodeRequest(
            prompt="hello",
            provider="google",
            model="deepseek-chat",
        )

        assert req.provider == "google"
        assert req.model == "deepseek-chat"

    @pytest.mark.parametrize(
        ("provider", "expected_key"),
        [
            ("google", "GEMINI_API_KEY"),
            ("anthropic", "ANTHROPIC_API_KEY"),
            ("deepseek", "DEEPSEEK_API_KEY"),
            ("openrouter", "OPENROUTER_API_KEY"),
            ("custom", "CUSTOM_API_KEY"),
        ],
    )
    def test_env_maps_provider_specific_api_key_names(self, provider, expected_key):
        req = PiCodeRequest(prompt="hello", provider=provider, api_key="test-key")

        assert req.env() == {expected_key: "test-key"}


@pytest.mark.asyncio
async def test_stream_pi_cli_parses_jsonl_agent_events(monkeypatch):
    events = _fixture_events("pi_cli_events.jsonl")

    async def fake_events(_request):
        for event in events:
            yield event

    monkeypatch.setattr(pi_models, "stream_pi_cli_events", fake_events)

    seen = []
    session = None
    async for item in pi_models.stream_pi_cli(PiCodeRequest(prompt="hello")):
        if isinstance(item, PiSession):
            session = item
        else:
            seen.append(item)

    text_chunks = [c.text for c in seen if isinstance(c, PiChunk) and c.text]
    thinking_chunks = [c.thinking for c in seen if isinstance(c, PiChunk) and c.thinking]
    tool_uses = [c.tool_use for c in seen if isinstance(c, PiChunk) and c.tool_use]
    tool_results = [c.tool_result for c in seen if isinstance(c, PiChunk) and c.tool_result]

    assert text_chunks == ["Hello "]
    assert thinking_chunks == ["consider"]
    assert {"id": "call_1", "name": "read", "input": {"path": "README.md"}} in tool_uses
    assert tool_results == [
        {
            "tool_use_id": "call_1",
            "name": "read",
            "content": {
                "content": [{"type": "text", "text": "contents"}],
                "details": {},
            },
            "is_error": False,
        }
    ]
    assert session is not None
    assert session.result == "Hello world"
    assert session.model == "gemini-2.5-flash"
    assert session.usage == {"input": 10, "output": 5, "totalTokens": 15}
    assert session.num_turns == 1


@pytest.mark.asyncio
async def test_stream_pi_cli_handles_nested_and_top_level_errors(monkeypatch):
    events = [
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "error",
                "reason": "error",
                "error": {
                    "role": "assistant",
                    "content": [],
                    "errorMessage": "nested failure",
                },
            },
        },
        {"type": "error", "errorMessage": "top-level failure"},
    ]

    async def fake_events(_request):
        for event in events:
            yield event

    monkeypatch.setattr(pi_models, "stream_pi_cli_events", fake_events)

    session = None
    async for item in pi_models.stream_pi_cli(PiCodeRequest(prompt="hello")):
        if isinstance(item, PiSession):
            session = item

    assert session is not None
    assert session.is_error is True
    assert session.result == "top-level failure"


@pytest.mark.asyncio
async def test_pi_cli_endpoint_stream_maps_pi_chunks_to_stream_chunks(monkeypatch):
    async def fake_stream(_request_obj, _session=None):
        yield PiChunk(raw={}, type="message_update", text="hello")
        yield PiChunk(raw={}, type="message_update", thinking="reasoning")
        yield PiChunk(
            raw={},
            type="message_update",
            tool_use={"id": "call_1", "name": "read", "input": {"path": "a.py"}},
        )
        yield PiChunk(
            raw={},
            type="tool_execution_end",
            tool_result={
                "tool_use_id": "call_1",
                "content": {"ok": True},
                "is_error": False,
            },
        )
        yield PiChunk(raw={"result": "done"}, type="agent_end")
        yield PiSession(result="done")

    monkeypatch.setattr(
        "lionagi.providers.pi.cli.endpoint.stream_pi_cli",
        fake_stream,
    )

    endpoint = PiCLIEndpoint()
    request = {"request": PiCodeRequest(prompt="hello")}

    chunks = [chunk async for chunk in endpoint.stream(request)]

    assert [chunk.type for chunk in chunks] == [
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "result",
    ]
    assert chunks[0].content == "hello"
    assert chunks[1].content == "reasoning"
    assert chunks[2].tool_name == "read"
    assert chunks[2].tool_input == {"path": "a.py"}
    assert chunks[3].tool_output == {"ok": True}
    assert chunks[4].content == "done"
