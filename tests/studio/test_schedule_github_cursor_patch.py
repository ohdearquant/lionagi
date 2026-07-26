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


# ---------------------------------------------------------------------------
# _svc_validate_github_cursor — pure logic
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The API request model — the gate that was actually missing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# update_schedule — end to end against a mocked store
# ---------------------------------------------------------------------------


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


def test_update_rejects_a_malformed_cursor_before_any_write():
    _expect_rejected({"github_cursor": "2026-07-26 07:00:00"}, "github_cursor")


def test_update_rejects_an_impossible_cursor_before_any_write():
    _expect_rejected({"github_cursor": "2026-02-30T00:00:00Z"}, "not a real timestamp")
