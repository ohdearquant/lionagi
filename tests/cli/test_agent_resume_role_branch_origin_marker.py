# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: resuming a role-composed branch must not clobber its persisted
system message even when the CURRENT invocation's profile no longer signals
`role:` — either because a different, plain `-a` profile is supplied, or
because the same profile file has since had its `role:` key removed.

A prior guard derived "was this branch composed via create_agent?" from the
profile reloaded for the *resuming* invocation (`has_role_key`). That is the
wrong signal on a resumed leg: `has_role_key` describes only what was passed
to this particular `-a`, not how the persisted branch was originally built.
Resuming a role-composed branch under a mismatched profile made the guard
treat it as a plain branch and call `branch.msgs.add_message(system=...)`,
which replaces the branch's composed role header + policy block with the
current profile's bare body.

The fix stamps every create_agent-built branch with an immutable origin
marker in `branch.metadata` (`CREATE_AGENT_BRANCH_ORIGIN_KEY`) that
round-trips through save/resume, and consults that marker — not the current
profile — on any resumed/continued leg.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._providers import _parse_profile

_DIFFERENT_PLAIN_PROFILE_TEXT = "---\nmodel: claude_code/sonnet\n---\nUnrelated plain body."
_ROLE_KEY_REMOVED_PROFILE_TEXT = "---\nmodel: claude_code/sonnet\n---\nExtra reviewer body."


def _rendered_system(branch) -> str:
    """See test_agent_resume_role_system_message.py for why this scans the
    pile instead of trusting `branch.msgs.system` after a from_dict roundtrip."""
    from lionagi.protocols.messages.system import System

    if branch.msgs.system is not None:
        return branch.msgs.system.rendered
    for m in branch.msgs.messages:
        if isinstance(m, System):
            return m.rendered
    raise AssertionError("branch has no System message")


def _wire_agent_stubs(monkeypatch, tmp_path: Path, *, operate_result="done"):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    branches_created: list = []
    real_branch_init = Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(Branch, "__init__", spy_branch_init)

    async def fake_operate(self, instruction=None, **kw):
        return operate_result

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")

    async def fake_setup(*a, **kw):
        return {"session_id": "sess-0"}

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
    return branches_created


def _stub_profile(monkeypatch, name: str, text: str):
    import lionagi.cli.agent as agent_mod

    profile = _parse_profile(name, text)
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda n: profile)
    return profile


async def _make_persisted_role_branch(tmp_path: Path) -> tuple[str, str]:
    """Build a real role-profile branch via create_agent (composed system
    message with role header + policy block, and the immutable branch-origin
    marker in metadata), snapshot it to disk, and return
    (branch_id, rendered_system_message)."""
    from lionagi.agent.factory import create_agent
    from lionagi.agent.spec import AgentSpec

    spec = AgentSpec.coding(cwd=str(tmp_path), effort="high", role="reviewer")
    branch = await create_agent(
        spec,
        chat_model="claude_code/sonnet",
        load_settings=False,
    )
    rendered = branch.msgs.system.rendered
    assert "## Authority" in rendered, (
        "sanity: the composed system message carries the policy block"
    )

    branch_dir = tmp_path / "branches"
    branch_dir.mkdir(parents=True, exist_ok=True)
    branch_id = str(branch.id)
    (branch_dir / f"{branch_id}.json").write_text(json.dumps(branch.to_dict()))
    return branch_id, rendered


def _make_markerless_role_branch(tmp_path: Path) -> tuple[str, str]:
    """Persist a role-composed-looking branch without using the factory marker."""
    from lionagi import Branch

    rendered = (
        "# Reviewer\n\nReview the requested work critically.\n\n"
        "## Authority\n- Inspect and report on the supplied implementation."
    )
    branch = Branch(chat_model="claude_code/sonnet")
    branch.msgs.set_system(branch.msgs.create_system(system=rendered))

    branch_dir = tmp_path / "branches"
    branch_dir.mkdir(parents=True, exist_ok=True)
    branch_id = str(branch.id)
    (branch_dir / f"{branch_id}.json").write_text(json.dumps(branch.to_dict()))
    return branch_id, rendered


@pytest.mark.asyncio
async def test_resume_with_different_plain_profile_preserves_composed_system_message(
    monkeypatch, tmp_path
):
    """Reviewer case 1: resume a role-composed branch, but this leg's `-a`
    names a different, plain (no `role:`) profile."""
    branch_id, original_rendered = await _make_persisted_role_branch(tmp_path)

    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(monkeypatch, "plain", _DIFFERENT_PLAIN_PROFILE_TEXT)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", resume=branch_id, agent_name="plain")

    branch = branches_created[-1]
    assert _rendered_system(branch) == original_rendered, (
        "resuming (-r) a role-composed branch under a different plain profile "
        "must not replace its persisted composed system message"
    )


@pytest.mark.asyncio
async def test_resume_after_role_key_removed_preserves_composed_system_message(
    monkeypatch, tmp_path
):
    """Reviewer case 2: resume a role-composed branch after the SAME profile
    file has had its `role:` key removed."""
    branch_id, original_rendered = await _make_persisted_role_branch(tmp_path)

    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(monkeypatch, "reviewer", _ROLE_KEY_REMOVED_PROFILE_TEXT)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", resume=branch_id, agent_name="reviewer")

    branch = branches_created[-1]
    assert _rendered_system(branch) == original_rendered, (
        "resuming (-r) a role-composed branch after its profile's `role:` key "
        "was removed must not replace its persisted composed system message"
    )


@pytest.mark.asyncio
async def test_continue_last_with_different_plain_profile_preserves_composed_system_message(
    monkeypatch, tmp_path
):
    """Same as case 1, via --continue-last instead of -r."""
    branch_id, original_rendered = await _make_persisted_role_branch(tmp_path)

    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(monkeypatch, "plain", _DIFFERENT_PLAIN_PROFILE_TEXT)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "load_last_branch", lambda: ("run-x", branch_id))
    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", continue_last=True, agent_name="plain")

    branch = branches_created[-1]
    assert _rendered_system(branch) == original_rendered, (
        "--continue-last on a role-composed branch under a different plain "
        "profile must not replace its persisted composed system message"
    )


@pytest.mark.asyncio
async def test_resume_markerless_role_branch_backfills_origin_and_preserves_system(
    monkeypatch, tmp_path
):
    """Older role branches with a System message are protected and upgraded on resume."""
    from lionagi.agent.factory import CREATE_AGENT_BRANCH_ORIGIN_KEY

    branch_id, original_rendered = _make_markerless_role_branch(tmp_path)
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "reviewer",
        "---\nmodel: claude_code/sonnet\nrole: reviewer\n---\nUpdated reviewer body.",
    )

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", resume=branch_id, agent_name="reviewer")

    branch = branches_created[-1]
    assert _rendered_system(branch) == original_rendered
    assert branch.metadata[CREATE_AGENT_BRANCH_ORIGIN_KEY] is True
