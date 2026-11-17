# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""Single-target kill previews and stale classification use read-only process observations."""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import psutil
import pytest

from lionagi.ln import _proc
from lionagi.ln.concurrency import run_async
from lionagi.state import db as db_module
from lionagi.state import engine
from lionagi.state.reasons import RunReasons
from lionhub.orchestration import kill as orchestration_kill
from lionhub.orchestration.runs import PERSISTENCE_DEGRADED_REASON_FIELD
from lionhub.serve.cli import kill as cli_kill
from lionhub.serve.cli.main import main
from tests._kill_degraded_run_test_support import RUN_ID as RUN_ID
from tests._kill_degraded_run_test_support import _write_run
from tests._kill_test_support import _script_create_time, _seed_invocation, _seed_session
from tests._kill_test_support import temp_db_path as temp_db_path


def _unexpected(*_args, **_kwargs):
    raise AssertionError("unexpected signal, write, or process lookup")


def _command_identity(pid: int) -> dict:
    return {
        "action_kind": "command",
        "pid": pid,
        "pgid": pid,
        "pid_create_time": float(pid),
        "pid_host": "command-host",
        "pid_boot_time": 100.0,
        "process_identity_mode": "local",
    }


def _observe_command_groups(monkeypatch, *, live=(), occupied=(), empty=()) -> None:
    monkeypatch.setattr(_proc.socket, "gethostname", lambda: "command-host")
    monkeypatch.setattr(psutil, "boot_time", lambda: 100.0)
    monkeypatch.setattr(os, "getpgrp", lambda: 9000, raising=False)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(os, "kill", _unexpected)
    monkeypatch.setattr(os, "killpg", _unexpected, raising=False)

    def birth(pid, *, expected_created=None):
        if pid in live:
            return "found", float(pid)
        assert pid in (*occupied, *empty)
        return "gone", None

    def members(pgid):
        if pgid in live:
            return [(pgid, float(pgid)), (pgid + 100, float(pgid + 100))], True
        if pgid in occupied:
            return [(pgid + 100, float(pgid + 100))], True
        assert pgid in empty
        return [], True

    monkeypatch.setattr(_proc, "_recorded_process_create_time", birth)
    monkeypatch.setattr(_proc, "_group_member_identities", members)
    monkeypatch.setattr(cli_kill, "_pid_alive", _unexpected)
    monkeypatch.setattr(cli_kill, "check_pid_identity", _unexpected)


async def _invocation(db, metadata, *, name=None):
    invocation_id = name or str(uuid.uuid4())
    await db.create_invocation(
        {
            "id": invocation_id,
            "skill": "test",
            "started_at": time.time() - 7_200,
            "status": "running",
            "node_metadata": metadata,
        }
    )
    return invocation_id


async def _read_invocation(invocation_id):
    async with db_module.StateDB(readonly=True) as db:
        row = await db.get_invocation(invocation_id)
        transitions = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (invocation_id,)
        )
    return row, transitions


@pytest.mark.parametrize(
    ("metadata", "status", "text"),
    [
        (_command_identity(5001), 0, "would send SIGTERM, then SIGKILL if still live after 2.5s"),
        ({"action_kind": "command"}, 0, "signal=no_pid"),
        ({**_command_identity(5001), "pgid": None}, 1, "signal=identity_unverifiable"),
        ({**_command_identity(5001), "pid_host": "another-host"}, 1, "signal=host_mismatch"),
    ],
    ids=["live", "no-pid", "partial", "foreign"],
)
def test_single_id_preview_preserves_row_and_transition_history(
    temp_db_path, monkeypatch, capsys, metadata, status, text
):
    async def seed():
        async with db_module.StateDB() as db:
            return await _invocation(db, metadata)

    invocation_id = run_async(seed())
    before = run_async(_read_invocation(invocation_id))
    store_bytes = temp_db_path.read_bytes()
    _observe_command_groups(monkeypatch, live=(5001,))
    monkeypatch.setattr(db_module.StateDB, "_apply_schema", _unexpected)
    monkeypatch.setattr(db_module.StateDB, "_tx", _unexpected)
    monkeypatch.setattr(cli_kill, "kill_one", _unexpected)

    assert main(["kill", invocation_id[:12], "--dry-run", "--grace", "2.5"]) == status

    output = capsys.readouterr().out
    assert f"(dry-run) invocation {invocation_id}" in output
    assert text in output
    if status == 0 and "would send" in text:
        assert "pid=5001, pgid=5001" in output
    assert run_async(_read_invocation(invocation_id)) == before
    assert temp_db_path.read_bytes() == store_bytes


def test_preview_resolves_ambiguity_and_uses_the_recursive_child_order(
    temp_db_path, monkeypatch, capsys
):
    async def seed():
        async with db_module.StateDB() as db:
            first = await _invocation(db, _command_identity(5001), name="abc-first")
            second = await _invocation(db, {}, name="abc-second")
            child = await _seed_session(db)
            await db.execute("UPDATE sessions SET invocation_id = ? WHERE id = ?", (first, child))
            await db.execute("UPDATE invocations SET status = 'completed' WHERE id = ?", (second,))
            return first, second, child

    first, second, child = run_async(seed())
    _observe_command_groups(monkeypatch, live=(5001,))
    monkeypatch.setattr(db_module.StateDB, "_apply_schema", _unexpected)
    monkeypatch.setattr(db_module.StateDB, "_tx", _unexpected)
    monkeypatch.setattr(cli_kill, "kill_one", _unexpected)

    assert main(["kill", "abc", "--dry-run", "--recursive"]) == 1
    assert "ambiguous" in capsys.readouterr().err.lower()
    assert main(["kill", second, "--dry-run"]) == 1
    assert "terminal state" in capsys.readouterr().err
    assert main(["kill", first, "--dry-run", "--recursive"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"session {child}: no signal (signal=no_pid)" in lines[0]
    assert f"invocation {first} (pid=5001, pgid=5001): would send SIGTERM" in lines[1]


def test_degraded_preview_never_creates_the_store_or_locks_the_manifest(
    temp_db_path, tmp_path, monkeypatch, capsys
):
    root = tmp_path / "runs"
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", root)
    _observe_command_groups(monkeypatch)
    manifest = _write_run(
        root,
        {
            **_command_identity(5001),
            "status": "running",
            PERSISTENCE_DEGRADED_REASON_FIELD: "store unavailable",
        },
    )
    expected = manifest.read_bytes()
    process = MagicMock()
    process.status.return_value = psutil.STATUS_SLEEPING
    process.cmdline.return_value = ["li", "agent"]
    _script_create_time(process, 5001.0)
    monkeypatch.setattr(_proc, "_read_raw_create_time", _proc._select_raw_create_time_reader())
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(db_module.StateDB, "_open", _unexpected)
    monkeypatch.setattr(orchestration_kill, "_settle_degraded_run", _unexpected)

    assert main(["kill", RUN_ID, "--dry-run"]) == 0
    assert f"run {RUN_ID} (pid=5001): would send SIGTERM" in capsys.readouterr().out
    assert not temp_db_path.exists()
    assert manifest.read_bytes() == expected
    assert list(manifest.parent.iterdir()) == [manifest]


def test_preview_reports_a_terminal_sessions_recorded_worker_without_settling_it(
    temp_db_path, monkeypatch, capsys
):
    session, generation, progression, spawn = (str(uuid.uuid4()) for _ in range(4))

    async def seed():
        async with db_module.StateDB() as db:
            await db.create_progression(progression)
            await db.create_session(
                {
                    "id": session,
                    "progression_id": progression,
                    "cli_worker_generation": generation,
                    "invocation_kind": "agent",
                    "status": "running",
                }
            )
            identity = _command_identity(5001)
            identity.pop("action_kind")
            await db.register_cli_worker_spawn(
                {"spawn_id": spawn, "session_id": session, "generation_id": generation, **identity}
            )
            await db.update_status(
                "session",
                session,
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary="runner failed before worker ended",
            )

    async def read():
        async with db_module.StateDB(readonly=True) as db:
            return await db.snapshot_cli_worker_spawns(session)

    run_async(seed())
    before = run_async(read())
    _observe_command_groups(monkeypatch, live=(5001,))
    monkeypatch.setattr(db_module.StateDB, "_apply_schema", _unexpected)
    monkeypatch.setattr(db_module.StateDB, "_tx", _unexpected)
    monkeypatch.setattr(db_module.StateDB, "guard_cli_worker_signal", _unexpected)

    assert main(["kill", session, "--dry-run"]) == 0
    assert (
        f"session {session} worker {spawn} (pid=5001, pgid=5001): would send SIGTERM"
        in capsys.readouterr().out
    )
    assert run_async(read()) == before


async def test_stale_sweep_settles_pidless_commands_and_verified_empty_groups(
    temp_db_path, monkeypatch, capsys
):
    async with db_module.StateDB() as db:
        live = await _invocation(db, _command_identity(5001))
        empty = await _invocation(db, _command_identity(5003))
        historical = await _invocation(db, {"action_kind": "command"})
        ordinary = await _seed_invocation(db, started_at=time.time() - 7_200)
    before = {row: await _read_invocation(row) for row in (live, empty, historical, ordinary)}
    _observe_command_groups(monkeypatch, live=(5001,), empty=(5003,))

    with monkeypatch.context() as dry_run:
        dry_run.setattr(cli_kill, "_persist_cancel", _unexpected)
        assert (
            await cli_kill._do_kill_all_stale(threshold_seconds=1, dry_run=True, verbose=True) == 0
        )
    output = capsys.readouterr().out
    assert "command group 5001 is still alive" in output
    assert f"would cancel invocation {historical[:12]} (pid=None," in output
    assert f"would cancel invocation {empty[:12]}" in output
    assert {row: await _read_invocation(row) for row in before} == before

    assert await cli_kill._do_kill_all_stale(threshold_seconds=1) == 0
    assert (await _read_invocation(live))[0]["status"] == "running"
    for invocation_id in (empty, historical, ordinary):
        row, transitions = await _read_invocation(invocation_id)
        assert row["status"] == "cancelled"
        assert transitions[-1]["reason_code"] == RunReasons.CANCELLED_STALE_AUTO


async def test_stale_sweep_cancels_a_previous_boot_command_group_without_signalling(
    temp_db_path, monkeypatch, capsys
):
    earlier = {**_command_identity(5001), "pid_boot_time": 10.0, "pid_create_time": 60.0}
    async with db_module.StateDB() as db:
        invocation_id = await _invocation(db, earlier)
        pidless = await _invocation(db, {"action_kind": "command", "pid_boot_time": 10.0})
        remote = await _invocation(db, {**earlier, "pid_host": "other-host"})
    before = await _read_invocation(invocation_id)
    retained = {row: await _read_invocation(row) for row in (pidless, remote)}
    _observe_command_groups(monkeypatch)

    with monkeypatch.context() as dry_run:
        dry_run.setattr(cli_kill, "_persist_cancel", _unexpected)
        assert await cli_kill._do_kill_all_stale(threshold_seconds=1, dry_run=True) == 0
    assert f"would cancel invocation {invocation_id[:12]}" in capsys.readouterr().out
    assert await _read_invocation(invocation_id) == before

    assert await cli_kill._do_kill_all_stale(threshold_seconds=1) == 0
    row, transitions = await _read_invocation(invocation_id)
    assert row["status"] == "cancelled"
    assert len(transitions) == len(before[1]) + 1
    transition = transitions[-1]
    assert transition["reason_code"] == RunReasons.CANCELLED_STALE_AUTO
    assert transition["reason_summary"] == "Stale auto-cancel: command group predates this boot."
    assert {row: await _read_invocation(row) for row in retained} == retained


async def test_stale_sweep_records_only_locally_bound_unverifiable_command_groups(
    temp_db_path, monkeypatch, capsys
):
    async with db_module.StateDB() as db:
        occupied = await _invocation(db, _command_identity(5002))
        controls = [
            await _invocation(db, {**_command_identity(5001), **override})
            for override in (
                {"pgid": None},
                {"pid_create_time": "5001"},
                {"pid_host": ""},
                {"pid_host": "other-host"},
                {"pid_boot_time": None},
                {"pid_boot_time": 190.0},
                {"process_identity_mode": "external"},
                {"pid_create_time": 5000.0},
            )
        ]
    _observe_command_groups(monkeypatch, live=(5001,), occupied=(5002,))
    before = {row: await _read_invocation(row) for row in (occupied, *controls)}

    with monkeypatch.context() as dry_run:
        dry_run.setattr(db_module.StateDB, "observe_unverifiable_pid", _unexpected)
        dry_run.setattr(db_module.StateDB, "clear_unverifiable_pid", _unexpected)
        assert await cli_kill._do_kill_all_stale(threshold_seconds=1, dry_run=True) == 0
    assert {row: await _read_invocation(row) for row in before} == before

    for count in range(1, 4):
        assert await cli_kill._do_kill_all_stale(threshold_seconds=1) == 0
        row, transitions = await _read_invocation(occupied)
        assert row["status"] == "running"
        assert row["node_metadata"]["unverifiable_count"] == count
        assert row["node_metadata"]["unverifiable_quarantined"] is (count == 3)
        assert transitions == before[occupied][1]
    assert f"quarantined identity-unverified rows (1): {occupied}" in capsys.readouterr().out
    assert {row: await _read_invocation(row) for row in controls} == {
        row: before[row] for row in controls
    }

    _observe_command_groups(monkeypatch, live=(5001, 5002))
    assert await cli_kill._do_kill_all_stale(threshold_seconds=1) == 0
    row, transitions = await _read_invocation(occupied)
    assert "unverifiable_count" not in row["node_metadata"]
    assert row["status"] == "running"
    assert transitions == before[occupied][1]


@pytest.mark.parametrize("group_live", [False, True], ids=["unverifiable", "verified"])
async def test_resolve_command_quarantine_rechecks_the_group(temp_db_path, monkeypatch, group_live):
    async with db_module.StateDB() as db:
        invocation_id = await _invocation(db, _command_identity(5002))
    _observe_command_groups(monkeypatch, occupied=(5002,))
    for _ in range(3):
        assert await cli_kill._do_kill_all_stale(threshold_seconds=1) == 0
    before, transitions = await _read_invocation(invocation_id)
    assert before["node_metadata"]["unverifiable_quarantined"] is True

    if group_live:
        _observe_command_groups(monkeypatch, live=(5002,))
    result = await cli_kill._resolve_quarantine(
        invocation_id, target="failed", reason="inspected manually"
    )
    if group_live:
        assert result == 1
        assert await _read_invocation(invocation_id) == (before, transitions)
    else:
        assert result == 0
        after, updated_transitions = await _read_invocation(invocation_id)
        assert after["status"] == "failed"
        assert len(updated_transitions) == len(transitions) + 1


@pytest.mark.parametrize(
    "metadata",
    [{"action_kind": "command"}, {**_command_identity(5003), "pid_boot_time": 10.0}],
    ids=["pidless", "previous-boot"],
)
async def test_stale_snapshot_cannot_cancel_a_command_group_published_during_the_sweep(
    temp_db_path, monkeypatch, metadata
):
    async with db_module.StateDB() as db:
        invocation_id = await _invocation(db, metadata)
    _, original_transitions = await _read_invocation(invocation_id)

    real_persist = cli_kill._persist_cancel
    attempted = False

    async def publish_first(db, entity_type, entity_id, **kwargs):
        nonlocal attempted
        attempted = True
        assert entity_id == invocation_id
        assert await db.merge_invocation_node_metadata_if_running(
            entity_id, _command_identity(5001)
        )
        return await real_persist(db, entity_type, entity_id, **kwargs)

    _observe_command_groups(monkeypatch, live=(5001,))
    monkeypatch.setattr(cli_kill, "_persist_cancel", publish_first)

    assert await cli_kill._do_kill_all_stale(threshold_seconds=1) == 0
    assert attempted
    row, transitions = await _read_invocation(invocation_id)
    assert row["status"] == "running"
    assert row["node_metadata"] == _command_identity(5001)
    assert transitions == original_transitions


def test_postgres_preview_reads_a_transaction_without_native_schema_setup(monkeypatch, capsys):
    invocation_id = "pg-invocation"
    history = []
    settings = db_module.settings.model_copy(
        update={"LIONAGI_STATE_DB_URL": "postgresql://unit-test.invalid/state"}
    )
    monkeypatch.setattr(db_module, "settings", settings)
    monkeypatch.setattr(db_module.StateDB, "_apply_schema", _unexpected)
    _observe_command_groups(monkeypatch)

    class Connection:
        async def execution_options(self, **options):
            history.append(options)
            return self

        @asynccontextmanager
        async def begin(self):
            history.append("begin")
            yield

        async def exec_driver_sql(self, sql):
            history.append(sql)

        async def execute(self, statement, values):
            statement = str(statement)
            history.append(statement)
            assert history[-2] == "SET TRANSACTION READ ONLY"
            assert values == {"p0": invocation_id}
            assert statement.startswith("SELECT * FROM ")
            row = (
                {"id": invocation_id, "status": "running", "node_metadata": {}}
                if "FROM invocations" in statement
                else None
            )
            result = MagicMock()
            result.mappings.return_value.first.return_value = row
            return result

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield Connection()

        async def dispose(self):
            history.append("dispose")

    def make_engine(url):
        assert url == "postgresql+asyncpg://unit-test.invalid/state"
        return Engine()

    monkeypatch.setattr(engine, "make_engine", make_engine)
    assert main(["kill", invocation_id, "--dry-run"]) == 0
    assert f"invocation {invocation_id}: no signal (signal=no_pid)" in capsys.readouterr().out
    assert history[:3] == [
        {"isolation_level": "REPEATABLE READ"},
        "begin",
        "SET TRANSACTION READ ONLY",
    ]
    assert history[-1] == "dispose"
