# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The account name a launcher hands a run is recorded in run.json, and a value that is not an
account name is refused before anything is written, without being shown (ADR-0126 Amendment 4)."""

import json

import pytest

from lionagi._errors import ConfigurationError
from tests.cli.test_agent_context_from import _wire_agent_stubs
from tests.cli.test_agent_run_json_error import _run


@pytest.mark.asyncio
async def test_the_launchers_account_name_lands_in_run_json(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "acct-b")
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    assert json.loads(run.manifest_path.read_text())["api_account"] == "acct-b"


@pytest.mark.asyncio
async def test_without_the_variable_run_json_names_no_account(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    monkeypatch.delenv("LIONAGI_API_ACCOUNT", raising=False)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    assert "api_account" not in json.loads(run.manifest_path.read_text())


def _real_runs_root(monkeypatch, tmp_path):
    """The real allocate_run under a scratch root, so a run that gets that far leaves run.json."""
    from lionagi.cli import _runs

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    return runs_root


@pytest.mark.asyncio
async def test_an_accepted_name_reaches_the_real_run_json(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "team_b-1")
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    (manifest,) = runs_root.rglob("run.json")
    assert json.loads(manifest.read_text())["api_account"] == "team_b-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-api03-" + "x" * 90,
        "sk-short",
        "Xy7q" * 10,
        "Team-B",
        "team.b",
        "team@b",
        "a" * 65,
        "two words",
        "",
    ],
    ids=["key", "key prefix", "mixed case", "capital", "dot", "at", "too long", "space", "empty"],
)
async def test_a_value_that_is_not_an_account_name_is_refused_without_being_shown(
    monkeypatch, tmp_path, value
):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", value)
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError, match="LIONAGI_API_ACCOUNT") as info:
        await _run_agent("claude/sonnet", "go")

    if value:
        assert value not in str(info.value)
    assert not runs_root.exists() or not list(runs_root.rglob("run.json"))


@pytest.mark.asyncio
async def test_the_value_of_a_credential_variable_is_refused_without_being_shown(
    monkeypatch, tmp_path
):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    stand_in = "0f3a9c27e1b84d56"
    monkeypatch.setenv("EXAMPLE_API_KEY", stand_in)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", stand_in)
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError, match="value of EXAMPLE_API_KEY") as info:
        await _run_agent("claude/sonnet", "go")

    assert stand_in not in str(info.value)
    assert not runs_root.exists() or not list(runs_root.rglob("run.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [("EXAMPLE_API_KEY", "a-different-value"), ("EXAMPLE_TEAM", "team-b")],
    ids=["credential holds another value", "a plain variable holds the same value"],
)
async def test_a_name_that_is_no_credentials_value_is_recorded(monkeypatch, tmp_path, name, value):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "team-b")
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    (manifest,) = runs_root.rglob("run.json")
    assert json.loads(manifest.read_text())["api_account"] == "team-b"
