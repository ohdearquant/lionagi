# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reclaiming a message body has to stay distinguishable from never having one.

The prune selects sessions. The bytes live on messages, and a message any
surviving progression still names is kept whatever its age, so a store can be
almost entirely message content, have every message inside the keep-window, and
give a prune nothing to delete. This command selects on the axis the bytes are
actually on.

The condition that shapes it: an emptied body is indistinguishable from a turn
that genuinely produced nothing, and nothing downstream has a second source for
that distinction, so the two would collapse into one state permanently. A
reclaimed body therefore carries a marker, and the arms that matter most here
assert the difference is visible AT THE CONSUMER rather than at the writer --
the writer knowing what it wrote is not the property anyone depends on.

No LLM and no network: these run against a temp SQLite file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lionagi.cli.state import (
    _null_content,
    _null_content_candidates,
    _null_content_targets,
)
from lionagi.state.content_pruned import (
    CONTENT_PRUNED_KEY,
    content_was_pruned,
)
from lionagi.state.db import StateDB

DAY = 86400.0


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp file DB: patches DEFAULT_DB_PATH so StateDB() opens it."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_message(
    db: StateDB,
    *,
    mid: str,
    age_days: float,
    role: str = "assistant",
    content: dict | None = None,
) -> str:
    await db.insert_message(
        {
            "id": mid,
            "created_at": time.time() - age_days * DAY,
            "node_metadata": {"lion_class": "AssistantResponse"},
            "content": {"assistant_response": "x" * 500} if content is None else content,
            "embedding": None,
            "sender": "s",
            "recipient": "r",
            "channel": None,
            "role": role,
        }
    )
    return mid


async def _raw_content(db: StateDB, mid: str) -> str | None:
    """The column as SQLite holds it, before any reader hydrates it."""
    from sqlalchemy import text

    async with db._read() as conn:
        row = (
            (await conn.execute(text("SELECT content FROM messages WHERE id = :id"), {"id": mid}))
            .mappings()
            .first()
        )
    return None if row is None else row["content"]


class TestAReclaimedBodyIsDistinguishableFromAnEmptyOne:
    """The condition the whole design turns on, asserted where it has to hold."""

    async def test_a_reclaimed_body_reads_as_reclaimed_through_get_message(self, temp_db_path):
        """The consumer is StateDB.get_message, not the command that wrote it.

        A writer can always tell what it just wrote. The property anyone
        downstream depends on is that a reader arriving later, with no memory of
        the operation, can still tell.
        """
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            got = await db.get_message("old")

        assert got is not None
        assert content_was_pruned(got["content"]) is True

    async def test_a_body_that_was_genuinely_empty_does_not_read_as_reclaimed(self, temp_db_path):
        """The must-NOT-match half. Without this, a predicate that answered True
        for everything would satisfy the arm above and destroy the distinction
        it exists to make."""
        async with StateDB() as db:
            await _seed_message(db, mid="empty", age_days=40, content={})
            got = await db.get_message("empty")

        assert got is not None
        assert content_was_pruned(got["content"]) is False

    async def test_a_body_holding_an_empty_string_does_not_read_as_reclaimed(self, temp_db_path):
        """The shape a turn that produced nothing actually writes -- a real
        content key with nothing in it, which is the case the marker exists to
        stay separable from."""
        async with StateDB() as db:
            await _seed_message(db, mid="blank", age_days=40, content={"assistant_response": ""})
            got = await db.get_message("blank")

        assert got is not None
        assert content_was_pruned(got["content"]) is False

    async def test_the_marker_is_never_null_and_never_an_empty_string(self, temp_db_path):
        """Named separately from the predicate because a marker could satisfy
        content_was_pruned and still be written as something a different reader
        -- one that never imports this module -- reads as absence."""
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            raw = await _raw_content(db, "old")
            got = await db.get_message("old")

        assert raw is not None
        assert raw != ""
        assert got["content"] is not None
        assert got["content"] != {}
        assert got["content"] != ""

    async def test_the_predicate_answers_the_same_raw_and_hydrated(self, temp_db_path):
        """Consumers reach this column by two routes -- the raw JSON text and an
        already-parsed dict -- and a predicate serving only one of them returns
        the wrong answer for the other, which is the same failure as having no
        marker at all."""
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            raw = await _raw_content(db, "old")
            hydrated = (await db.get_message("old"))["content"]

        assert isinstance(raw, str)
        assert isinstance(hydrated, dict)
        assert content_was_pruned(raw) == content_was_pruned(hydrated) is True

    def test_a_body_merely_mentioning_the_key_is_not_reclaimed(self):
        """The raw path short-circuits on a substring before parsing, so a body
        whose TEXT contains the key has to be judged on its structure anyway --
        otherwise quoting the marker's name in a message would forge one."""
        forged = json.dumps({"assistant_response": f"we set {CONTENT_PRUNED_KEY} here"})

        assert content_was_pruned(forged) is False

    async def test_the_marker_records_the_size_of_the_body_that_row_held(self, temp_db_path):
        """original_bytes is per-row, and this is the arm that keeps it honest.

        The marker is written by a SQL expression so LENGTH(content) is
        evaluated against each row as it is overwritten. Computing one size in
        Python and writing the same marker everywhere would record a batch
        average under a per-row name -- a number that is not wrong so much as
        answering a different question than its label, which is the failure the
        marker exists to prevent rather than commit.

        The fixture is two bodies of deliberately different sizes, because a
        batch average and a per-row size are the SAME NUMBER for any fixture
        whose rows are equal, and such a fixture cannot tell the two apart.
        """
        async with StateDB() as db:
            await _seed_message(
                db, mid="small", age_days=40, content={"assistant_response": "x" * 10}
            )
            await _seed_message(
                db, mid="big", age_days=40, content={"assistant_response": "x" * 5000}
            )
            small_before = len(await _raw_content(db, "small"))
            big_before = len(await _raw_content(db, "big"))

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            small = (await db.get_message("small"))["content"][CONTENT_PRUNED_KEY]
            big = (await db.get_message("big"))["content"][CONTENT_PRUNED_KEY]

        assert small["original_bytes"] == small_before
        assert big["original_bytes"] == big_before
        # The average of the two would be identical for both rows.
        assert small["original_bytes"] != big["original_bytes"]

    async def test_the_marker_carries_when_it_happened(self, temp_db_path):
        before = time.time()
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            marker = (await db.get_message("old"))["content"][CONTENT_PRUNED_KEY]

        assert before <= marker["at"] <= time.time()

    async def test_the_stored_marker_has_exactly_the_documented_shape(self, temp_db_path):
        """The marker is built in SQL, so nothing in Python states its shape.

        This arm is that statement, written out independently: a field added or
        renamed on the SQL side without the readers being told turns this red
        instead of silently changing a format that is permanent once rows carry
        it.
        """
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            content = (await db.get_message("old"))["content"]

        assert set(content) == {CONTENT_PRUNED_KEY}
        assert set(content[CONTENT_PRUNED_KEY]) == {"at", "original_bytes"}


class TestTheReclaimSelectsOnTheAxisTheBytesLiveOn:
    async def test_a_body_inside_the_window_is_untouched(self, temp_db_path):
        async with StateDB() as db:
            await _seed_message(db, mid="recent", age_days=5)

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            got = await db.get_message("recent")

        assert content_was_pruned(got["content"]) is False
        assert got["content"]["assistant_response"] == "x" * 500

    async def test_the_row_and_everything_but_the_body_survives(self, temp_db_path):
        """The point of reclaiming rather than deleting: the prune removes the
        row and every reference to it, this keeps them. A message still named by
        a progression stays nameable."""
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40, role="assistant")
            before = await db.get_message("old")

        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            after = await db.get_message("old")

        assert after is not None
        assert after["id"] == before["id"]
        assert after["role"] == before["role"]
        assert after["created_at"] == before["created_at"]
        assert after["sender"] == before["sender"]

    async def test_a_role_filter_reclaims_only_that_role(self, temp_db_path):
        async with StateDB() as db:
            await _seed_message(db, mid="act", age_days=40, role="action")
            await _seed_message(db, mid="asst", age_days=40, role="assistant")

        result = await _null_content(older_than_days=30, roles=("action",), dry_run=False)

        async with StateDB() as db:
            act = await db.get_message("act")
            asst = await db.get_message("asst")

        assert result["messages"] == 1
        assert content_was_pruned(act["content"]) is True
        assert content_was_pruned(asst["content"]) is False


class TestTheReclaimCannotReportAnOutcomeNothingContradicts:
    async def test_a_real_run_leaves_nothing_selected(self, temp_db_path):
        async with StateDB() as db:
            for i in range(3):
                await _seed_message(db, mid=f"old{i}", age_days=40)

        result = await _null_content(older_than_days=30, roles=(), dry_run=False)
        check = await _null_content_candidates(older_than_days=30, roles=())

        assert result["messages"] == 3
        assert check["candidates"] == 0

    async def test_a_preview_leaves_exactly_what_it_reported(self, temp_db_path):
        """A preview performs the update and rolls it back, so the rows have to
        still be there and still be selected afterwards. A preview that silently
        committed would pass every other arm in this file."""
        async with StateDB() as db:
            for i in range(3):
                await _seed_message(db, mid=f"old{i}", age_days=40)

        preview = await _null_content(older_than_days=30, roles=(), dry_run=True)
        check = await _null_content_candidates(older_than_days=30, roles=())

        assert preview["messages"] == 3
        assert check["candidates"] == 3

        async with StateDB() as db:
            got = await db.get_message("old0")
        assert content_was_pruned(got["content"]) is False

    async def test_preview_and_recount_agree_where_the_role_filter_binds(self, temp_db_path):
        """The drift arm has to exercise the half of the predicate the others
        cannot. Every arm above passes roles=(), where the role clause is absent
        from the SQL entirely -- so a recount that dropped the role filter would
        agree with the operation throughout and the pair would certify nothing.

        This fixture is built so the two answers differ if the clause is lost:
        four rows past the window, only one of them the requested role.
        """
        async with StateDB() as db:
            await _seed_message(db, mid="act", age_days=40, role="action")
            for i in range(3):
                await _seed_message(db, mid=f"asst{i}", age_days=40, role="assistant")

        preview = await _null_content(older_than_days=30, roles=("action",), dry_run=True)
        check = await _null_content_candidates(older_than_days=30, roles=("action",))

        assert preview["messages"] == 1
        assert check["candidates"] == 1

    async def test_a_second_run_reclaims_nothing(self, temp_db_path):
        """Already-reclaimed rows are excluded by the predicate, so a re-run is a
        no-op rather than a marker rewrite reporting work it did not do -- which
        would also reset every recorded original size to the marker's own."""
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)

        first = await _null_content(older_than_days=30, roles=(), dry_run=False)
        second = await _null_content(older_than_days=30, roles=(), dry_run=False)

        assert first["messages"] == 1
        assert second["messages"] == 0

        async with StateDB() as db:
            got = await db.get_message("old")
        assert got["content"][CONTENT_PRUNED_KEY]["original_bytes"] > 0

    async def test_a_row_with_no_body_at_all_could_not_be_marked_as_having_had_one(
        self, temp_db_path
    ):
        """A NULL body satisfies ``json_extract(...) IS NULL`` the same way an
        unreclaimed body does, so without a clause of its own it would be handed
        a marker asserting a body was there -- fabricating the one fact the
        marker exists to preserve.

        Two things stand between the store and that state, and this arm is here
        because they are different kinds of thing. The predicate carries
        ``content IS NOT NULL``, which is cheap and correct whatever the schema
        does. The schema carries ``NOT NULL`` on the column, which is what makes
        the state unreachable TODAY -- so the arm asserts the constraint rather
        than pretending to construct a row it cannot construct. If that
        constraint is ever relaxed this turns red at the same moment the
        predicate's clause becomes the only thing left.
        """
        from sqlalchemy.exc import IntegrityError

        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)
            # Positive control: the write path reaches the row at all, so a
            # rejection below is the constraint and not a statement that missed.
            await db.execute("UPDATE messages SET role = 'action' WHERE id = 'old'")
            assert (await db.get_message("old"))["role"] == "action"

            with pytest.raises(IntegrityError, match="NOT NULL"):
                await db.execute("UPDATE messages SET content = NULL WHERE id = 'old'")

        assert "content IS NOT NULL" in _null_content_targets(())
        assert "content IS NOT NULL" in _null_content_targets(("action",))

    def test_both_readings_are_built_from_one_predicate(self):
        """The operation and the recount call the same builder. Two copies of
        this drifting apart would make the pair agree while counting different
        populations, which is worse than printing no check."""
        assert "role IN" in _null_content_targets(("action",))
        assert "role IN" not in _null_content_targets(())
        assert _null_content_targets(("a", "b")).count(":role") == 2


class TestTheSizesReported:
    async def test_the_before_size_is_the_bodies_it_reached(self, temp_db_path):
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=40)
            raw = await _raw_content(db, "old")
        original = len(raw)

        result = await _null_content(older_than_days=30, roles=(), dry_run=False)

        assert result["bytes_before"] == original
        assert result["bytes_after"] < result["bytes_before"]

    async def test_the_after_size_excludes_markers_an_earlier_run_left(self, temp_db_path):
        """Sizes are measured over the batch's own rows. Summing 'rows carrying a
        marker' instead would fold in every previous run's markers and inflate
        the after-size of every reclaim after the first."""
        async with StateDB() as db:
            await _seed_message(db, mid="first", age_days=40)
        await _null_content(older_than_days=30, roles=(), dry_run=False)

        async with StateDB() as db:
            await _seed_message(db, mid="second", age_days=40)
        second = await _null_content(older_than_days=30, roles=(), dry_run=False)

        assert second["messages"] == 1
        # One row's marker, not two. A sum over all marked rows would roughly
        # double this and the arm would be the only thing that noticed.
        async with StateDB() as db:
            one_marker = len(await _raw_content(db, "second"))
        assert second["bytes_after"] == one_marker

    async def test_an_empty_selection_reports_no_oldest_age_rather_than_zero(self, temp_db_path):
        """No rows and rows written this instant are different states, and only
        one of them means the window has nothing to reach. Reporting 0.0 for the
        first makes it read as the second."""
        async with StateDB() as db:
            await _seed_message(db, mid="recent", age_days=1)

        check = await _null_content_candidates(older_than_days=30, roles=())

        assert check["candidates"] == 0
        assert check["oldest_age_days"] is None

    async def test_a_non_empty_selection_reports_the_age_of_its_oldest(self, temp_db_path):
        """The companion. Without it, a function hardcoded to return None would
        satisfy the arm above."""
        async with StateDB() as db:
            await _seed_message(db, mid="old", age_days=45)

        check = await _null_content_candidates(older_than_days=30, roles=())

        assert check["candidates"] == 1
        assert check["oldest_age_days"] == pytest.approx(45.0, abs=0.1)
