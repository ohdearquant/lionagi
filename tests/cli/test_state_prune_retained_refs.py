# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pruning keeps sessions referenced by retained records and reports why."""

from __future__ import annotations

import argparse
import re
import time
import uuid
from pathlib import Path

import anyio
import pytest

from lionagi.cli.state import _prune, add_state_subparser, run_state
from lionagi.state.db import StateDB

TABLES = ("artifacts", "plays", "team_messages", "dispatch_outbox", "approvals")


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.delenv("LIONAGI_STATE_DB_URL", raising=False)
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


async def _session(db: StateDB, *, age_days: int) -> str:
    sid, progression, branch_progression, branch = (str(uuid.uuid4()) for _ in range(4))
    await db.create_progression(progression)
    await db.create_progression(branch_progression)
    await db.create_session({"id": sid, "progression_id": progression, "status": "completed"})
    await db.create_branch({"id": branch, "session_id": sid, "progression_id": branch_progression})
    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time() - age_days * 86400, sid)
    )
    return sid


async def _seed() -> tuple[dict[str, str], dict[str, list[dict]]]:
    async with StateDB() as db:
        ids = {name: await _session(db, age_days=60) for name in (*TABLES, "multi", "free")}
        ids["recent"] = await _session(db, age_days=1)
        now = time.time()
        await db.execute(
            "INSERT INTO shows (id, topic, show_dir, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("show", "retention", "/tmp/show", now, now),
        )
        await db.execute(
            "INSERT INTO teams (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("team", "retention", now, now),
        )

        # Each table names its own old session and the same multi-referenced
        # session. Two artifacts point at one session to distinguish rows from
        # distinct sessions; a recent artifact is outside the prune candidates.
        for index, sid in enumerate(
            (ids["artifacts"], ids["artifacts"], ids["multi"], ids["recent"])
        ):
            await db.execute(
                "INSERT INTO artifacts (id, session_id, created_at, updated_at, kind, name, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"artifact-{index}", sid, now, now, "test", f"artifact-{index}", "{}"),
            )
        for index, sid in enumerate((ids["plays"], ids["multi"])):
            await db.execute(
                "INSERT INTO plays (id, show_id, name, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"play-{index}", "show", f"play-{index}", sid, now, now),
            )
        for index, sid in enumerate((ids["team_messages"], ids["multi"])):
            await db.execute(
                "INSERT INTO team_messages (id, team_id, created_at, sender, content, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"team-message-{index}", "team", now, "sender", "message", sid),
            )
        for index, sid in enumerate((ids["dispatch_outbox"], ids["multi"])):
            await db.execute(
                "INSERT INTO dispatch_outbox (id, kind, deliver_to, payload, next_attempt_at, "
                "session_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"outbox-{index}", "test", "recipient", "{}", now, sid, now),
            )
        for index, sid in enumerate((ids["approvals"], ids["multi"])):
            await db.execute(
                "INSERT INTO approvals (id, action_kind, params_hash, session_id, proposed_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"approval-{index}", "test", "hash", sid, now, now + 60),
            )
        rows = {
            table: [dict(row) for row in await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")]  # noqa: S608
            for table in TABLES
        }
    return ids, rows


async def _check_rows(ids: dict[str, str], before: dict[str, list[dict]], *, dry_run: bool) -> None:
    async with StateDB() as db:
        for table in TABLES:
            after = [dict(row) for row in await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")]  # noqa: S608
            assert after == before[table], table
        for name, sid in ids.items():
            assert (await db.get_session(sid) is not None) == (dry_run or name != "free"), name
            branch = await db.fetch_one("SELECT id FROM branches WHERE session_id = ?", (sid,))
            assert (branch is not None) == (dry_run or name != "free"), name


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "delete"])
async def test_prune_skips_referenced_sessions_and_counts_distinct_sessions(
    isolated_store: Path, dry_run: bool
) -> None:
    ids, before = await _seed()
    result = await _prune(keep_days=30, keep_n=0, dry_run=dry_run)
    assert result["sessions"] == 1
    assert result["branches"] == 1
    assert result["skipped_sessions"] == 6
    assert result["skipped_by_reference"] == dict.fromkeys(TABLES, 2)
    await _check_rows(ids, before, dry_run=dry_run)


async def test_unreferenced_prune_preserves_existing_result_shape(isolated_store: Path) -> None:
    async with StateDB() as db:
        await _session(db, age_days=60)
    result = await _prune(keep_days=30, keep_n=0, dry_run=False)
    assert result == {"sessions": 1, "branches": 1, "messages": 0}


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "delete"])
async def test_all_candidates_referenced_are_reported_without_deleting(
    isolated_store: Path, dry_run: bool
) -> None:
    ids, before = await _seed()
    async with StateDB() as db:
        # The only unreferenced old session is no longer a candidate.
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), ids["free"])
        )
    result = await _prune(keep_days=30, keep_n=0, dry_run=dry_run)
    assert result["sessions"] == 0
    assert result["branches"] == 0
    assert result["messages"] == 0
    assert result["skipped_sessions"] == 6
    assert result["skipped_by_reference"] == dict.fromkeys(TABLES, 2)
    await _check_rows(ids, before, dry_run=True)


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "delete"])
async def test_prune_keeps_permanent_tables_named_like_its_scratch_tables(
    isolated_store: Path, dry_run: bool
) -> None:
    async with StateDB() as db:
        await _session(db, age_days=60)
        for table in ("prune_orphan_progressions", "prune_direct_messages", "prune_victims"):
            await db.execute(f"CREATE TABLE main.{table} (id TEXT PRIMARY KEY)")  # noqa: S608
            await db.execute(f"INSERT INTO main.{table} (id) VALUES (?)", ("sentinel",))  # noqa: S608

    result = await _prune(keep_days=30, keep_n=0, dry_run=dry_run)
    assert result["sessions"] == 1
    async with StateDB() as db:
        for table in ("prune_orphan_progressions", "prune_direct_messages", "prune_victims"):
            rows = await db.fetch_all(f"SELECT id FROM main.{table}")  # noqa: S608
            assert [row["id"] for row in rows] == ["sentinel"], table


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "delete"])
def test_cli_reports_total_and_each_reference_table_and_succeeds(
    isolated_store: Path, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    ids, before = anyio.run(_seed)
    parser = argparse.ArgumentParser()
    add_state_subparser(parser.add_subparsers(dest="command"))
    argv = ["state", "prune", "--keep-days", "30", "--keep-n", "0"]
    if dry_run:
        argv.append("--dry-run")
    assert run_state(parser.parse_args(argv)) == 0
    output = capsys.readouterr().out
    assert re.search(r"skipp\w*\s+6\s+session", output, re.IGNORECASE)
    for table in TABLES:
        assert re.search(rf"{table}\s*[:=]\s*2\b", output), output

    async def check() -> None:
        await _check_rows(ids, before, dry_run=dry_run)

    anyio.run(check)
