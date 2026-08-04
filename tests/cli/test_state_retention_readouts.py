# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The two readouts a retention decision is made from, and the pair that contradicts a prune.

The failure these close: a store whose oldest row is newer than the prune's
keep-window frees nothing at any window the prune accepts, deletes zero, and
says so in the words of a success. Nothing in the old output could tell that
apart from a store that had already been pruned clean.

No LLM and no network: every arm seeds a temp SQLite file and reads it back.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.state import (
    _collect_message_breakdown,
    _print_stats,
    _prune,
    _prune_candidates,
)
from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_message(db: StateDB, *, role: str, age_days: float, text: str = "x") -> str:
    """One message of a given role, aged by writing created_at directly."""
    mid = str(uuid.uuid4())
    await db.insert_message(
        {
            "id": mid,
            "created_at": time.time() - age_days * 86400,
            "node_metadata": {},
            "content": {"text": text},
            "role": role,
            "sender": "u",
            "recipient": "x",
            "channel": "test",
        }
    )
    return mid


async def _seed_session_aged(db: StateDB, *, age_days: float) -> str:
    sid = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    await db.create_progression(pid)
    await db.create_session(
        {"id": sid, "progression_id": pid, "status": "completed", "started_at": time.time()}
    )
    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (time.time() - age_days * 86400, sid),
    )
    return sid


class TestTheBreakdownSeparatesRoles:
    async def test_each_role_is_counted_on_its_own(self, temp_db_path: Path):
        async with StateDB() as db:
            for _ in range(4):
                await _seed_message(db, role="action", age_days=0)
            await _seed_message(db, role="user", age_days=0)

            got = await _collect_message_breakdown(db)

        counts = {e["role"]: e["count"] for e in got["messages_by_role"]}
        assert counts == {"action": 4, "user": 1}

    async def test_the_byte_sum_is_withheld_unless_asked_for(self, temp_db_path: Path):
        """The companion arm. Every assertion that bytes ARRIVE would pass just as
        well with the scan running unconditionally, and that is the cost this
        option exists to avoid paying on every invocation."""
        async with StateDB() as db:
            await _seed_message(db, role="action", age_days=0, text="some content")

            without = await _collect_message_breakdown(db)
            with_bytes = await _collect_message_breakdown(db, content_bytes=True)

        assert "content_bytes" not in without["messages_by_role"][0]
        assert with_bytes["messages_by_role"][0]["content_bytes"] > 0


class TestTheAgeHistogramIsWhatMakesAKeepWindowCheckable:
    async def test_a_store_entirely_inside_the_window_reports_zero_in_every_bucket(
        self, temp_db_path: Path
    ):
        """The exact shape that made the wrong-axis default invisible: rows exist,
        the store is large, and no keep-window the prune accepts reaches any of
        them."""
        async with StateDB() as db:
            for _ in range(5):
                await _seed_message(db, role="action", age_days=1)

            got = await _collect_message_breakdown(db)

        assert all(e["count"] == 0 for e in got["messages_by_age"])
        assert got["oldest_message_age_days"] == pytest.approx(1.0, abs=0.1)

    async def test_an_older_store_populates_the_buckets_it_passes(self, temp_db_path: Path):
        """Must-match beside the must-not-match above: the same instrument has to
        report a non-zero somewhere, or the zeros prove nothing."""
        async with StateDB() as db:
            await _seed_message(db, role="action", age_days=40)

            got = await _collect_message_breakdown(db)

        buckets = {e["older_than_days"]: e["count"] for e in got["messages_by_age"]}
        assert buckets[7] == 1
        assert buckets[30] == 1
        assert buckets[90] == 0

    async def test_no_messages_is_not_an_age_of_zero(self, temp_db_path: Path):
        """An empty store and a store written this instant are different states, and
        only one of them means a prune has nothing to reach. Collapsing them is
        the missing-subject-reads-as-empty-subject failure."""
        async with StateDB() as db:
            got = await _collect_message_breakdown(db)

        assert got["oldest_message_age_days"] is None
        assert got["messages_by_role"] == []

    async def test_the_printout_says_so_rather_than_printing_a_number(
        self, temp_db_path: Path, capsys: pytest.CaptureFixture
    ):
        async with StateDB() as db:
            await _seed_session_aged(db, age_days=0)

        await _print_stats()
        out = capsys.readouterr().out
        assert "(no messages)" in out
        assert "Messages by age:" in out


class TestThePruneCannotReportAnOutcomeNothingContradicts:
    async def test_after_a_real_prune_nothing_its_predicate_selects_remains(
        self, temp_db_path: Path
    ):
        async with StateDB() as db:
            await _seed_session_aged(db, age_days=60)
            await _seed_session_aged(db, age_days=60)

        result = await _prune(keep_days=30, keep_n=0, dry_run=False)
        check = await _prune_candidates(keep_days=30, keep_n=0)

        assert result["sessions"] == 2
        assert check["candidates"] == 0

    async def test_a_preview_leaves_exactly_what_it_said_it_would_delete(self, temp_db_path: Path):
        """The pair's other reading, and the one that proves the two SQL uses have
        not drifted: a preview rolls back, so the recount must return the same
        number the preview reported, computed by a separate query outside the
        preview's transaction."""
        async with StateDB() as db:
            for _ in range(3):
                await _seed_session_aged(db, age_days=60)

        preview = await _prune(keep_days=30, keep_n=0, dry_run=True)
        check = await _prune_candidates(keep_days=30, keep_n=0)

        assert preview["sessions"] == 3
        assert check["candidates"] == 3

    async def test_the_recount_honours_keep_n_and_not_only_the_age(self, temp_db_path: Path):
        """keep_n is half the predicate. A recount that read age alone would agree
        with the prune on every age-only fixture and disagree here."""
        async with StateDB() as db:
            for _ in range(4):
                await _seed_session_aged(db, age_days=60)

        check = await _prune_candidates(keep_days=30, keep_n=3)

        assert check["candidates"] == 1

    async def test_preview_and_recount_agree_on_a_fixture_where_keep_n_binds(
        self, temp_db_path: Path
    ):
        """The drift check has to exercise BOTH halves of the predicate. Every
        other arm here passes keep_n=0, where the clause cannot change an answer,
        so a copy that dropped keep_n would agree with the prune throughout and
        the pair would read as confirming what it never tested."""
        async with StateDB() as db:
            for _ in range(5):
                await _seed_session_aged(db, age_days=60)

        preview = await _prune(keep_days=30, keep_n=2, dry_run=True)
        check = await _prune_candidates(keep_days=30, keep_n=2)

        assert preview["sessions"] == 3
        assert check["candidates"] == 3

    async def test_the_oldest_session_age_is_reported_beside_the_count(self, temp_db_path: Path):
        async with StateDB() as db:
            await _seed_session_aged(db, age_days=12)

        check = await _prune_candidates(keep_days=30, keep_n=0)

        assert check["candidates"] == 0
        assert check["oldest_session_age_days"] == pytest.approx(12.0, abs=0.1)

    async def test_no_sessions_reports_no_age_rather_than_zero(self, temp_db_path: Path):
        async with StateDB() as db:
            await db.execute("SELECT 1")

        check = await _prune_candidates(keep_days=30, keep_n=0)

        assert check["candidates"] == 0
        assert check["oldest_session_age_days"] is None
