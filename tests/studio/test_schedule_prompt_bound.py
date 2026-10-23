# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule prompt admission stays bounded at rest and after rendering."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.studio.scheduler.subprocess import render_action_prompt
from lionagi.studio.services.schedules import create_schedule, update_schedule


def _schedule_data(prompt: str) -> dict:
    return {
        "name": "prompt-bound",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_prompt": prompt,
    }


def test_create_rejects_prompt_over_shared_limit_before_db_write():
    async def _run() -> None:
        with patch("lionagi.studio.services.schedules.StateDB") as state_db:
            db = AsyncMock()
            state_db.return_value.__aenter__ = AsyncMock(return_value=db)
            state_db.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match=str(MAX_SPEC_PROMPT_CHARS)):
                await create_schedule(_schedule_data("x" * (MAX_SPEC_PROMPT_CHARS + 1)))
            db.create_schedule.assert_not_awaited()

    asyncio.run(_run())


def test_update_rejects_prompt_over_shared_limit_before_db_write():
    async def _run() -> None:
        with patch("lionagi.studio.services.schedules.StateDB") as state_db:
            db = AsyncMock()
            db.get_schedule.return_value = _schedule_data("old") | {"id": "sched-1"}
            state_db.return_value.__aenter__ = AsyncMock(return_value=db)
            state_db.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match=str(MAX_SPEC_PROMPT_CHARS)):
                await update_schedule(
                    "sched-1", {"action_prompt": "x" * (MAX_SPEC_PROMPT_CHARS + 1)}
                )
            db.update_schedule.assert_not_awaited()

    asyncio.run(_run())


def test_render_rejects_template_expansion_over_shared_limit():
    with pytest.raises(ValueError, match=str(MAX_SPEC_PROMPT_CHARS)):
        render_action_prompt(
            {"action_prompt": "{{payload}}"},
            {"payload": "x" * (MAX_SPEC_PROMPT_CHARS + 1)},
        )


def test_render_accepts_prompt_at_shared_limit():
    prompt = "x" * MAX_SPEC_PROMPT_CHARS
    assert render_action_prompt({"action_prompt": prompt}, {}) == prompt


def _play_data(prompt: str, **overrides) -> dict:
    return {
        **_schedule_data(prompt),
        "action_kind": "play",
        "action_playbook": "triage",
        **overrides,
    }


@pytest.mark.parametrize("kind", ["play", "playbook"])
def test_create_rejects_a_play_prompt_that_opens_with_a_dash_before_db_write(kind):
    async def _run() -> None:
        with patch("lionagi.studio.services.schedules.StateDB") as state_db:
            db = AsyncMock()
            state_db.return_value.__aenter__ = AsyncMock(return_value=db)
            state_db.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="must not start with '-'"):
                await create_schedule(_play_data("--bypass", action_kind=kind))
            db.create_schedule.assert_not_awaited()

    asyncio.run(_run())


@pytest.mark.parametrize("kind", ["play", "playbook"])
def test_create_keeps_a_dash_prompt_on_a_play_without_a_playbook(kind):
    # Without a playbook the launch sends no prompt, so nothing re-parses it as a flag.
    async def _run() -> None:
        with patch("lionagi.studio.services.schedules.StateDB") as state_db:
            db = AsyncMock()
            state_db.return_value.__aenter__ = AsyncMock(return_value=db)
            state_db.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_schedule(_play_data("--bypass", action_kind=kind, action_playbook=None))
            db.create_schedule.assert_awaited_once()

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("stored", "fields"),
    [
        (_play_data("ok"), {"action_prompt": "--bypass"}),
        # The update that adds the playbook is the one that makes the stored prompt sent.
        (_play_data("--bypass", action_playbook=None), {"action_playbook": "triage"}),
        (_play_data("--bypass", action_kind="agent"), {"action_kind": "play"}),
    ],
)
def test_update_rejects_a_play_prompt_that_opens_with_a_dash_before_db_write(stored, fields):
    async def _run() -> None:
        with patch("lionagi.studio.services.schedules.StateDB") as state_db:
            db = AsyncMock()
            db.get_schedule.return_value = stored | {"id": "sched-1"}
            state_db.return_value.__aenter__ = AsyncMock(return_value=db)
            state_db.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="must not start with '-'"):
                await update_schedule("sched-1", fields)
            db.update_schedule.assert_not_awaited()

    asyncio.run(_run())


def test_update_of_another_field_on_a_stored_dash_play_prompt_is_written():
    # A row stored before the check stays editable; it is refused when it fires.
    async def _run() -> None:
        with patch("lionagi.studio.services.schedules.StateDB") as state_db:
            db = AsyncMock()
            db.get_schedule.return_value = _play_data("--bypass") | {"id": "sched-1"}
            state_db.return_value.__aenter__ = AsyncMock(return_value=db)
            state_db.return_value.__aexit__ = AsyncMock(return_value=False)
            assert await update_schedule("sched-1", {"name": "renamed"}) is True
            db.update_schedule.assert_awaited_once_with("sched-1", name="renamed")

    asyncio.run(_run())
