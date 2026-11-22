# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Per-account admission: a named subscription account's ``max_concurrent`` caps how many
CLI agents on that account run at once, as one more pool beside the machine-wide ones. The
account and its cap reach the spawn in the environment the caller built for the child, and
the child never sees them."""

from __future__ import annotations

import contextlib
import os
import sys

import pytest

from lionagi.service import _subagent_admission as admission
from lionagi.service._subagent_admission import (
    ACCOUNT_CAP_ENV,
    ACCOUNT_ENV,
    SubagentAdmissionRefusedError,
    account_admission,
    admit_subagent,
    subagent_admission_status,
)
from lionhub.providers._cli_subprocess import ndjson_from_cli

WAIT_NONE = {"LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
ALPHA = "claude_code.alpha"

# Reports which of the two account names it was handed, then blocks until killed.
_REPORT_AND_SLEEP = (
    "import json, os, sys, time\n"
    f"names = ({ACCOUNT_ENV!r}, {ACCOUNT_CAP_ENV!r})\n"
    "print(json.dumps({'seen': [n for n in names if n in os.environ]}), flush=True)\n"
    "time.sleep(30)\n"
)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from lionagi import _paths

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "home")
    monkeypatch.setattr(admission, "_POLL_SECONDS", 0.05)
    for name in list(os.environ):
        if name.startswith("LIONAGI_MAX_SUBAGENTS") or name in (ACCOUNT_ENV, ACCOUNT_CAP_ENV):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS", "0")
    return tmp_path / "home"


async def _admit(account: str | None, cap: int, env: dict[str, str] | None = None):
    return await admit_subagent(
        engine="claude",
        workload="light",
        environ=WAIT_NONE if env is None else env,
        account=account,
        account_cap=cap,
    )


# ---- the account pool ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_cap_holds_back_that_account_only(home):
    first = await _admit(ALPHA, 1)
    try:
        assert [slot.pool for slot in first.slots] == ["account.claude_code.alpha"]
        with pytest.raises(SubagentAdmissionRefusedError) as refused:
            await _admit(ALPHA, 1)
        assert refused.value.pool == "account.claude_code.alpha"
        assert "cap of 1 (max_concurrent of account claude_code.alpha)" in str(refused.value)
        assert refused.value.holders == (os.getpid(),)
        other = await _admit("claude_code.beta", 1)
        assert [slot.pool for slot in other.slots] == ["account.claude_code.beta"]
        other.release()
        no_account = await _admit(None, 0)
        assert no_account.slots == []
    finally:
        first.release()
    again = await _admit(ALPHA, 1)
    assert len(again.slots) == 1
    again.release()


@pytest.mark.asyncio
async def test_an_account_with_a_cap_of_zero_is_refused_never_uncapped(home):
    with pytest.raises(SubagentAdmissionRefusedError, match="1 or more"):
        await _admit(ALPHA, 0)
    assert not (home / "leases").exists()


@pytest.mark.asyncio
async def test_the_account_pool_is_taken_with_the_machine_pools_or_not_at_all(home):
    """Full on the machine-wide pool, an agent holds no slot in its account's pool either."""
    env = {**WAIT_NONE, "LIONAGI_MAX_SUBAGENTS": "1"}
    first = await _admit(ALPHA, 5, env)
    try:
        assert sorted(slot.pool for slot in first.slots) == ["account.claude_code.alpha", "all"]
        with pytest.raises(SubagentAdmissionRefusedError) as refused:
            await _admit(ALPHA, 5, env)
        assert refused.value.pool == "all"
        assert subagent_admission_status(env)["pools"]["account.claude_code.alpha"]["held"] == 1
    finally:
        first.release()
    assert subagent_admission_status(env)["pools"]["account.claude_code.alpha"]["held"] == 0


@pytest.mark.asyncio
async def test_a_smaller_account_cap_a_holder_read_bounds_a_larger_one(home):
    """Lowering max_concurrent while agents run takes hold at the next admission."""
    first = await _admit(ALPHA, 1)
    try:
        with pytest.raises(SubagentAdmissionRefusedError, match="cap of 1 a holder read"):
            await _admit(ALPHA, 5)
    finally:
        first.release()


@pytest.mark.asyncio
async def test_status_reports_an_account_pool_with_the_cap_its_holders_read(home):
    lease = await _admit(ALPHA, 2)
    lease.record_child(4242, None)
    try:
        status = subagent_admission_status(WAIT_NONE)
    finally:
        lease.release()
    pool = status["pools"]["account.claude_code.alpha"]
    assert (pool["cap"], pool["held"]) == (2, 1)
    [holder] = pool["holders"]
    assert (holder["account"], holder["child_pid"], holder["cap"]) == (ALPHA, 4242, 2)
    assert subagent_admission_status(WAIT_NONE)["pools"]["account.claude_code.alpha"] == {
        "cap": 0,
        "held": 0,
        "holders": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["alpha", "Claude.alpha", "claude_code.", "../x", 7])
async def test_an_account_that_cannot_name_a_pool_is_refused(home, account):
    with pytest.raises(SubagentAdmissionRefusedError, match="<provider>.<name>"):
        await _admit(account, 1)  # type: ignore[arg-type]
    assert not (home / "leases").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [True, -1, "2", 1.5, 0])
async def test_an_account_cap_that_is_not_a_whole_number_is_refused(home, cap):
    with pytest.raises(SubagentAdmissionRefusedError, match="not a whole number"):
        await _admit(ALPHA, cap)  # type: ignore[arg-type]


# ---- reading the account from a child environment ---------------------------------------


def test_an_environment_naming_no_account_names_none():
    assert account_admission(None) == (None, 0)
    assert account_admission({}) == (None, 0)
    assert account_admission({ACCOUNT_ENV: "  "}) == (None, 0)


def test_an_environment_names_its_account_and_cap():
    assert account_admission({ACCOUNT_ENV: ALPHA, ACCOUNT_CAP_ENV: "3"}) == (ALPHA, 3)
    assert account_admission({ACCOUNT_ENV: "codex.work-2", ACCOUNT_CAP_ENV: "1"}) == (
        "codex.work-2",
        1,
    )


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({ACCOUNT_ENV: "sk-ant-pasted-by-mistake"}, "does not name an account"),
        ({ACCOUNT_ENV: ALPHA, ACCOUNT_CAP_ENV: "two"}, ACCOUNT_CAP_ENV),
        ({ACCOUNT_ENV: ALPHA, ACCOUNT_CAP_ENV: "-1"}, ACCOUNT_CAP_ENV),
        ({ACCOUNT_ENV: ALPHA, ACCOUNT_CAP_ENV: "0"}, "1 or more"),
        ({ACCOUNT_ENV: ALPHA}, "1 or more"),
        ({ACCOUNT_CAP_ENV: "2"}, "names no account"),
    ],
)
def test_a_malformed_account_environment_is_refused(env, match):
    with pytest.raises(SubagentAdmissionRefusedError, match=match) as refused:
        account_admission(env)
    assert "sk-ant" not in str(refused.value)


# ---- the spawn ----------------------------------------------------------------------------


def _account_env(cap: str = "1") -> dict[str, str]:
    return {**os.environ, ACCOUNT_ENV: ALPHA, ACCOUNT_CAP_ENV: cap}


@pytest.mark.asyncio
async def test_a_spawn_on_a_capped_account_holds_its_pool_and_hides_the_names(home):
    first = ndjson_from_cli(
        [sys.executable, "-c", _REPORT_AND_SLEEP], engine="claude", env=_account_env()
    )
    async with contextlib.aclosing(first):
        assert (await first.__anext__()) == {"seen": []}
        pool = subagent_admission_status()["pools"]["account.claude_code.alpha"]
        assert pool["held"] == 1
        [holder] = pool["holders"]
        assert holder["account"] == ALPHA and isinstance(holder["child_pid"], int)

        second = ndjson_from_cli(
            [sys.executable, "-c", _REPORT_AND_SLEEP], engine="claude", env=_account_env()
        )
        with pytest.raises(SubagentAdmissionRefusedError, match="max_concurrent of account"):
            async with contextlib.aclosing(second):
                await second.__anext__()

        # The machine's own login takes no account pool and is not held back by it.
        own = ndjson_from_cli([sys.executable, "-c", _REPORT_AND_SLEEP], engine="claude")
        async with contextlib.aclosing(own):
            assert (await own.__anext__()) == {"seen": []}
    assert subagent_admission_status()["pools"]["account.claude_code.alpha"]["held"] == 0


@pytest.mark.asyncio
async def test_account_names_this_process_inherited_take_no_pool_and_reach_no_child(
    home, monkeypatch
):
    """A shell that exported the names chose no account for the agent it starts."""
    monkeypatch.setenv(ACCOUNT_ENV, ALPHA)
    monkeypatch.setenv(ACCOUNT_CAP_ENV, "1")
    streams = [
        ndjson_from_cli([sys.executable, "-c", _REPORT_AND_SLEEP], engine="claude")
        for _ in range(2)
    ]
    async with contextlib.aclosing(streams[0]), contextlib.aclosing(streams[1]):
        for stream in streams:
            assert (await stream.__anext__()) == {"seen": []}
        assert "account.claude_code.alpha" not in subagent_admission_status()["pools"]


@pytest.mark.asyncio
async def test_a_malformed_account_cap_refuses_before_the_child_starts(home, tmp_path):
    marker = tmp_path / "started"
    stream = ndjson_from_cli(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
        engine="claude",
        env=_account_env(cap="many"),
    )
    with pytest.raises(SubagentAdmissionRefusedError, match=ACCOUNT_CAP_ENV):
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert not marker.exists()
