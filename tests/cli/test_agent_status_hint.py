# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the post-run hint surfacing session_id alongside the resume line.

`li agent`'s post-run hint prints the branch_id as a `-r <branch_id>` resume
command, but that id alone isn't a `li agent status` lookup key an operator
already has memorized — they'd have to know the session_id separately. These
tests pin: the hint also prints `li agent status <session_id>` whenever live
persistence produced a session, and omits that line cleanly when persistence
never started (setup failure, or the mangled resume-model-override guard
firing before persistence is even set up).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _reset_channel(name: str) -> logging.Logger:
    """Clear handlers + force propagate=True so caplog captures this channel.

    Other tests in the suite call configure_cli_logging(), which sets
    propagate=False and attaches a stderr handler on these channels.
    """
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = True
    return logger


def _wire_agent_stubs(monkeypatch, tmp_path, *, session_id: str | None):
    """Monkeypatch all external I/O in _run_agent so tests run without real
    I/O. setup_agent_persist returns a ctx carrying *session_id*, or None
    (simulating persistence never starting) when *session_id* is None.
    """
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fake_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())

    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return {"session_id": session_id} if session_id else None

    async def fake_teardown(
        ctx, *, status="completed", exception=None, cwd=None, engine_session_uid=None
    ):
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


def _agent_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        query=["claude", "hello"],
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
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _run_agent: session_id is the 5th return value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_returns_session_id_when_persisted(monkeypatch, tmp_path):
    """_run_agent's 5th return value is the live-persist session_id."""
    _wire_agent_stubs(monkeypatch, tmp_path, session_id="sess-abc123")

    from lionagi.cli.agent import _run_agent

    _result, _provider, _branch_id, _status, session_id = await _run_agent("claude", "hello")

    assert session_id == "sess-abc123"


@pytest.mark.asyncio
async def test_run_agent_session_id_none_when_persist_disabled(monkeypatch, tmp_path):
    """setup_agent_persist failure (returns None) -> session_id is None, not a crash."""
    _wire_agent_stubs(monkeypatch, tmp_path, session_id=None)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _branch_id, _status, session_id = await _run_agent("claude", "hello")

    assert session_id is None


# ---------------------------------------------------------------------------
# run_agent(): post-run hint includes the status line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_run_hint_includes_status_line_with_session_id(monkeypatch, tmp_path, caplog):
    """The post-run hint must surface `li agent status <session_id>` alongside
    the existing resume line, so an operator has both ids up front."""
    _wire_agent_stubs(monkeypatch, tmp_path, session_id="sess-xyz789")

    import lionagi.cli.agent as agent_mod
    from lionagi.ln.concurrency import run_async as _real_run_async

    monkeypatch.setattr(agent_mod, "run_async", _real_run_async)

    _reset_channel("lionagi.cli.hint")

    from lionagi.cli.agent import run_agent

    with caplog.at_level(logging.INFO, logger="lionagi.cli.hint"):
        rc = run_agent(_agent_args())

    assert rc == 0
    hint_text = "\n".join(rec.message for rec in caplog.records)
    assert "[to resume]" in hint_text
    assert "li agent -r " in hint_text
    assert "li agent status sess-xyz789" in hint_text


@pytest.mark.asyncio
async def test_post_run_hint_omits_status_line_when_no_session(monkeypatch, tmp_path, caplog):
    """No live session (persistence disabled/failed) -> the resume line still
    prints, but no status line — there's nothing to point it at."""
    _wire_agent_stubs(monkeypatch, tmp_path, session_id=None)

    import lionagi.cli.agent as agent_mod
    from lionagi.ln.concurrency import run_async as _real_run_async

    monkeypatch.setattr(agent_mod, "run_async", _real_run_async)

    _reset_channel("lionagi.cli.hint")

    from lionagi.cli.agent import run_agent

    with caplog.at_level(logging.INFO, logger="lionagi.cli.hint"):
        rc = run_agent(_agent_args())

    assert rc == 0
    hint_text = "\n".join(rec.message for rec in caplog.records)
    assert "[to resume]" in hint_text
    assert "li agent status" not in hint_text
