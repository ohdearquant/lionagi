# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A leg's workload class comes from ``--workload``, else its role's ``workload``, else
heavy; it reaches the CLI provider on a new run, a resume and every orchestration
worker; and an agent refused a slot exits 75 with the refusal on one line. A role's
``tools``, ``allowed_tools`` and ``disallowed_tools`` reach a Claude Code agent, and a role
that declares any of them for another provider is refused."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli.agent import _admission_refusal
from lionagi.service._subagent_admission import SubagentAdmissionRefusedError
from lionhub.orchestration.spawn import (
    _is_light_codex,
    _parse_profile,
    _profile_tool_settings,
    build_chat_model,
    build_imodel_from_spec,
    profile_config,
)
from tests._spawn_workload_test_support import _LIGHT_ROLE as _LIGHT_ROLE
from tests._spawn_workload_test_support import _PLAIN_ROLE as _PLAIN_ROLE
from tests._spawn_workload_test_support import _named_as_a_whole_word as _named_as_a_whole_word
from tests.cli.orchestrate.test_worker_artifact_directive import _build_worker
from tests.cli.test_agent_resume_model_override import _wire_agent_stubs

_CAPTURED: dict[str, Any] = {}


async def _fake_run_agent(model_str, prompt, **kwargs):
    _CAPTURED.update(kwargs)
    return "output", "provider", "branch-id", "completed", "sess-001"


def _main(argv: list[str], run_agent=_fake_run_agent) -> int:
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.main import main

    _CAPTURED.clear()
    with patch.object(agent_mod, "_run_agent", run_agent):
        return main(argv)


@pytest.mark.parametrize(("argv", "expected"), [([], None), (["--workload", "light"], "light")])
def test_the_flag_reaches_the_run(argv, expected):
    assert _main(["agent", "codex", *argv, "do it"]) == 0
    assert _CAPTURED["workload"] == expected


def test_the_flag_takes_only_the_two_classes(capsys):
    with pytest.raises(SystemExit):
        _main(["agent", "codex", "--workload", "medium", "do it"])
    assert "--workload" in capsys.readouterr().err


def test_a_leg_refused_a_slot_exits_75():
    async def refused(model_str, prompt, **kwargs):
        raise SubagentAdmissionRefusedError("subagent admission refused: pool 'all' at cap")

    assert _main(["agent", "codex", "do it"], run_agent=refused) == 75


def _use_role(monkeypatch, text: str) -> None:
    import lionagi.cli.agent as agent_mod

    profile = _parse_profile("role", text)
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)


def _capture_chat_kwargs(monkeypatch) -> list[dict]:
    from lionagi import Branch

    captured: list[dict] = []

    async def fake_operate(self, instruction=None, **kw):
        captured.append(dict(self.chat_model.endpoint.config.kwargs))
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "flag", "expected"),
    [
        (_LIGHT_ROLE, None, "light"),
        (_LIGHT_ROLE, "heavy", None),
        (_PLAIN_ROLE, "light", "light"),
        (_PLAIN_ROLE, None, None),
    ],
)
async def test_a_new_run_takes_the_flag_else_the_role(monkeypatch, tmp_path, role, flag, expected):
    from lionagi.cli.agent import _run_agent

    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, role)
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent("codex/gpt-5.5", "do the thing", agent_name="role", workload=flag)

    assert captured[-1].get("workload") == expected


def _stored_light_codex_branch(
    tmp_path: Path, provider: str = "codex", contained: bool = False
) -> tuple[str, Path]:
    from lionagi import Branch, iModel

    branch = Branch(chat_model=iModel(provider=provider, model="gpt-5.5"))
    data = branch.to_dict()
    data["chat_model"]["endpoint"]["config"]["kwargs"]["workload"] = "light"
    if contained:
        data["chat_model"]["endpoint"]["config"]["kwargs"]["contain_account_integrations"] = True
    path = tmp_path / f"{branch.id}.json"
    path.write_text(json.dumps(data))
    return str(branch.id), path


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "openai-codex"])
@pytest.mark.parametrize(("role", "expected"), [(_PLAIN_ROLE, None), (_LIGHT_ROLE, "light")])
async def test_a_resume_runs_with_this_launchs_class_not_the_stored_one(
    monkeypatch, tmp_path, role, expected, provider
):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.agent import _run_agent

    branch_id, path = _stored_light_codex_branch(tmp_path, provider)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, role)
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent(None, "continue", resume=branch_id, agent_name="role")

    assert captured[-1].get("workload") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "openai-codex"])
@pytest.mark.parametrize(("flag", "expected"), [(None, None), ("light", "light")])
async def test_a_resume_without_a_role_runs_heavy_unless_the_flag_says_light(
    monkeypatch, tmp_path, flag, expected, provider
):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.agent import _run_agent

    branch_id, path = _stored_light_codex_branch(tmp_path, provider)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent(None, "continue", resume=branch_id, workload=flag)

    assert captured[-1].get("workload") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "openai-codex"])
async def test_a_resume_drops_the_stored_containment_whatever_the_codex_spelling(
    monkeypatch, tmp_path, provider
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli.agent import _run_agent

    branch_id, path = _stored_light_codex_branch(tmp_path, provider, contained=True)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    captured: list[tuple[str, dict]] = []

    async def fake_operate(self, instruction=None, **kw):
        config = self.chat_model.endpoint.config
        captured.append((config.provider, dict(config.kwargs)))
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)

    # No role and no model: a role's model would name the provider and hide the stored spelling.
    await _run_agent(None, "continue", resume=branch_id)

    resumed_on, kwargs = captured[-1]
    assert resumed_on == provider
    assert "contain_account_integrations" not in kwargs
    assert kwargs.get("workload") is None


@pytest.mark.parametrize(("bare", "expected"), [(False, "light"), (True, "heavy")])
def test_a_worker_takes_its_roles_class_and_a_bare_worker_none(tmp_path, bare, expected):
    profile = SimpleNamespace(
        raw_body=True,
        system_prompt="You are a reader.",
        effort=None,
        yolo=False,
        fast_mode=False,
        khive_injection=None,
        workload="light",
    )
    _env, _imodel, built = _build_worker(
        tmp_path, agent_id="reader", role="reader", bare=bare, profile=profile, provider="codex"
    )
    assert built["imodel_kwargs"]["workload"] == expected


@pytest.mark.asyncio
async def test_a_leg_at_the_cap_is_refused_through_the_real_provider_path(
    monkeypatch, tmp_path, caplog
):
    """No stand-in for the turn: the refusal raised under the codex provider reaches
    ``li agent`` as itself, and the codex binary is never started."""
    from lionagi import Branch, _paths
    from lionagi.cli.agent import _run_agent
    from lionagi.service import _subagent_admission as admission
    from lionagi.service._subagent_admission import admit_subagent
    from lionhub.providers.openai import codex as codex_mod

    real_operate = Branch.operate
    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(Branch, "operate", real_operate)
    marker = tmp_path / "codex-started"
    fake = tmp_path / "codex"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)
    monkeypatch.setattr(codex_mod, "CODEX_CLI", str(fake))
    monkeypatch.setattr(codex_mod, "HAS_CODEX_CLI", True)
    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "home")
    monkeypatch.setattr(admission, "_POLL_SECONDS", 0.05)
    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS", "1")
    monkeypatch.setenv("LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS", "0")

    held = await admit_subagent(engine="claude")
    try:
        with pytest.raises(Exception) as raised:
            await _run_agent("codex/gpt-5.5", "do the thing")
    finally:
        held.release()
    refusal = _admission_refusal(raised.value)
    assert refusal is not None and "cap of 1" in str(refusal)
    assert not marker.exists()
    assert "SubagentAdmissionRefusedError: subagent admission refused" in caplog.text
    assert "Failed to stream API call" not in caplog.text


def test_a_wrapped_refusal_still_exits_75():
    async def refused(model_str, prompt, **kwargs):
        try:
            raise SubagentAdmissionRefusedError("subagent admission refused: pool 'all' at cap")
        except SubagentAdmissionRefusedError as exc:
            raise ValueError(f"Failed to stream API call: {exc}") from exc

    assert _main(["agent", "codex", "do it"], run_agent=refused) == 75


def test_any_other_failure_keeps_its_exit():
    async def failed(model_str, prompt, **kwargs):
        raise ValueError("Failed to stream API call: something else")

    with pytest.raises(ValueError):
        _main(["agent", "codex", "do it"], run_agent=failed)


def test_the_run_row_names_a_wrapped_refusal_as_a_retryable_provider_failure():
    from lionagi.cli._runs import resolve_run_reason
    from lionagi.state.reasons import RunReasons

    try:
        try:
            raise SubagentAdmissionRefusedError("subagent admission refused: pool 'all' at cap")
        except SubagentAdmissionRefusedError as exc:
            raise ValueError(f"Failed to stream API call: {exc}") from exc
    except ValueError as wrapped:
        code, summary, _ = resolve_run_reason(status="failed", exception=wrapped)
    assert code == RunReasons.FAILED_PROVIDER_RETRYABLE
    assert summary.startswith("SubagentAdmissionRefusedError: subagent admission refused")


# ── a role's tool set for a Claude Code agent ─────────────────────────────────

# What each field puts on the command line: ``--tools`` takes one comma-separated token.


_CLAUDE_TOOLS_ROLE = (
    "---\nmodel: claude_code/sonnet\ntools: [Read, Grep, Glob]\n"
    "allowed_tools: [Read, Grep]\ndisallowed_tools: [WebFetch]\n---\n\nRead.\n"
)


@pytest.mark.asyncio
async def test_a_new_run_hands_the_roles_tool_set_to_a_claude_agent(monkeypatch, tmp_path):
    from lionagi.cli.agent import _run_agent

    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, _CLAUDE_TOOLS_ROLE)
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent("claude_code/sonnet", "do the thing", agent_name="role")

    assert captured[-1]["tools"] == "Read,Grep,Glob"
    assert captured[-1]["allowed_tools"] == ["Read", "Grep"]
    assert captured[-1]["disallowed_tools"] == ["WebFetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["codex/gpt-5.5", "openai/gpt-4.1-mini"])
async def test_a_new_run_on_another_provider_refuses_the_roles_tool_set(
    monkeypatch, tmp_path, model
):
    from lionagi.cli.agent import _run_agent

    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, _CLAUDE_TOOLS_ROLE)
    _capture_chat_kwargs(monkeypatch)

    with pytest.raises(ConfigurationError, match=_named_as_a_whole_word("tools")):
        await _run_agent(model, "do the thing", agent_name="role")


@pytest.mark.asyncio
async def test_a_new_run_refuses_a_tool_field_that_is_not_a_list(monkeypatch, tmp_path):
    from lionagi.cli.agent import _run_agent

    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, "---\nmodel: claude_code/sonnet\ntools: Read\n---\n\nRead.\n")
    _capture_chat_kwargs(monkeypatch)

    with pytest.raises(ConfigurationError, match=_named_as_a_whole_word("tools")):
        await _run_agent("claude_code/sonnet", "do the thing", agent_name="role")


def _stored_branch(tmp_path: Path, provider: str, model: str, **kwargs: Any) -> tuple[str, Path]:
    from lionagi import Branch, iModel

    branch = Branch(chat_model=iModel(provider=provider, model=model))
    data = branch.to_dict()
    data["chat_model"]["endpoint"]["config"]["kwargs"].update(kwargs)
    path = tmp_path / f"{branch.id}.json"
    path.write_text(json.dumps(data))
    return str(branch.id), path


_STORED_TOOLS = {"tools": "Bash", "allowed_tools": ["Bash"], "disallowed_tools": ["Write"]}


@pytest.mark.asyncio
async def test_a_resume_takes_the_tool_fields_its_role_declares_and_keeps_the_rest(
    monkeypatch, tmp_path
):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.agent import _run_agent

    branch_id, path = _stored_branch(tmp_path, "claude_code", "sonnet", **_STORED_TOOLS)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, "---\nmodel: claude_code/sonnet\ntools: [Read]\n---\n\nRead.\n")
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent(None, "continue", resume=branch_id, agent_name="role")

    assert captured[-1]["tools"] == "Read"
    assert captured[-1]["allowed_tools"] == ["Bash"]
    assert captured[-1]["disallowed_tools"] == ["Write"]


@pytest.mark.asyncio
async def test_a_resume_without_a_role_keeps_the_stored_tool_set(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.agent import _run_agent

    branch_id, path = _stored_branch(tmp_path, "claude_code", "sonnet", **_STORED_TOOLS)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent(None, "continue", resume=branch_id)

    for field, value in _STORED_TOOLS.items():
        assert captured[-1][field] == value


@pytest.mark.asyncio
async def test_a_resume_of_another_provider_refuses_the_roles_tool_set(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.agent import _run_agent

    branch_id, path = _stored_branch(tmp_path, "codex", "gpt-5.5")
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, "---\ntools: [Read]\n---\n\nRead.\n")
    _capture_chat_kwargs(monkeypatch)

    with pytest.raises(ConfigurationError, match=_named_as_a_whole_word("tools")):
        await _run_agent(None, "continue", resume=branch_id, agent_name="role")
