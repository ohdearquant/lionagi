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
