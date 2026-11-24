# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""account.list: declared accounts, their slots, and what this server's jobs did on them."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from lionagi.ln import now_utc
from lionhub.agent import subscription_accounts as sa
from lionhub.serve.mcp import accounts, config, dispatch, jobs

QUOTA = (
    "ProviderQuotaError: You\u2019ve hit your usage limit. Visit "
    "https://chatgpt.com/settings/usage to purchase more credits or try again at {at}."
)


@pytest.fixture
def logins(monkeypatch, tmp_path) -> Path:
    directory = tmp_path / "logins"
    directory.mkdir()
    home = tmp_path / "codex-work"
    home.mkdir()
    spare = tmp_path / "codex-spare"
    spare.mkdir()
    (directory / "accounts.toml").write_text(
        "[claude_code.main]\n"
        'keychain = { service = "svc", account = "acct" }\n'
        "max_concurrent = 2\n"
        "[claude_code.solo]\n"
        "max_concurrent = 1\n"
        "[codex.work]\n"
        f'home = "{home}"\n'
        "max_concurrent = 1\n"
        "[codex.spare]\n"
        f'home = "{spare}"\n'
        "[codex.gone]\n"
        f'home = "{tmp_path / "nowhere"}"\n'
    )
    (directory / "claude-solo").write_text("not read\n")
    (directory / "claude-Not_A_Name").write_text("not read\n")
    monkeypatch.setenv(sa.LOGINS_DIR_ENV, str(directory))
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", tmp_path / "lionagi-home")
    return directory


def _status(
    run_id: str,
    *,
    kind: str = "agent",
    account: Any = None,
    provider: Any = None,
    terminal: bool = False,
    hours_ago: float | None = None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finished = None if hours_ago is None else (now_utc() - timedelta(hours=hours_ago)).isoformat()
    return {
        "run_id": run_id,
        "kind": kind,
        "account": account,
        "account_provider": provider,
        "terminal": terminal,
        "finished_at": finished,
        "run": run,
    }


def _with_jobs(monkeypatch, statuses: list[dict[str, Any]], pools: dict[str, Any] | None = None):
    by_id = {st["run_id"]: st for st in statuses}
    monkeypatch.setattr(jobs, "job_run_ids", lambda: list(by_id))
    monkeypatch.setattr(jobs, "status", lambda run_id: by_id[run_id])
    monkeypatch.setattr(
        accounts, "subagent_admission_status", lambda: {"caps": {}, "pools": pools or {}}
    )


def _rows(reply: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["provider"], r["name"]): r for r in reply["accounts"]}


# ---- the declared rows ---------------------------------------------------------


def test_every_declared_account_is_a_row_and_no_credential_location_is_returned(
    logins, monkeypatch, tmp_path
):
    _with_jobs(monkeypatch, [])

    reply = accounts.account_list()

    rows = _rows(reply)
    assert sorted(rows) == [
        ("claude_code", "main"),
        ("claude_code", "solo"),
        ("codex", "spare"),
        ("codex", "work"),
    ]
    assert (
        rows[("claude_code", "main")]["source"],
        rows[("claude_code", "main")]["max_concurrent"],
    ) == ("keychain", 2)
    # a token file capped by a table holding only max_concurrent
    assert (
        rows[("claude_code", "solo")]["source"],
        rows[("claude_code", "solo")]["max_concurrent"],
    ) == ("file", 1)
    assert (rows[("codex", "spare")]["source"], rows[("codex", "spare")]["max_concurrent"]) == (
        "home",
        None,
    )
    assert reply["unresolved"] == [{"provider": "codex", "name": "gone"}]
    text = json.dumps(reply)
    assert str(tmp_path) not in text
    assert "svc" not in text and "acct" not in text
    assert "Not_A_Name" not in text


def test_the_reply_states_its_window_read_time_and_history(logins, monkeypatch):
    _with_jobs(monkeypatch, [])
    before = now_utc()

    reply = accounts.account_list()

    assert reply["window_hours"] == 24
    assert before.isoformat() <= reply["read_at"] <= now_utc().isoformat()
    assert "nothing is backfilled" in reply["history"]


# ---- counts from job records ---------------------------------------------------


def test_counts_follow_the_account_each_job_ran_on(logins, monkeypatch):
    statuses = [
        _status("r-asked", account="main", provider="claude_code"),
        # the run resolved another account than the record asked for; the run wins
        _status(
            "r-moved",
            account="main",
            provider="claude_code",
            run={"account": "work", "account_provider": "codex"},
        ),
        _status("r-done", account="main", provider="claude_code", terminal=True, hours_ago=1),
        _status("r-old", account="main", provider="claude_code", terminal=True, hours_ago=30),
        _status("r-no-provider", account="main", provider=None),
        _status("r-no-provider-done", account="main", provider=None, terminal=True, hours_ago=1),
        _status("r-flow", kind="flow", account="main", provider="claude_code"),
        _status("r-before-accounts"),
    ]
    _with_jobs(monkeypatch, statuses)

    reply = accounts.account_list()

    rows = _rows(reply)
    main, work = rows[("claude_code", "main")], rows[("codex", "work")]
    assert (main["running"], main["completed_24h"]) == (1, 1)
    assert (work["running"], work["completed_24h"]) == (1, 0)
    assert reply["unattributed_running"] == 1


def test_last_limit_is_the_newest_codex_quota_refusal_in_the_provider_s_words(logins, monkeypatch):
    codex = {"account": "work", "account_provider": "codex"}
    statuses = [
        _status(
            "r-new",
            terminal=True,
            hours_ago=2,
            run={**codex, "error": QUOTA.format(at="Nov 22nd, 2026 3:32 PM")},
        ),
        _status(
            "r-older",
            terminal=True,
            hours_ago=50,
            run={**codex, "error": QUOTA.format(at="Nov 20th, 2026 9:00 AM")},
        ),
        _status("r-other-error", terminal=True, hours_ago=1, run={**codex, "error": "Boom"}),
    ]
    _with_jobs(monkeypatch, statuses)

    rows = _rows(accounts.account_list())

    work = rows[("codex", "work")]
    assert work["last_limit"] == {
        "run_id": "r-new",
        "at": statuses[0]["finished_at"],
        "reset_text": "Nov 22nd, 2026 3:32 PM",
    }
    assert work["last_limit_reason"] is None
    assert work["completed_24h"] == 2
    assert (
        rows[("codex", "spare")]["last_limit"],
        rows[("codex", "spare")]["last_limit_reason"],
    ) == (
        None,
        accounts.LIMIT_NONE_IN_JOB_RECORDS,
    )


def test_claude_code_limits_are_not_recorded_whatever_the_error_says(logins, monkeypatch):
    run = {"account": "main", "account_provider": "claude_code", "error": QUOTA.format(at="x")}
    _with_jobs(monkeypatch, [_status("r", terminal=True, hours_ago=1, run=run)])

    main = _rows(accounts.account_list())[("claude_code", "main")]

    assert (main["last_limit"], main["last_limit_reason"]) == (None, accounts.LIMIT_NOT_RECORDED)


# ---- slots from the admission pools --------------------------------------------


def test_slots_come_from_the_account_s_admission_pool(logins, monkeypatch):
    pools = {
        "account.claude_code.main": {"cap": 2, "held": 1, "holders": []},
        "account.codex.spare": {"cap": 0, "error": "PermissionError"},
        "all": {"cap": 8, "held": 3, "holders": []},
    }
    _with_jobs(monkeypatch, [], pools)

    reply = accounts.account_list()

    rows = _rows(reply)
    pick = lambda key: (rows[key]["slots_held"], rows[key]["enforced_cap"])  # noqa: E731
    assert pick(("claude_code", "main")) == (1, 2)
    assert pick(("codex", "work")) == (0, None)
    assert pick(("codex", "spare")) == (None, None)
    assert reply["slots_error"] is None


def test_an_unreadable_pool_root_leaves_every_slot_count_unknown(logins, monkeypatch):
    _with_jobs(monkeypatch, [])

    def refuse():
        raise PermissionError("leases")

    monkeypatch.setattr(accounts, "subagent_admission_status", refuse)

    reply = accounts.account_list()

    assert reply["slots_error"] == "PermissionError"
    assert {r["slots_held"] for r in reply["accounts"]} == {None}


# ---- through the verb ----------------------------------------------------------


def _call_account_list() -> dict[str, Any]:
    return asyncio.run(dispatch.request(ops=[{"op": "account.list", "args": {}}]))["ops"][0]


def test_the_verb_answers_from_the_real_job_and_pool_readers(logins):
    answer = _call_account_list()

    assert answer["ok"] is True, answer
    result = answer["result"]
    assert result["slots_error"] is None
    assert all(r["slots_held"] == 0 and r["running"] == 0 for r in result["accounts"])


def test_an_unreadable_accounts_file_is_unavailable(logins):
    (logins / "accounts.toml").write_text("[claude_code.main\n")

    answer = _call_account_list()

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "unavailable"
    assert "accounts file" in answer["error"]["message"]


def test_the_verb_takes_no_arguments():
    answer = asyncio.run(
        dispatch.request(ops=[{"op": "account.list", "args": {"provider": "codex"}}])
    )["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input"


# ---- list_accounts -------------------------------------------------------------


def test_a_name_defined_twice_is_unresolved_not_dropped(logins):
    (logins / "claude-main").write_text("not read\n")

    resolved, unresolved = sa.list_accounts()

    assert ("claude_code", "main") in unresolved
    assert "main" not in {a.name for a in resolved if a.provider == "claude_code"}


def test_no_accounts_directory_is_no_accounts(monkeypatch, tmp_path):
    monkeypatch.setenv(sa.LOGINS_DIR_ENV, str(tmp_path / "absent"))

    assert sa.list_accounts() == ((), ())
