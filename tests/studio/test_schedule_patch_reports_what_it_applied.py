# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A schedule PATCH must not answer success for a change it did not make.

Both request models used to ignore unknown keys. A caller sending a field the
model does not declare got ``{"ok": True}`` and no change, which is the worst
direction for an API to fail in: the operator believes the write landed and
only finds out when the behaviour they were configuring never happens.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lionagi.studio.services.schedules import (
    CreateScheduleRequest,
    UpdateScheduleRequest,
    update_schedule_route,
)


def test_update_rejects_a_field_it_cannot_apply():
    """``enabled`` is in the store's allowlist but not on this model.

    It has dedicated enable/disable routes, so its absence here is a choice.
    Answering ok to a PATCH that sets it is not.
    """
    with pytest.raises(ValidationError) as exc:
        UpdateScheduleRequest(enabled=True)
    assert "enabled" in str(exc.value)


def test_update_rejects_a_plain_typo():
    with pytest.raises(ValidationError) as exc:
        UpdateScheduleRequest(github_cursors="2026-07-26T00:00:00Z")
    assert "github_cursors" in str(exc.value)


def test_create_rejects_an_unknown_field_too():
    """A schedule created without the option you asked for is the same defect.

    Both models are now consistent with the declaration models next door,
    which have forbidden extras all along.
    """
    with pytest.raises(ValidationError) as exc:
        CreateScheduleRequest(
            name="t", trigger_type="cron", action_kind="agent", polling_interval_sec=60
        )
    assert "polling_interval_sec" in str(exc.value)


def test_a_declared_field_still_passes():
    """The guard must not reject the fields the route exists to accept."""
    req = UpdateScheduleRequest(github_cursor="2026-07-26T00:00:00Z")
    assert req.model_dump(exclude_unset=True) == {"github_cursor": "2026-07-26T00:00:00Z"}


def test_an_explicit_null_still_passes_through():
    """exclude_unset, not exclude_none: clearing a field is a real request."""
    req = UpdateScheduleRequest(github_cursor=None)
    assert req.model_dump(exclude_unset=True) == {"github_cursor": None}


@pytest.mark.asyncio
async def test_the_response_names_what_landed(monkeypatch):
    seen: dict = {}

    async def fake_update(schedule_id, fields):
        seen["fields"] = fields
        return True

    monkeypatch.setattr("lionagi.studio.services.schedules.update_schedule", fake_update)
    body = UpdateScheduleRequest(github_cursor="2026-07-26T00:00:00Z", name="renamed")
    result = await update_schedule_route("sched-001", body)

    assert result == {"ok": True, "updated": ["github_cursor", "name"]}
    assert seen["fields"] == {"github_cursor": "2026-07-26T00:00:00Z", "name": "renamed"}


@pytest.mark.asyncio
async def test_an_empty_body_is_still_a_no_op_but_says_so(monkeypatch):
    """An empty PATCH remains legitimate; it just stops looking like a change."""

    async def fake_update(schedule_id, fields):
        return True

    monkeypatch.setattr("lionagi.studio.services.schedules.update_schedule", fake_update)
    result = await update_schedule_route("sched-001", UpdateScheduleRequest())

    assert result == {"ok": True, "updated": []}
