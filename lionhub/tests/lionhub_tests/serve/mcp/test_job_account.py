# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An agent job records the subscription account its submission asked for.

Names only: the account's name from accounts.toml and the provider table it sits in. The
run resolves the account itself and writes what it resolved to its run.json; the job
record carries the request, so a listing can count a run before its run.json exists.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lionhub.serve.mcp import config, dispatch, jobs


class _FakeProc:
    pid = 4242

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Job state under tmp; the run's own spawn is recorded, not started."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    monkeypatch.delenv(config.CHILD_ACTOR_SUFFIX_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    spawned: list[list[str]] = []
    real_popen = jobs.subprocess.Popen

    def fake_popen(argv, **kw):
        if argv[0] != "echo":
            return real_popen(argv, **kw)
        spawned.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    return spawned


@pytest.fixture
def profiles(monkeypatch, sandbox, tmp_path):
    """Profiles under a redirected ~/.lionagi/agents, the only place `account` is honoured."""
    home = tmp_path / "home"
    agents = home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (agents / "on-main.md").write_text(
        "---\nmodel: claude_code/sonnet\naccount: main\n---\nWork.\n"
    )
    (agents / "plain.md").write_text("---\nmodel: claude_code/sonnet\n---\nWork.\n")
    (agents / "in-a-home.md").write_text(
        f"---\nmodel: codex/gpt-5\ncodex_home: {codex_home}\n---\nWork.\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return sandbox


def _call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def _submit(args: dict[str, Any]) -> dict[str, Any]:
    fingerprint = _call(help="agent.submit")["schema_fingerprint"]
    op = {"op": "agent.submit", "args": args, "schema_fingerprint": fingerprint}
    return _call(ops=[op])["ops"][0]


def _account_fields(run_id: str) -> list[tuple[Any, Any]]:
    """The pair as the record, a status read and the listing each carry it."""
    record = jobs._read_job(run_id)
    status = jobs.status(run_id)
    (listed,) = [entry for entry in jobs.list_jobs() if entry["run_id"] == run_id]
    return [
        (record["account"], record["account_provider"]),
        (status["account"], status["account_provider"]),
        (listed["account"], listed["account_provider"]),
    ]


# ---- jobs.submit --------------------------------------------------------------


def test_a_submit_naming_an_account_records_it_everywhere_it_is_read(sandbox):
    handle = jobs.submit(
        "agent",
        [],
        prompt="work",
        no_mcp_config=True,
        account="main",
        account_provider="claude_code",
    )

    assert (handle["account"], handle["account_provider"]) == ("main", "claude_code")
    assert _account_fields(handle["run_id"]) == [("main", "claude_code")] * 3


def test_a_submit_naming_no_account_records_nulls(sandbox):
    handle = jobs.submit("agent", [], prompt="work", no_mcp_config=True)

    assert (handle["account"], handle["account_provider"]) == (None, None)
    assert _account_fields(handle["run_id"]) == [(None, None)] * 3


def test_a_record_written_before_the_fields_existed_reads_null(sandbox):
    handle = jobs.submit("agent", [], prompt="work", no_mcp_config=True, account="main")
    run_id = handle["run_id"]
    with jobs._locked_job(run_id) as guard:
        assert guard.record is not None
        del guard.record["account"]
        del guard.record["account_provider"]

    assert "account" not in jobs._read_job(run_id)
    status = jobs.status(run_id)
    (listed,) = [entry for entry in jobs.list_jobs() if entry["run_id"] == run_id]
    assert (status["account"], status["account_provider"]) == (None, None)
    assert (listed["account"], listed["account_provider"]) == (None, None)


# ---- agent.submit -------------------------------------------------------------


def test_the_profile_s_account_is_recorded_with_its_model_s_provider(profiles):
    answer = _submit({"agent": "on-main", "prompt": "w", "no_mcp_config": True})

    assert answer["ok"] is True, answer
    run_id = answer["result"]["run_id"]
    assert _account_fields(run_id) == [("main", "claude_code")] * 3


def test_the_account_argument_wins_over_the_profile_s(profiles):
    spawned = profiles
    args = {"agent": "on-main", "account": "spare", "prompt": "w", "no_mcp_config": True}

    answer = _submit(args)

    assert answer["ok"] is True, answer
    assert _account_fields(answer["result"]["run_id"])[0] == ("spare", "claude_code")
    # the run itself still receives the argument and resolves it
    (argv,) = spawned
    assert any(token in ("--account=spare", "spare") for token in argv), argv


def test_a_named_model_decides_the_provider(profiles):
    args = {
        "agent": "plain",
        "query": ["codex/gpt-5"],
        "account": "work",
        "prompt": "w",
        "no_mcp_config": True,
    }

    answer = _submit(args)

    assert answer["ok"] is True, answer
    assert _account_fields(answer["result"]["run_id"])[0] == ("work", "codex")


def test_a_profile_without_an_account_records_none(profiles):
    answer = _submit({"agent": "plain", "prompt": "w", "no_mcp_config": True})

    assert answer["ok"] is True, answer
    assert _account_fields(answer["result"]["run_id"])[0] == (None, None)


def test_a_codex_home_without_a_name_records_a_codex_run_on_no_named_account(profiles):
    answer = _submit({"agent": "in-a-home", "prompt": "w", "no_mcp_config": True})

    assert answer["ok"] is True, answer
    assert _account_fields(answer["result"]["run_id"])[0] == (None, "codex")


# ---- what is read, directly ----------------------------------------------------

_AGENT = SimpleNamespace(job_kind="agent")


def test_a_resumed_run_with_no_model_here_has_no_provider_yet():
    """The branch carries the model, so the provider is the run's to resolve."""
    assert dispatch._requested_account(_AGENT, {"account": "main"}, None, "go on") == (
        "main",
        None,
    )


def test_a_lone_positional_is_the_prompt_not_a_model():
    profile = SimpleNamespace(model="codex/gpt-5", account=None, codex_home=None)
    args = {"account": "main", "query": ["claude_code/sonnet"]}

    assert dispatch._requested_account(_AGENT, args, profile, None) == ("main", "codex")


def test_a_provider_that_takes_no_account_records_none_for_it():
    args = {"account": "main", "query": ["openai/gpt-5", "work"]}

    assert dispatch._requested_account(_AGENT, args, None, None) == ("main", None)


@pytest.mark.parametrize("kind", ["flow", "fanout", "play"])
def test_orchestrating_kinds_ask_for_no_account_at_submit(kind):
    profile = SimpleNamespace(model="claude_code/sonnet", account="main", codex_home=None)
    verb = SimpleNamespace(job_kind=kind)

    assert dispatch._requested_account(verb, {"account": "main"}, profile, "w") == (None, None)
