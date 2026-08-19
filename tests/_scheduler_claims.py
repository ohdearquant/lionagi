# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for tests that exercise the scheduler's due-instant claim."""

from __future__ import annotations

from typing import Any

from lionagi.state.db import NO_CURSOR_CLAIM

__all__ = (
    "claim_and_advance",
    "claiming_advance",
    "claim_holds",
    "fire_inner_with_claim",
    "fire_with_claim",
    "persisting_update_schedule",
)


async def claim_and_advance(db, run, *, schedule_id, schedule_fields):
    """Write an occurrence holding whatever cursor the schedule currently has.

    For tests that predate the claim and are about something else; the claim itself is
    covered in tests/state/test_schedule_due_cursor_claim.py.
    """
    current = (await db.get_schedule(schedule_id))["next_fire_at"]
    return await db.create_schedule_run_and_advance(
        run,
        schedule_id=schedule_id,
        schedule_fields=schedule_fields,
        expect_next_fire_at=current,
    )


async def fire_with_claim(engine, schedule, run_id, **kwargs):
    """Call ``_fire`` the way the ordinary due tick does, claiming the schedule's cursor."""
    kwargs.setdefault("expect_next_fire_at", schedule.get("next_fire_at"))
    return await engine._fire(schedule, run_id, **kwargs)


async def fire_inner_with_claim(engine, schedule, run_id, **kwargs):
    """``fire_with_claim`` for tests that drive ``_fire_inner`` directly."""
    kwargs.setdefault("expect_next_fire_at", schedule.get("next_fire_at"))
    return await engine._fire_inner(schedule, run_id, **kwargs)


def claim_holds(stored_next_fire_at: Any, expect: Any) -> bool:
    """Whether a claim of *expect* still holds against *stored_next_fire_at*.

    Test doubles route their claim through this so a mismatched claim is refused there too;
    a double that always returned True could not tell a correct cursor from a stale one.
    """
    return expect is NO_CURSOR_CLAIM or stored_next_fire_at == expect


def persisting_update_schedule(schedule: dict):
    """An ``update_schedule`` double that persists into *schedule* and honors the claim.

    Mirrors the real signature rather than swallowing it into ``**fields``: a double that
    accepted the claim as an ordinary field would write it onto the row and report success
    whatever the caller claimed.
    """

    async def _update(
        schedule_id: str,
        *,
        guard_cursor_forward: bool = False,
        expect_next_fire_at: Any = NO_CURSOR_CLAIM,
        **fields: Any,
    ) -> bool:
        if schedule_id != schedule["id"]:
            return False
        if not claim_holds(schedule.get("next_fire_at"), expect_next_fire_at):
            return False
        schedule.update(fields)
        return True

    return _update


def claiming_advance(stored: dict, *, on_write=None):
    """A ``create_schedule_run_and_advance`` double that honors both claims.

    Mirrors the real signature instead of absorbing the claims into ``**fields``. A double
    that always returns True cannot tell a winning claim from a losing one, so every test
    using it would pass whether or not the engine claims anything.

    *stored* stands for the schedule row and is updated on a winning write, which is what
    makes a second caller's stale claim fail the way the database would fail it.
    """

    async def _advance(
        payload,
        *,
        schedule_id,
        schedule_fields,
        expect_next_fire_at,
        expect_github_cursor=NO_CURSOR_CLAIM,
    ):
        if not claim_holds(stored.get("next_fire_at"), expect_next_fire_at):
            return False
        if not claim_holds(stored.get("github_cursor"), expect_github_cursor):
            return False
        stored.update(schedule_fields)
        if on_write is not None:
            on_write(payload)
        return True

    return _advance
