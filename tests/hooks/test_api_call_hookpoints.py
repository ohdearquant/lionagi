# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0047 delta row 2 (issue #1965): API_PRE_CALL / API_POST_CALL / API_STREAM_CHUNK
gain a real emit site through the typed, optional service-to-session adapter in
``operations/_api_hooks.py``, wired at ``operations/chat/chat.py`` (one-shot API
calls) and ``operations/run/run.py`` (CLI streaming). A standalone iModel (no
Branch, no HookBus) must be completely unaffected -- these callsites live in the
Branch-facing operation layer, never inside ``iModel``/``HookRegistry`` itself.
"""

from __future__ import annotations

import types

import pytest

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.ln import json_dumps
from lionagi.operations.run.run import run
from lionagi.operations.types import RunParam
from lionagi.protocols.generic.event import EventStatus
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


def _wire(branch: Branch, *points: HookPoint) -> dict[HookPoint, list[dict]]:
    bus = HookBus()
    calls: dict[HookPoint, list[dict]] = {p: [] for p in points}

    def _make(point):
        async def _handler(**kw):
            calls[point].append(kw)

        return _handler

    for p in points:
        bus.on(p, _make(p))
    branch._hooks = bus
    return calls


def _make_fake_cli_model(chunks: list[StreamChunk]) -> iModel:
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    m.endpoint = types.SimpleNamespace(is_cli=True, session_id=None, to_dict=lambda: {})
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    from unittest.mock import AsyncMock

    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream
    return m


# ── chat() (one-shot API path) ───────────────────────────────────────────────


async def test_chat_emits_api_pre_and_post_call_on_success():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    result = await branch.chat("hi there")

    assert result == "hello"
    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1

    pre = calls[HookPoint.API_PRE_CALL][0]
    assert pre["branch_id"] == str(branch.id)
    assert pre["model"] == "gpt-4o-mini"
    assert pre["provider"] == "openai"

    post = calls[HookPoint.API_POST_CALL][0]
    assert post["branch_id"] == str(branch.id)
    assert post["status"] == "completed"
    assert post["error"] is None
    # The mocked APICalling never runs through Event.invoke()'s timing
    # wrapper, so execution.duration stays unset — latency_ms is correctly
    # None rather than fabricated. "latency_ms" key presence is what matters.
    assert "latency_ms" in post


async def test_chat_emits_api_post_call_with_failed_status_before_raising():
    branch = LionAGIMockFactory.create_mocked_branch(response="broken", status=EventStatus.FAILED)
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    from lionagi._errors import ExecutionError

    with pytest.raises(ExecutionError):
        await branch.chat("hi there")

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    assert calls[HookPoint.API_POST_CALL][0]["status"] == "failed"


async def test_chat_emits_api_post_call_with_error_status_when_invoke_raises():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    async def _boom(**kw):
        raise RuntimeError("provider is down")

    branch.chat_model.invoke = _boom

    with pytest.raises(RuntimeError, match="provider is down"):
        await branch.chat("hi there")

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    post = calls[HookPoint.API_POST_CALL][0]
    assert post["status"] == "error"
    # Class-name-only summary -- the raised exception's message must never
    # reach the emitted payload (it can carry request bodies, full URLs, or
    # credential fragments from the provider).
    assert post["error"] == "RuntimeError"
    assert "provider is down" not in post["error"]


async def test_chat_emits_api_post_call_error_from_failed_status_without_raise():
    """A provider-returned FAILED APICalling carries its failure reason on
    ``api_call.execution.error``, not as a raised exception -- the payload's
    error field must still reflect it rather than staying null, and must not
    leak the raw failure text either."""
    branch = LionAGIMockFactory.create_mocked_branch(response="broken", status=EventStatus.FAILED)

    async def _failed_invoke(**kw):
        api_call = LionAGIMockFactory.create_api_calling_mock(
            response_data="broken", status=EventStatus.FAILED
        )
        api_call.execution.error = "rate limited: sk-secret-token-12345"
        return api_call

    branch.chat_model.invoke = _failed_invoke
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    from lionagi._errors import ExecutionError

    with pytest.raises(ExecutionError):
        await branch.chat("hi there")

    post = calls[HookPoint.API_POST_CALL][0]
    assert post["status"] == "failed"
    assert post["error"] is not None
    assert "sk-secret-token-12345" not in post["error"]


async def test_chat_emits_paired_post_call_on_cancellation_during_invoke():
    """asyncio.CancelledError is a BaseException, not an Exception -- a bare
    ``except Exception`` around ``imodel.invoke()`` lets cancellation mid-call
    skip the post emission entirely, leaving an unpaired open span."""
    import asyncio

    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    invoke_started = asyncio.Event()

    async def _blocking_invoke(**kwargs):
        invoke_started.set()
        await asyncio.Event().wait()  # never resolves; only cancellation ends this

    branch.chat_model.invoke = _blocking_invoke

    task = asyncio.ensure_future(branch.chat("hi there"))
    await invoke_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    assert calls[HookPoint.API_POST_CALL][0]["status"] == "error"


async def test_chat_without_session_bus_does_not_crash():
    """A Branch with no HookBus attached (branch._hooks is None, the default)
    must behave exactly as before -- the adapter is a strict no-op."""
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    assert branch._hooks is None

    result = await branch.chat("hi there")
    assert result == "hello"


# ── run() (CLI streaming path) ───────────────────────────────────────────────


async def test_run_emits_pre_call_stream_chunks_and_post_call():
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="hel"),
            StreamChunk(type="text", content="lo"),
        ]
    )
    calls = _wire(
        branch, HookPoint.API_PRE_CALL, HookPoint.API_STREAM_CHUNK, HookPoint.API_POST_CALL
    )

    results = []
    async for msg in run(branch, "hi there", RunParam()):
        results.append(msg)

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_STREAM_CHUNK]) == 2
    assert all(c["chunk_type"] == "text" for c in calls[HookPoint.API_STREAM_CHUNK])
    assert len(calls[HookPoint.API_POST_CALL]) == 1


async def test_run_emits_post_call_once_even_on_stream_failure():
    """A provider error mid-stream must still settle exactly one API_POST_CALL
    (status="error"), pairing with the one API_PRE_CALL that started the call."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content="boom")])
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    with pytest.raises(Exception):
        async for _ in run(branch, "hi there", RunParam()):
            pass

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    assert calls[HookPoint.API_POST_CALL][0]["status"] == "error"


async def test_run_without_session_bus_never_calls_stream_chunk_adapter():
    """The no-bus guard belongs at the run.py call site, not only inside the
    adapter -- otherwise every streamed chunk still pays for constructing and
    awaiting a coroutine object that only checks ``branch._hooks is None``
    and returns. Patching the call site's own reference to
    ``emit_api_stream_chunk`` and asserting zero calls proves no coroutine
    for it is ever created on the no-bus path (not just that its body
    short-circuits)."""
    from unittest.mock import patch

    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [StreamChunk(type="text", content="a"), StreamChunk(type="text", content="b")]
    )
    assert branch._hooks is None

    with patch("lionagi.operations.run.run.emit_api_stream_chunk") as mock_emit:
        results = []
        async for msg in run(branch, "hi there", RunParam()):
            results.append(msg)

    assert results
    mock_emit.assert_not_called()


async def test_run_without_session_bus_does_not_crash():
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    assert branch._hooks is None

    results = []
    async for msg in run(branch, "hi there", RunParam()):
        results.append(msg)
    assert results  # ran to completion unaffected


async def test_run_terminal_tokens_survive_the_final_response_flush():
    """The last text flush (which happens after the stream loop ends, at the
    same point the terminal API_POST_CALL is emitted) clears result_meta
    right after stamping it onto the AssistantResponse -- a run whose
    "result" chunk arrives before its final text must still report the
    tokens it received, not None, at the terminal post-call emission."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="hello"),
            StreamChunk(
                type="result",
                metadata={"usage": {"input_tokens": 5, "output_tokens": 3}},
            ),
        ]
    )
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    results = []
    async for msg in run(branch, "hi there", RunParam()):
        results.append(msg)

    post = calls[HookPoint.API_POST_CALL][0]
    assert post["tokens"] == {"input_tokens": 5, "output_tokens": 3}


async def test_chat_api_post_call_never_leaks_secret_shaped_error_text():
    """A provider exception's message can carry request bodies, full URLs
    with query parameters, or credential fragments -- none of that may reach
    the emitted API_POST_CALL payload, which is persisted verbatim to
    observer telemetry."""
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    secret = "sk-live-do-not-leak-4f3a9c2b"

    async def _boom(**kw):
        raise RuntimeError(
            f"POST https://api.openai.com/v1/chat/completions?api_key={secret} failed"
        )

    branch.chat_model.invoke = _boom

    with pytest.raises(RuntimeError):
        await branch.chat("hi there")

    post = calls[HookPoint.API_POST_CALL][0]
    assert secret not in json_dumps(post)


# ── unified pairing/field invariant ──────────────────────────────────────────
#
# Every API_PRE_CALL is followed by exactly one API_POST_CALL carrying
# whatever is actually known at that point about how the call ended:
#
# | exit path                       | status      | error            | tokens         |
# |----------------------------------|-------------|------------------|----------------|
# | success                          | "completed" | None             | best-effort    |
# | provider FAILED, not raised      | "failed"    | class-name/label | best-effort    |
# | raised exception                 | "error"     | class-name/label | None           |
# | cancellation (BaseException)     | "error"     | "CancelledError" | None           |
# | CLI stream completion            | known/None   | None             | best-effort    |
# | CLI stream failure               | "error"     | class-name/label | partial/None   |
#
# A single parametrized test asserting this table generically -- rather than
# separate hardcoded scenario tests -- is what catches an exit path added later
# that forgets to populate `error` on failure, or that
# reintroduces raw message text: a scenario-specific test only checks the
# exact value it was written against, so a new path with the same shape but
# a different bug (e.g. `error` populated but with the raw message again)
# would slip through unless every failure path is checked against the same
# generic assertions.


async def _run_chat_scenario(scenario: str):
    if scenario == "success":
        branch = LionAGIMockFactory.create_mocked_branch(response="hello")
        calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)
        await branch.chat("hi there")
        return calls

    if scenario == "failed_status":
        branch = LionAGIMockFactory.create_mocked_branch(response="hello")

        async def _failed_invoke(**kw):
            api_call = LionAGIMockFactory.create_api_calling_mock(
                response_data="broken", status=EventStatus.FAILED
            )
            api_call.execution.error = "boom: leak-me-not"
            return api_call

        branch.chat_model.invoke = _failed_invoke
        calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)
        with pytest.raises(Exception):
            await branch.chat("hi there")
        return calls

    if scenario == "raised":
        branch = LionAGIMockFactory.create_mocked_branch(response="hello")

        async def _boom(**kw):
            raise RuntimeError("leak-me-not")

        branch.chat_model.invoke = _boom
        calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)
        with pytest.raises(RuntimeError):
            await branch.chat("hi there")
        return calls

    if scenario == "cancelled":
        import asyncio

        branch = LionAGIMockFactory.create_mocked_branch(response="hello")
        calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)
        invoke_started = asyncio.Event()

        async def _blocking_invoke(**kwargs):
            invoke_started.set()
            await asyncio.Event().wait()

        branch.chat_model.invoke = _blocking_invoke
        task = asyncio.ensure_future(branch.chat("hi there"))
        await invoke_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return calls

    raise AssertionError(f"unknown scenario {scenario!r}")


async def _run_stream_scenario(scenario: str):
    branch = Branch()
    if scenario == "stream_success":
        chunks = [StreamChunk(type="text", content="hello")]
    elif scenario == "stream_failure":
        chunks = [StreamChunk(type="error", content="boom: leak-me-not")]
    else:  # pragma: no cover - caller is parametrized below
        raise AssertionError(f"unknown scenario {scenario!r}")

    branch.chat_model = _make_fake_cli_model(chunks)
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)
    if scenario == "stream_failure":
        with pytest.raises(Exception):
            async for _ in run(branch, "hi there", RunParam()):
                pass
    else:
        async for _ in run(branch, "hi there", RunParam()):
            pass
    return calls


@pytest.mark.parametrize(
    "scenario",
    ["success", "failed_status", "raised", "cancelled", "stream_success", "stream_failure"],
)
async def test_api_post_call_field_invariant_holds_across_exit_paths(scenario):
    if scenario.startswith("stream_"):
        calls = await _run_stream_scenario(scenario)
    else:
        calls = await _run_chat_scenario(scenario)

    # Pairing: exactly one pre-call, exactly one post-call, regardless of exit path.
    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    post = calls[HookPoint.API_POST_CALL][0]

    if scenario in ("success", "stream_success"):
        if scenario == "success":
            assert post["status"] == "completed"
        assert post["error"] is None
    else:
        # Every non-success exit path must populate error (never silently
        # null just because the failure surfaced via a status field instead
        # of a raised exception), and it must never carry raw message text --
        # every scenario above embeds a distinct marker string that would
        # appear verbatim in `error` if the fix regressed to str(error) /
        # str(execution.error).
        assert post["error"] is not None
        assert "leak-me-not" not in post["error"]
        assert post["status"] in ("failed", "error")


# ── closed telemetry payload (issue #1965 fix leg r3) ───────────────────────


async def test_api_post_call_closed_payload_scrubs_secret_shaped_marker_everywhere():
    """SessionObserver-backed probe: a settled call whose status object,
    tokens mapping, model name, and provider name all carry the SAME
    secret-shaped marker. The adapter boundary must build a closed,
    allowlisted payload -- none of the four fields may reach persistence
    with the marker intact, because field-by-field redaction is a treadmill
    and the wrong class of fix."""
    import types as _types

    from lionagi.hooks.bus import HookBus, HookSignal
    from lionagi.session.observer import SessionObserver, _sanitize_signal_payload

    secret = "sk-LEAK var=name;drop-table"

    branch = Branch()
    observer = SessionObserver()
    branch._hooks = HookBus(observer=observer)

    imodel = _types.SimpleNamespace(
        model_name=secret,
        endpoint=_types.SimpleNamespace(config=_types.SimpleNamespace(provider=secret)),
    )
    api_call = _types.SimpleNamespace(
        # A status object whose .value is raw text, not validated against
        # EventStatus -- exactly the shape a non-exception provider failure
        # reason can take without ever raising.
        status=_types.SimpleNamespace(value=secret),
        execution=_types.SimpleNamespace(duration=None, error=None),
        response=None,
    )
    tokens = {"input_tokens": 5, "output_tokens": 3, "raw_provider_debug": secret}

    from lionagi.operations._api_hooks import emit_api_post_call

    await emit_api_post_call(branch, imodel, api_call, tokens=tokens)

    signals = observer.by_type(HookSignal)
    assert len(signals) == 1
    payload = _sanitize_signal_payload(signals[0])

    assert secret not in json_dumps(payload)
    kwargs = payload["kwargs"]
    assert kwargs["status"] == "unknown"
    assert kwargs["model"] == "unknown"
    assert kwargs["provider"] == "unknown"
    assert kwargs["tokens"] == {"input_tokens": 5, "output_tokens": 3}


async def test_api_post_call_closed_payload_preserves_legitimate_values():
    """The closed-payload rewrite must not collapse ordinary, well-shaped
    values to "unknown" -- only out-of-vocabulary/out-of-shape input is
    redacted."""
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    await branch.chat("hi there")

    pre = calls[HookPoint.API_PRE_CALL][0]
    post = calls[HookPoint.API_POST_CALL][0]
    assert pre["model"] == "gpt-4o-mini"
    assert pre["provider"] == "openai"
    assert post["status"] == "completed"


# ── multi-turn usage accumulation (issue #1965 fix leg r3) ──────────────────


async def test_run_accumulates_usage_across_multiple_flush_windows():
    """Codex splits one run() call into multiple flush windows (a tool-call
    round-trip forces a flush between "result" chunks) and reports marginal
    per-window deltas, not a running total. The terminal API_POST_CALL must
    report the SUM of every window's usage, not just the last window's --
    overwriting last_usage from each window's result_meta silently drops
    every earlier window's tokens."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="hello"),
            StreamChunk(
                type="result",
                metadata={"usage": {"input_tokens": 2, "output_tokens": 1}},
            ),
            StreamChunk(
                type="tool_use",
                tool_name="lookup",
                tool_id="t1",
                tool_input={"q": "x"},
            ),
            StreamChunk(type="tool_result", tool_id="t1", tool_output="ok"),
            StreamChunk(type="text", content="world"),
            StreamChunk(
                type="result",
                metadata={"usage": {"input_tokens": 10, "output_tokens": 7}},
            ),
        ]
    )
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    results = []
    async for msg in run(branch, "hi there", RunParam()):
        results.append(msg)

    post = calls[HookPoint.API_POST_CALL][0]
    assert post["tokens"] == {"input_tokens": 12, "output_tokens": 8}


async def test_run_single_result_chunk_usage_unaffected_by_accumulator():
    """A provider that emits exactly one "result" chunk per run() call
    (claude_code, gemini_code) must report exactly that chunk's usage --
    the whole-call accumulator must not introduce a double-count when there
    is nothing to accumulate across."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="hello"),
            StreamChunk(
                type="result",
                metadata={"usage": {"input_tokens": 5, "output_tokens": 3}},
            ),
        ]
    )
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    async for _ in run(branch, "hi there", RunParam()):
        pass

    post = calls[HookPoint.API_POST_CALL][0]
    assert post["tokens"] == {"input_tokens": 5, "output_tokens": 3}
