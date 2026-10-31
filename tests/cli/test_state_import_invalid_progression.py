# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Invalid imported message IDs must not leave a partially imported run."""

import json
import os
import re
import uuid
from pathlib import Path

import pytest

from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB


def _branch(path: Path, branch_id: str, messages: list[dict], *, order=(), stamp=1):
    path.write_text(
        json.dumps(
            {
                "id": branch_id,
                "messages": {"collections": messages, "progression": {"order": list(order)}},
            }
        )
    )
    os.utime(path, (1_000_000 + stamp, 1_000_000 + stamp))


def _message(message_id, text):
    return {"id": message_id, "created_at": 1.0, "role": "user", "content": {"text": text}}


async def _snapshot(db):
    result = {}
    for table in ("sessions", "progressions", "messages", "branches"):
        result[table] = await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", ["", None, 42], ids=["empty", "null", "integer"])
@pytest.mark.parametrize("ordered", [False, True], ids=["all-selected", "selected-by-order"])
async def test_invalid_later_branch_leaves_no_partial_import_and_can_be_retried(
    tmp_path, monkeypatch, invalid_id, ordered
):
    path = tmp_path / "state.db"
    runs = tmp_path / "runs"
    run_dir = runs / "run-1"
    branches = run_dir / "branches"
    branches.mkdir(parents=True)
    manifest = {"run_id": "run-1", "kind": "agent", "status": "completed"}
    (run_dir / "run.json").write_text(json.dumps(manifest))
    _branch(branches / "first.json", "branch-1", [_message("shared", "replacement")], stamp=1)
    _branch(
        branches / "second.json",
        "branch-2",
        [_message(invalid_id, "bad")],
        order=[invalid_id] if ordered else (),
        stamp=2,
    )
    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )

    async with StateDB(path) as db:
        # The first branch would upsert a message belonging to another session.
        keeper, progression = str(uuid.uuid4()), str(uuid.uuid4())
        await db.create_progression(progression)
        await db.create_session({"id": keeper, "progression_id": progression, "status": "running"})
        await db.insert_message(
            {"id": "shared", "created_at": 1.0, "role": "user", "content": {"text": "original"}}
        )
        await db.append_to_progression(progression, "shared")
        before = await _snapshot(db)
        with pytest.raises(ValueError, match="non-empty string") as caught:
            await state_mod._import_one_run(db, "run-1", run_dir, manifest)
        assert "second.json" in str(caught.value)
        assert re.search(r"\b0\b", str(caught.value))
        assert await _snapshot(db) == before

    # Correct both errors; a shared message must keep the keeper's content.
    # No partially committed session from the failed try may suppress this import.
    _branch(branches / "first.json", "branch-1", [_message("shared", "original")], stamp=1)
    _branch(branches / "second.json", "branch-2", [_message("second-msg", "fixed")], stamp=2)
    assert await state_mod._import_runs() == {
        "sessions": 1,
        "branches": 2,
        "messages": 2,
        "skipped": 0,
        "errors": 0,
    }
    async with StateDB(path) as db:
        assert await db.get_session("run-1") is not None
        assert (
            await db.fetch_one(
                "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?", ("run-1",)
            )
        )["n"] == 2
        assert (await db.get_session(keeper))["progression_id"] == progression
        assert await db.get_progression(progression) == ["shared"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", ["", None, 42], ids=["empty", "null", "integer"])
async def test_ordered_invalid_entry_is_excluded_from_import(tmp_path, invalid_id):
    run_dir = tmp_path / "run-2"
    branches = run_dir / "branches"
    branches.mkdir(parents=True)
    _branch(branches / "first.json", "branch-1", [_message("valid-1", "ok")], stamp=1)
    _branch(
        branches / "second.json",
        "branch-2",
        [_message("valid-2", "included"), _message(invalid_id, "excluded")],
        order=["valid-2"],
        stamp=2,
    )
    async with StateDB(tmp_path / "state.db") as db:
        assert await state_mod._import_one_run(db, "run-2", run_dir, {"kind": "agent"}) == (1, 2, 2)
        messages = await db.fetch_all("SELECT id FROM messages ORDER BY id")
        assert [row["id"] for row in messages] == ["valid-1", "valid-2"]
        progressions = await db.fetch_all("SELECT id FROM progressions ORDER BY id")
        collections = [await db.get_progression(row["id"]) for row in progressions]
        assert sorted(collections) == [["valid-1"], ["valid-1", "valid-2"], ["valid-2"]]
        assert all(invalid_id not in collection for collection in collections)


@pytest.mark.asyncio
async def test_malformed_branch_still_warns_and_skips_while_valid_branches_import(tmp_path, capsys):
    run_dir = tmp_path / "run-3"
    branches = run_dir / "branches"
    branches.mkdir(parents=True)
    _branch(branches / "first.json", "branch-1", [_message("valid-1", "ok")], stamp=1)
    _branch(branches / "second.json", "branch-2", [_message("valid-2", "ok")], stamp=2)
    malformed = branches / "third.json"
    malformed.write_text("{not json")
    os.utime(malformed, (1_000_003, 1_000_003))
    async with StateDB(tmp_path / "state.db") as db:
        assert await state_mod._import_one_run(db, "run-3", run_dir, {"kind": "agent"}) == (1, 2, 2)
        assert (await db.fetch_one("SELECT COUNT(*) AS n FROM messages"))["n"] == 2
        assert (await db.fetch_one("SELECT COUNT(*) AS n FROM branches"))["n"] == 2
    assert "third.json: failed to read" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_import_uses_checked_snapshot_if_branch_changes_before_first_write(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run-4"
    branches = run_dir / "branches"
    branches.mkdir(parents=True)
    _branch(branches / "first.json", "branch-1", [_message("valid-1", "ok")], stamp=1)
    second = branches / "second.json"
    _branch(second, "branch-2", [_message("checked-id", "checked")], stamp=2)
    async with StateDB(tmp_path / "state.db") as db:
        original = db._create_progression_in_tx
        changed = False

        async def replace_file_at_first_write(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                _branch(second, "branch-2", [_message("replacement-id", "replacement")], stamp=2)
            return await original(*args, **kwargs)

        monkeypatch.setattr(db, "_create_progression_in_tx", replace_file_at_first_write)
        assert await state_mod._import_one_run(db, "run-4", run_dir, {"kind": "agent"}) == (1, 2, 2)
        assert changed is True
        assert changed
        rows = await db.fetch_all("SELECT id FROM messages ORDER BY id")
        assert [row["id"] for row in rows] == ["checked-id", "valid-1"]
