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
