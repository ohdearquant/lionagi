# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` answers a profile's ``require_hooks`` inside the run (ADR-0048 Amendment 2 R3).

A refusal ends the run failed, with the refusal's class and text in the cause file the job
record reads, before any turn is taken. A branch launched under the key keeps it: a resume
that names no profile is checked as its launch was.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi._errors import RequireHooksRefusedError
from lionhub.orchestration.spawn import AgentProfile
from lionhub.serve.mcp import config

_SETTINGS_HOOK = "hooks_external:\n  - event: pre_tool\n    command: [/bin/true]\n"


@pytest.fixture
def agent_run(tmp_path, monkeypatch):
    """`_run_agent` with its I/O stubbed, the turn recorded, and a cause file configured."""
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    cause = tmp_path / "terminal_cause.json"
    monkeypatch.setenv(config.CAUSE_FILE_ENV_VAR, str(cause))
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: None)
    ended: list[str] = []

    async def setup(*a, **kw):
        return None

    async def teardown(ctx, *, status="completed", **kw):
        ended.append(status)
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}", agent_definition_hash=lambda n: "abc"
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    allocated: list[str] = []

    def allocate():
        allocated.append("r")
        return SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        )

    monkeypatch.setattr(agent_mod, "allocate_run", allocate)
    turns: list[dict] = []

    async def operate(self, instruction=None, **kw):
        turns.append(dict(self.chat_model.endpoint.config.kwargs))
        return "done"

    monkeypatch.setattr(Branch, "operate", operate)
    return SimpleNamespace(
        mod=agent_mod,
        home=home,
        cause=cause,
        allocated=allocated,
        turns=turns,
        ended=ended,
    )


def _use_profile(monkeypatch, agent_mod, profile: AgentProfile) -> None:
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)


def _saved_branch(tmp_path: Path, monkeypatch, agent_mod, chat_model, metadata: dict) -> str:
    """Save a branch on *chat_model* with *metadata*, found by id as `--resume` finds one."""
    from lionagi import Branch

    branch = Branch(chat_model=chat_model)
    branch.metadata.update(metadata)
    path = tmp_path / f"{branch.id}.json"
    path.write_text(json.dumps(branch.to_dict()))
    monkeypatch.setattr(agent_mod, "find_branch", lambda branch_id: (None, path))
    return str(branch.id)


# ── a refusal at launch ends the run failed, with its text ─────────────────


@pytest.mark.asyncio
async def test_a_cli_provider_refusal_ends_the_run_failed_with_its_text(agent_run, monkeypatch):
    _use_profile(
        monkeypatch,
        agent_run.mod,
        AgentProfile(name="guarded", model="claude_code/sonnet", require_hooks=True),
    )

    with pytest.raises(RequireHooksRefusedError) as raised:
        await agent_run.mod._run_agent(None, "do the thing", agent_name="guarded")

    # The run was allocated, so its terminal path ran; no turn was taken.
    assert agent_run.allocated == ["r"]
    assert agent_run.turns == []
    assert agent_run.ended == ["failed"]
    cause = json.loads(agent_run.cause.read_text())
    assert cause["class"] == "RequireHooksRefusedError"
    assert cause["detail"] == str(raised.value)
    assert "checked for codex and API-model legs only" in cause["detail"]


@pytest.mark.asyncio
async def test_a_profile_without_the_key_takes_its_turn(agent_run, monkeypatch):
    _use_profile(monkeypatch, agent_run.mod, AgentProfile(name="plain", model="claude_code/sonnet"))

    *_, terminal_status, _sid = await agent_run.mod._run_agent(
        None, "do the thing", agent_name="plain"
    )

    assert terminal_status == "completed"
    assert len(agent_run.turns) == 1
    assert not agent_run.cause.exists()


@pytest.mark.asyncio
async def test_a_refusal_at_launch_names_the_hook_entries_a_hooks_file_carries(
    agent_run, monkeypatch, tmp_path
):
    """A trusted carried entry runs on the API leg, so the refusal for the settings hook
    names it as one that runs (ADR-0048 Amendment 2 R2)."""
    import hashlib
    import importlib
    import tempfile

    from lionhub.hooks.external import compute_trust_record

    user_settings = importlib.import_module("lionagi.plugins._user_settings")
    child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")
    (agent_run.home / ".lionagi" / "settings.yaml").write_text(_SETTINGS_HOOK)
    work, guards, private = (tmp_path.resolve() / name for name in ("work", "guards", "private"))
    for directory in (work, guards, private):
        directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    program = guards / "guard.sh"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    record = compute_trust_record([str(program)], str(work))
    monkeypatch.setattr(
        user_settings, "read_user_settings", lambda: {"trusted_hook_commands": [record]}
    )
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {"child_sandbox": {}})
    copy = tmp_path.resolve() / "hooks-from.json"
    hook = {"type": "command", "command": [str(program)]}
    copy.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}}))
    spec = {
        "copy": str(copy),
        "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(),
        "format": "claude",
        "carry": ["hooks"],
        "source": str(tmp_path.resolve() / "guards.json"),
    }
    _use_profile(
        monkeypatch,
        agent_run.mod,
        AgentProfile(
            name="guarded",
            model="openai/gpt-4.1",
            require_hooks=True,
            extra={"api_tools": "read"},
        ),
    )

    with pytest.raises(RequireHooksRefusedError) as raised:
        await agent_run.mod._run_agent(
            None,
            "do the thing",
            agent_name="guarded",
            cwd=str(work),
            hooks_from_snapshot=json.dumps(spec),
        )

    text = str(raised.value)
    assert "these would not run: hooks_external in ~/.lionagi/settings.yaml." in text
    assert (
        "It does run the hook entries its hooks_from file carries: hooks.PreToolUse[0].hooks[0]."
        in text
    )
    assert agent_run.turns == []
    assert json.loads(agent_run.cause.read_text())["detail"] == text


# ── a resume keeps the requirement its branch was launched with ────────────


@pytest.mark.asyncio
async def test_a_resume_naming_no_profile_is_refused_when_a_hook_cannot_run(
    agent_run, monkeypatch, tmp_path
):
    from lionagi import iModel

    (agent_run.home / ".lionagi" / "settings.yaml").write_text(_SETTINGS_HOOK)
    branch_id = _saved_branch(
        tmp_path,
        monkeypatch,
        agent_run.mod,
        iModel(model="openai/gpt-4.1"),
        {agent_run.mod._REQUIRE_HOOKS_KEY: True},
    )

    with pytest.raises(RequireHooksRefusedError) as raised:
        await agent_run.mod._run_agent(None, "go on", resume=branch_id)

    assert "this branch was launched with require_hooks" in str(raised.value)
    assert "hooks_external in ~/.lionagi/settings.yaml" in str(raised.value)
    assert agent_run.turns == []
    assert json.loads(agent_run.cause.read_text())["class"] == "RequireHooksRefusedError"


@pytest.mark.asyncio
async def test_the_same_resume_of_a_branch_launched_without_the_key_takes_its_turn(
    agent_run, monkeypatch, tmp_path
):
    from lionagi import iModel

    (agent_run.home / ".lionagi" / "settings.yaml").write_text(_SETTINGS_HOOK)
    branch_id = _saved_branch(
        tmp_path, monkeypatch, agent_run.mod, iModel(model="openai/gpt-4.1"), {}
    )

    *_, terminal_status, _sid = await agent_run.mod._run_agent(None, "go on", resume=branch_id)

    assert terminal_status == "completed"
    assert len(agent_run.turns) == 1


@pytest.mark.asyncio
async def test_a_resumed_codex_branch_carries_the_key_to_the_request_it_launches(
    agent_run, monkeypatch, tmp_path
):
    """The request is what refuses a codex launch, so the key is put back on it even when
    the saved request lacks it."""
    from lionagi import iModel

    model = iModel(model="codex/gpt-5")
    assert "require_hooks" not in model.endpoint.config.kwargs
    branch_id = _saved_branch(
        tmp_path, monkeypatch, agent_run.mod, model, {agent_run.mod._REQUIRE_HOOKS_KEY: True}
    )

    await agent_run.mod._run_agent(None, "go on", resume=branch_id)

    assert [turn.get("require_hooks") for turn in agent_run.turns] == [True]


@pytest.mark.asyncio
async def test_a_resumed_codex_branch_launched_without_the_key_does_not_gain_it(
    agent_run, monkeypatch, tmp_path
):
    from lionagi import iModel

    model = iModel(model="codex/gpt-5")
    branch_id = _saved_branch(tmp_path, monkeypatch, agent_run.mod, model, {})

    await agent_run.mod._run_agent(None, "go on", resume=branch_id)

    assert [turn.get("require_hooks") for turn in agent_run.turns] == [None]


@pytest.mark.asyncio
async def test_a_new_branch_records_the_requirement_for_its_resumes(agent_run, monkeypatch):
    from lionagi import Branch

    _use_profile(
        monkeypatch,
        agent_run.mod,
        AgentProfile(name="guarded", model="codex/gpt-5", require_hooks=True, yolo=True),
    )
    seen: list[dict] = []

    async def operate(self, instruction=None, **kw):
        seen.append(dict(self.metadata))
        return "done"

    monkeypatch.setattr(Branch, "operate", operate)

    await agent_run.mod._run_agent(None, "do the thing", agent_name="guarded")

    assert [meta.get(agent_run.mod._REQUIRE_HOOKS_KEY) for meta in seen] == [True]
