# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li monitor run` / `li monitor --run` — scriptable
wait-for-terminal primitive over schedule_runs (additive to the `li monitor`
dashboard, not a replacement)."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from lionagi.cli.monitor import (
    _advance_chains,
    _dispatch_wait,
    _effective_session_status,
    _format_wait_line,
    _new_chain_state,
    _poll_pending_once,
    _query_schedule_runs_since,
    _resolve_schedule_run,
    _resolve_session_run,
    _split_watched_ids,
    add_monitor_subparser,
    run_monitor_wait,
)
from lionagi.cli.status import EXIT_RUNNING, EXIT_UNKNOWN
from lionagi.state.db import StateDB

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp DB; patch DEFAULT_DB_PATH so StateDB() opens it."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_schedule(
    db: StateDB,
    *,
    name: str | None = None,
    on_success: dict[str, Any] | None = None,
    on_fail: dict[str, Any] | None = None,
) -> str:
    sid = uuid.uuid4().hex[:12]
    await db.create_schedule(
        {
            "id": sid,
            "name": name or f"sched-{sid}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
            "on_success": on_success,
            "on_fail": on_fail,
        }
    )
    return sid


async def _make_schedule_run(
    db: StateDB,
    schedule_id: str,
    *,
    status: str = "running",
    exit_code: int | None = None,
    chain_depth: int = 0,
    chain_parent_id: str | None = None,
    invocation_id: str | None = None,
) -> str:
    rid = uuid.uuid4().hex[:12]
    await db.create_schedule_run(
        {
            "id": rid,
            "schedule_id": schedule_id,
            "invocation_id": invocation_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": status,
            "exit_code": exit_code,
            "chain_depth": chain_depth,
            "chain_parent_id": chain_parent_id,
            "fired_at": time.time(),
        }
    )
    return rid


async def _set_fields(db: StateDB, table: str, id_: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), id_))


# ── _split_watched_ids ───────────────────────────────────────────────────────


def test_split_watched_ids_multiple_positional_tokens():
    assert _split_watched_ids(["a", "b", "c"]) == ["a", "b", "c"]


def test_split_watched_ids_comma_separated_single_token():
    assert _split_watched_ids(["a,b,c"]) == ["a", "b", "c"]


def test_split_watched_ids_mixed_tokens_and_commas():
    assert _split_watched_ids(["a,b", "c"]) == ["a", "b", "c"]


def test_split_watched_ids_strips_whitespace():
    assert _split_watched_ids([" a , b "]) == ["a", "b"]


def test_split_watched_ids_dedupes_preserving_first_seen_order():
    assert _split_watched_ids(["a,b,a", "b", "c"]) == ["a", "b", "c"]


def test_split_watched_ids_drops_empty_pieces():
    assert _split_watched_ids(["a,,b", ""]) == ["a", "b"]


# ── _format_wait_line ────────────────────────────────────────────────────────


def test_format_wait_line_contains_all_fields():
    row = {"id": "abc123", "chain_depth": 2, "status": "completed", "exit_code": 0}
    line = _format_wait_line(row, "my-schedule")
    assert "abc123" in line
    assert "my-schedule" in line
    assert "chain_depth=2" in line
    assert "status=completed" in line
    assert "exit_code=0" in line


def test_format_wait_line_none_exit_code_renders_dash():
    row = {"id": "abc123", "chain_depth": 0, "status": "cancelled", "exit_code": None}
    line = _format_wait_line(row, "my-schedule")
    assert "exit_code=-" in line


# ── _resolve_schedule_run ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_schedule_run_exact_match(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        row = await _resolve_schedule_run(db, run_id)
        assert row is not None
        assert row["id"] == run_id


@pytest.mark.asyncio
async def test_resolve_schedule_run_prefix_match(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        row = await _resolve_schedule_run(db, run_id[:6])
        assert row is not None
        assert row["id"] == run_id


@pytest.mark.asyncio
async def test_resolve_schedule_run_not_found_returns_none(temp_db_path: Path) -> None:
    async with StateDB() as db:
        row = await _resolve_schedule_run(db, "totally-unknown-id")
        assert row is None


# ── _poll_pending_once (the testable inner tick — no real sleeps needed) ────


@pytest.mark.asyncio
async def test_poll_pending_once_reports_immediately_terminal_run(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="nightly-build")
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        pending = {run_id: await db.get_schedule_run(run_id)}
        done: list[dict[str, Any]] = []
        await _poll_pending_once(db, pending, {}, done)

    assert [r["id"] for r in done] == [run_id]
    assert run_id not in pending
    out = capsys.readouterr().out
    assert run_id in out
    assert "nightly-build" in out
    assert "status=completed" in out


@pytest.mark.asyncio
async def test_poll_pending_once_prints_coordination_line_when_nonzero(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The wait primitive's per-run print (`li monitor run <id>`) appends a
    coordination one-liner sourced from the run's invocation node_metadata,
    written by the scheduler engine's finalize path -- only when non-zero."""
    coordination = {
        "signals": {"emitted": {"ScheduleRunSucceeded": 1}, "received": 1, "acted_on": 1},
        "files_overlap": {"count": 1, "top": [{"path": "/repo/shared.py", "workers": 2}]},
    }
    async with StateDB() as db:
        inv_id = uuid.uuid4().hex[:12]
        await db.create_invocation(
            {
                "id": inv_id,
                "skill": "scheduled:test",
                "started_at": time.time(),
                "node_metadata": {"coordination": coordination},
            }
        )
        sched_id = await _make_schedule(db, name="coord-sched")
        run_id = await _make_schedule_run(
            db, sched_id, status="completed", exit_code=0, invocation_id=inv_id
        )
        pending = {run_id: await db.get_schedule_run(run_id)}
        done: list[dict[str, Any]] = []
        await _poll_pending_once(db, pending, {}, done)

    out = capsys.readouterr().out
    assert "coordination: emitted=1 received=1 acted_on=1 files_overlap=1" in out


@pytest.mark.asyncio
async def test_poll_pending_once_omits_coordination_line_when_all_zero(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    coordination = {
        "signals": {"emitted": {}, "received": 0, "acted_on": 0},
        "files_overlap": {"count": 0, "top": []},
    }
    async with StateDB() as db:
        inv_id = uuid.uuid4().hex[:12]
        await db.create_invocation(
            {
                "id": inv_id,
                "skill": "scheduled:test",
                "started_at": time.time(),
                "node_metadata": {"coordination": coordination},
            }
        )
        sched_id = await _make_schedule(db, name="zero-sched")
        run_id = await _make_schedule_run(
            db, sched_id, status="completed", exit_code=0, invocation_id=inv_id
        )
        pending = {run_id: await db.get_schedule_run(run_id)}
        done: list[dict[str, Any]] = []
        await _poll_pending_once(db, pending, {}, done)

    out = capsys.readouterr().out
    assert "coordination" not in out


@pytest.mark.asyncio
async def test_poll_pending_once_no_invocation_id_omits_coordination_line(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A schedule_run with no invocation_id (or none found) must not error;
    it simply prints no coordination line."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="no-inv-sched")
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        pending = {run_id: await db.get_schedule_run(run_id)}
        done: list[dict[str, Any]] = []
        await _poll_pending_once(db, pending, {}, done)

    out = capsys.readouterr().out
    assert "coordination" not in out


@pytest.mark.asyncio
async def test_poll_pending_once_leaves_running_run_pending(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="running")
        pending = {run_id: await db.get_schedule_run(run_id)}
        done: list[dict[str, Any]] = []
        await _poll_pending_once(db, pending, {}, done)

    assert done == []
    assert run_id in pending
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_poll_pending_once_across_two_ticks_with_db_mutation_no_real_sleep(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Drives the run from running -> completed between two direct calls to
    the tick function — the deterministic, zero-wall-clock way to exercise
    'terminal across different poll iterations' (see also the real-thread
    variant against _dispatch_wait below)."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="running")
        pending = {run_id: await db.get_schedule_run(run_id)}
        done: list[dict[str, Any]] = []

        await _poll_pending_once(db, pending, {}, done)
        assert done == []
        assert run_id in pending

        await _set_fields(db, "schedule_runs", run_id, status="completed", exit_code=0)

        await _poll_pending_once(db, pending, {}, done)
        assert [r["id"] for r in done] == [run_id]
        assert run_id not in pending

    out = capsys.readouterr().out
    assert out.count(run_id) == 1


@pytest.mark.asyncio
async def test_poll_pending_once_row_vanished_mid_wait_resolves_as_failure(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="running")
        pending = {run_id: await db.get_schedule_run(run_id)}
        await db.execute("DELETE FROM schedule_runs WHERE id = ?", (run_id,))

        done: list[dict[str, Any]] = []
        with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
            await _poll_pending_once(db, pending, {}, done)

    assert run_id not in pending
    assert len(done) == 1
    assert done[0]["exit_code"] is None
    assert "disappeared" in caplog.text.lower()


# ── _advance_chains (the testable chain-follow tick — no real sleeps needed) ─
#
# Mirrors _poll_pending_once's testing style: direct calls with DB mutations
# in between, driving the exact same pending/done/chain_state a real
# `_dispatch_wait` tick would, but deterministically and with zero wall-clock
# waiting for the grace window itself.


async def _chain_tick(
    db: StateDB,
    pending: dict[str, Any],
    done: list[dict[str, Any]],
    chain_state: dict[str, Any],
    processed: int,
) -> int:
    """One `_dispatch_wait` tick's worth of work: poll pending runs, then
    fold newly-terminal rows into chain bookkeeping — exactly the order
    `_dispatch_wait`'s own `_tick()` closure uses."""
    await _poll_pending_once(db, pending, {}, done)
    return await _advance_chains(db, pending, done, chain_state=chain_state, processed=processed)


@pytest.mark.asyncio
async def test_advance_chains_extends_frontier_and_child_exit_code_decides(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """(a) parent terminal + child fires within the grace window -> the
    frontier extends to the child, and the chain concludes on the child's
    own exit code (not the parent's, even though the parent succeeded)."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="parent-sched", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="running")
        pending = {parent_id: await db.get_schedule_run(parent_id)}
        done: list[dict[str, Any]] = []
        chain_state = _new_chain_state(pending, chain=True)
        processed = 0

        # tick 1: parent still running -- nothing to do yet
        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert done == []
        assert chain_state["resolved_roots"] == set()

        # parent goes terminal (success); on_success is declared -> grace starts
        await _set_fields(db, "schedule_runs", parent_id, status="completed", exit_code=0)
        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert parent_id not in pending
        assert parent_id in chain_state["awaiting_grace"]
        assert chain_state["resolved_roots"] == set()

        # child fires (simulating the scheduler engine) before grace expires
        child_id = await _make_schedule_run(
            db, sched_id, status="running", chain_depth=1, chain_parent_id=parent_id
        )
        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert child_id in pending  # frontier extended
        assert not chain_state["awaiting_grace"]
        assert chain_state["resolved_roots"] == set()

        # child completes -- but FAILS this time: the schedule only declares
        # on_success, so a failing child has no further chain action and
        # the chain concludes immediately on the child's own (nonzero) exit
        # code, proving the child -- not the successful parent -- decides.
        await _set_fields(db, "schedule_runs", child_id, status="failed", exit_code=1)
        processed = await _chain_tick(db, pending, done, chain_state, processed)

    assert chain_state["resolved_roots"] == {parent_id}
    assert chain_state["chain_tail_exit"][parent_id] == 1
    out = capsys.readouterr().out
    assert parent_id in out
    assert child_id in out


@pytest.mark.asyncio
async def test_advance_chains_no_declared_action_resolves_without_grace(
    temp_db_path: Path,
) -> None:
    """(c) a schedule declaring on_success only, hit by a FAILED run, has no
    on_fail counterpart -- the root resolves immediately, no grace entry at
    all."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="success-only", on_success={"kind": "agent"})
        run_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)
        pending = {run_id: await db.get_schedule_run(run_id)}
        chain_state = _new_chain_state(pending, chain=True)

        await _chain_tick(db, pending, [], chain_state, 0)

    assert chain_state["resolved_roots"] == {run_id}
    assert chain_state["chain_tail_exit"][run_id] == 1
    assert not chain_state["awaiting_grace"]


@pytest.mark.asyncio
async def test_advance_chains_grace_expires_without_child_resolves_on_parent_exit_code(
    temp_db_path: Path,
) -> None:
    """(d) a schedule declares on_success, but the child never fires within
    the grace window -- the chain concludes on the parent's own exit code
    rather than hanging."""
    async with StateDB() as db:
        sched_id = await _make_schedule(
            db, name="declares-but-never-fires", on_success={"kind": "agent"}
        )
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        pending = {parent_id: await db.get_schedule_run(parent_id)}
        done: list[dict[str, Any]] = []
        chain_state = _new_chain_state(pending, chain=True)
        processed = 0

        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert parent_id in chain_state["awaiting_grace"]
        assert chain_state["resolved_roots"] == set()

        # second tick, still no child anywhere -- grace expires
        processed = await _chain_tick(db, pending, done, chain_state, processed)

    assert chain_state["resolved_roots"] == {parent_id}
    assert chain_state["chain_tail_exit"][parent_id] == 0
    assert not chain_state["awaiting_grace"]


@pytest.mark.asyncio
async def test_advance_chains_cancelled_run_resolves_without_grace(
    temp_db_path: Path,
) -> None:
    """A watched run that lands status="cancelled" can never get a chain
    child fired for it -- the engine's CancelledError branch sets
    status="cancelled" and skips its chain-fire block entirely -- so even
    though the schedule declares a matching on_fail, no grace window opens;
    the root resolves on the very next tick."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="declares-on-fail", on_fail={"kind": "agent"})
        run_id = await _make_schedule_run(db, sched_id, status="cancelled", exit_code=None)
        pending = {run_id: await db.get_schedule_run(run_id)}
        chain_state = _new_chain_state(pending, chain=True)

        await _chain_tick(db, pending, [], chain_state, 0)

    assert chain_state["resolved_roots"] == {run_id}
    assert not chain_state["awaiting_grace"]


@pytest.mark.asyncio
async def test_advance_chains_skipped_run_resolves_without_grace(
    temp_db_path: Path,
) -> None:
    """A skipped run (overlap or missed-fire policy) is created terminal by
    create_skipped_run and never goes through the engine's fire path, so no
    chain child can ever follow it -- even though its schedule declares a
    matching on_fail (skipped runs have exit_code=None), no grace window
    opens; the root resolves on the very next tick."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="declares-on-fail-skip", on_fail={"kind": "agent"})
        run_id = await _make_schedule_run(db, sched_id, status="skipped", exit_code=None)
        pending = {run_id: await db.get_schedule_run(run_id)}
        chain_state = _new_chain_state(pending, chain=True)

        await _chain_tick(db, pending, [], chain_state, 0)

    assert chain_state["resolved_roots"] == {run_id}
    assert not chain_state["awaiting_grace"]


@pytest.mark.asyncio
async def test_advance_chains_failed_run_without_exit_code_resolves_without_grace(
    temp_db_path: Path,
) -> None:
    """A run that failed before its subprocess ever spawned (argv build
    error or internal exception) lands status="failed" with exit_code=None.
    The engine's chain block sits after the subprocess returns a real exit
    code, so such a run can never get a chain child -- even with a matching
    on_fail declared, no grace window opens; the root resolves on the very
    next tick."""
    async with StateDB() as db:
        sched_id = await _make_schedule(
            db, name="declares-on-fail-noexit", on_fail={"kind": "agent"}
        )
        run_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=None)
        pending = {run_id: await db.get_schedule_run(run_id)}
        chain_state = _new_chain_state(pending, chain=True)

        await _chain_tick(db, pending, [], chain_state, 0)

    assert chain_state["resolved_roots"] == {run_id}
    assert not chain_state["awaiting_grace"]


@pytest.mark.asyncio
async def test_advance_chains_chain_depth_at_cap_resolves_without_grace(
    temp_db_path: Path,
) -> None:
    """A watched run already at the engine's chain-depth cap can never get a
    chain child fired for it either -- the engine only fires when
    chain_depth < _MAX_CHAIN_DEPTH (10) -- so a schedule declaring a
    matching on_success still gets no grace window; the root resolves on
    the very next tick."""
    async with StateDB() as db:
        sched_id = await _make_schedule(
            db, name="declares-on-success", on_success={"kind": "agent"}
        )
        run_id = await _make_schedule_run(
            db, sched_id, status="completed", exit_code=0, chain_depth=10
        )
        pending = {run_id: await db.get_schedule_run(run_id)}
        chain_state = _new_chain_state(pending, chain=True)

        await _chain_tick(db, pending, [], chain_state, 0)

    assert chain_state["resolved_roots"] == {run_id}
    assert not chain_state["awaiting_grace"]


@pytest.mark.asyncio
async def test_advance_chains_multi_hop_chain_followed_to_final_link(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """(f) a retry-on-failure chain (on_fail declared, no on_success):
    parent fails -> child1 fails -> child2 succeeds. Depth-2 chain; the
    final link (child2) decides the aggregate, and child2's own schedule
    has no on_success declared so it resolves immediately once it succeeds
    (no further grace, no infinite chase)."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="retry-on-fail", on_fail={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)
        pending = {parent_id: await db.get_schedule_run(parent_id)}
        done: list[dict[str, Any]] = []
        chain_state = _new_chain_state(pending, chain=True)
        processed = 0

        # parent already terminal (failed); on_fail declared -> grace starts
        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert parent_id in chain_state["awaiting_grace"]

        # hop 1 fires and also fails
        child1_id = await _make_schedule_run(
            db, sched_id, status="failed", exit_code=1, chain_depth=1, chain_parent_id=parent_id
        )
        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert child1_id in pending  # discovered, not yet polled this tick
        assert not chain_state["awaiting_grace"]

        # next tick resolves child1 (already terminal) -> on_fail declared
        # again for its own outcome -> grace starts for hop 2
        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert chain_state["chain_tail_exit"][parent_id] == 1
        assert child1_id in chain_state["awaiting_grace"]

        # hop 2 fires and succeeds -- final link
        child2_id = await _make_schedule_run(
            db, sched_id, status="completed", exit_code=0, chain_depth=2, chain_parent_id=child1_id
        )
        processed = await _chain_tick(db, pending, done, chain_state, processed)
        assert child2_id in pending

        processed = await _chain_tick(db, pending, done, chain_state, processed)

    assert chain_state["resolved_roots"] == {parent_id}
    assert chain_state["chain_tail_exit"][parent_id] == 0
    assert not chain_state["awaiting_grace"]
    out = capsys.readouterr().out
    assert parent_id in out
    assert child1_id in out
    assert child2_id in out


# ── _dispatch_wait: bounded wait, no --follow ────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_wait_single_immediately_terminal_success(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="my-sched")
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    started = time.monotonic()
    exit_code = _dispatch_wait([run_id], interval=5.0, follow=False)
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert elapsed < 2.0, "already-terminal run must not wait out a full poll interval"
    out = capsys.readouterr().out
    assert run_id in out
    assert "status=completed" in out


@pytest.mark.asyncio
async def test_dispatch_wait_single_immediately_terminal_failure(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)

    exit_code = _dispatch_wait([run_id], interval=5.0, follow=False)
    assert exit_code == 1


@pytest.mark.asyncio
async def test_dispatch_wait_multi_id_all_terminal_success(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_a = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        run_b = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    exit_code = _dispatch_wait([run_a, run_b], interval=5.0, follow=False)
    assert exit_code == 0


@pytest.mark.asyncio
async def test_dispatch_wait_multi_id_one_nonzero_exit_fails_aggregate(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_a = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        run_b = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)

    exit_code = _dispatch_wait([run_a, run_b], interval=5.0, follow=False)
    assert exit_code == 1


@pytest.mark.asyncio
async def test_dispatch_wait_unknown_id_returns_exit_unknown_without_blocking(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Open+close once so state.db actually exists on disk — this test is
    # about an id that isn't among the (existing) schedule_runs, not about
    # a missing state.db file entirely (see test_dispatch_wait_no_db_file).
    async with StateDB():
        pass

    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        exit_code = _dispatch_wait(["nonexistent-id"], interval=5.0, follow=False)
    elapsed = time.monotonic() - started

    assert exit_code == EXIT_UNKNOWN
    assert elapsed < 2.0, "unresolved id must not enter the poll loop at all"
    assert "nonexistent-id" in caplog.text


def test_dispatch_wait_no_db_file_returns_exit_unknown(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """state.db does not exist yet (fresh install, `li agent` never run) —
    must fail fast with EXIT_UNKNOWN, not try to open/create it."""
    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        exit_code = _dispatch_wait(["anything"], interval=5.0, follow=False)

    assert exit_code == EXIT_UNKNOWN
    assert not temp_db_path.exists()
    assert "state.db not found" in caplog.text


@pytest.mark.asyncio
async def test_dispatch_wait_mixed_unknown_and_resolved_still_returns_exit_unknown(
    temp_db_path: Path, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad id must not be masked by other watched runs succeeding — the
    whole invocation reports EXIT_UNKNOWN, but the good run's line still
    prints (it did finish; the caller should see that)."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        good_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        exit_code = _dispatch_wait([good_id, "bogus-id"], interval=5.0, follow=False)

    assert exit_code == EXIT_UNKNOWN
    assert good_id in capsys.readouterr().out
    assert "bogus-id" in caplog.text


@pytest.mark.asyncio
async def test_dispatch_wait_polls_across_real_iterations_until_background_mutation(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Integration-level companion to the deterministic two-tick test above:
    a background thread flips the row to terminal partway through, proving
    the sync orchestration loop (not just the inner tick function) actually
    polls on a real cadence rather than resolving everything up front."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="running")

    def _flip_to_completed() -> None:
        import asyncio

        async def _go() -> None:
            async with StateDB() as db2:
                await _set_fields(db2, "schedule_runs", run_id, status="completed", exit_code=0)

        time.sleep(0.25)
        asyncio.run(_go())

    t = threading.Thread(target=_flip_to_completed, daemon=True)
    t.start()

    exit_code = _dispatch_wait([run_id], interval=0.05, follow=False)
    t.join(timeout=5)

    assert exit_code == 0
    assert run_id in capsys.readouterr().out


def _interrupt_dispatch_on_tick(monkeypatch: pytest.MonkeyPatch, *, tick: int) -> None:
    """Deterministically interrupt a ``_dispatch_wait`` poll/follow loop.

    Firing ``os.kill(os.getpid(), SIGINT)`` from a wall-clock timer thread aims
    at the shared pytest-xdist worker: if the signal lands after
    ``_dispatch_wait`` has already returned (during teardown) or while a
    KeyboardInterrupt-raising handler is momentarily installed, it escapes and
    crashes the worker. Making ``run_async`` raise ``KeyboardInterrupt`` on the
    ``tick``-th call exercises the exact clean-exit path ``_dispatch_wait``
    takes when SIGINT lands mid-tick, with no signal that can outlive the call.
    Call 1 is the initial resolve, call 2 the first bounded tick, later calls
    the follow-tail ticks.
    """
    import lionagi.ln.concurrency as concurrency_mod

    real_run_async = concurrency_mod.run_async
    calls = {"n": 0}

    def _fake_run_async(coro: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == tick:
            coro.close()
            raise KeyboardInterrupt
        return real_run_async(coro)

    monkeypatch.setattr(concurrency_mod, "run_async", _fake_run_async)


@pytest.mark.asyncio
async def test_dispatch_wait_sigint_while_still_pending_returns_exit_running(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that never goes terminal + an interrupt mid-wait must report
    EXIT_RUNNING, not silently succeed."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="running")

    # call 1 resolves the still-running run; interrupt the first poll tick.
    _interrupt_dispatch_on_tick(monkeypatch, tick=2)

    exit_code = _dispatch_wait([run_id], interval=0.05, follow=False)

    assert exit_code == EXIT_RUNNING


@pytest.mark.asyncio
async def test_dispatch_wait_interrupted_during_resolve_returns_exit_running(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If SIGINT lands during the very first resolve call (before we even
    know whether the requested ids exist), we cannot claim success -- must
    report EXIT_RUNNING, never a vacuous 0."""
    async with StateDB():
        pass  # ensure state.db exists on disk before _dispatch_wait checks it

    import lionagi.ln.concurrency as concurrency_mod

    def _fake_run_async(coro: Any) -> Any:
        coro.close()  # avoid "coroutine was never awaited" warning
        raise KeyboardInterrupt

    monkeypatch.setattr(concurrency_mod, "run_async", _fake_run_async)

    exit_code = _dispatch_wait(["some-id"], interval=5.0, follow=False)
    assert exit_code == EXIT_RUNNING


@pytest.mark.asyncio
async def test_dispatch_wait_keyboard_interrupt_after_tick_mutates_state_still_reports_failure(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: run_async can complete a tick's real work (printing a
    terminal row, mutating `pending`/`done`) and *then* raise
    KeyboardInterrupt before _dispatch_wait ever sees the tick's return
    value -- exactly what happens when SIGINT is delivered right as the
    tick's coroutine finishes. The already-observed failure must still be
    reflected in the exit code, not lost to a vacuous empty-`done` success.
    """
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)

    import lionagi.ln.concurrency as concurrency_mod

    real_run_async = concurrency_mod.run_async
    calls = {"n": 0}

    def _fake_run_async(coro: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # call 1 = resolve; call 2 = the first tick
            real_run_async(coro)  # let it actually run and mutate state...
            raise KeyboardInterrupt  # ...then simulate SIGINT racing the return
        return real_run_async(coro)

    monkeypatch.setattr(concurrency_mod, "run_async", _fake_run_async)

    exit_code = _dispatch_wait([run_id], interval=5.0, follow=False)
    assert exit_code == 1


# ── _dispatch_wait: chain-following (li monitor run's default; --no-chain) ──


@pytest.mark.asyncio
async def test_dispatch_wait_chain_follow_on_fail_recovery_final_link_wins(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """(b) a failed parent whose schedule declares on_fail, and whose
    already-fired child succeeded, must report exit 0 -- the chain
    recovered, and the *final* link decides, not the failed first one. Both
    links' lines are printed."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="flaky", on_fail={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)
        child_id = await _make_schedule_run(
            db, sched_id, status="completed", exit_code=0, chain_depth=1, chain_parent_id=parent_id
        )

    exit_code = _dispatch_wait([parent_id], interval=0.05, follow=False)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert parent_id in out
    assert child_id in out


@pytest.mark.asyncio
async def test_dispatch_wait_chain_follow_no_declared_action_resolves_immediately(
    temp_db_path: Path,
) -> None:
    """(c) a schedule declaring on_success only, hit by a FAILED run, needs
    no grace wait at all -- must resolve well before a full poll interval,
    not just before some generous timeout."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="success-only", on_success={"kind": "agent"})
        run_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)

    started = time.monotonic()
    exit_code = _dispatch_wait([run_id], interval=5.0, follow=False)
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert elapsed < 2.0, "no matching chain action declared -- must not enter a grace wait"


@pytest.mark.asyncio
async def test_dispatch_wait_chain_follow_grace_expiry_does_not_hang(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """(d) a schedule declares on_success but the child never fires -- the
    wait must still conclude (on the parent's own exit code), not hang
    forever waiting on a chain child that's never coming."""
    async with StateDB() as db:
        sched_id = await _make_schedule(
            db, name="declares-but-never-fires", on_success={"kind": "agent"}
        )
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    started = time.monotonic()
    exit_code = _dispatch_wait([parent_id], interval=0.02, follow=False)
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert elapsed < 3.0
    assert parent_id in capsys.readouterr().out


@pytest.mark.asyncio
async def test_dispatch_wait_no_chain_ignores_fired_children(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """(e) --no-chain (chain=False) watches only the literal id given, even
    when a chain child has already fired for it."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="chained", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        child_id = await _make_schedule_run(
            db, sched_id, status="failed", exit_code=1, chain_depth=1, chain_parent_id=parent_id
        )

    exit_code = _dispatch_wait([parent_id], interval=5.0, follow=False, chain=False)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert parent_id in out
    assert child_id not in out


@pytest.mark.asyncio
async def test_dispatch_wait_overlapping_roots_child_watched_directly_succeeds(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Regression: a parent AND its already-linked chain child are both
    passed as initial watch roots (e.g. from comma/list expansion). The
    parent is terminal from the start, which starts a grace window that
    discovers the child via chain_parent_id -- but the child is *also* one
    of the originally-watched roots, and is still running at that moment.
    The discovery must not clobber the child's own root ownership: once the
    child later completes on its own, both the parent's root (resolved via
    the child as its final link) and the child's own root must resolve, not
    just the parent's."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="chained", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        child_id = await _make_schedule_run(
            db, sched_id, status="running", chain_depth=1, chain_parent_id=parent_id
        )

    def _flip_child_to_completed() -> None:
        import asyncio

        async def _go() -> None:
            async with StateDB() as db2:
                await _set_fields(db2, "schedule_runs", child_id, status="completed", exit_code=0)

        time.sleep(0.2)
        asyncio.run(_go())

    t = threading.Thread(target=_flip_child_to_completed, daemon=True)
    t.start()

    exit_code = _dispatch_wait([parent_id, child_id], interval=0.05, follow=False)
    t.join(timeout=5)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count(parent_id) == 1
    assert out.count(child_id) == 1


@pytest.mark.asyncio
async def test_dispatch_wait_overlapping_roots_child_watched_directly_fails(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Failure-side variant of the overlapping-roots regression above: the
    child (also a directly-watched root) completes with a nonzero exit code
    -- the aggregate must report failure (1), not fall back to EXIT_RUNNING
    because the child's own root never resolved."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="chained", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        child_id = await _make_schedule_run(
            db, sched_id, status="running", chain_depth=1, chain_parent_id=parent_id
        )

    def _flip_child_to_failed() -> None:
        import asyncio

        async def _go() -> None:
            async with StateDB() as db2:
                await _set_fields(db2, "schedule_runs", child_id, status="failed", exit_code=1)

        time.sleep(0.2)
        asyncio.run(_go())

    t = threading.Thread(target=_flip_child_to_failed, daemon=True)
    t.start()

    exit_code = _dispatch_wait([parent_id, child_id], interval=0.05, follow=False)
    t.join(timeout=5)

    assert exit_code == 1
    out = capsys.readouterr().out
    assert out.count(parent_id) == 1
    assert out.count(child_id) == 1


@pytest.mark.asyncio
async def test_dispatch_wait_child_already_terminal_same_tick_prints_once(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Regression: parent AND its chain child are *both* already terminal
    before `_dispatch_wait` even starts (no background flip needed) -- the
    very first poll tick prints both directly, then the parent's grace-
    window discovery finds the child via chain_parent_id. The child's own
    schedule declares no chain action of its own, so it should already be
    resolved outright by the time discovery reaches it -- re-adding it to
    `pending` would make the next tick's `_poll_pending_once` print it a
    second time."""
    async with StateDB() as db:
        parent_sched = await _make_schedule(db, name="parent-sched", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, parent_sched, status="completed", exit_code=0)
        child_sched = await _make_schedule(db, name="child-sched")
        child_id = await _make_schedule_run(
            db,
            child_sched,
            status="completed",
            exit_code=0,
            chain_depth=1,
            chain_parent_id=parent_id,
        )

    exit_code = _dispatch_wait([parent_id, child_id], interval=0.05, follow=False)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count(parent_id) == 1
    assert out.count(child_id) == 1


@pytest.mark.asyncio
async def test_dispatch_wait_child_already_terminal_joins_own_grace_prints_once(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Variant of the above: the already-terminal child's own schedule
    *also* declares a matching chain action (on_success), so discovery must
    join the parent's root into the child's own `awaiting_grace` entry
    instead of resolving it outright -- and once that grace window expires
    (no grandchild ever fires), both roots resolve together on the child's
    exit code, each line still printed exactly once."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="chained", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        child_id = await _make_schedule_run(
            db, sched_id, status="completed", exit_code=0, chain_depth=1, chain_parent_id=parent_id
        )

    exit_code = _dispatch_wait([parent_id, child_id], interval=0.02, follow=False)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count(parent_id) == 1
    assert out.count(child_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("descendant_first", [False, True])
async def test_dispatch_wait_deep_chain_follows_handoff_past_terminal_child(
    temp_db_path: Path, capsys: pytest.CaptureFixture, descendant_first: bool
) -> None:
    """Regression: a three-link chain (parent failed -> child failed ->
    grandchild succeeded), all terminal before the wait starts, watched via
    both the parent AND the child as overlapping roots. When the child is
    listed first, its grace window discovers the grandchild and hands its
    root off BEFORE the parent's grace window discovers the child — so the
    parent's discovery finds an already-processed child that is no longer
    in awaiting_grace. It must follow the child's handoff forward to the
    grandchild (the chain's real tail) instead of resolving the parent's
    root on the child's intermediate failure: final-link-wins means this
    recovered chain reports success in both watch orders, each run printed
    exactly once."""
    async with StateDB() as db:
        parent_sched = await _make_schedule(db, name="parent-sched", on_fail={"kind": "agent"})
        parent_id = await _make_schedule_run(db, parent_sched, status="failed", exit_code=1)
        child_sched = await _make_schedule(db, name="child-sched", on_fail={"kind": "agent"})
        child_id = await _make_schedule_run(
            db, child_sched, status="failed", exit_code=1, chain_depth=1, chain_parent_id=parent_id
        )
        grand_sched = await _make_schedule(db, name="grandchild-sched")
        grandchild_id = await _make_schedule_run(
            db,
            grand_sched,
            status="completed",
            exit_code=0,
            chain_depth=2,
            chain_parent_id=child_id,
        )

    roots = [child_id, parent_id] if descendant_first else [parent_id, child_id]
    exit_code = _dispatch_wait(roots, interval=0.02, follow=False)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count(parent_id) == 1
    assert out.count(child_id) == 1
    assert out.count(grandchild_id) == 1


# ── _query_schedule_runs_since (the --follow baseline boundary, deterministic) ──


@pytest.mark.asyncio
async def test_query_schedule_runs_since_only_returns_strictly_newer(temp_db_path: Path) -> None:
    """The exact SQL boundary --follow relies on: a run created AT baseline
    must not be re-returned (strict '>'), only ones created after it."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        old_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        old_row = await db.get_schedule_run(old_id)
        baseline = old_row["created_at"]

        new_id = await _make_schedule_run(db, sched_id, status="running")
        new_rows = await _query_schedule_runs_since(db, baseline)

    assert [r["id"] for r in new_rows] == [new_id]


@pytest.mark.asyncio
async def test_query_schedule_runs_since_empty_when_nothing_newer(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        row = await db.get_schedule_run(run_id)
        new_rows = await _query_schedule_runs_since(db, row["created_at"])

    assert new_rows == []


# ── _dispatch_wait: --follow (baseline-first tail behavior) ─────────────────


@pytest.mark.asyncio
async def test_dispatch_wait_follow_sigint_clean(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors test_watch_mode_sigint_clean in test_monitor.py: --follow must
    exit cleanly (not hang, not traceback) on interrupt once the initial set
    has drained."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    # call 1 resolves, call 2 drains the initial set; interrupt the first
    # follow-tail tick (call 3).
    _interrupt_dispatch_on_tick(monkeypatch, tick=3)

    exit_code = _dispatch_wait([run_id], interval=0.05, follow=True)

    assert exit_code == 0


@pytest.mark.asyncio
async def test_dispatch_wait_follow_preserves_initial_failure_exit_code(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: --follow must not collapse an initial bounded-set FAILURE
    into a false success just because the follow phase was entered and later
    interrupted. The initial watched set's aggregate result is the contract
    --follow's exit code honors; the open-ended tail has no result of its
    own to report."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="failed", exit_code=1)

    # call 1 resolves, call 2 drains the initial (failed) set; interrupt the
    # first follow-tail tick (call 3).
    _interrupt_dispatch_on_tick(monkeypatch, tick=3)

    exit_code = _dispatch_wait([run_id], interval=0.05, follow=True)

    assert exit_code == 1


@pytest.mark.asyncio
async def test_dispatch_wait_follow_ignores_pre_existing_runs_reports_only_new(
    temp_db_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline-first discipline: a schedule_run that already existed before
    --follow started watching must never be (re-)reported during the follow
    phase; only runs created after the baseline was captured are new."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        watched_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        pre_existing_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    new_run_id: dict[str, str] = {}

    async def _create_new_run() -> None:
        async with StateDB() as db2:
            new_run_id["id"] = await _make_schedule_run(
                db2, sched_id, status="completed", exit_code=0
            )

    # call 1 resolves, call 2 drains the watched set and enters follow; on the
    # first follow-tail tick (call 3) create a new run (created after the
    # baseline, so the same tick reports it), then interrupt the next tick.
    import lionagi.ln.concurrency as concurrency_mod

    real_run_async = concurrency_mod.run_async
    calls = {"n": 0}

    def _fake_run_async(coro: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            real_run_async(_create_new_run())
            return real_run_async(coro)
        if calls["n"] == 4:
            coro.close()
            raise KeyboardInterrupt
        return real_run_async(coro)

    monkeypatch.setattr(concurrency_mod, "run_async", _fake_run_async)

    exit_code = _dispatch_wait([watched_id], interval=0.05, follow=True)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert watched_id in out  # the originally-requested id: reported during the bounded phase
    assert new_run_id["id"] in out  # created after baseline: reported during follow
    assert pre_existing_id not in out  # existed before baseline: never re-reported


# ── argv parsing: `--run` flag form + `--interval`/`--follow` on the main parser ──


def test_add_monitor_subparser_run_flag_and_new_options():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_monitor_subparser(sub)

    args = parser.parse_args(["monitor", "--run", "id1,id2,id3"])
    assert args.run_ids == "id1,id2,id3"
    assert args.interval == 3.0
    assert args.follow is False
    assert args.chain is True  # chain-following is the default

    args = parser.parse_args(
        ["monitor", "--run", "id1", "--interval", "0.5", "--follow", "--no-chain"]
    )
    assert args.run_ids == "id1"
    assert args.interval == 0.5
    assert args.follow is True
    assert args.chain is False


@pytest.mark.asyncio
async def test_run_monitor_run_flag_no_chain_ignores_fired_children(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """CLI wiring: `li monitor --run <id> --no-chain` must actually thread
    chain=False through to _dispatch_wait, not just parse the flag."""
    import argparse

    from lionagi.cli.monitor import run_monitor

    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="chained", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        child_id = await _make_schedule_run(
            db, sched_id, status="failed", exit_code=1, chain_depth=1, chain_parent_id=parent_id
        )

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_monitor_subparser(sub)
    args = parser.parse_args(["monitor", "--run", parent_id, "--no-chain"])

    exit_code = run_monitor(args)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert parent_id in out
    assert child_id not in out


def test_run_monitor_run_flag_comma_only_ids_is_usage_error(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`li monitor --run ,,,` is truthy but splits to zero ids; it must fail
    as a usage error, not dispatch an empty watch set and exit 0."""
    import argparse

    from lionagi.cli.monitor import run_monitor

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_monitor_subparser(sub)
    args = parser.parse_args(["monitor", "--run", ",,,"])

    with caplog.at_level(logging.ERROR):
        exit_code = run_monitor(args)
    assert exit_code == 2
    assert "no schedule_run ids" in caplog.text


def test_add_monitor_subparser_existing_dashboard_args_unaffected():
    """Regression: adding --run/--interval/--follow must not disturb the
    existing bare / --watch / --since / --type / --project surface."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_monitor_subparser(sub)

    args = parser.parse_args(["monitor"])
    assert args.id is None
    assert not args.watch
    assert args.run_ids is None

    args = parser.parse_args(["monitor", "abc123", "--watch"])
    assert args.id == "abc123"
    assert args.watch


# ── run_monitor_wait: the `li monitor run <id>...` positional entry point ───


@pytest.mark.asyncio
async def test_run_monitor_wait_single_positional_id(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    exit_code = run_monitor_wait([run_id])
    assert exit_code == 0
    assert run_id in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_monitor_wait_no_chain_flag_ignores_fired_children(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`li monitor run <id> --no-chain` — the positional entry point wires
    --no-chain through to _dispatch_wait too, not just the --run flag form."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="chained", on_success={"kind": "agent"})
        parent_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        child_id = await _make_schedule_run(
            db, sched_id, status="failed", exit_code=1, chain_depth=1, chain_parent_id=parent_id
        )

    exit_code = run_monitor_wait([parent_id, "--no-chain"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert parent_id in out
    assert child_id not in out


@pytest.mark.asyncio
async def test_run_monitor_wait_comma_separated_single_token(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`li monitor run id1,id2` — one argv token, both ids must resolve."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_a = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        run_b = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    exit_code = run_monitor_wait([f"{run_a},{run_b}"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert run_a in out
    assert run_b in out


@pytest.mark.asyncio
async def test_run_monitor_wait_multiple_positional_tokens(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`li monitor run id1 id2` — two argv tokens, both ids must resolve."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_a = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)
        run_b = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    exit_code = run_monitor_wait([run_a, run_b])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert run_a in out
    assert run_b in out


def test_run_monitor_wait_unknown_id_returns_exit_unknown(temp_db_path: Path) -> None:
    assert run_monitor_wait(["nonexistent-id"]) == EXIT_UNKNOWN


def test_run_monitor_wait_requires_at_least_one_id():
    with pytest.raises(SystemExit):
        run_monitor_wait([])


def test_run_monitor_wait_comma_only_token_rejected_as_usage_error():
    """nargs="+" only guarantees a non-empty argv list, not that any token
    survives comma-splitting -- a single "," token must be treated the same
    as no ids at all, not silently dispatched as a zero-id wait."""
    with pytest.raises(SystemExit) as exc_info:
        run_monitor_wait([",,,"])
    assert exc_info.value.code == 2


def test_run_monitor_wait_empty_string_token_rejected_as_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        run_monitor_wait([""])
    assert exc_info.value.code == 2


# ── CLI wiring: `li monitor run --help` / `li monitor --help` subprocess ────


# ── ADR-0094 regression: `li monitor run` output format is untouched ───────


@pytest.mark.asyncio
async def test_monitor_run_output_format_byte_identical_after_adr_0094(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The new `li wait` verb (ADR-0094) must not change a single byte of
    `li monitor run`'s own line format — it is a distinct contract
    (`name=`/`chain_depth=`, no `reason=`/`artifact_dir=`)."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db, name="regression-check")
        run_id = await _make_schedule_run(db, sched_id, status="completed", exit_code=0)

    exit_code = run_monitor_wait([run_id])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert (
        out.strip()
        == f"{run_id}  name=regression-check  chain_depth=0  status=completed  exit_code=0"
    )


def test_cli_monitor_run_help_subprocess():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "monitor", "run", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "follow" in result.stdout.lower()
    assert "interval" in result.stdout.lower()
    assert "chain" in result.stdout.lower()


def test_cli_monitor_help_still_shows_dashboard_usage():
    """Regression: `li monitor --help` (no 'run') must still describe the
    existing dashboard, proving the pre-dispatch interception in main.py
    only intercepts the literal 'run' token, not all of `li monitor`."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "monitor", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--watch" in result.stdout


# ── li monitor run: resolving agent session ids (issue: profile-typed agent
# sessions previously errored with "schedule_run not found") ────────────────


async def _make_agent_session(db: StateDB, *, status: str = "completed") -> str:
    """A minimal 'sessions' row shaped like a li agent run, bypassing schedule_runs."""
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist, teardown_agent_persist

    branch = Branch(name="b1")
    ctx = await setup_agent_persist(branch, agent_name="implementer")
    assert ctx is not None
    await teardown_agent_persist(ctx, status=status)
    return ctx["session_id"]


@pytest.mark.asyncio
async def test_resolve_session_run_exact_match(temp_db_path: Path) -> None:
    async with StateDB() as db:
        session_id = await _make_agent_session(db, status="completed")
        row = await _resolve_session_run(db, session_id)
    assert row is not None
    assert row["id"] == session_id


@pytest.mark.asyncio
async def test_resolve_session_run_prefix_match(temp_db_path: Path) -> None:
    async with StateDB() as db:
        session_id = await _make_agent_session(db, status="completed")
        row = await _resolve_session_run(db, session_id[:12])
    assert row is not None
    assert row["id"] == session_id


@pytest.mark.asyncio
async def test_resolve_session_run_not_found_returns_none(temp_db_path: Path) -> None:
    async with StateDB() as db:
        row = await _resolve_session_run(db, "no-such-session")
    assert row is None


@pytest.mark.asyncio
async def test_dispatch_wait_resolves_completed_agent_session(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`li monitor run <session_id>` must resolve an agent session id — not
    error 'schedule_run not found' — and report its terminal status."""
    async with StateDB() as db:
        session_id = await _make_agent_session(db, status="completed")

    exit_code = _dispatch_wait([session_id], interval=5.0, follow=False)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert session_id in out
    assert "status=completed" in out


@pytest.mark.asyncio
async def test_dispatch_wait_resolves_failed_agent_session_nonzero_exit(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        session_id = await _make_agent_session(db, status="failed")

    exit_code = _dispatch_wait([session_id], interval=5.0, follow=False)
    assert exit_code == 1


@pytest.mark.asyncio
async def test_dispatch_wait_agent_session_still_running_returns_exit_running(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that never goes terminal + an interrupt mid-wait must
    report EXIT_RUNNING, not hang forever or silently succeed."""
    async with StateDB() as db:
        from lionagi import Branch
        from lionagi.cli._runs import setup_agent_persist

        branch = Branch(name="b1")
        ctx = await setup_agent_persist(branch, agent_name="implementer")
        assert ctx is not None
        session_id = ctx["session_id"]

    # call 1 resolves the still-running session; interrupt the first poll tick.
    _interrupt_dispatch_on_tick(monkeypatch, tick=2)

    exit_code = _dispatch_wait([session_id], interval=0.05, follow=False)

    assert exit_code == EXIT_RUNNING


# ── li monitor run: linked_engine_session_id drill-in + bounded --max-wait ──


@pytest.mark.asyncio
async def test_dispatch_wait_follows_linked_engine_session_to_completion(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A profile-typed session teardown pinned at 'running' (linked engine
    session was alive but not yet terminal) must resolve via the linked
    engine row's status, not hang on its own frozen 'running' column."""
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist, teardown_agent_persist
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-333333333333"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working"}],
                    },
                }
            ],
            tool_names={},
            status="running",
        )

        branch = Branch(name="b1")
        ctx = await setup_agent_persist(branch, agent_name="implementer")
        assert ctx is not None
        session_id = ctx["session_id"]
        final = await teardown_agent_persist(
            ctx,
            status="failed",
            exception=ProviderError("abandoned stream reader"),
            engine_session_uid=engine_uid,
        )
    assert final == "running"

    def _flip_engine_to_completed() -> None:
        import asyncio

        from lionagi.state.claude_mirror import session_db_id

        async def _go() -> None:
            async with StateDB() as db2:
                await _set_fields(db2, "sessions", session_db_id(engine_uid), status="completed")

        time.sleep(0.25)
        asyncio.run(_go())

    t = threading.Thread(target=_flip_engine_to_completed, daemon=True)
    t.start()

    exit_code = _dispatch_wait([session_id], interval=0.05, follow=False)
    t.join(timeout=5)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert session_id in out
    assert "status=completed" in out


@pytest.mark.asyncio
async def test_dispatch_wait_persists_reconciled_status_to_profile_row(
    temp_db_path: Path,
) -> None:
    """`li monitor run` resolving a profile session via its linked engine row must
    not just SYNTHESIZE the terminal status in memory for this one call -- it must
    PERSIST it through StateDB.update_status() so the profile session's own DB row
    reads the reconciled terminal status too, not stuck at 'running' forever."""
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist, teardown_agent_persist
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session, session_db_id

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-333333333334"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working"}],
                    },
                }
            ],
            tool_names={},
            status="running",
        )

        branch = Branch(name="b1")
        ctx = await setup_agent_persist(branch, agent_name="implementer")
        assert ctx is not None
        session_id = ctx["session_id"]
        final = await teardown_agent_persist(
            ctx,
            status="failed",
            exception=ProviderError("abandoned stream reader"),
            engine_session_uid=engine_uid,
        )
    assert final == "running"

    async with StateDB() as db:
        await _set_fields(db, "sessions", session_db_id(engine_uid), status="completed")

    exit_code = _dispatch_wait([session_id], interval=0.05, follow=False, max_wait=1.0)
    assert exit_code == 0

    async with StateDB() as db:
        persisted = await db.get_session(session_id)
    assert persisted is not None
    assert persisted["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_wait_reconciliation_never_flips_an_already_terminal_row(
    temp_db_path: Path,
) -> None:
    """A profile row that is ALREADY terminal ('failed') must never be
    force-reconciled to a *different* terminal status the linked engine
    later reports ('completed') -- ADR-0094's terminal guard rejects that
    write, and `li monitor run` must report the persisted 'failed' status
    (the terminal row is authoritative) instead of crashing on the
    rejected transition."""
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist, teardown_agent_persist
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session, session_db_id

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-333333333335"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working"}],
                    },
                }
            ],
            tool_names={},
            status="running",
        )

        branch = Branch(name="b1")
        ctx = await setup_agent_persist(branch, agent_name="implementer")
        assert ctx is not None
        session_id = ctx["session_id"]
        final = await teardown_agent_persist(
            ctx,
            status="failed",
            exception=ProviderError("abandoned stream reader"),
            engine_session_uid=engine_uid,
        )
    assert final == "running"

    async with StateDB() as db:
        # The profile row resolved to failure on its own (independent of the
        # linked engine mirror) and is now terminal -- while the linked engine
        # later reports a *different* terminal status.
        await _set_fields(db, "sessions", session_id, status="failed")
        await _set_fields(db, "sessions", session_db_id(engine_uid), status="completed")

    exit_code = _dispatch_wait([session_id], interval=0.05, follow=False, max_wait=1.0)

    assert exit_code == 1

    async with StateDB() as db:
        persisted = await db.get_session(session_id)
    assert persisted is not None
    assert persisted["status"] == "failed"


@pytest.mark.asyncio
async def test_effective_session_status_cas_mismatch_reports_persisted_status(
    temp_db_path: Path,
) -> None:
    """`db.update_status()` returns `False` (rather than raising
    `TransitionRejectedError`) when the persisted row simply no longer matches
    `expected_statuses` at write time -- e.g. it moved between our stale read
    and this write, but the guard rejects on the CAS mismatch before it ever
    reaches ADR-0094's terminal check. `_effective_session_status()` must not
    ignore that `False` and fall through to the synthesized linked-engine
    status; it must re-read and report the persisted row instead."""
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist, teardown_agent_persist
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session, session_db_id

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-333333333336"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working"}],
                    },
                }
            ],
            tool_names={},
            status="running",
        )

        branch = Branch(name="b1")
        ctx = await setup_agent_persist(branch, agent_name="implementer")
        assert ctx is not None
        session_id = ctx["session_id"]
        final = await teardown_agent_persist(
            ctx,
            status="failed",
            exception=ProviderError("abandoned stream reader"),
            engine_session_uid=engine_uid,
        )
        assert final == "running"

        # This is the stale read `_effective_session_status()` would act on --
        # status="running", still linked to the engine session.
        stale_row = await db.get_session(session_id)
        assert stale_row is not None
        assert stale_row["status"] == "running"

        # Simulate the race: between that read and the reconciliation write,
        # something else (e.g. the profile session's own executor) persisted
        # this row to "failed", while the linked engine session went
        # "completed" independently.
        await _set_fields(db, "sessions", session_id, status="failed")
        await _set_fields(db, "sessions", session_db_id(engine_uid), status="completed")

        result = await _effective_session_status(db, stale_row)

        assert result["status"] == "failed"

        persisted = await db.get_session(session_id)
        assert persisted is not None
        assert persisted["status"] == "failed"


@pytest.mark.asyncio
async def test_dispatch_wait_max_wait_bounds_a_stuck_session(temp_db_path: Path) -> None:
    """A session that never reconciles (no --max-wait would hang this forever)
    must give up after max_wait seconds and report EXIT_RUNNING."""
    async with StateDB() as db:
        from lionagi import Branch
        from lionagi.cli._runs import setup_agent_persist

        branch = Branch(name="b1")
        ctx = await setup_agent_persist(branch, agent_name="implementer")
        assert ctx is not None
        session_id = ctx["session_id"]

    started = time.monotonic()
    exit_code = _dispatch_wait([session_id], interval=0.05, follow=False, max_wait=0.3)
    elapsed = time.monotonic() - started

    assert exit_code == EXIT_RUNNING
    assert elapsed < 3.0, "max_wait must bound the loop instead of hanging"


@pytest.mark.asyncio
async def test_dispatch_wait_default_max_wait_bounds_a_stuck_session_without_caller_bound(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller supplies no max_wait at all (the real production default path for
    `li monitor run` / `li monitor --run` when --max-wait is omitted) and there is no
    external interrupt -- the built-in bounded default must still stop the wait and
    report EXIT_RUNNING instead of hanging forever. The module-level default is
    monkeypatched down to keep this test fast; the caller-facing contract under test
    is that omitting max_wait entirely still bounds the wait."""
    import lionagi.cli.monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "_DEFAULT_MAX_WAIT_SECONDS", 0.3)

    async with StateDB() as db:
        from lionagi import Branch
        from lionagi.cli._runs import setup_agent_persist

        branch = Branch(name="b1")
        ctx = await setup_agent_persist(branch, agent_name="implementer")
        assert ctx is not None
        session_id = ctx["session_id"]

    started = time.monotonic()
    exit_code = _dispatch_wait([session_id], interval=0.05, follow=False)
    elapsed = time.monotonic() - started

    assert exit_code == EXIT_RUNNING
    assert elapsed < 3.0, "the default bound must stop the loop instead of hanging"
