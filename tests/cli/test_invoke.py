# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from lionagi.cli.invoke import _end_invocation
from lionagi.state.reasons import RunReasons


async def test_end_invocation_writes_terminal_status_and_ended_at_through_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {"id": "invocation-1", "status": "running", "node_metadata": {"kept": True}}
    updated = {**existing, "status": "completed", "ended_at": 123.0}

    class FakeStateDB:
        instance = None

        def __init__(self):
            self.get_invocation = AsyncMock(side_effect=[existing, updated])
            self.update_invocation = AsyncMock()
            self.update_status = AsyncMock(return_value=True)
            type(self).instance = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("lionagi.state.db.StateDB", FakeStateDB)
    monkeypatch.setattr("lionagi.cli.invoke.time.time", lambda: 123.0)

    result = await _end_invocation(
        "invocation-1",
        status="completed",
        metadata={"added": "value"},
    )

    db = FakeStateDB.instance
    db.update_invocation.assert_awaited_once_with(
        "invocation-1", node_metadata={"kept": True, "added": "value"}
    )
    db.update_status.assert_awaited_once_with(
        "invocation",
        "invocation-1",
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_summary="Invocation completed.",
        source="executor",
        actor="invocation-1",
        extra_fields={"ended_at": 123.0},
    )
    assert result == updated
