# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: resuming/continuing a role-profile branch must not clobber its
persisted system message.

A brand-new role-profile branch composes its system message via
create_agent (role header + policy block + profile body). Once that branch
is persisted and later reopened (-r / --continue-last / the automatic
timeout-resume leg), `took_create_agent_path` is False for the reopened leg
(neither --preset nor a fresh profile-role branch is being created — an
existing branch is just being loaded back). Before the fix, the
profile-system-prompt block ran unconditionally whenever
`not took_create_agent_path`, so it called `branch.msgs.add_message(system=...)`
-> `set_system` -> replaced the persisted composed system message with the
bare profile body, silently dropping the role header and policy block.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._providers import _parse_profile

_REVIEWER_PROFILE_TEXT = "---\nmodel: claude_code/sonnet\nrole: reviewer\n---\nExtra reviewer body."


def _rendered_system(branch) -> str:
    """Return the rendered System message content.

    ``branch.msgs.system`` (a plain attribute set by set_system/__init__) is
    None after a Branch.from_dict roundtrip even though the System message is
    still present as the first entry of the messages Pile — a from_dict gap
    unrelated to this fix. Find it by scanning the pile so this works for
    both freshly created and resumed/reloaded branches.
    """
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
    message with role header + policy block), snapshot it to disk, and
    return (branch_id, rendered_system_message)."""
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


@pytest.mark.asyncio
async def test_resume_flag_does_not_clobber_composed_system_message(monkeypatch, tmp_path):
    branch_id, original_rendered = await _make_persisted_role_branch(tmp_path)

    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(monkeypatch, "reviewer", _REVIEWER_PROFILE_TEXT)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", resume=branch_id, agent_name="reviewer")

    branch = branches_created[-1]
    assert _rendered_system(branch) == original_rendered, (
        "resuming (-r) a role-profile branch must not replace its persisted "
        "composed system message with the bare profile body"
    )


@pytest.mark.asyncio
async def test_continue_last_does_not_clobber_composed_system_message(monkeypatch, tmp_path):
    branch_id, original_rendered = await _make_persisted_role_branch(tmp_path)

    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(monkeypatch, "reviewer", _REVIEWER_PROFILE_TEXT)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "load_last_branch", lambda: ("run-x", branch_id))
    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "keep going", continue_last=True, agent_name="reviewer")

    branch = branches_created[-1]
    assert _rendered_system(branch) == original_rendered, (
        "--continue-last on a role-profile branch must not replace its persisted "
        "composed system message with the bare profile body"
    )


@pytest.mark.asyncio
async def test_timeout_auto_resume_does_not_clobber_composed_system_message(monkeypatch, tmp_path):
    """The automatic timeout-resume leg (resume_on_timeout=True) recurses into
    _run_agent with resume=<branch_id> — same clobber risk as an explicit -r."""
    branch_id, original_rendered = await _make_persisted_role_branch(tmp_path)

    branches_created: list = []
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    real_branch_init = Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(Branch, "__init__", spy_branch_init)

    call_count = {"n": 0}

    async def fake_operate(self, instruction=None, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        if idx == 0:
            raise TimeoutError("boom")
        return "concluded"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")

    async def fake_setup(*a, **kw):
        return {"session_id": f"sess-{call_count['n']}"}

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
    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: ("run-x", tmp_path / "branches" / f"{bid}.json")
    )
    _stub_profile(monkeypatch, "reviewer", _REVIEWER_PROFILE_TEXT)

    from lionagi.cli.agent import _run_agent

    result, _provider, _bid, status, _sid = await _run_agent(
        None,
        "keep going",
        resume=branch_id,
        agent_name="reviewer",
        timeout=30,
        resume_on_timeout=True,
    )

    assert call_count["n"] == 2, "expected the first leg to time out and the auto-resume leg to run"
    assert status == "completed"
    assert result == "concluded"

    resumed_branch = branches_created[-1]
    assert _rendered_system(resumed_branch) == original_rendered, (
        "the automatic timeout-resume leg must not replace the persisted "
        "composed system message with the bare profile body"
    )
