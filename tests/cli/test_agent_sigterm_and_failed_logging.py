# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for li agent diagnostic logging on failure/SIGTERM.

Covers the two `classify_exception(exc) == "failed"` log_error sites (the
inner _run_agent except and the outer run_agent except) plus run_agent()'s
SigtermInterrupt dispatch and _util.classify_exception()'s SigtermInterrupt
mapping. Previously both "failed" sites re-raised silently, relying on
Python's default traceback printer — unreliable under SIGTERM/process death.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _wire_agent_stubs(monkeypatch, tmp_path: Path):
    """Monkeypatch all external I/O in _run_agent so tests run without real I/O."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )
    return Branch


class _BoomError(RuntimeError):
    """Distinctive exception type so assertions can check for its name."""


def _agent_args(**overrides) -> SimpleNamespace:
    base = dict(
        query=["codex/model", "do the thing"],
        prompt_flag=None,
        prompt_file=None,
        yolo=False,
        verbose=False,
        theme=None,
        resume=None,
        continue_last=False,
        effort=None,
        agent=None,
        cwd=None,
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        preset=None,
        form=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Site 1: _run_agent's inner except — "failed" bucket must log before raising
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_inner_logs_failed_exception_before_propagating(monkeypatch, tmp_path):
    """_run_agent must log_error a 'failed'-classified exception before it propagates."""
    import lionagi.cli.agent as agent_mod

    Branch = _wire_agent_stubs(monkeypatch, tmp_path)

    errors_emitted: list[str] = []
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors_emitted.append(msg))

    async def fake_operate(self, instruction=None, **kw):
        raise _BoomError("simulated inner failure")

    monkeypatch.setattr(Branch, "operate", fake_operate)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(_BoomError):
        await _run_agent("codex/model", "do the thing")

    assert errors_emitted, "log_error must be called for a 'failed'-classified exception"
    combined = " ".join(errors_emitted)
    assert "_BoomError" in combined, (
        f"log_error message should name the exception type; got: {errors_emitted}"
    )
    assert "simulated inner failure" in combined


@pytest.mark.asyncio
async def test_run_agent_inner_does_not_log_on_success(monkeypatch, tmp_path):
    """Regression guard: a normal completion must not call log_error at all."""
    import lionagi.cli.agent as agent_mod

    Branch = _wire_agent_stubs(monkeypatch, tmp_path)

    errors_emitted: list[str] = []
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors_emitted.append(msg))

    async def fake_operate(self, instruction=None, **kw):
        return "all good"

    monkeypatch.setattr(Branch, "operate", fake_operate)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
        "codex/model", "do the thing"
    )

    assert terminal_status == "completed"
    assert not errors_emitted, f"log_error should not fire on success; got: {errors_emitted}"


# ---------------------------------------------------------------------------
# Site 2: run_agent's outer except — "failed" bucket must log before raising
# ---------------------------------------------------------------------------


def test_run_agent_outer_logs_failed_exception_before_reraising(monkeypatch):
    """run_agent()'s outer except must log_error a non-cancellation exception before re-raising."""
    import lionagi.cli.agent as agent_mod

    errors_emitted: list[str] = []
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors_emitted.append(msg))

    def fake_run_async(coro):
        coro.close()  # avoid an "coroutine was never awaited" warning
        raise _BoomError("outer boundary failure")

    monkeypatch.setattr(agent_mod, "run_async", fake_run_async)

    from lionagi.cli.agent import run_agent

    with pytest.raises(_BoomError):
        run_agent(_agent_args())

    assert errors_emitted, "log_error must be called before the outer except re-raises"
    combined = " ".join(errors_emitted)
    assert "_BoomError" in combined
    assert "outer boundary failure" in combined


# ---------------------------------------------------------------------------
# Site 2: run_agent's new SigtermInterrupt branch
# ---------------------------------------------------------------------------


def test_run_agent_outer_handles_sigterm_interrupt(monkeypatch):
    """SigtermInterrupt from run_async() must warn + exit 'cancelled' (143), not raise."""
    import lionagi.cli._logging as logging_mod
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._util import EXIT_CODE_BY_STATUS
    from lionagi.ln.concurrency import SigtermInterrupt

    warnings_emitted: list[str] = []
    monkeypatch.setattr(logging_mod, "warn", lambda msg: warnings_emitted.append(msg))

    def fake_run_async(coro):
        coro.close()
        raise SigtermInterrupt("process received SIGTERM; inner task cancelled")

    monkeypatch.setattr(agent_mod, "run_async", fake_run_async)

    from lionagi.cli.agent import run_agent

    rc = run_agent(_agent_args())

    assert rc == EXIT_CODE_BY_STATUS["cancelled"] == 143
    assert warnings_emitted, "a SIGTERM run must leave a clear warning"
    assert any("sigterm" in w.lower() for w in warnings_emitted), (
        f"warning should name SIGTERM; got: {warnings_emitted}"
    )


def test_run_agent_outer_sigterm_not_caught_as_generic_failure(monkeypatch):
    """SigtermInterrupt must be dispatched by its own branch, not fall into the failed-bucket log."""
    import lionagi.cli._logging as logging_mod
    import lionagi.cli.agent as agent_mod
    from lionagi.ln.concurrency import SigtermInterrupt

    errors_emitted: list[str] = []
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors_emitted.append(msg))
    monkeypatch.setattr(logging_mod, "warn", lambda msg: None)

    def fake_run_async(coro):
        coro.close()
        raise SigtermInterrupt("process received SIGTERM; inner task cancelled")

    monkeypatch.setattr(agent_mod, "run_async", fake_run_async)

    from lionagi.cli.agent import run_agent

    run_agent(_agent_args())

    assert not errors_emitted, (
        f"SigtermInterrupt should not hit the generic failed-bucket log_error; got: {errors_emitted}"
    )


# ---------------------------------------------------------------------------
# _util.classify_exception: SigtermInterrupt maps to the "cancelled" bucket
# ---------------------------------------------------------------------------


def test_classify_exception_sigterm_interrupt_maps_to_cancelled():
    """classify_exception(SigtermInterrupt(...)) must return 'cancelled' (exit code 143)."""
    from lionagi.cli._util import classify_exception
    from lionagi.ln.concurrency import SigtermInterrupt

    assert classify_exception(SigtermInterrupt("received SIGTERM")) == "cancelled"


def test_classify_exception_sigterm_interrupt_distinct_from_aborted():
    """SigtermInterrupt must not be classified as 'aborted' (that bucket is SIGINT/Ctrl-C only)."""
    from lionagi.cli._util import classify_exception
    from lionagi.ln.concurrency import SigtermInterrupt

    assert classify_exception(SigtermInterrupt("received SIGTERM")) != "aborted"
