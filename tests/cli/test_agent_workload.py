# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A leg's workload class comes from ``--workload``, else its role's ``workload``, else
heavy; it reaches the CLI provider on a new run, a resume and every orchestration
worker; and a leg refused a slot exits 75 with the refusal on one line."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from lionagi.cli._providers import (
    _is_light_codex,
    _parse_profile,
    build_chat_model,
    build_imodel_from_spec,
    profile_config,
)
from lionagi.cli.agent import _admission_refusal
from lionagi.service._subagent_admission import SubagentAdmissionRefusedError
from tests.cli.orchestrate.test_worker_artifact_directive import _build_worker
from tests.cli.test_agent_resume_model_override import _wire_agent_stubs

_LIGHT_ROLE = "---\nmodel: codex/gpt-5.5\nworkload: light\n---\n\nRead.\n"
_PLAIN_ROLE = "---\nmodel: codex/gpt-5.5\n---\n\nWork.\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_LIGHT_ROLE, "light"),
        ("---\nworkload: heavy\n---\n", "heavy"),
        (_PLAIN_ROLE, "heavy"),
    ],
)
def test_a_role_declares_its_workload(text, expected):
    profile = _parse_profile("role", text)
    assert profile.workload == expected
    assert "workload" not in profile.extra


@pytest.mark.parametrize("raw", ["lite", "LIGHT", "true", "[light]"])
def test_a_workload_the_role_misspells_reads_as_heavy_with_a_warning(raw, caplog):
    profile = _parse_profile("role", f"---\nworkload: {raw}\n---\n")
    assert profile.workload == "heavy"
    assert "reading it as heavy" in caplog.text


def test_discovery_reports_a_light_role_and_leaves_a_heavy_one_unchanged():
    assert profile_config(_parse_profile("r", _LIGHT_ROLE))["workload"] == "light"
    assert "workload" not in profile_config(_parse_profile("r", _PLAIN_ROLE))


def _kwargs(model) -> dict[str, Any]:
    return dict(model.endpoint.config.kwargs)


@pytest.mark.parametrize(
    "provider",
    [
        "codex",
        "openai-codex",
        "claude_code",
        "claude-code",
        "claude",
        "gemini_code",
        "gemini-cli",
        "pi",
        "pi-code",
        "pi_code",
    ],
)
def test_a_light_cli_leg_hands_its_workload_to_the_provider(provider):
    model = build_chat_model(provider, "m", False, False, None, workload="light")
    assert _kwargs(model)["workload"] == "light"
    spec_model = build_imodel_from_spec(f"{provider}/m", workload="light")
    assert _kwargs(spec_model)["workload"] == "light"


@pytest.mark.parametrize("provider", ["codex", "openai-codex", "Codex"])
def test_every_codex_spelling_keeps_the_light_class_and_the_containment(provider):
    asked = {
        "workload": "light",
        "contain_account_integrations": True,
        "account_connectors": ("github",),
    }
    for model in (
        build_imodel_from_spec(f"{provider}/gpt-5.5", **asked),
        build_chat_model(provider, "gpt-5.5", False, False, None, **asked),
    ):
        kwargs = _kwargs(model)
        assert kwargs["workload"] == "light"
        assert kwargs["contain_account_integrations"] is True
        assert kwargs["account_connectors"] == ["github"]
        assert _is_light_codex(model)


def test_a_heavy_leg_adds_nothing_to_the_request():
    model = build_imodel_from_spec("codex/m")
    assert "workload" not in _kwargs(model)
    assert "workload" not in _kwargs(build_imodel_from_spec("codex/m", workload="heavy"))


def test_an_api_model_takes_no_workload():
    model = build_imodel_from_spec("openai/gpt-4.1-mini", workload="light")
    assert "workload" not in _kwargs(model)


def test_a_bare_openai_codex_model_builds_an_api_endpoint_without_a_workload():
    """A slashless spec falls through to a bare model name, which iModel resolves through the
    API openai provider: nothing spawns, so there is no workload to hand a CLI."""
    from lionagi.cli._providers import _add_workload_kwarg
    from lionagi.service.connections.registry import EndpointRegistry

    model = build_imodel_from_spec("openai-codex", workload="light")
    assert type(model.endpoint).__name__ == "Endpoint"
    assert EndpointRegistry.canonical_provider(model.endpoint.config.provider) == "openai"
    assert "workload" not in _kwargs(model)
    model.endpoint.config._api_key = "dummy-key"
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    assert "workload" not in payload

    kwargs: dict = {}
    _add_workload_kwarg(kwargs, model.endpoint, "light")
    assert "workload" not in kwargs


def test_a_bare_openai_codex_model_takes_no_account_integration_settings():
    """The containment settings are codex CLI options too, and an API request would carry
    them as payload keys."""
    asked = {"contain_account_integrations": True, "account_connectors": ("github",)}
    model = build_imodel_from_spec("openai-codex", **asked)
    assert type(model.endpoint).__name__ == "Endpoint"
    assert not ({"contain_account_integrations", "account_connectors"} & set(_kwargs(model)))
    model.endpoint.config._api_key = "dummy-key"
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    assert not ({"contain_account_integrations", "account_connectors"} & set(payload))


def test_a_slashless_model_on_a_codex_default_provider_keeps_the_containment(monkeypatch):
    """A slashless name takes the configured chat provider; when that is codex, the model is a
    codex agent and gets the containment settings."""
    from lionagi import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "settings",
        config_mod.settings.model_copy(update={"LIONAGI_CHAT_PROVIDER": "codex"}),
    )
    model = build_imodel_from_spec(
        "gpt-5.5", contain_account_integrations=True, account_connectors=("github",)
    )
    assert model.endpoint.is_cli
    assert _kwargs(model)["contain_account_integrations"] is True
    assert _kwargs(model)["account_connectors"] == ["github"]


def test_a_codex_cli_model_carries_the_workload_and_not_to_an_api_request():
    model = build_imodel_from_spec("codex/gpt-5.5", workload="light")
    assert _kwargs(model)["workload"] == "light"
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    assert payload["request"].workload == "light"


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
    from lionagi.providers.openai import codex as codex_mod
    from lionagi.service import _subagent_admission as admission
    from lionagi.service._subagent_admission import admit_subagent

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
