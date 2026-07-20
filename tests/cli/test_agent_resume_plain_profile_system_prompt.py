# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: resuming a profile WITHOUT a `role:` key must still reapply
the profile's system prompt.

A prior fix addressed a role/preset branch's create_agent-composed system
message (role header + policy block) getting clobbered by a bare
`add_message(system=profile.system_prompt)` on resume. That fix's guard —
skip reapplication whenever the leg is a resume/continue-last — was
unconditional though: it also disabled reapplication for *plain* profiles
(no `role:` key), which never go through create_agent and never had a
composed message to protect. `load_agent_profile(agent_name)` re-reads the
profile file from disk on every `_run_agent` call, so editing a plain
profile's body and then `-r`/`-c`-ing back into an existing branch is
expected to pick up the edit on the next turn — for plain profiles there is
no role/preset composition to protect in the first place.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._providers import _parse_profile

_PLAIN_PROFILE_TEXT_V1 = "---\nmodel: claude_code/sonnet\n---\nOriginal plain profile body."
_PLAIN_PROFILE_TEXT_V2 = "---\nmodel: claude_code/sonnet\n---\nEdited plain profile body."


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


async def _make_persisted_plain_branch(tmp_path: Path) -> str:
    """Build a plain (non-role) Branch the same way a brand-new `-a` leg
    would, snapshot it to disk, and return the branch id."""
    from lionagi import Branch
    from lionagi.protocols.generic.log import DataLoggerConfig

    branch = Branch(
        chat_model="claude_code/sonnet",
        log_config=DataLoggerConfig(auto_save_on_exit=False),
    )
    branch_dir = tmp_path / "branches"
    branch_dir.mkdir(parents=True, exist_ok=True)
    branch_id = str(branch.id)
    (branch_dir / f"{branch_id}.json").write_text(json.dumps(branch.to_dict()))
    return branch_id


@pytest.mark.asyncio
async def test_resume_flag_reapplies_plain_profile_system_prompt(monkeypatch, tmp_path):
    branch_id = await _make_persisted_plain_branch(tmp_path)

    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    # The profile file was edited between the original leg and this resume —
    # the resumed branch must pick up the new body.
    _stub_profile(monkeypatch, "plain", _PLAIN_PROFILE_TEXT_V2)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", resume=branch_id, agent_name="plain")

    branch = branches_created[-1]
    assert "Edited plain profile body." in _rendered_system(branch), (
        "resuming (-r) a plain (no role:) profile branch must still reapply "
        "the profile's system prompt"
    )


@pytest.mark.asyncio
async def test_continue_last_reapplies_plain_profile_system_prompt(monkeypatch, tmp_path):
    branch_id = await _make_persisted_plain_branch(tmp_path)

    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(monkeypatch, "plain", _PLAIN_PROFILE_TEXT_V2)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "load_last_branch", lambda: ("run-x", branch_id))
    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", continue_last=True, agent_name="plain")

    branch = branches_created[-1]
    assert "Edited plain profile body." in _rendered_system(branch), (
        "--continue-last on a plain (no role:) profile branch must still "
        "reapply the profile's system prompt"
    )
