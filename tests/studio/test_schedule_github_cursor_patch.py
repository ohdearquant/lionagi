# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for patching a schedule's github_cursor.

The cursor is the merged-PR poller's bookmark. Until it was patchable there was
no supported way to move it at all -- not the CLI, not the API, not the
declarative apply path -- so an operator facing a schedule whose backlog would
dispatch all at once had no mechanism short of writing to the store by hand.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from unittest.mock import AsyncMock, patch

from lionagi.studio.services.schedules import (
    UpdateScheduleRequest,
    _svc_validate_github_cursor,
    update_schedule,
)

_EXISTING = {
    "id": "sid-cursor-1",
    "name": "cursor-patch-test",
    "trigger_type": "github_poll",
    "github_repo": "owner/name",
    "action_kind": "agent",
    "github_cursor": "2026-07-20T15:21:57Z",
}


def _patched_db():
    mock_db = AsyncMock()
    mock_db.get_schedule = AsyncMock(return_value=dict(_EXISTING))
    mock_db.update_schedule = AsyncMock()
    ctx = patch("lionagi.studio.services.schedules.StateDB")
    return ctx, mock_db


def _run_update(fields):
    """Drive update_schedule against a mocked StateDB; return (result, mock_db)."""

    async def _run():
        ctx, mock_db = _patched_db()
        with ctx as MockDB:
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await update_schedule("sid-cursor-1", fields)
            return result, mock_db

    return asyncio.run(_run())


def _expect_rejected(fields, match):
    async def _run():
        ctx, mock_db = _patched_db()
        with ctx as MockDB:
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match=match):
                await update_schedule("sid-cursor-1", fields)
            mock_db.update_schedule.assert_not_called()

    asyncio.run(_run())


# _svc_validate_github_cursor — pure logic


def test_validate_cursor_accepts_the_api_spelling():
    _svc_validate_github_cursor("2026-07-20T15:21:57Z")
    _svc_validate_github_cursor("2020-01-01T00:00:00Z")


def test_validate_cursor_none_clears_and_is_allowed():
    """None means "no bookmark". Consequential, but the operator's call."""
    _svc_validate_github_cursor(None)


@pytest.mark.parametrize(
    "bad",
    [
        "2026-07-20 15:21:57Z",  # space separator
        "2026-07-20T15:21:57+00:00",  # offset instead of Z
        "2026-07-20T15:21:57.123Z",  # fractional seconds
        "2026-07-20T15:21:57",  # no zone at all
        "2026-07-20",  # date only
        "  2026-07-20T15:21:57Z",  # leading whitespace
        "2026-07-20T15:21:57Z ",  # trailing whitespace
        "",
    ],
)
def test_validate_cursor_rejects_other_spellings_of_the_same_instant(bad):
    with pytest.raises(ValueError, match="github_cursor"):
        _svc_validate_github_cursor(bad)


def test_validate_cursor_rejects_non_string():
    with pytest.raises(ValueError, match="github_cursor"):
        _svc_validate_github_cursor(1753025000)


def test_validate_cursor_rejects_well_formed_but_impossible_timestamp():
    """The shape regex alone would pass month 13; the parse is what catches it."""
    with pytest.raises(ValueError, match="not a real timestamp"):
        _svc_validate_github_cursor("2026-13-45T99:00:00Z")


def test_why_the_format_is_strict_rather_than_pedantic():
    """The poller compares cursors as strings, so spelling decides ordering.

    Each rejected spelling below is compared against a real API timestamp and
    sorts the wrong way round, which is what makes a lenient validator a
    correctness bug rather than a style preference. The three cases fail
    differently, so they are spelled out rather than generalized.
    """
    api = "2026-07-20T15:21:57Z"

    # A space separator sorts below 'T' (0x20 < 0x54), so a LATER instant reads
    # as older and the poller re-dispatches everything between the two.
    assert "2026-07-20 16:00:00Z" < api

    # '+00:00' is the SAME instant as 'Z', and '+' sorts below 'Z' (0x2B <
    # 0x5A), so the event sitting exactly on the cursor stops being excluded
    # and fires again.
    assert "2026-07-20T15:21:57+00:00" < api

    # A fractional part sorts below 'Z' too ('.' is 0x2E), so a timestamp half
    # a second LATER also reads as older.
    assert "2026-07-20T15:21:57.500Z" < api

    for wrong in (
        "2026-07-20 16:00:00Z",
        "2026-07-20T15:21:57+00:00",
        "2026-07-20T15:21:57.500Z",
    ):
        with pytest.raises(ValueError):
            _svc_validate_github_cursor(wrong)


# The API request model — the gate that was actually missing


def test_patch_model_carries_github_cursor():
    """The validator is useless if the field never survives the request model.

    update_schedule is driven entirely by UpdateScheduleRequest.model_dump
    (exclude_unset), so a field absent from the model is silently dropped with
    a 200 and no write -- which is exactly how the cursor came to be
    unsettable while every layer beneath it already supported the column.
    """
    body = UpdateScheduleRequest(github_cursor="2026-07-26T07:00:00Z")
    assert body.model_dump(exclude_unset=True) == {"github_cursor": "2026-07-26T07:00:00Z"}


def test_patch_model_distinguishes_unset_from_explicit_null():
    """Clearing the cursor and not mentioning it must not collapse together."""
    assert "github_cursor" not in UpdateScheduleRequest(name="x").model_dump(exclude_unset=True)
    assert UpdateScheduleRequest(github_cursor=None).model_dump(exclude_unset=True) == {
        "github_cursor": None
    }


# update_schedule — end to end against a mocked store


def test_update_persists_the_cursor_verbatim():
    result, mock_db = _run_update({"github_cursor": "2026-07-26T07:00:00Z"})
    assert result is True
    mock_db.update_schedule.assert_awaited_once_with(
        "sid-cursor-1", github_cursor="2026-07-26T07:00:00Z"
    )


def test_update_can_move_the_cursor_backwards_to_replay():
    """Not just forward. Replaying a band is a legitimate operator action."""
    result, mock_db = _run_update({"github_cursor": "2026-07-01T00:00:00Z"})
    assert result is True
    mock_db.update_schedule.assert_awaited_once_with(
        "sid-cursor-1", github_cursor="2026-07-01T00:00:00Z"
    )


def test_update_can_clear_the_cursor():
    result, mock_db = _run_update({"github_cursor": None})
    assert result is True
    mock_db.update_schedule.assert_awaited_once_with("sid-cursor-1", github_cursor=None)


# What the poller actually writes. The validator and the writer are two spellings of one
# format, and they drifted: the engine persisted a cursor naming the event within its
# second while this validator still accepted only the bare instant, so the system wrote a
# value its own API refused and an operator replaying a stored cursor got an error on the
# scheduler's own output. These pin the round trip rather than the spelling, so the next
# format change cannot reintroduce the split quietly.


@pytest.mark.parametrize("pr_number", [0, 1, 42, 999999, 9999999999])
def test_the_validator_accepts_every_cursor_the_poller_writes(pr_number):
    from lionagi.studio.scheduler.github import _cursor_for

    _svc_validate_github_cursor(_cursor_for("2026-07-20T15:21:57Z", pr_number))


def test_the_stored_cursor_survives_being_patched_back():
    """The operator flow the split broke: read a persisted cursor, PATCH it back."""
    from lionagi.studio.scheduler.github import _cursor_for

    stored = _cursor_for("2026-07-26T07:00:00Z", 3389)
    result, mock_db = _run_update({"github_cursor": stored})

    assert result is True
    mock_db.update_schedule.assert_awaited_once_with("sid-cursor-1", github_cursor=stored)


def test_the_bare_instant_a_cursor_had_before_the_number_is_still_accepted():
    """Every cursor stored before the number existed is one of these."""
    _svc_validate_github_cursor("2026-07-20T15:21:57Z")


@pytest.mark.parametrize(
    "bad",
    [
        "2026-07-20T15:21:57Z#1",  # not padded
        "2026-07-20T15:21:57Z#00000000001",  # one digit too wide
        "2026-07-20T15:21:57Z#",  # separator, no number
        "2026-07-20T15:21:57Z#00000000ab",  # not digits
        "2026-07-20T15:21:57Z#0000000001#0000000002",  # two of them
    ],
)
def test_the_validator_rejects_a_number_the_poller_would_never_write(bad):
    with pytest.raises(ValueError, match="github_cursor"):
        _svc_validate_github_cursor(bad)


def test_why_the_number_is_fixed_width_rather_than_pedantic():
    """Same reason the instant is strict: the comparison is lexical.

    An unpadded number sorts by its first digit, so the cursor for PR 9 reads as
    later than the cursor for PR 10 and the poller drops an event it never
    dispatched. The padding is what makes string order agree with numeric order.
    """
    from lionagi.studio.scheduler.github import _cursor_for

    at = "2026-07-20T15:21:57Z"
    assert _cursor_for(at, 9) < _cursor_for(at, 10), "padded: 9 sorts before 10"
    assert f"{at}#9" > f"{at}#10", "unpadded: 9 sorts after 10, which is the defect"


def test_update_rejects_a_malformed_cursor_before_any_write():
    _expect_rejected({"github_cursor": "2026-07-26 07:00:00"}, "github_cursor")


def test_update_rejects_an_impossible_cursor_before_any_write():
    _expect_rejected({"github_cursor": "2026-02-30T00:00:00Z"}, "not a real timestamp")


# The interleaving: an operator PATCH vs an in-flight poll, against a real store

from lionagi.state.db import StateDB  # noqa: E402
from tests._scheduler_claims import claim_and_advance


def _gh_schedule(sid: str, cursor: str | None) -> dict:
    return {
        "id": sid,
        "name": f"gh-{sid}",
        "trigger_type": "github_poll",
        "github_repo": "owner/name",
        "github_filter": {"event": "pr_merged"},
        "github_cursor": cursor,
        "action_kind": "agent",
        "action_prompt": "review",
        "enabled": 1,
        "missed_fire_policy": "skip",
    }


def _run_row(run_id: str, sid: str) -> dict:
    return {
        "id": run_id,
        "schedule_id": sid,
        "trigger_context": {"source": "github_poll"},
        "action_kind": "agent",
        "action_args": {"prompt": "review"},
        "status": "running",
        "fired_at": 1000.0,
    }


@pytest.mark.asyncio
async def test_operator_patch_survives_an_in_flight_polls_cursor_write(tmp_path):
    """The race the patchable cursor creates, and the reason for the guard.

    A tick reads the schedule at its start and writes the cursor back much
    later, so its value is a snapshot. If an operator moves the cursor forward
    in between -- the whole point of the field -- an unguarded write from that
    tick puts it back, and the backlog the operator declined becomes eligible
    again on the next scan. Silent: the PATCH returned 200 and the run row is
    perfectly valid.
    """
    db_path = tmp_path / "state.db"
    sid = "sched-race"
    async with StateDB(db_path) as db:
        await db.create_schedule(_gh_schedule(sid, "2026-07-20T00:00:00Z"))

    # Operator declines the backlog while a poll is in flight.
    async with StateDB(db_path) as db:
        await db.update_schedule(sid, github_cursor="2026-08-01T00:00:00Z")

    # The in-flight tick now lands, carrying the value it computed from the
    # cursor it read before the PATCH.
    async with StateDB(db_path) as db:
        await claim_and_advance(
            db,
            _run_row("run-race", sid),
            schedule_id=sid,
            schedule_fields={"github_cursor": "2026-07-21T00:00:00Z", "last_fired_at": 1000.0},
        )

    async with StateDB(db_path) as db:
        schedule = await db.get_schedule(sid)
        runs = await db.list_schedule_runs(sid)

    assert schedule["github_cursor"] == "2026-08-01T00:00:00Z", (
        "a stale poll walked the operator's cursor backwards"
    )
    # The occurrence still had to be recorded: the event DID fire, and a
    # refused cursor advance must not discard the record of it.
    assert len(runs) == 1
    # Sibling fields in the same statement land regardless of the guard.
    assert schedule["last_fired_at"] == 1000.0


@pytest.mark.asyncio
async def test_normal_forward_advance_is_unaffected_by_the_guard(tmp_path):
    """The guard must not break the ordinary case it wraps."""
    db_path = tmp_path / "state.db"
    sid = "sched-fwd"
    async with StateDB(db_path) as db:
        await db.create_schedule(_gh_schedule(sid, "2026-07-20T00:00:00Z"))
        await claim_and_advance(
            db,
            _run_row("run-fwd", sid),
            schedule_id=sid,
            schedule_fields={"github_cursor": "2026-07-22T00:00:00Z"},
        )
        schedule = await db.get_schedule(sid)
    assert schedule["github_cursor"] == "2026-07-22T00:00:00Z"


@pytest.mark.asyncio
async def test_guard_advances_from_a_null_cursor(tmp_path):
    """A NULL cursor is below everything; ``NULL < x`` is NULL, not true, so
    the explicit IS NULL branch is what makes a first advance work at all."""
    db_path = tmp_path / "state.db"
    sid = "sched-null"
    async with StateDB(db_path) as db:
        await db.create_schedule(_gh_schedule(sid, None))
        await claim_and_advance(
            db,
            _run_row("run-null", sid),
            schedule_id=sid,
            schedule_fields={"github_cursor": "2026-07-22T00:00:00Z"},
        )
        schedule = await db.get_schedule(sid)
    assert schedule["github_cursor"] == "2026-07-22T00:00:00Z"


@pytest.mark.asyncio
async def test_operator_path_is_not_guarded_so_a_replay_is_still_possible(tmp_path):
    """The guard is the ENGINE's invariant, not the operator's.

    Moving the cursor backwards to replay a band is a legitimate action and
    must not be silently ignored -- which is exactly what a guard applied to
    both writers would do.
    """
    db_path = tmp_path / "state.db"
    sid = "sched-replay"
    async with StateDB(db_path) as db:
        await db.create_schedule(_gh_schedule(sid, "2026-07-25T00:00:00Z"))
        await db.update_schedule(sid, github_cursor="2026-07-01T00:00:00Z")
        schedule = await db.get_schedule(sid)
    assert schedule["github_cursor"] == "2026-07-01T00:00:00Z"


@pytest.mark.asyncio
async def test_guard_is_inert_for_schedules_with_no_cursor_field(tmp_path):
    """Non-github schedules go through the same statement builder."""
    db_path = tmp_path / "state.db"
    sid = "sched-cron"
    async with StateDB(db_path) as db:
        await db.create_schedule(
            {
                "id": sid,
                "name": "cron-x",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "agent",
                "action_prompt": "ping",
                "enabled": 1,
                "missed_fire_policy": "skip",
                "next_fire_at": 1000.0,
            }
        )
        await claim_and_advance(
            db,
            _run_row("run-cron", sid),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )
        schedule = await db.get_schedule(sid)
    assert schedule["next_fire_at"] == 2000.0
    assert schedule["last_fired_at"] == 1000.0


def test_guarded_statement_compiles_on_both_dialects():
    """The guard repeats one bound parameter three times in a single statement.

    SQLite renders positional placeholders and PostgreSQL renders named ones,
    so the repetition is the part worth pinning: it is where a hand-written
    predicate would break on one backend and not the other. The PostgreSQL
    integration tests need a driver that is not installed in every
    environment, so this compiles the statement instead of executing it --
    which checks the parameter shape, not the runtime semantics.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    from lionagi.state.db import StateDB as _StateDB

    stmt, _params = _StateDB._build_update_schedule_stmt(
        "sid",
        {"github_cursor": "2026-07-22T00:00:00Z", "last_fired_at": 1.0},
        guard_cursor_forward=True,
    )
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        sql = str(stmt.compile(dialect=dialect))
        assert "CASE WHEN github_cursor IS NULL" in sql
        assert '"last_fired_at"' in sql  # sibling fields still assigned plainly
