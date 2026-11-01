"""Regression tests for per-provider CLI subprocess behaviour parity.

Covers: Claude tail repair, Codex double-workspace, Pi stdin inheritance,
Gemini/agy DEVNULL stdin (headless print mode).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi._errors import ExecutionError
from lionagi._provider_errors import ProviderError
from lionagi.protocols.generic.event import EventStatus
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch


def _chat_model(provider: str, model: str | None = None) -> iModel:
    kwargs = {"provider": provider, "api_key": "test-key"}
    if model is not None:
        kwargs["model"] = model
    return iModel(**kwargs)


async def _failed_chat(branch: Branch, provider: str, events: list[dict], seam: str):
    async def fake_events(_request):
        for event in events:
            yield event

    with patch(seam, side_effect=fake_events):
        with pytest.raises(ExecutionError) as info:
            await branch.chat("go on")

    return info.value


def _assert_failed_call(branch: Branch, error: ExecutionError, expected: str) -> None:
    assert isinstance(error.__cause__, Exception)
    assert expected in str(error.__cause__)
    assert len(branch.logs) == 1
    execution = branch.logs[-1].content["execution"]
    assert execution["status"] == EventStatus.FAILED.value
    assert execution["response"] is None
    assert expected in str(execution["error"])


@pytest.mark.asyncio
async def test_an_api_provider_failed_call_records_failure_and_raises():
    """Characterizes the path every CLI endpoint's non-streamed call must match."""
    model = _chat_model("openai", "gpt-4.1-mini")
    branch = Branch(chat_model=model)

    with patch.object(
        model.endpoint,
        "call",
        side_effect=RuntimeError("the provider said no"),
    ):
        with pytest.raises(ExecutionError) as info:
            await branch.chat("go on")

    _assert_failed_call(branch, info.value, "the provider said no")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "events", "seam"),
    [
        (
            "codex",
            "gpt-5.1-codex",
            [{"type": "turn.failed", "error": {"message": "rate limit exceeded"}}],
            "lionhub.providers.openai.codex.stream_codex_cli_events",
        ),
        (
            "claude_code",
            "sonnet",
            [
                {
                    "type": "result",
                    "result": "rate limit exceeded",
                    "is_error": True,
                    "api_error_status": 429,
                }
            ],
            "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
        ),
        (
            "gemini_code",
            "gemini-4-argon",
            [
                {
                    "conversation_id": "conv-1",
                    "status": "ERROR",
                    "response": "",
                    "error": "rate limit exceeded",
                }
            ],
            "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
        ),
        (
            "pi",
            "gemini-2.5-flash",
            [{"type": "error", "error": {"message": "rate limit exceeded"}}],
            "lionhub.providers.pi.cli.stream_pi_cli_events",
        ),
    ],
    ids=["codex", "claude_code", "gemini_code", "pi"],
)
async def test_a_cli_failed_non_streamed_call_matches_the_api_path(
    provider,
    model,
    events,
    seam,
):
    branch = Branch(chat_model=_chat_model(provider, model))
    error = await _failed_chat(branch, provider, events, seam)

    assert isinstance(error.__cause__, ProviderError)
    _assert_failed_call(branch, error, "rate limit exceeded")


_ASSISTANT_TEXT = "Investigating why the user is not logged in"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "events", "seam"),
    [
        (
            "codex",
            "gpt-5.1-codex",
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": _ASSISTANT_TEXT},
                },
                {"type": "turn.failed", "error": {"message": "rate limit exceeded"}},
            ],
            "lionhub.providers.openai.codex.stream_codex_cli_events",
        ),
        (
            "claude_code",
            "sonnet",
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": _ASSISTANT_TEXT}]},
                },
                {"type": "result", "result": "rate limit exceeded", "is_error": True},
            ],
            "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
        ),
        (
            "pi",
            "gemini-2.5-flash",
            [
                {"type": "error", "error": {"message": "rate limit exceeded"}},
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": _ASSISTANT_TEXT}],
                        }
                    ],
                },
            ],
            "lionhub.providers.pi.cli.stream_pi_cli_events",
        ),
    ],
    ids=["codex", "claude_code", "pi"],
)
async def test_a_cli_failure_is_classified_on_its_own_text_not_the_assistant_text(
    provider,
    model,
    events,
    seam,
):
    """Assistant text the run produced before it failed is not the failure: the
    error is classified, and reported, on what the provider said went wrong."""
    from lionagi._provider_errors import classify_provider_error

    branch = Branch(chat_model=_chat_model(provider, model))
    error = await _failed_chat(branch, provider, events, seam)

    assert type(error.__cause__) is type(classify_provider_error("rate limit exceeded"))
    assert _ASSISTANT_TEXT not in str(error.__cause__)
    _assert_failed_call(branch, error, "rate limit exceeded")


@pytest.mark.asyncio
async def test_a_codex_failure_with_no_text_is_not_reported_as_the_assistant_text():
    """A failed turn that says nothing is classified as an unexplained failure,
    not on the assistant text the stream would otherwise fill the result with."""
    from lionagi._provider_errors import classify_provider_error

    branch = Branch(chat_model=_chat_model("codex", "gpt-5.1-codex"))
    error = await _failed_chat(
        branch,
        "codex",
        [
            {"type": "item.completed", "item": {"type": "agent_message", "text": _ASSISTANT_TEXT}},
            {"type": "turn.failed", "error": ""},
        ],
        "lionhub.providers.openai.codex.stream_codex_cli_events",
    )

    assert type(error.__cause__) is type(classify_provider_error("Codex session failed"))
    assert _ASSISTANT_TEXT not in str(error.__cause__)
    _assert_failed_call(branch, error, "Codex session failed")


@pytest.mark.asyncio
async def test_a_bare_gemini_error_status_is_reported_by_its_status():
    branch = Branch(chat_model=_chat_model("gemini_code", "gemini-4-argon"))
    error = await _failed_chat(
        branch,
        "gemini_code",
        [{"conversation_id": "conv-1", "status": "ERROR"}],
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
    )

    assert isinstance(error.__cause__, ProviderError)
    _assert_failed_call(branch, error, "agy returned status=ERROR")


@pytest.mark.asyncio
async def test_a_gemini_teardown_failure_is_reported_as_one():
    from lionagi._provider_errors import ProviderTeardownError

    async def failing_events(_request):
        raise RuntimeError("Event loop is closed")
        yield  # pragma: no cover - makes this an async generator

    branch = Branch(chat_model=_chat_model("gemini_code", "gemini-4-argon"))
    with patch(
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=failing_events,
    ):
        with pytest.raises(ExecutionError) as info:
            await branch.chat("go on")

    assert isinstance(info.value.__cause__, ProviderTeardownError)
    _assert_failed_call(branch, info.value, "agy teardown failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "events", "seam", "said"),
    [
        (
            "gemini_code",
            "gemini-4-argon",
            [
                {
                    "conversation_id": "conv-1",
                    "status": "ERROR",
                    "response": _ASSISTANT_TEXT,
                    "error": "",
                }
            ],
            "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
            "agy returned status=ERROR",
        ),
        (
            "pi",
            "gemini-2.5-flash",
            [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "error",
                        "reason": "aborted",
                        "error": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": _ASSISTANT_TEXT}],
                            "stopReason": "aborted",
                        },
                    },
                }
            ],
            "lionhub.providers.pi.cli.stream_pi_cli_events",
            "aborted",
        ),
        (
            "pi",
            "gemini-2.5-flash",
            [
                {"type": "error", "error": {"message": "rate limit exceeded"}},
                {"type": "error", "error": ""},
            ],
            "lionhub.providers.pi.cli.stream_pi_cli_events",
            "rate limit exceeded",
        ),
        (
            "claude_code",
            "sonnet",
            [
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "errors": ["API Error: 429 rate limit exceeded"],
                }
            ],
            "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
            "API Error: 429 rate limit exceeded",
        ),
        (
            "claude_code",
            "sonnet",
            [
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": None,
                    "errors": ["API Error: 429 rate limit exceeded"],
                }
            ],
            "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
            "API Error: 429 rate limit exceeded",
        ),
        (
            "codex",
            "gpt-5.1-codex",
            [
                {"type": "error", "message": "rate limit exceeded"},
                {"type": "turn.failed", "error": {}},
            ],
            "lionhub.providers.openai.codex.stream_codex_cli_events",
            "rate limit exceeded",
        ),
        (
            "codex",
            "gpt-5.1-codex",
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": _ASSISTANT_TEXT},
                },
                {"type": "result", "result": "rate limit exceeded", "is_error": True},
            ],
            "lionhub.providers.openai.codex.stream_codex_cli_events",
            "rate limit exceeded",
        ),
    ],
    ids=[
        "gemini-status-without-error",
        "pi-aborted-turn",
        "pi-empty-error-after-a-message",
        "claude-errors-list",
        "claude-null-result",
        "codex-empty-payload-after-a-message",
        "codex-terminal-result-event",
    ],
)
async def test_a_cli_failure_is_classified_on_the_text_that_names_it(
    provider,
    model,
    events,
    seam,
    said,
):
    """The failure's own text decides the class: not the response or assistant
    message the turn produced, and not a later event that names nothing."""
    from lionagi._provider_errors import classify_provider_error

    branch = Branch(chat_model=_chat_model(provider, model))
    error = await _failed_chat(branch, provider, events, seam)

    assert type(error.__cause__) is type(classify_provider_error(said))
    assert _ASSISTANT_TEXT not in str(error.__cause__)
    _assert_failed_call(branch, error, said)


_CODEX = "lionhub.providers.openai.codex.stream_codex_cli_events"
_GEMINI = "lionhub.providers.google.gemini_code.stream_gemini_cli_events"
_CLAUDE = "lionhub.providers.anthropic.claude_code.stream_cc_cli_events"
_PI = "lionhub.providers.pi.cli.stream_pi_cli_events"


def _agy(status: str, error: str) -> dict:
    return {"conversation_id": "conv-1", "status": status, "response": "", "error": error}


def _claude_error(**fields) -> dict:
    return {"type": "result", "subtype": "error_during_execution", "is_error": True, **fields}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "events", "seam", "expected", "said"),
    [
        (
            "codex",
            "gpt-5.1-codex",
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "rate limit exceeded"},
                },
                {"type": "result", "result": "rate limit exceeded", "is_error": True},
            ],
            _CODEX,
            "ProviderQuotaError",
            "rate limit exceeded",
        ),
        (
            "codex",
            "gpt-5.1-codex",
            [
                {"type": "error", "message": "rate limit exceeded"},
                {"type": "turn.failed", "error": {"message": ""}},
            ],
            _CODEX,
            "ProviderQuotaError",
            "rate limit exceeded",
        ),
        (
            "codex",
            "gpt-5.1-codex",
            [
                {"type": "error", "message": "stream disconnected before completion"},
                {"type": "turn.failed", "error": {"message": "Reconnecting... 5/5 failed"}},
            ],
            _CODEX,
            "ProviderStreamDisconnectError",
            "stream disconnected before completion",
        ),
        (
            "codex",
            "gpt-5.1-codex",
            [
                {"type": "turn.failed", "error": {}},
                {"type": "result", "result": "rate limit exceeded", "is_error": True},
            ],
            _CODEX,
            "ProviderQuotaError",
            "rate limit exceeded",
        ),
        (
            "codex",
            "gpt-5.1-codex",
            [
                {"type": "result", "result": "rate limit exceeded", "is_error": True},
                {"type": "turn.failed", "error": {}},
            ],
            _CODEX,
            "ProviderQuotaError",
            "rate limit exceeded",
        ),
        (
            "codex",
            "gpt-5.1-codex",
            [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "gh auth status",
                        "aggregated_output": "You are not logged into any GitHub hosts",
                        "exit_code": 1,
                        "status": "failed",
                    },
                },
                {"type": "turn.failed", "error": {}},
            ],
            _CODEX,
            "ProviderError",
            "CLI failure (empty error payload",
        ),
        (
            "gemini_code",
            "gemini-4-argon",
            [_agy("ERROR", "Invalid API key"), _agy("ERROR", "")],
            _GEMINI,
            "ProviderAuthError",
            "Invalid API key",
        ),
        (
            "gemini_code",
            "gemini-4-argon",
            [_agy("SUCCESS", "Tool call rejected")],
            _GEMINI,
            "ProviderPermissionError",
            "Tool call rejected",
        ),
        (
            "claude_code",
            "sonnet",
            [_claude_error(errors=[None])],
            _CLAUDE,
            "ProviderError",
            "Claude Code session failed",
        ),
        (
            "claude_code",
            "sonnet",
            [_claude_error(errors=[{"message": ""}])],
            _CLAUDE,
            "ProviderError",
            "Claude Code session failed",
        ),
        (
            "claude_code",
            "sonnet",
            [
                _claude_error(
                    result=_ASSISTANT_TEXT,
                    errors=[{"message": "API Error: 429 rate limit exceeded"}],
                )
            ],
            _CLAUDE,
            "ProviderQuotaError",
            "API Error: 429 rate limit exceeded",
        ),
        (
            "pi",
            "gemini-2.5-flash",
            [
                {"type": "error", "error": {"message": "rate limit exceeded"}},
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "error",
                        "reason": "aborted",
                        "error": {"role": "assistant", "content": [], "stopReason": "aborted"},
                    },
                },
            ],
            _PI,
            "ProviderQuotaError",
            "rate limit exceeded",
        ),
    ],
    ids=[
        "codex-result-event-repeating-the-message",
        "codex-empty-message-after-a-message",
        "codex-two-failures-one-a-disconnect",
        "codex-result-event-after-an-empty-payload",
        "codex-empty-payload-after-a-result-event",
        "codex-failed-command-before-the-failure",
        "gemini-empty-error-after-an-error",
        "gemini-success-with-a-rejected-tool",
        "claude-errors-holding-nothing",
        "claude-errors-holding-an-empty-message",
        "claude-errors-beside-a-result",
        "pi-reason-after-a-message",
    ],
)
async def test_every_failure_a_cli_reported_decides_the_class(
    provider, model, events, seam, expected, said
):
    """A failure is classified on every failure text the CLI reported, in order: one event
    repeating the assistant text, a later event with an empty or placeholder payload, a failed
    command's output, a status or a reason with no message beside it, and a result's text beside
    its errors neither hide nor replace the text that names the failure."""
    from lionagi import _provider_errors

    branch = Branch(chat_model=_chat_model(provider, model))
    error = await _failed_chat(branch, provider, events, seam)

    assert type(error.__cause__) is getattr(_provider_errors, expected)
    assert "not logged in" not in str(error.__cause__)
    _assert_failed_call(branch, error, said)


@pytest.mark.asyncio
async def test_a_gemini_error_is_reported_over_the_teardown_failure_after_it():
    from lionagi._provider_errors import classify_provider_error

    async def events(_request):
        yield {
            "conversation_id": "conv-1",
            "status": "ERROR",
            "response": "",
            "error": "Invalid API key",
        }
        raise RuntimeError("Event loop is closed")

    branch = Branch(chat_model=_chat_model("gemini_code", "gemini-4-argon"))
    with patch(
        "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
        side_effect=events,
    ):
        with pytest.raises(ExecutionError) as info:
            await branch.chat("go on")

    assert type(info.value.__cause__) is type(classify_provider_error("Invalid API key"))
    _assert_failed_call(branch, info.value, "Invalid API key")


@pytest.mark.asyncio
async def test_a_codex_result_event_failure_with_no_text_is_not_the_assistant_text():
    from lionagi._provider_errors import classify_provider_error

    branch = Branch(chat_model=_chat_model("codex", "gpt-5.1-codex"))
    error = await _failed_chat(
        branch,
        "codex",
        [
            {"type": "item.completed", "item": {"type": "agent_message", "text": _ASSISTANT_TEXT}},
            {"type": "result", "is_error": True},
        ],
        "lionhub.providers.openai.codex.stream_codex_cli_events",
    )

    assert type(error.__cause__) is type(classify_provider_error("Codex session failed"))
    assert _ASSISTANT_TEXT not in str(error.__cause__)
    _assert_failed_call(branch, error, "Codex session failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "events", "seam", "session_id"),
    [
        (
            "codex",
            "gpt-5.1-codex",
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.failed", "error": {"message": "rate limit exceeded"}},
            ],
            "lionhub.providers.openai.codex.stream_codex_cli_events",
            "thread-1",
        ),
        (
            "claude_code",
            "sonnet",
            [
                {"type": "system", "subtype": "init", "session_id": "session-1"},
                {"type": "result", "result": "rate limit exceeded", "is_error": True},
            ],
            "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
            "session-1",
        ),
        (
            "gemini_code",
            "gemini-4-argon",
            [{"conversation_id": "conv-1", "status": "ERROR", "error": "rate limit exceeded"}],
            "lionhub.providers.google.gemini_code.stream_gemini_cli_events",
            "conv-1",
        ),
    ],
    ids=["codex", "claude_code", "gemini_code"],
)
async def test_a_cli_failed_call_keeps_the_provider_session_it_ran_in(
    provider,
    model,
    events,
    seam,
    session_id,
):
    """A failed non-streamed call leaves the session id on the endpoint, as a
    returned call and a streamed call do, so the next call can resume it."""
    chat_model = _chat_model(provider, model)
    branch = Branch(chat_model=chat_model)
    await _failed_chat(branch, provider, events, seam)

    assert chat_model.endpoint.session_id == session_id


@pytest.mark.asyncio
async def test_ndjson_from_cli_tail_repair_yields_repaired_object():
    """tail_repair callback is invoked on a malformed final JSON fragment and
    the repaired dict is yielded rather than silently dropped."""
    from lionhub.providers._cli_subprocess import SpawnedProcess, ndjson_from_cli

    malformed = b'{"type": "done", "missing_close":'

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.pid = 2  # >1 so the PGID guard passes
        proc.stdout = AsyncMock()
        # First read returns the malformed bytes; second signals EOF.
        proc.stdout.read = AsyncMock(side_effect=[malformed, b""])
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    repaired_obj = {"type": "done", "missing_close": None}

    def _repair(buf: str) -> dict | None:
        return repaired_obj

    collected = []
    with (
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch(
            "lionhub.providers._cli_subprocess.observe_spawned",
            side_effect=lambda pid, *, expected_marker: SpawnedProcess(
                pid=pid, pgid=pid, create_time=None
            ),
        ),
        patch("lionhub.providers._cli_subprocess._group_has_members", return_value=False),
    ):
        async with __import__("contextlib").aclosing(
            ndjson_from_cli([sys.executable], tail_repair=_repair)
        ) as stream:
            async for obj in stream:
                collected.append(obj)

    assert collected == [repaired_obj], (
        "tail_repair should yield the repaired object when raw_decode fails on the tail"
    )


@pytest.mark.asyncio
async def test_ndjson_from_cli_no_tail_repair_drops_bad_tail(caplog):
    """Without tail_repair the malformed tail is logged and dropped (default)."""
    import logging

    from lionhub.providers._cli_subprocess import SpawnedProcess, ndjson_from_cli

    malformed = b'{"type": "bad"'

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.pid = 2
        proc.stdout = AsyncMock()
        proc.stdout.read = AsyncMock(side_effect=[malformed, b""])
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    collected = []
    with (
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch(
            "lionhub.providers._cli_subprocess.observe_spawned",
            side_effect=lambda pid, *, expected_marker: SpawnedProcess(
                pid=pid, pgid=pid, create_time=None
            ),
        ),
        patch("lionhub.providers._cli_subprocess._group_has_members", return_value=False),
    ):
        with caplog.at_level(logging.ERROR):
            async with __import__("contextlib").aclosing(
                ndjson_from_cli([sys.executable])
            ) as stream:
                async for obj in stream:
                    collected.append(obj)

    assert collected == [], "No objects should be yielded for an unrecoverable tail"
    assert any("Skipped unrecoverable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_claude_ndjson_uses_repair_callback():
    """_ndjson_from_cli in Claude models passes tail_repair to ndjson_from_cli."""
    from lionhub.providers.anthropic import claude_code as cc_models
    from lionhub.providers.anthropic import claude_code as cc_pkg

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield  # make it an async generator

    with (
        patch.object(cc_models, "CLAUDE_CLI", "/fake/claude"),
        patch.object(cc_models, "ndjson_from_cli", _fake_ndjson),
    ):
        req = cc_models.ClaudeCodeRequest(prompt="hi")
        workspace = req.cwd()
        workspace.mkdir(parents=True, exist_ok=True)
        async for _ in cc_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    kw = call_kwargs[0]
    assert "tail_repair" in kw, "Claude must pass tail_repair to ndjson_from_cli"
    assert kw["tail_repair"] is not None, "tail_repair must not be None for Claude"


@pytest.mark.asyncio
async def test_codex_ndjson_does_not_pass_cwd(tmp_path):
    """Codex _ndjson_from_cli must NOT pass cwd= to ndjson_from_cli.

    The Codex CLI already receives the workspace via '-C <repo>' in as_cmd_args().
    Passing cwd= as well causes a relative repo path to be resolved twice.
    """
    from lionhub.providers.openai import codex as codex_models

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield

    with (
        patch.object(codex_models, "CODEX_CLI", "/fake/codex"),
        patch.object(codex_models, "ndjson_from_cli", _fake_ndjson),
    ):
        # repo must exist on disk: resolve_cli_workspace() now validates it
        # (a nonexistent --cwd must fail fast, not spawn) — the fictional
        # relative path this test previously used only worked because that
        # validation didn't exist yet.
        req = codex_models.CodexCodeRequest(prompt="p", repo=tmp_path)
        async for _ in codex_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    kw = call_kwargs[0]
    assert "cwd" not in kw or kw.get("cwd") is None, (
        "Codex must not pass cwd= to ndjson_from_cli (workspace is set via -C in argv)"
    )


def test_codex_as_cmd_args_contains_dash_c_flag(tmp_path):
    """Codex as_cmd_args() emits '-C <repo>' so the CLI handles the workspace itself."""
    from lionhub.providers.openai.codex import CodexCodeRequest

    # repo must exist on disk (see comment in test_codex_ndjson_does_not_pass_cwd).
    req = CodexCodeRequest(prompt="hello", repo=tmp_path)
    args = req.as_cmd_args()

    assert "-C" in args
    c_idx = args.index("-C")
    assert args[c_idx + 1] == str(tmp_path)


@pytest.mark.asyncio
async def test_gemini_ndjson_defaults_stdin_devnull():
    """agy runs headless print mode and reads nothing from stdin — gemini must
    NOT pass a stdin override, leaving ndjson_from_cli's DEVNULL default."""
    from lionhub.providers.google import gemini_code as gemini_models

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield

    with (
        patch.object(gemini_models, "AGY_CLI", "/fake/agy"),
        patch.object(gemini_models, "ndjson_from_cli", _fake_ndjson),
    ):
        req = gemini_models.GeminiCodeRequest(prompt="hello")
        workspace = req.cwd()
        workspace.mkdir(parents=True, exist_ok=True)
        async for _ in gemini_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    assert "stdin" not in call_kwargs[0], (
        "agy print mode must not override stdin (DEVNULL default applies)"
    )


@pytest.mark.asyncio
async def test_pi_ndjson_passes_inherit_stdin():
    """Pi _ndjson_from_cli must pass stdin=_INHERIT_STDIN to ndjson_from_cli."""
    from lionhub.providers._cli_subprocess import _INHERIT_STDIN
    from lionhub.providers.pi import cli as pi_models

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield

    with (
        patch.object(pi_models, "PI_CLI", "/fake/pi"),
        patch.object(pi_models, "ndjson_from_cli", _fake_ndjson),
    ):
        req = pi_models.PiCodeRequest(prompt="hello")
        async for _ in pi_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    kw = call_kwargs[0]
    assert kw.get("stdin") is _INHERIT_STDIN, (
        "Pi must pass stdin=_INHERIT_STDIN so the child inherits the parent stdin"
    )


@pytest.mark.asyncio
async def test_ndjson_from_cli_inherit_stdin_omits_stdin_kwarg():
    """When stdin=_INHERIT_STDIN the helper must NOT pass stdin to create_subprocess_exec."""
    from lionhub.providers._cli_subprocess import _INHERIT_STDIN, SpawnedProcess, ndjson_from_cli

    captured_kwargs: list[dict] = []

    async def _fake_exec(*args, **kwargs):
        captured_kwargs.append(kwargs)
        proc = MagicMock()
        proc.pid = 2
        proc.stdout = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"")
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch(
            "lionhub.providers._cli_subprocess.observe_spawned",
            side_effect=lambda pid, *, expected_marker: SpawnedProcess(
                pid=pid, pgid=pid, create_time=None
            ),
        ),
        patch("lionhub.providers._cli_subprocess._group_has_members", return_value=False),
    ):
        async with __import__("contextlib").aclosing(
            ndjson_from_cli([sys.executable], stdin=_INHERIT_STDIN)
        ) as stream:
            async for _ in stream:
                pass

    assert captured_kwargs, "create_subprocess_exec must be called"
    kw = captured_kwargs[0]
    assert "stdin" not in kw, (
        "_INHERIT_STDIN sentinel must cause ndjson_from_cli to omit stdin "
        "from create_subprocess_exec, so the child inherits parent stdin"
    )


@pytest.mark.asyncio
async def test_ndjson_from_cli_devnull_passes_stdin_devnull():
    """Default stdin=DEVNULL must be forwarded to create_subprocess_exec (Claude/Codex)."""
    from lionhub.providers._cli_subprocess import SpawnedProcess, ndjson_from_cli

    captured_kwargs: list[dict] = []

    async def _fake_exec(*args, **kwargs):
        captured_kwargs.append(kwargs)
        proc = MagicMock()
        proc.pid = 2
        proc.stdout = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"")
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch(
            "lionhub.providers._cli_subprocess.observe_spawned",
            side_effect=lambda pid, *, expected_marker: SpawnedProcess(
                pid=pid, pgid=pid, create_time=None
            ),
        ),
        patch("lionhub.providers._cli_subprocess._group_has_members", return_value=False),
    ):
        async with __import__("contextlib").aclosing(ndjson_from_cli([sys.executable])) as stream:
            async for _ in stream:
                pass

    assert captured_kwargs
    kw = captured_kwargs[0]
    assert kw.get("stdin") == asyncio.subprocess.DEVNULL, (
        "Default behaviour (DEVNULL) must be forwarded to create_subprocess_exec"
    )
