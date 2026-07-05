# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li agent -r` resume-path model-override validation.

A mangled `-r <branch-id>` invocation (e.g. the id accidentally split across
two argv tokens) leaves the stray fragment as the MODEL positional. On
resume that fragment is grafted into the existing branch's config with no
validation, silently poisoning the persisted snapshot. These tests pin:

  * an implausible bare token as the resume MODEL override is rejected
    (non-zero exit, actionable error) instead of being grafted,
  * a legitimate `provider/model` override still proceeds and is announced
    with a loud warning before it's grafted,
  * a prefix-matched `--resume` id is announced via a hint.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _wire_agent_stubs(monkeypatch, tmp_path: Path, operate_return=None):
    """Monkeypatch all external I/O in _run_agent so tests run without real I/O."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fake_operate(self, instruction=None, **kw):
        return operate_return

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())

    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

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


def _make_branch_json(tmp_path: Path) -> tuple[str, Path]:
    """Persist a minimal serialised Branch and return (branch_id, path)."""
    from lionagi import Branch

    b = Branch()
    branch_id = str(b.id)
    p = tmp_path / f"{branch_id}.json"
    p.write_text(json.dumps(b.to_dict()))
    return branch_id, p


def _reset_channel(name: str) -> logging.Logger:
    """Clear handlers + force propagate=True so caplog captures this channel.

    Other tests in the suite call configure_cli_logging(), which sets
    propagate=False and attaches a stderr handler on these channels.
    """
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = True
    return logger


# ---------------------------------------------------------------------------
# Implausible bare token as resume MODEL override → rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_garbage_model_token_on_resume_is_rejected(monkeypatch, tmp_path, caplog):
    """A mangled bare token (e.g. a split --resume id) as MODEL must not graft."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path)

    _reset_channel("lionagi.cli.error")

    from lionagi.cli.agent import _run_agent

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        result, _provider, _bid, terminal_status, _sid = await _run_agent(
            "c95-32617270327a",
            "actual prompt",
            resume=branch_id,
        )

    assert terminal_status == "failed"
    assert result == ""
    error_text = " ".join(rec.message for rec in caplog.records)
    assert "c95-32617270327a" in error_text
    assert "MODEL" in error_text or "model spec" in error_text.lower()


@pytest.mark.asyncio
async def test_garbage_model_token_on_resume_exits_nonzero(monkeypatch, tmp_path, caplog):
    """run_agent() must return non-zero exit code for a garbage resume MODEL token."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli.agent as agent_mod
    from lionagi.ln.concurrency import run_async as _real_run_async

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "run_async", _real_run_async)

    _reset_channel("lionagi.cli.error")
    _reset_channel("lionagi.cli.hint")

    args = SimpleNamespace(
        query=["c95-32617270327a", "actual prompt"],
        prompt_flag=None,
        prompt_file=None,
        yolo=False,
        verbose=False,
        theme=None,
        resume=branch_id,
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

    from lionagi.cli.agent import run_agent

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        rc = run_agent(args)

    assert rc != 0, f"Expected non-zero exit code for a garbage resume MODEL token, got {rc}"
    assert any("c95-32617270327a" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Legitimate provider/model override on resume → proceeds + warns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legitimate_model_override_on_resume_proceeds_and_warns(
    monkeypatch, tmp_path, caplog
):
    """A real provider/model override on resume is grafted, with a loud warning."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="here is the verdict")

    _reset_channel("lionagi.cli.warn")

    from lionagi.cli.agent import _run_agent

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        result, provider, _bid, terminal_status, _sid = await _run_agent(
            "claude_code/opus",
            "follow up",
            resume=branch_id,
        )

    assert terminal_status == "completed"
    assert result == "here is the verdict"
    assert provider == "claude_code"
    warn_text = " ".join(rec.message for rec in caplog.records)
    assert "resume model override" in warn_text
    assert "opus" in warn_text


@pytest.mark.asyncio
async def test_matching_model_override_on_resume_does_not_warn(monkeypatch, tmp_path, caplog):
    """Re-supplying the branch's own model on resume must not trigger the override warning."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")

    _reset_channel("lionagi.cli.warn")

    from lionagi.cli.agent import _run_agent

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        # The fixture branch's default chat_model is openai/gpt-4.1-mini
        # (see _make_branch_json → Branch()) — re-supply the same model.
        _result, _provider, _bid, terminal_status, _sid = await _run_agent(
            "openai/gpt-4.1-mini",
            "follow up",
            resume=branch_id,
        )

    assert terminal_status == "completed"
    override_warns = [
        rec.message for rec in caplog.records if "resume model override" in rec.message
    ]
    assert not override_warns, f"Unexpected override warning for a no-op override: {override_warns}"


# ---------------------------------------------------------------------------
# Prefix-matched resume id → hint emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefix_matched_resume_id_emits_hint(monkeypatch, tmp_path, caplog):
    """A truncated --resume id that prefix-matches a branch file is announced via hint."""
    _full_branch_id, branch_path = _make_branch_json(tmp_path)
    given_token = _full_branch_id[:8]  # truncated, as if the user shortened it

    import lionagi.cli.agent as agent_mod

    # find_branch() is what actually performs the prefix glob-match in the
    # real code path (lionagi/cli/_runs.py); stub it directly to return the
    # already-resolved full-id path, mirroring what a real prefix match does.
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")

    _reset_channel("lionagi.cli.hint")

    from lionagi.cli.agent import _run_agent

    with caplog.at_level(logging.INFO, logger="lionagi.cli.hint"):
        _result, _provider, _bid, terminal_status, _sid = await _run_agent(
            "codex/gpt-5.3-codex-spark",
            "follow up",
            resume=given_token,
        )

    assert terminal_status == "completed"
    hint_text = " ".join(rec.message for rec in caplog.records)
    assert "prefix-matched" in hint_text
    assert given_token in hint_text
    assert _full_branch_id in hint_text


@pytest.mark.asyncio
async def test_exact_resume_id_does_not_emit_prefix_hint(monkeypatch, tmp_path, caplog):
    """An exact --resume id match must not emit the prefix-matched hint."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")

    _reset_channel("lionagi.cli.hint")

    from lionagi.cli.agent import _run_agent

    with caplog.at_level(logging.INFO, logger="lionagi.cli.hint"):
        _result, _provider, _bid, terminal_status, _sid = await _run_agent(
            "codex/gpt-5.3-codex-spark",
            "follow up",
            resume=branch_id,
        )

    assert terminal_status == "completed"
    prefix_hints = [rec.message for rec in caplog.records if "prefix-matched" in rec.message]
    assert not prefix_hints, f"Unexpected prefix-match hint for an exact id: {prefix_hints}"
