# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `lionagi.cli._orphan`: pid-liveness classification, terminal
sweep with CAS guards, and recovery-capability projection."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from lionagi.cli._orphan import (
    ORPHAN_PID_CREATE_TIME_TOLERANCE,
    extract_pid_identity,
    recovery_capability,
    session_process_liveness,
    sweep_orphaned_sessions,
)
from lionagi.state.db import StateDB, TransitionRejectedError
from lionagi.state.reasons import RunReasons

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_session(
    db: StateDB,
    *,
    status: str = "running",
    node_metadata: dict | None = None,
) -> dict:
    sid = str(uuid.uuid4())
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "status": status,
            "started_at": time.time(),
            "node_metadata": node_metadata or {},
        }
    )
    return await db.get_session(sid)


# ── extract_pid_identity ───────────────────────────────────────────────────


def test_extract_pid_identity_from_dict():
    pid, ct = extract_pid_identity({"pid": 123, "pid_create_time": 456.0})
    assert pid == 123
    assert ct == 456.0


def test_extract_pid_identity_from_json_string():
    pid, ct = extract_pid_identity(json.dumps({"pid": 7, "pid_create_time": 8.5}))
    assert pid == 7
    assert ct == 8.5


def test_extract_pid_identity_missing_keys():
    assert extract_pid_identity({}) == (None, None)


def test_extract_pid_identity_none():
    assert extract_pid_identity(None) == (None, None)


def test_extract_pid_identity_malformed_json_string():
    assert extract_pid_identity("not json") == (None, None)


def test_extract_pid_identity_non_numeric_pid():
    pid, ct = extract_pid_identity({"pid": "not-a-number"})
    assert pid is None
    assert ct is None


# ── session_process_liveness ────────────────────────────────────────────────


def test_liveness_none_when_no_pid_recorded():
    assert session_process_liveness({}) is None
    assert session_process_liveness(None) is None


def test_liveness_none_for_pid_zero_or_negative():
    assert session_process_liveness({"pid": 0}) is None
    assert session_process_liveness({"pid": -5}) is None


def test_liveness_none_for_pid_one():
    # Never dereference pid 1 (init) even with a real liveness probe.
    assert session_process_liveness({"pid": 1}) is None


def test_liveness_false_for_dead_pid():
    # A pid virtually guaranteed not to exist.
    assert session_process_liveness({"pid": 999999999}) is False


def test_liveness_true_for_own_process():
    own_pid = os.getpid()
    own_create_time = psutil.Process(own_pid).create_time()
    assert session_process_liveness({"pid": own_pid, "pid_create_time": own_create_time}) is True


def test_liveness_true_for_own_process_no_create_time_recorded():
    assert session_process_liveness({"pid": os.getpid()}) is True


def test_liveness_false_when_create_time_mismatches_recycled_pid():
    own_pid = os.getpid()
    wrong_create_time = psutil.Process(own_pid).create_time() - (
        ORPHAN_PID_CREATE_TIME_TOLERANCE * 10
    )
    assert session_process_liveness({"pid": own_pid, "pid_create_time": wrong_create_time}) is False


def test_liveness_false_for_zombie_process():
    fake_proc = MagicMock()
    fake_proc.status.return_value = psutil.STATUS_ZOMBIE
    with (
        patch("lionagi.cli._orphan._pid_alive", return_value=True),
        patch("psutil.Process", return_value=fake_proc),
    ):
        assert session_process_liveness({"pid": 42}) is False


def test_liveness_true_when_psutil_process_lookup_raises_unexpectedly():
    # pid_alive() already confirmed liveness; an unrelated psutil failure on
    # the follow-up status/create-time check must not flip the verdict.
    with (
        patch("lionagi.cli._orphan._pid_alive", return_value=True),
        patch("psutil.Process", side_effect=RuntimeError("boom")),
    ):
        assert session_process_liveness({"pid": 42}) is True


# ── recovery_capability ──────────────────────────────────────────────────────


async def test_recovery_capability_checkpoint_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    async with StateDB() as db:
        sess = await _seed_session(db, node_metadata={"run_id": "run-abc"})

        run_dir = runs_root / "run-abc"
        run_dir.mkdir(parents=True)
        (run_dir / "checkpoint.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "session_id": sess["id"],
                    "prompt": "p",
                    "plan": [],
                    "flow_context": {},
                    "ops": {},
                    "spawned": [],
                    "config": {},
                }
            )
        )

        capability, resume_command = await recovery_capability(sess["id"])
        assert capability == "checkpoint_resume"
        assert resume_command == f"li o flow --resume {sess['id']}"


async def test_recovery_capability_rerun_only_without_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    monkeypatch.setattr(ckmod, "RUNS_ROOT", tmp_path / "runs")

    async with StateDB() as db:
        sess = await _seed_session(db, node_metadata={"pid": os.getpid()})
        capability, resume_command = await recovery_capability(sess["id"])
        assert capability == "rerun_only"
        assert resume_command is None


# ── sweep_orphaned_sessions ───────────────────────────────────────────────────


async def test_sweep_leaves_live_session_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    monkeypatch.setattr(ckmod, "RUNS_ROOT", tmp_path / "runs")

    async with StateDB() as db:
        sess = await _seed_session(
            db,
            node_metadata={
                "pid": os.getpid(),
                "pid_create_time": psutil.Process(os.getpid()).create_time(),
            },
        )
        counts = await sweep_orphaned_sessions(db)
        assert counts["orphaned"] == 0
        assert counts["skipped_alive"] == 1

        row = await db.get_session(sess["id"])
        assert row["status"] == "running"


async def test_sweep_leaves_unknown_liveness_session_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    monkeypatch.setattr(ckmod, "RUNS_ROOT", tmp_path / "runs")

    async with StateDB() as db:
        sess = await _seed_session(db, node_metadata={})
        counts = await sweep_orphaned_sessions(db)
        assert counts["orphaned"] == 0
        assert counts["skipped_unknown"] == 1

        row = await db.get_session(sess["id"])
        assert row["status"] == "running"


async def test_sweep_terminalizes_confirmed_dead_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    monkeypatch.setattr(ckmod, "RUNS_ROOT", tmp_path / "runs")

    async with StateDB() as db:
        sess = await _seed_session(db, node_metadata={"pid": 999999999})
        counts = await sweep_orphaned_sessions(db)
        assert counts["scanned"] == 1
        assert counts["orphaned"] == 1

        row = await db.get_session(sess["id"])
        assert row["status"] == "failed"
        assert row["status_reason_code"] == RunReasons.FAILED_ORPHANED_PARENT

        evidence = row["status_evidence_refs"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        assert evidence[0]["kind"] == "orphan_evidence"
        assert evidence[0]["pid"] == 999999999
        assert evidence[0]["recovery_capability"] == "rerun_only"


async def test_sweep_is_idempotent_on_second_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    monkeypatch.setattr(ckmod, "RUNS_ROOT", tmp_path / "runs")

    async with StateDB() as db:
        await _seed_session(db, node_metadata={"pid": 999999999})
        first = await sweep_orphaned_sessions(db)
        assert first["orphaned"] == 1

        second = await sweep_orphaned_sessions(db)
        # Already-terminalized; nothing left in 'running' to scan.
        assert second["scanned"] == 0
        assert second["orphaned"] == 0


async def test_sweep_skips_when_cas_loses_the_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    """A session that legitimately finishes between the sweep's read and its
    guarded write must win the race — the sweep's update_status() call
    returns False (CAS lost) and the row is left untouched, not overwritten."""
    import lionagi.cli.orchestrate._checkpoint as ckmod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(ckmod, "RUNS_ROOT", tmp_path / "runs")

    async with StateDB() as db:
        sess = await _seed_session(db, node_metadata={"pid": 999999999})

        real_update_status = db.update_status

        async def _racing_update_status(*args, **kwargs):
            # Simulate the row finishing on its own right before the
            # guarded write: flip it to 'completed' out of band first, so
            # the sweep's own CAS-guarded call (still holding the stale
            # updated_at) loses.
            async with db._tx() as conn:
                from sqlalchemy import text

                await conn.execute(
                    text(
                        "UPDATE sessions SET status = 'completed', updated_at = :now WHERE id = :id"
                    ),
                    {"now": time.time(), "id": sess["id"]},
                )
            return await real_update_status(*args, **kwargs)

        with patch.object(db, "update_status", side_effect=_racing_update_status):
            counts = await sweep_orphaned_sessions(db)

        assert counts["orphaned"] == 0
        assert counts["skipped_race"] == 1

        row = await db.get_session(sess["id"])
        # The out-of-band write, not the sweep, decided the final status.
        assert row["status"] == "completed"


async def test_sweep_handles_transition_rejected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    """A row that went terminal-and-locked between the read and the sweep's
    write raises TransitionRejectedError inside update_status(); the sweep
    must swallow it as a lost race, not propagate."""
    import lionagi.cli.orchestrate._checkpoint as ckmod

    monkeypatch.setattr(ckmod, "RUNS_ROOT", tmp_path / "runs")

    async with StateDB() as db:
        await _seed_session(db, node_metadata={"pid": 999999999})

        async def _raise(*args, **kwargs):
            raise TransitionRejectedError("session", "x", "failed", "failed")

        with patch.object(db, "update_status", side_effect=_raise):
            counts = await sweep_orphaned_sessions(db)

        assert counts["orphaned"] == 0
        assert counts["skipped_race"] == 1
