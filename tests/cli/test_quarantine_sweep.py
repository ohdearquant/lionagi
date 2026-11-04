"""End-to-end sweep coverage for the PID quarantine disposition."""

from __future__ import annotations

import re
import time
import uuid

import pytest
from sqlalchemy import text

from lionagi.cli import kill as kill_cli
from lionagi.state.db import StateDB

GENERATION = "11111111-1111-4111-8111-111111111111"
NEXT_GENERATION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(kill_cli, "_pid_alive", lambda _pid: True)


async def _seed(kind="session", *, metadata=None, extras=None):
    entity_id = str(uuid.uuid4())
    meta = {"pid": 54321, "pid_create_time": 100.0} if metadata is None else metadata
    async with StateDB() as db:
        if kind == "session":
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": entity_id,
                    "progression_id": progression_id,
                    "cli_worker_generation": GENERATION,
                    "status": "running",
                    "started_at": time.time() - 7200,
                    "node_metadata": meta,
                    **(extras or {}),
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


async def _row(kind, entity_id):
    async with StateDB() as db:
        return await (
            db.get_session(entity_id) if kind == "session" else db.get_invocation(entity_id)
        )


def _probe(monkeypatch, verdicts):
    iterator = iter(verdicts)

    def identity(_pid, _name, **kwargs):
        result = next(iterator)
        if result == "unverifiable" and kwargs.get("_reason_out") is not None:
            kwargs["_reason_out"].append("permission_denied")
        return result

    monkeypatch.setattr(kill_cli, "check_pid_identity", identity)


async def _sweep(monkeypatch, verdicts, *, dry_run=False):
    _probe(monkeypatch, verdicts)
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600, dry_run=dry_run) == 0


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_third_observation_quarantines_and_reports_id(kind, monkeypatch, capsys):
    entity_id = await _seed(kind)
    first_since = None
    for expected_count in (1, 2, 3):
        await _sweep(monkeypatch, ["unverifiable"])
        row = await _row(kind, entity_id)
        meta = row["node_metadata"]
        assert row["status"] == "running"
        assert meta["unverifiable_count"] == expected_count
        assert meta["unverifiable_quarantined"] is (expected_count == 3)
        first_since = first_since or meta["unverifiable_since"]
        assert meta["unverifiable_since"] == first_since
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if entity_id in line)
    assert "quarantin" in line.lower()
    assert re.search(r"\b1\b", line.partition(entity_id)[0])


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_verified_live_closes_epoch_and_later_unknown_starts_again(kind, monkeypatch):
    entity_id = await _seed(kind)
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    old = (await _row(kind, entity_id))["node_metadata"]
    assert old["unverifiable_quarantined"] is True
    await _sweep(monkeypatch, ["ours"])
    live = (await _row(kind, entity_id))["node_metadata"]
    assert live.get("unverifiable_quarantined") is not True
    await _sweep(monkeypatch, ["unverifiable"])
    new = (await _row(kind, entity_id))["node_metadata"]
    assert new["unverifiable_count"] == 1
    assert new["unverifiable_quarantined"] is False
    assert new["unverifiable_epoch"] != old["unverifiable_epoch"]


@pytest.mark.parametrize("changed", ["generation", "pid", "create_time"])
async def test_identity_change_starts_new_epoch(changed, monkeypatch):
    entity_id = await _seed()
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    async with StateDB() as db:
        if changed == "generation":
            async with db._tx() as conn:
                await conn.execute(
                    text("UPDATE sessions SET cli_worker_generation = :gen WHERE id = :id"),
                    {"gen": NEXT_GENERATION, "id": entity_id},
                )
        else:
            key, value = ("pid", 54322) if changed == "pid" else ("pid_create_time", 101.0)
            await db.merge_session_node_metadata(entity_id, {key: value})
    await _sweep(monkeypatch, ["unverifiable"])
    meta = (await _row("session", entity_id))["node_metadata"]
    assert meta["unverifiable_count"] == 1
    assert meta["unverifiable_quarantined"] is False


@pytest.mark.parametrize("verdicts", [["ours", "unverifiable"], ["unverifiable", "ours"]])
async def test_live_result_wins_for_same_identity_in_either_order(verdicts, monkeypatch):
    first, second = await _seed(), await _seed()
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable", "unverifiable"])
    await _sweep(monkeypatch, verdicts)
    for entity_id in (first, second):
        row = await _row("session", entity_id)
        assert row["status"] == "running"
        assert row["node_metadata"].get("unverifiable_quarantined") is not True
        assert "unverifiable_count" not in row["node_metadata"]


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_dry_run_does_not_change_existing_quarantine(kind, monkeypatch):
    entity_id = await _seed(kind)
    for _ in range(3):
        await _sweep(monkeypatch, ["unverifiable"])
    before = await _row(kind, entity_id)
    await _sweep(monkeypatch, ["unverifiable"], dry_run=True)
    after = await _row(kind, entity_id)
    assert after["status"] == before["status"]
    assert after["node_metadata"] == before["node_metadata"]
    assert after["updated_at"] == before["updated_at"]


@pytest.mark.parametrize(
    "excluded",
    [
        {"pid_host": "definitely-not-the-local-host.invalid"},
        {"process_identity_mode": "in_process"},
    ],
)
async def test_foreign_and_unmanaged_processes_remain_out_of_scope(excluded, monkeypatch):
    entity_id = await _seed(metadata={"pid": 54321, **excluded})
    before = await _row("session", entity_id)
    await _sweep(monkeypatch, [])
    after = await _row("session", entity_id)
    assert after["status"] == "running"
    assert after["node_metadata"] == before["node_metadata"]
    assert after["updated_at"] == before["updated_at"]


@pytest.mark.parametrize("fallback", ["pid_file", "run_json"])
async def test_unbound_fallback_identity_warns_without_quarantine_or_signal(
    fallback, monkeypatch, tmp_path
):
    from unittest.mock import Mock

    if fallback == "pid_file":
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        (artifact_dir / ".pid").write_text("54321", encoding="utf-8")
        extras = {"artifacts_path": str(artifact_dir)}
    else:
        extras = {"source_kind": "imported_fs"}
        monkeypatch.setattr(
            kill_cli,
            "_imported_degraded_process_identity",
            lambda _entity_id: ("unverifiable", "permission_denied"),
        )
    entity_id = await _seed(metadata={}, extras=extras)
    warnings = []
    monkeypatch.setattr(kill_cli, "warn", warnings.append)
    os_kill = Mock(side_effect=AssertionError("unexpected signal"))
    monkeypatch.setattr("lionagi.cli._kill_service.os.kill", os_kill)
    await _sweep(monkeypatch, ["unverifiable"] if fallback == "pid_file" else [])
    row = await _row("session", entity_id)
    assert row["status"] == "running"
    assert row["node_metadata"].get("unverifiable_quarantined") is not True
    assert any("process identity not bound in the row" in warning for warning in warnings)
    os_kill.assert_not_called()
