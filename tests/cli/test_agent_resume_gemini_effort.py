# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li agent -r --effort` re-applying effort to gemini-code /
gemini-cli (agy) branches — issue #1595.

agy has no effort flag/kwarg; effort is folded into the `--model` display
name (e.g. "Gemini 3.5 Flash (High)"). On resume, `cfg["model"]` already
holds that resolved display name from the first turn. `resolve_agy_model`'s
exact-match short-circuit (correct for a caller-typed pin) was firing on
this *persisted* value too, silently dropping a new `--effort` passed on
resume with no new `--model`. These tests pin:

  * resume + explicit --effort (no new model) re-applies effort onto the
    persisted agy model name,
  * resume without --effort leaves the persisted model/effort untouched,
  * resume with a new --model AND --effort still lets the explicit model
    pin win over --effort (unchanged pre-existing semantics),
  * the same shared cfg["model"] resume path is exercised for both
    gemini-code and gemini-cli aliases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli.test_agent_resume_model_override import _wire_agent_stubs


def _capture_resolved_model(monkeypatch) -> list[str | None]:
    """Patch Branch.operate to record chat_model's kwargs["model"] at call
    time — _wire_agent_stubs' teardown stub never re-persists the branch to
    disk, so the on-disk fixture file can't be re-read post-resume."""
    from lionagi import Branch

    captured: list[str | None] = []

    async def fake_operate(self, instruction=None, **kw):
        captured.append(self.chat_model.endpoint.config.kwargs.get("model"))
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    return captured


def _make_gemini_branch_json(tmp_path: Path, provider: str, model: str) -> tuple[str, Path]:
    """Persist a serialised Branch whose chat_model is an agy provider with
    an already-resolved (...)-qualified model name, as if a prior turn had
    folded --effort into it."""
    from lionagi import Branch, iModel

    b = Branch(
        chat_model=iModel(
            provider=provider,
            endpoint="query_cli",
            model=model,
            api_key="dummy",
        )
    )
    branch_id = str(b.id)
    p = tmp_path / f"{branch_id}.json"
    p.write_text(json.dumps(b.to_dict()))
    return branch_id, p


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini-code", "gemini-cli"])
async def test_resume_explicit_effort_reapplies_onto_persisted_agy_model(
    monkeypatch, tmp_path, provider
):
    """`li agent -r <id> --effort high` (no new model) must replace the
    persisted 'Low' suffix with 'High' rather than no-op."""
    branch_id, branch_path = _make_gemini_branch_json(tmp_path, provider, "Gemini 3.5 Flash (Low)")

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    captured = _capture_resolved_model(monkeypatch)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
        None,
        "continue",
        resume=branch_id,
        effort="high",
    )

    assert terminal_status == "completed"
    assert captured == ["Gemini 3.5 Flash (High)"], (
        f"new --effort on resume must replace the persisted agy model suffix, got {captured}"
    )


@pytest.mark.asyncio
async def test_resume_without_effort_keeps_persisted_agy_model(monkeypatch, tmp_path):
    """`li agent -r <id>` with no --effort must leave the persisted agy
    model name (and its baked-in effort suffix) untouched."""
    branch_id, branch_path = _make_gemini_branch_json(
        tmp_path, "gemini-code", "Gemini 3.5 Flash (Low)"
    )

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    captured = _capture_resolved_model(monkeypatch)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
        None,
        "continue",
        resume=branch_id,
        effort=None,
    )

    assert terminal_status == "completed"
    assert captured == ["Gemini 3.5 Flash (Low)"]


@pytest.mark.asyncio
async def test_resume_new_model_pin_still_wins_over_effort(monkeypatch, tmp_path):
    """An explicit --model given on THIS resume call is a caller-typed pin —
    it must still win over --effort (unchanged pre-existing semantics),
    unlike the persisted-only case above."""
    branch_id, branch_path = _make_gemini_branch_json(
        tmp_path, "gemini-code", "Gemini 3.5 Flash (Low)"
    )

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    captured = _capture_resolved_model(monkeypatch)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
        "gemini-code/Gemini 3.1 Pro (Low)",
        "continue",
        resume=branch_id,
        effort="high",
    )

    assert terminal_status == "completed"
    assert captured == ["Gemini 3.1 Pro (Low)"]


@pytest.mark.asyncio
async def test_fresh_run_unaffected_by_reapply_effort(monkeypatch, tmp_path):
    """A fresh (non-resume) gemini-code run is unaffected: --effort folds
    into the model name exactly as before this fix."""
    from unittest.mock import AsyncMock

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fake_operate(self, instruction=None, **kw):
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: "high")

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(
        ctx,
        *,
        status="completed",
        exception=None,
        cwd=None,
        engine_session_uid=None,
        defer_terminal=False,
    ):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)

    from types import SimpleNamespace

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

    from lionagi.cli.agent import _run_agent

    _result, provider, _bid, terminal_status, _sid = await _run_agent(
        "gemini-code/gemini-3.5-flash",
        "hello",
        effort="high",
    )

    assert terminal_status == "completed"
    assert provider == "gemini-code"


@pytest.mark.asyncio
async def test_resume_mixed_case_effort_reapplies_correct_agy_tier(monkeypatch, tmp_path):
    """`li agent -r <id> --effort High` (mixed case, no new model) must
    replace the persisted 'Low' suffix with 'High', not silently misclamp
    to 'Medium' via a lowercase-keyed dict miss."""
    branch_id, branch_path = _make_gemini_branch_json(
        tmp_path, "gemini-code", "Gemini 3.5 Flash (Low)"
    )

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    captured = _capture_resolved_model(monkeypatch)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
        None,
        "continue",
        resume=branch_id,
        effort="High",
    )

    assert terminal_status == "completed"
    assert captured == ["Gemini 3.5 Flash (High)"], (
        f"mixed-case --effort on resume must not misclamp, got {captured}"
    )


@pytest.mark.asyncio
async def test_resume_mixed_case_profile_effort_reapplies_correct_agy_tier(monkeypatch, tmp_path):
    """A profile with `effort: High` (mixed case) merged in when no --effort
    is given must also resolve to 'High' on resume — the profile merge
    happens after the CLI arg is folded, so it needs its own normalization."""
    from types import SimpleNamespace

    branch_id, branch_path = _make_gemini_branch_json(
        tmp_path, "gemini-code", "Gemini 3.5 Flash (Low)"
    )

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    monkeypatch.setattr(
        agent_mod,
        "load_agent_profile",
        lambda name: SimpleNamespace(
            model=None,
            effort="High",
            yolo=False,
            fast_mode=False,
            system_prompt=None,
            artifact_defaults=None,
            timeout=None,
            resume_on_timeout=False,
        ),
    )
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    captured = _capture_resolved_model(monkeypatch)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
        None,
        "continue",
        resume=branch_id,
        effort=None,
        agent_name="mixed-case-profile",
    )

    assert terminal_status == "completed"
    assert captured == ["Gemini 3.5 Flash (High)"], (
        f"mixed-case profile effort on resume must not misclamp, got {captured}"
    )
