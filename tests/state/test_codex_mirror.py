# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the codex rollout mirror: provenance, record accounting, turn attribution."""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.state.codex_mirror import (
    ID_FIELD,
    SOURCE_KIND,
    RecordTally,
    _det,
    mirror_session,
    session_db_id,
    turn_context,
)
from lionagi.state.db import StateDB


def _rec(rtype: str, payload: dict, ts: str = "2026-07-29T12:00:00Z") -> dict:
    return {"type": rtype, "timestamp": ts, "payload": payload}


def _turn(model: str, effort: str) -> dict:
    return _rec("turn_context", {"model": model, "effort": effort, "cwd": "/x"})


def _user(text: str, pid: str) -> dict:
    return _rec(
        "response_item",
        {"type": "message", "role": "user", "id": pid, "content": [{"text": text}]},
    )


def _assistant(text: str, pid: str) -> dict:
    return _rec(
        "response_item",
        {"type": "message", "role": "assistant", "id": pid, "content": [{"text": text}]},
    )


ROLLOUT_UID = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _records() -> list[dict]:
    """A rollout that switches model mid-thread, and carries records of four types."""
    return [
        _rec("session_meta", {"id": ROLLOUT_UID, "cwd": "/x"}),
        _turn("gpt-5.6-terra", "high"),
        _user("first question", "m1"),
        _assistant("first answer", "m2"),
        _turn("gpt-5.6-sol", "xhigh"),  # the switch
        _user("second question", "m3"),
        _assistant("second answer", "m4"),
        _rec("world_state", {"anything": 1}),
        # developer turns are instruction plumbing: seen, never mirrored
        _rec("response_item", {"type": "message", "role": "developer", "content": [{"text": "d"}]}),
    ]


@pytest.fixture
async def db(tmp_path: Path):
    state = StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    async with state:
        yield state


async def _mirror(db, records, *, turn=None, unparseable=0, source_path="/tmp/rollout-x.jsonl"):
    return await mirror_session(
        db,
        rollout_uid=ROLLOUT_UID,
        records=records,
        tool_names={},
        turn=turn if turn is not None else {},
        unparseable=unparseable,
        source_path=source_path,
    )


async def test_mirrored_session_carries_codex_import_provenance(db):
    """The row records that it was imported, from which file, and on which id."""
    written, tally = await _mirror(db, _records())
    assert written == 4  # two user turns, two assistant turns

    row = await db.get_session(session_db_id(ROLLOUT_UID))
    assert row["source_kind"] == SOURCE_KIND
    assert row["cc_session_id"] == ROLLOUT_UID

    block = (row["node_metadata"] or {})["codex_import"]
    # A rollout carries three identifiers; the column that stores one of them is
    # silent about which, so the name travels with the value.
    assert block["id_field"] == ID_FIELD
    assert block["source_path"] == "/tmp/rollout-x.jsonl"
    assert tally.seen == block["records_seen"]


async def test_count_pairs_let_a_consumer_subtract_rather_than_trust(db):
    """Both sides of every record type are recorded, so completeness is arithmetic."""
    _, tally = await _mirror(db, _records())

    # Source side: what the file held, including the types nothing is mirrored from.
    assert tally.seen == {
        "session_meta": 1,
        "turn_context": 2,
        "response_item": 5,
        "world_state": 1,
    }
    # DB side: only the four conversation turns produced messages. The developer
    # response_item is seen and not mirrored, and the difference is visible.
    assert tally.mirrored == {"response_item": 4}
    assert tally.seen["response_item"] - tally.mirrored["response_item"] == 1
    # Types that mirror nothing at all are absent from the DB side, never zeroed
    # into it, so "seen but produced nothing" and "never seen" stay distinguishable.
    assert "world_state" not in tally.mirrored


async def test_unparseable_is_its_own_number_not_a_skip(db):
    """A line that could not be read never rolls into a type's deliberate skip."""
    _, tally = await _mirror(db, _records(), unparseable=3)
    assert tally.unparseable == 3
    # It is not attributed to any record type, because it has none — that is the
    # whole reason it is counted separately.
    assert sum(tally.seen.values()) == 9
    assert tally.as_provenance()["records_unparseable"] == 3

    row = await db.get_session(session_db_id(ROLLOUT_UID))
    assert row["node_metadata"]["codex_import"]["records_unparseable"] == 3


async def test_each_message_is_attributed_to_the_turn_that_produced_it(db):
    """Model and effort travel per message, so a mid-thread switch is not flattened."""
    await _mirror(db, _records())
    sid = session_db_id(ROLLOUT_UID)
    messages = await db.get_branch_messages(_det(ROLLOUT_UID, "branch"))
    turns = [(m["node_metadata"] or {}).get("codex_turn") for m in messages]

    assert all(t is not None for t in turns), "a mirrored turn must not be an unattributed quote"
    models = [t["model"] for t in turns]
    # The first two turns predate the switch, the last two follow it. A session-level
    # model would report one value for all four and misattribute half of them.
    assert models == ["gpt-5.6-terra"] * 2 + ["gpt-5.6-sol"] * 2
    assert [t["effort"] for t in turns] == ["high"] * 2 + ["xhigh"] * 2


async def test_turn_attribution_survives_a_split_batch(db):
    """A file mirrored across two passes keeps attributing to the carried turn.

    The turn_context arrives in the first batch only; without carrying it, every
    message in the second batch would be written with no attribution at all.
    """
    records = _records()
    carried: dict[str, str] = {}
    await _mirror(db, records[:4], turn=carried)
    assert carried == {"model": "gpt-5.6-terra", "effort": "high"}

    await _mirror(db, [_user("later question", "m9")], turn=carried)
    messages = await db.get_branch_messages(_det(ROLLOUT_UID, "branch"))
    last = messages[-1]
    assert (last["node_metadata"] or {})["codex_turn"]["model"] == "gpt-5.6-terra"


async def test_successive_passes_accumulate_the_counts(db):
    """The recorded tally describes the whole file, not the most recent batch."""
    records = _records()
    await _mirror(db, records[:4])
    await _mirror(db, records[4:])

    block = (await db.get_session(session_db_id(ROLLOUT_UID)))["node_metadata"]["codex_import"]
    assert block["records_seen"] == {
        "session_meta": 1,
        "turn_context": 2,
        "response_item": 5,
        "world_state": 1,
    }
    assert block["messages_mirrored"] == {"response_item": 4}


async def test_re_mirroring_the_same_records_writes_no_duplicates(db):
    """Ids are derived from the rollout, so a re-read is an update, not an insert."""
    await _mirror(db, _records())
    before = await db.get_branch_messages(_det(ROLLOUT_UID, "branch"))
    await _mirror(db, _records())
    after = await db.get_branch_messages(_det(ROLLOUT_UID, "branch"))
    assert [m["id"] for m in before] == [m["id"] for m in after]


async def test_legacy_flat_rollout_is_recorded_even_though_it_mirrors_nothing(db):
    """A file this reader cannot parse gets a row, or it leaves no trace at all.

    Pre-2025-09-20 rollouts carry no ``{type, timestamp, payload}`` envelope, so
    every record falls through as untyped. Skipping the write would make the one
    outcome the count pair exists to expose the one outcome it cannot see.
    """
    flat = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    written, tally = await _mirror(db, flat)
    assert written == 0

    row = await db.get_session(session_db_id(ROLLOUT_UID))
    assert row is not None, "a rollout that mirrors nothing must still be accounted for"
    assert row["source_kind"] == SOURCE_KIND
    block = row["node_metadata"]["codex_import"]
    assert block["records_seen"] == {"<untyped>": 2}
    assert block["messages_mirrored"] == {}
    assert block["source_path"] == "/tmp/rollout-x.jsonl"
    # NOT NULL columns, and there is no message to take a time from.
    assert row["created_at"] is not None


async def test_fully_injected_rollout_is_recorded_with_its_counts(db):
    """Every user turn filtered is exactly the shape an unenumerated form takes.

    The filter list cannot be closed against a format another program owns, so a
    future injection prefix will read as a rollout that mirrors nothing. That has
    to arrive as a row whose seen count exceeds its mirrored count.
    """
    records = [
        _rec("session_meta", {"id": ROLLOUT_UID, "cwd": "/x"}),
        _turn("gpt-5.6-terra", "high"),
        _user("<environment_context>\ncwd is /x", "i1"),
        _user("# AGENTS.md instructions for /x\nbe good", "i2"),
    ]
    written, tally = await _mirror(db, records)
    assert written == 0

    row = await db.get_session(session_db_id(ROLLOUT_UID))
    assert row is not None
    block = row["node_metadata"]["codex_import"]
    # Two response_items read, none mirrored: the subtraction a consumer does
    # returns 2, and that is the signal.
    assert block["records_seen"]["response_item"] == 2
    assert "response_item" not in block["messages_mirrored"]
    assert row["created_at"] is not None
    assert await db.get_branch_messages(_det(ROLLOUT_UID, "branch")) == []


async def test_a_later_batch_fills_in_a_rollout_that_began_barren(db):
    """The row written for a barren batch is the same row its real turns land in."""
    await _mirror(db, [_rec("session_meta", {"id": ROLLOUT_UID, "cwd": "/x"})])
    sid = session_db_id(ROLLOUT_UID)
    created_first = (await db.get_session(sid))["created_at"]

    await _mirror(db, [_turn("gpt-5.6-sol", "xhigh"), _user("a real question", "m1")])
    row = await db.get_session(sid)
    # Accumulated across both batches rather than replaced by the second.
    assert row["node_metadata"]["codex_import"]["records_seen"] == {
        "session_meta": 1,
        "turn_context": 1,
        "response_item": 1,
    }
    assert row["created_at"] == created_first
    assert len(await db.get_branch_messages(_det(ROLLOUT_UID, "branch"))) == 1


async def test_a_batch_that_read_nothing_writes_no_row(db):
    """An empty poll on a file is not a rollout; it must not mint a session."""
    written, tally = await _mirror(db, [])
    assert written == 0
    assert tally.seen == {}
    assert await db.get_session(session_db_id(ROLLOUT_UID)) is None


async def test_a_batch_of_only_unreadable_lines_is_recorded(db):
    """Lines that failed to parse are a finding; the row is where the count lives."""
    written, _ = await _mirror(db, [], unparseable=4)
    assert written == 0
    row = await db.get_session(session_db_id(ROLLOUT_UID))
    assert row is not None
    assert row["node_metadata"]["codex_import"]["records_unparseable"] == 4
    assert row["created_at"] is not None


def test_turn_context_reads_only_turn_context_records():
    assert turn_context(_turn("m", "e")) == {"model": "m", "effort": "e"}
    assert turn_context(_user("hi", "m1")) is None
    assert turn_context({"type": "turn_context", "payload": None}) is None
    # A turn_context carrying none of the retained fields is None rather than {},
    # so it never clears a good carried attribution with an empty one.
    assert turn_context(_rec("turn_context", {"cwd": "/x"})) is None


def test_tally_merge_is_additive_on_both_sides():
    a = RecordTally({"x": 1}, {"x": 1}, 1)
    b = RecordTally({"x": 2, "y": 3}, {}, 2)
    merged = a.merged(b)
    assert merged.seen == {"x": 3, "y": 3}
    assert merged.mirrored == {"x": 1}
    assert merged.unparseable == 3


@pytest.mark.parametrize(
    "opening",
    [
        "<recommended_plugins>",
        "<environment_context>",
        "<skill>",
        "<turn_aborted>",
        "# AGENTS.md instructions for /some/repo",
        "# Context from my IDE setup:",
        "# Files mentioned by the user:",
    ],
)
async def test_harness_injected_user_turns_are_not_mirrored_as_prompts(db, opening):
    """Codex delivers repo instructions, skills and notices through the user role.

    None was typed by a person, so mirroring them puts machine text in the
    conversation and, at the top of a file, makes it the first thing a reader sees.
    """
    records = [
        _rec("session_meta", {"id": ROLLOUT_UID, "cwd": "/x"}),
        _turn("gpt-5.6-terra", "high"),
        _user(f"{opening}\nbody text here", "inj"),
        _user("the actual question", "real"),
    ]
    written, tally = await _mirror(db, records)
    assert written == 1
    # The injected record is still counted as seen, so the difference between what
    # the file held and what was mirrored stays visible rather than being erased.
    assert tally.seen["response_item"] == 2
    assert tally.mirrored["response_item"] == 1

    messages = await db.get_branch_messages(_det(ROLLOUT_UID, "branch"))
    assert [m["content"]["instruction"] for m in messages] == ["the actual question"]


# ── Orchestrated-rollout absorption ──────────────────────────────────────────


async def test_delete_imported_session_removes_the_whole_graph(db):
    """Deleting an imported session takes its branch, progressions and messages
    with it, and reports that it actually deleted something."""
    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)
    sprog = _det(ROLLOUT_UID, "sprog")
    msg_ids = await db.get_progression(sprog)
    assert msg_ids  # the graph exists before the delete

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True

    assert await db.get_session(sid) is None
    assert await db.get_progression(sprog) == []
    for mid in msg_ids:
        assert await db.get_message(mid) is None
    # Idempotent: a second delete finds nothing and says so.
    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is False


async def test_delete_imported_session_refuses_live_rows(db):
    """The delete is owned by importers: a live run's session is never eligible,
    so a reconciliation sweep can never destroy real run history."""
    prog = "live-prog-1"
    await db.create_progression(prog)
    await db.create_session(
        {
            "id": "live-session-1",
            "created_at": 1.0,
            "progression_id": prog,
            "name": "agent",
            "status": "running",
            "source_kind": "live",
        }
    )
    assert (
        await db.delete_imported_session("live-session-1", require_source_kind="imported_live")
        is False
    )
    # Even naming the row's own kind cannot make a non-imported row eligible.
    assert await db.delete_imported_session("live-session-1", require_source_kind="live") is False
    assert await db.get_session("live-session-1") is not None


async def test_backfill_absorbs_only_orchestrated_imports(db):
    """The backfill removes imported rows whose recorded originator marks them as
    orchestrator-spawned, and leaves interactive and unattributed rows alone."""
    from lionagi.state.codex_mirror import absorb_orchestrated_backfill

    def rollout(uid: str) -> list[dict]:
        return [
            _rec("session_meta", {"id": uid, "cwd": "/x"}),
            _rec(
                "response_item",
                {"type": "message", "role": "user", "id": "m1", "content": [{"text": "q"}]},
            ),
        ]

    uid_exec = "0199aaaa-0000-0000-0000-000000000001"
    uid_desktop = "0199aaaa-0000-0000-0000-000000000002"
    uid_bare = "0199aaaa-0000-0000-0000-000000000003"
    for uid, meta in (
        (uid_exec, {"codex": {"originator": "codex_exec"}}),
        (uid_desktop, {"codex": {"originator": "Codex Desktop"}}),
        (uid_bare, None),
    ):
        await mirror_session(
            db,
            rollout_uid=uid,
            records=rollout(uid),
            tool_names={},
            node_metadata=meta,
            source_path=f"/tmp/rollout-{uid}.jsonl",
        )

    removed, failed = await absorb_orchestrated_backfill(db)

    assert (removed, failed) == (1, 0)
    assert await db.get_session(session_db_id(uid_exec)) is None
    # Interactive history stays; a row with no recorded originator is not treated
    # as orchestrated, because absent provenance is not evidence.
    assert await db.get_session(session_db_id(uid_desktop)) is not None
    assert await db.get_session(session_db_id(uid_bare)) is not None


async def test_backfill_skips_malformed_originator_without_dying(db):
    """One row whose recorded originator is not a string must neither crash the
    sweep nor be treated as orchestrated; the rest of the sweep still runs."""
    from lionagi.state.codex_mirror import absorb_orchestrated_backfill

    def rollout(uid: str) -> list[dict]:
        return [
            _rec("session_meta", {"id": uid, "cwd": "/x"}),
            _rec(
                "response_item",
                {"type": "message", "role": "user", "id": "m1", "content": [{"text": "q"}]},
            ),
        ]

    uid_bad = "0199cccc-0000-0000-0000-000000000001"
    uid_exec = "0199cccc-0000-0000-0000-000000000002"
    for uid, meta in (
        (uid_bad, {"codex": {"originator": []}}),
        (uid_exec, {"codex": {"originator": "codex_exec"}}),
    ):
        await mirror_session(
            db,
            rollout_uid=uid,
            records=rollout(uid),
            tool_names={},
            node_metadata=meta,
            source_path=f"/tmp/rollout-{uid}.jsonl",
        )

    removed, failed = await absorb_orchestrated_backfill(db)

    assert (removed, failed) == (1, 0)
    assert await db.get_session(session_db_id(uid_bad)) is not None
    assert await db.get_session(session_db_id(uid_exec)) is None


async def test_delete_retains_messages_another_progression_references(db):
    """A message some other progression also holds is not this session's to
    destroy: the delete removes the session graph but keeps that message."""
    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)
    msg_ids = await db.get_progression(_det(ROLLOUT_UID, "sprog"))
    shared_mid, other_mids = msg_ids[0], msg_ids[1:]

    outside_prog = "outside-progression-1"
    await db.create_progression(outside_prog)
    await db.append_to_progression(outside_prog, shared_mid)

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True

    assert await db.get_session(sid) is None
    assert await db.get_message(shared_mid) is not None  # retained: still referenced
    for mid in other_mids:
        assert await db.get_message(mid) is None
    assert await db.get_progression(outside_prog) == [shared_mid]


async def test_delete_retains_a_message_a_live_session_points_at(db):
    """A surviving session's first/last message pointer blocks deletion of that
    message: direct FK references count as ownership, not just progressions."""
    from sqlalchemy import text

    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)
    msg_ids = await db.get_progression(_det(ROLLOUT_UID, "sprog"))
    pointed_mid, other_mids = msg_ids[0], msg_ids[1:]

    prog = "live-prog-ptr"
    await db.create_progression(prog)
    await db.create_session(
        {
            "id": "live-session-ptr",
            "created_at": 1.0,
            "progression_id": prog,
            "name": "agent",
            "status": "running",
            "source_kind": "live",
        }
    )
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET first_msg_id = :m WHERE id = 'live-session-ptr'"),
            {"m": pointed_mid},
        )

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True
    assert await db.get_session(sid) is None
    assert await db.get_message(pointed_mid) is not None  # retained: pointed at
    for mid in other_mids:
        assert await db.get_message(mid) is None
    assert await db.get_session("live-session-ptr") is not None


async def test_delete_survives_soft_references_and_delivery_acks(db):
    """An artifact pointing at the imported session keeps its row with the
    pointer nullified, and an acked terminal delivery goes with its transition
    instead of aborting the whole absorb on the FK."""
    from sqlalchemy import text

    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)

    async with db._tx() as conn:
        await conn.execute(
            text(
                "INSERT INTO artifacts (id, session_id, created_at, updated_at, kind, name, content) "
                "VALUES ('art-1', :sid, 1.0, 1.0, 'review', 'verdict', '{}')"
            ),
            {"sid": sid},
        )
        await conn.execute(
            text(
                "INSERT INTO status_transitions "
                "(id, entity_type, entity_id, status, reason_code, source, created_at) "
                "VALUES ('tr-1', 'session', :sid, 'completed', 'imported', 'system', 1.0)"
            ),
            {"sid": sid},
        )
        await conn.execute(
            text(
                "INSERT INTO terminal_deliveries (transition_id, consumer, acked_at) "
                "VALUES ('tr-1', 'test-consumer', 2.0)"
            )
        )

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True
    assert await db.get_session(sid) is None
    async with db._tx() as conn:
        art = (
            await conn.execute(text("SELECT session_id FROM artifacts WHERE id = 'art-1'"))
        ).first()
        assert art is not None and art[0] is None  # row kept, pointer cleared
        assert (
            await conn.execute(text("SELECT 1 FROM status_transitions WHERE id = 'tr-1'"))
        ).first() is None
        assert (
            await conn.execute(
                text("SELECT 1 FROM terminal_deliveries WHERE transition_id = 'tr-1'")
            )
        ).first() is None


async def test_delete_tolerates_a_malformed_unrelated_progression(db):
    """One damaged collection elsewhere in the store must not sink an absorb:
    the shared-reference check filters it out rather than erroring globally."""
    from sqlalchemy import text

    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)
    msg_ids = await db.get_progression(_det(ROLLOUT_UID, "sprog"))

    await db.create_progression("damaged-prog")
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE progressions SET collection = 'not-json' WHERE id = 'damaged-prog'")
        )

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True
    assert await db.get_session(sid) is None
    for mid in msg_ids:
        assert await db.get_message(mid) is None


async def test_delete_retains_a_progression_a_survivor_references(db):
    """A survivor session whose progression_id names one of the imported
    session's progressions keeps that progression and its messages; the absorb
    still completes instead of aborting on the FK.

    Both directions live in this one fixture on purpose. Asserting only that the
    referenced progression survives cannot distinguish a correct retention rule
    from one that retains every progression, so the unreferenced sibling below is
    the arm that makes the retained one mean something.
    """
    from sqlalchemy import text

    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)
    sprog = _det(ROLLOUT_UID, "sprog")
    msgs_in_sprog = await db.get_progression(sprog)
    assert msgs_in_sprog

    # The sibling nobody will reference: same imported session, must be deleted.
    async with db._tx() as conn:
        unreferenced = [
            r["progression_id"]
            for r in (
                await conn.execute(
                    text(
                        "SELECT progression_id FROM branches "
                        "WHERE session_id = :sid AND progression_id IS NOT NULL"
                    ),
                    {"sid": sid},
                )
            ).mappings()
            if r["progression_id"] != sprog
        ]
    assert unreferenced, "fixture must contain a progression no survivor references"
    msgs_unreferenced = await db.get_progression(unreferenced[0])
    assert msgs_unreferenced

    await db.create_progression("survivor-own-prog")
    await db.create_session(
        {
            "id": "survivor-1",
            "created_at": 1.0,
            "progression_id": "survivor-own-prog",
            "name": "agent",
            "status": "running",
            "source_kind": "live",
        }
    )
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET progression_id = :p WHERE id = 'survivor-1'"),
            {"p": sprog},
        )

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True
    assert await db.get_session(sid) is None
    assert await db.get_session("survivor-1") is not None
    assert await db.get_progression(sprog) == msgs_in_sprog  # progression + messages kept
    for mid in msgs_in_sprog:
        assert await db.get_message(mid) is not None

    # The opposite direction, same fixture: no survivor names this progression,
    # so it and its messages go. Without this arm a retain-everything bug passes.
    async with db._tx() as conn:
        assert (
            await conn.execute(
                text("SELECT 1 FROM progressions WHERE id = :p"), {"p": unreferenced[0]}
            )
        ).first() is None
    for mid in msgs_unreferenced:
        if mid not in msgs_in_sprog:  # a message both progressions hold is retained
            assert await db.get_message(mid) is None


async def test_detached_artifact_renames_instead_of_colliding(db):
    """Detaching a session-scoped artifact whose (kind, name) is already taken
    in the unattached index domain renames the detached row deterministically
    instead of aborting the absorb on the unique constraint."""
    from sqlalchemy import text

    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)

    async with db._tx() as conn:
        await conn.execute(
            text(
                "INSERT INTO artifacts (id, session_id, created_at, updated_at, kind, name, content) "
                "VALUES ('art-free', NULL, 1.0, 1.0, 'review', 'verdict', '{}'), "
                "('art-att', :sid, 1.0, 1.0, 'review', 'verdict', '{}')"
            ),
            {"sid": sid},
        )

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True
    assert await db.get_session(sid) is None
    async with db._tx() as conn:
        rows = {
            r["id"]: dict(r)
            for r in (
                await conn.execute(text("SELECT id, name, session_id FROM artifacts"))
            ).mappings()
        }
    assert rows["art-free"]["name"] == "verdict"  # untouched
    assert rows["art-att"]["session_id"] is None
    assert rows["art-att"]["name"] == f"verdict (detached {sid})"


async def test_detached_artifact_suffix_allocation_is_collision_free(db):
    """The derived "(detached <sid>)" name can itself be occupied — by an
    unattached row or by another artifact of the deleted session — and the
    invocation-only domain collides independently of the unattached one. Each
    final name must be proven unused before the session_id flips, or the
    UNIQUE failure rolls back the whole absorption."""
    from sqlalchemy import text

    written, _ = await _mirror(db, _records())
    assert written == 4
    sid = session_db_id(ROLLOUT_UID)

    async with db._tx() as conn:
        await conn.execute(
            text(
                "INSERT INTO invocations (id, skill, started_at, created_at, updated_at) "
                "VALUES ('inv-1', 's', 1.0, 1.0, 1.0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO artifacts "
                "(id, session_id, invocation_id, created_at, updated_at, kind, name, content) VALUES "
                "('art-free', NULL, NULL, 1.0, 1.0, 'review', 'verdict', '{}'), "
                "('art-sfx', NULL, NULL, 1.0, 1.0, 'review', 'verdict (detached ' || :sid || ')', '{}'), "
                "('art-att', :sid, NULL, 1.0, 1.0, 'review', 'verdict', '{}'), "
                "('art-att2', :sid, NULL, 1.0, 1.0, 'review', 'verdict (detached ' || :sid || ')', '{}'), "
                "('art-inv-free', NULL, 'inv-1', 1.0, 1.0, 'review', 'verdict', '{}'), "
                "('art-inv-att', :sid, 'inv-1', 1.0, 1.0, 'review', 'verdict', '{}')"
            ),
            {"sid": sid},
        )

    assert await db.delete_imported_session(sid, require_source_kind=SOURCE_KIND) is True
    assert await db.get_session(sid) is None
    async with db._tx() as conn:
        rows = {
            r["id"]: dict(r)
            for r in (
                await conn.execute(
                    text("SELECT id, name, session_id, invocation_id FROM artifacts")
                )
            ).mappings()
        }
    assert rows["art-free"]["name"] == "verdict"  # untouched
    assert rows["art-sfx"]["name"] == f"verdict (detached {sid})"  # untouched
    # base and first suffix both occupied in the unattached domain
    assert rows["art-att"]["name"] == f"verdict (detached {sid} 2)"
    # a session row whose own name IS the derived name of another
    assert rows["art-att2"]["name"] == f"verdict (detached {sid}) (detached {sid})"
    # invocation-only domain collides independently; its base suffix is free
    assert rows["art-inv-free"]["name"] == "verdict"
    assert rows["art-inv-att"]["name"] == f"verdict (detached {sid})"
    for aid in ("art-att", "art-att2", "art-inv-att"):
        assert rows[aid]["session_id"] is None


@pytest.mark.asyncio
async def test_delete_refuses_a_mismatched_kind_and_still_honours_survivor_pointers(db):
    """``require_source_kind`` is a mismatch guard, and the teardown honours a
    survivor's first/last message pointers whichever importer owns the row.

    The fs importer is the other writer of an ``imported_`` kind, and it sets
    ``first_msg_id``/``last_msg_id`` where the codex mirror does not. This pins
    both halves of what that means: the codex mirror cannot reach an fs row by
    naming its own kind, and a caller that does name the fs kind still gets the
    retention rule, because a message a surviving session points at is not the
    deleted session's to destroy.

    Both directions are here on purpose. The retained message alone cannot tell
    a correct rule from one that retains everything, so the unpointed sibling is
    what makes it evidence.
    """
    msg_pointed = _det("fs-case", "pointed")
    msg_own = _det("fs-case", "own")
    for mid in (msg_pointed, msg_own):
        await db.insert_message(
            {"id": mid, "created_at": 1.0, "content": {"text": "x"}, "role": "user"}
        )

    fs_prog, fs_sid = _det("fs-case", "prog"), _det("fs-case", "sid")
    await db.create_progression(fs_prog, [msg_pointed, msg_own])
    await db.create_session(
        {
            "id": fs_sid,
            "progression_id": fs_prog,
            "status": "completed",
            "created_at": 1.0,
            "updated_at": 1.0,
            "source_kind": "imported_fs",
        }
    )

    # A live survivor whose last_msg_id is the only thing pointing at
    # msg_pointed: no progression carries it once the fs row goes.
    survivor_sid = _det("fs-case", "survivor")
    survivor_prog = _det("fs-case", "survivor-prog")
    await db.create_progression(survivor_prog, [])
    await db.create_session(
        {
            "id": survivor_sid,
            "progression_id": survivor_prog,
            "last_msg_id": msg_pointed,
            "status": "running",
            "created_at": 1.0,
            "updated_at": 1.0,
            "source_kind": "live",
        }
    )

    # The codex mirror names its own kind, so an fs row is not reachable.
    assert await db.delete_imported_session(fs_sid, require_source_kind=SOURCE_KIND) is False
    assert await db.get_session(fs_sid) is not None

    # Naming the fs kind does tear the fs row down, and the retention rule
    # applies to it exactly as it does to a codex row.
    assert await db.delete_imported_session(fs_sid, require_source_kind="imported_fs") is True
    assert await db.get_session(fs_sid) is None
    assert await db.get_session(survivor_sid) is not None
    assert await db.get_message(msg_pointed) is not None, (
        "a message a surviving session's last_msg_id points at was deleted"
    )
    assert await db.get_message(msg_own) is None, (
        "the message nobody points at survived, so the arm above proves nothing"
    )
