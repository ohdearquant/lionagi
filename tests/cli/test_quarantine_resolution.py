"""A quarantined PID can be administratively resolved without signalling it."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from lionagi.cli import kill as kill_cli
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

_GENERATION_ONE = "11111111-1111-4111-8111-111111111111"
_GENERATION_TWO = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")


def _parser():
    parser = argparse.ArgumentParser()
    kill_cli.add_kill_subparser(parser.add_subparsers(dest="command"))
    return parser


def _args(entity_id, target, reason="operator investigated"):
    return _parser().parse_args(
        ["kill", entity_id, "--resolve-quarantine", target, "--reason", reason]
    )


async def _seed(kind):
    entity_id = str(uuid.uuid4())
    meta = {"pid": 54321, "pid_create_time": 100.0}
    async with StateDB() as db:
        if kind == "session":
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": entity_id,
                    "progression_id": progression_id,
                    "status": "running",
                    "started_at": time.time() - 7200,
                    "node_metadata": meta,
                    "cli_worker_generation": _GENERATION_ONE,
                }
            )
        else:
            await db.create_invocation(
                {
                    "id": entity_id,
                    "skill": "test",
                    "status": "running",
                    "started_at": time.time() - 7200,
                    "node_metadata": meta,
                }
            )
    return entity_id


async def _quarantine(monkeypatch):
    monkeypatch.setattr(kill_cli, "_pid_alive", lambda _pid: True)

    def unverifiable(_pid, _name, **kwargs):
        if kwargs.get("_reason_out") is not None:
            kwargs["_reason_out"].append("permission_denied")
        return "unverifiable"

    monkeypatch.setattr(kill_cli, "check_pid_identity", unverifiable)
    for _ in range(3):
        assert await kill_cli._do_kill_all_stale(threshold_seconds=3600) == 0


def _assert_no_signals(monkeypatch):
    # A successful disposition must not enter ordinary kill/cleanup paths.
    terminate = Mock(side_effect=AssertionError("unexpected signal path"))
    cleanup = Mock(side_effect=AssertionError("unexpected worker cleanup"))
    os_kill = Mock(side_effect=AssertionError("unexpected os.kill"))
    monkeypatch.setattr("lionagi.cli._kill_service._terminate_pid", terminate)
    monkeypatch.setattr("lionagi.cli._kill_service.os.kill", os_kill)
    monkeypatch.setattr(kill_cli, "_try_recorded_worker_cleanup", cleanup)
    return terminate, cleanup, os_kill


async def _row_and_history(kind, entity_id):
    async with StateDB() as db:
        row = await (
            db.get_session(entity_id) if kind == "session" else db.get_invocation(entity_id)
        )
        history = await db.fetch_all(
            "SELECT status, reason_summary, evidence_refs FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running'",
            (entity_id,),
        )
    return row, history


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("target", ["failed", "aborted", "cancelled"])
async def test_quarantined_row_can_be_resolved_without_a_signal(kind, target, monkeypatch):
    entity_id = await _seed(kind)
    await _quarantine(monkeypatch)
    terminate, cleanup, os_kill = _assert_no_signals(monkeypatch)

    assert await asyncio.to_thread(kill_cli.run_kill, _args(entity_id, target)) == 0
    row, history = await _row_and_history(kind, entity_id)
    assert row["status"] == target
    assert len(history) == 1
    assert history[0]["status"] == target
    assert "identity unverified, operator disposition" in history[0]["reason_summary"]
    assert "operator investigated" in history[0]["reason_summary"]
    evidence = history[0]["evidence_refs"]
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    assert "operator investigated" in json.dumps(evidence)
    terminate.assert_not_called()
    cleanup.assert_not_called()
    os_kill.assert_not_called()


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_resolution_refuses_missing_quarantine_or_blank_reason(kind, monkeypatch):
    entity_id = await _seed(kind)
    terminate, cleanup, os_kill = _assert_no_signals(monkeypatch)
    assert await asyncio.to_thread(kill_cli.run_kill, _args(entity_id, "failed")) != 0
    await _quarantine(monkeypatch)
    assert await asyncio.to_thread(kill_cli.run_kill, _args(entity_id, "failed", "  ")) != 0
    row, history = await _row_and_history(kind, entity_id)
    assert row["status"] == "running"
    assert history == []
    terminate.assert_not_called()
    cleanup.assert_not_called()
    os_kill.assert_not_called()


def test_resolution_parser_rejects_non_admin_target():
    with pytest.raises(SystemExit):
        _args("sid", "completed")


async def test_resolution_refuses_if_generation_changes_after_lookup(monkeypatch):
    entity_id = await _seed("session")
    await _quarantine(monkeypatch)
    terminate, cleanup, os_kill = _assert_no_signals(monkeypatch)
    real_resolve = kill_cli._resolve_entity

    async def changed_after_lookup(db, identifier):
        result = await real_resolve(db, identifier)
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE sessions SET cli_worker_generation = :gen WHERE id = :id"),
                {"gen": _GENERATION_TWO, "id": entity_id},
            )
        return result

    monkeypatch.setattr(kill_cli, "_resolve_entity", changed_after_lookup)
    assert await asyncio.to_thread(kill_cli.run_kill, _args(entity_id, "cancelled")) != 0
    row, history = await _row_and_history("session", entity_id)
    assert row["status"] == "running"
    assert row["cli_worker_generation"] == _GENERATION_TWO
    assert history == []
    terminate.assert_not_called()
    cleanup.assert_not_called()
    os_kill.assert_not_called()


async def test_resolution_refuses_when_completion_wins_after_lookup(monkeypatch):
    entity_id = await _seed("session")
    await _quarantine(monkeypatch)
    terminate, cleanup, os_kill = _assert_no_signals(monkeypatch)
    real_resolve = kill_cli._resolve_entity

    async def completed_after_lookup(db, identifier):
        result = await real_resolve(db, identifier)
        await db.update_status(
            "session", entity_id, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        return result

    monkeypatch.setattr(kill_cli, "_resolve_entity", completed_after_lookup)
    assert await asyncio.to_thread(kill_cli.run_kill, _args(entity_id, "failed")) != 0
    row, history = await _row_and_history("session", entity_id)
    assert row["status"] == "completed"
    assert len(history) == 1 and history[0]["status"] == "completed"
    terminate.assert_not_called()
    cleanup.assert_not_called()
    os_kill.assert_not_called()


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_resolution_refuses_foreign_host(kind, monkeypatch):
    entity_id = await _seed(kind)
    await _quarantine(monkeypatch)
    async with StateDB() as db:
        merge = (
            db.merge_session_node_metadata
            if kind == "session"
            else db.merge_invocation_node_metadata
        )
        await merge(entity_id, {"pid_host": "definitely-not-the-local-host.invalid"})
    terminate, cleanup, os_kill = _assert_no_signals(monkeypatch)
    assert await asyncio.to_thread(kill_cli.run_kill, _args(entity_id, "failed")) != 0
    row, history = await _row_and_history(kind, entity_id)
    assert row["status"] == "running"
    assert history == []
    terminate.assert_not_called()
    cleanup.assert_not_called()
    os_kill.assert_not_called()


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("verdict", ["ours", "not_ours", "zombie", "dead", "probe_error"])
async def test_resolution_refuses_when_identity_can_be_verified_or_probe_fails(
    kind, verdict, monkeypatch, caplog
):
    entity_id = await _seed(kind)
    await _quarantine(monkeypatch)
    terminate, cleanup, os_kill = _assert_no_signals(monkeypatch)
    before, history_before = await _row_and_history(kind, entity_id)
    if verdict == "dead":
        monkeypatch.setattr(kill_cli, "_pid_alive", lambda _pid: False)
    elif verdict == "probe_error":

        def fail_probe(*_args, **_kwargs):
            raise RuntimeError("probe unavailable")

        monkeypatch.setattr(kill_cli, "check_pid_identity", fail_probe)
    else:
        monkeypatch.setattr(kill_cli, "check_pid_identity", lambda *_a, **_kw: verdict)

    assert await asyncio.to_thread(kill_cli.run_kill, _args(entity_id, "failed")) != 0
    after, history_after = await _row_and_history(kind, entity_id)
    assert after == before
    assert history_after == history_before == []
    assert "normal li kill" in caplog.text
    terminate.assert_not_called()
    cleanup.assert_not_called()
    os_kill.assert_not_called()


@pytest.mark.parametrize("verdict", ["ours", "not_ours", "zombie", "unverifiable"])
def test_resolution_boot_disagreement_still_checks_process_identity(verdict, monkeypatch):
    row = {"id": "session-1", "node_metadata": {"pid": 54321, "pid_create_time": 100.0}}
    monkeypatch.setattr(kill_cli, "unaddressable_pid_reason", lambda _meta: "boot_unverifiable")
    monkeypatch.setattr(
        kill_cli, "_pid_addressability", lambda _reason, _pid: ("boot_unverifiable", True)
    )
    probe = Mock(return_value=verdict)
    monkeypatch.setattr(kill_cli, "check_pid_identity", probe)
    assert kill_cli._quarantine_still_unverifiable(row, kind="session") is (
        verdict == "unverifiable"
    )
    probe.assert_called_once()
