"""End-to-end sweep coverage for the PID quarantine disposition."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB
from lionhub.serve.cli import kill as kill_cli

from .quarantine_helpers import (  # noqa: F401
    NEXT_GENERATION,
    fetch_row,
    isolated_store,
    seed,
    sweep,
)


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_third_observation_quarantines_and_reports_id(kind, monkeypatch, capsys):
    entity_id = await seed(kind)
    first_since = None
    for expected_count in (1, 2, 3):
        await sweep(monkeypatch, ["unverifiable"])
        row = await fetch_row(kind, entity_id)
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
    entity_id = await seed(kind)
    for _ in range(3):
        await sweep(monkeypatch, ["unverifiable"])
    old = (await fetch_row(kind, entity_id))["node_metadata"]
    assert old["unverifiable_quarantined"] is True
    await sweep(monkeypatch, ["ours"])
    live = (await fetch_row(kind, entity_id))["node_metadata"]
    assert live.get("unverifiable_quarantined") is not True
    await sweep(monkeypatch, ["unverifiable"])
    new = (await fetch_row(kind, entity_id))["node_metadata"]
    assert new["unverifiable_count"] == 1
    assert new["unverifiable_quarantined"] is False
    assert new["unverifiable_epoch"] != old["unverifiable_epoch"]


@pytest.mark.parametrize("changed", ["generation", "pid", "create_time"])
async def test_identity_change_starts_new_epoch(changed, monkeypatch):
    entity_id = await seed()
    for _ in range(3):
        await sweep(monkeypatch, ["unverifiable"])
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
    await sweep(monkeypatch, ["unverifiable"])
    meta = (await fetch_row("session", entity_id))["node_metadata"]
    assert meta["unverifiable_count"] == 1
    assert meta["unverifiable_quarantined"] is False


@pytest.mark.parametrize("verdicts", [["ours", "unverifiable"], ["unverifiable", "ours"]])
async def test_live_result_wins_for_same_identity_in_either_order(verdicts, monkeypatch):
    first, second = await seed(), await seed()
    for _ in range(3):
        await sweep(monkeypatch, ["unverifiable", "unverifiable"])
    await sweep(monkeypatch, verdicts)
    for entity_id in (first, second):
        row = await fetch_row("session", entity_id)
        assert row["status"] == "running"
        assert row["node_metadata"].get("unverifiable_quarantined") is not True
        assert "unverifiable_count" not in row["node_metadata"]


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_dry_run_does_not_change_existing_quarantine(kind, monkeypatch):
    entity_id = await seed(kind)
    for _ in range(3):
        await sweep(monkeypatch, ["unverifiable"])
    before = await fetch_row(kind, entity_id)
    await sweep(monkeypatch, ["unverifiable"], dry_run=True)
    after = await fetch_row(kind, entity_id)
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
    entity_id = await seed(metadata={"pid": 54321, **excluded})
    before = await fetch_row("session", entity_id)
    await sweep(monkeypatch, [])
    after = await fetch_row("session", entity_id)
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
    entity_id = await seed(metadata={}, extras=extras)
    warnings = []
    monkeypatch.setattr(kill_cli, "warn", warnings.append)
    os_kill = Mock(side_effect=AssertionError("unexpected signal"))
    monkeypatch.setattr("lionhub.orchestration.kill.os.kill", os_kill)
    await sweep(monkeypatch, ["unverifiable"] if fallback == "pid_file" else [])
    row = await fetch_row("session", entity_id)
    assert row["status"] == "running"
    assert row["node_metadata"].get("unverifiable_quarantined") is not True
    assert any("process identity not bound in the row" in warning for warning in warnings)
    os_kill.assert_not_called()
