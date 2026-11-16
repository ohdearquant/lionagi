# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A kill records cancellation only for the session execution it observed."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import lionhub.orchestration.kill as kill_service
from lionagi.state import db as db_mod
from lionagi.state.compat import StateCompatibilityRefused
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionhub.serve.cli import kill as kill_mod
from tests._kill_cancel_guards_test_support import _session as _session


@pytest.mark.asyncio
async def test_recursive_child_conflict_reports_signal_and_returns_failure(
    tmp_path, monkeypatch, capsys, caplog
):
    patched_degraded_run = lambda _sid: None
    monkeypatch.setattr(kill_mod, "_degraded_run", patched_degraded_run)
    monkeypatch.setattr(kill_service, "_degraded_run", patched_degraded_run)
    signals = []

    def terminate(pid, **_kwargs):
        signals.append(pid)
        return "sigterm"

    monkeypatch.setattr(kill_service, "_terminate_pid", terminate)
    async with StateDB(tmp_path / "state.db") as db:
        parent = await _session(db, generation=str(uuid.uuid4()), metadata={})
        child_generation = str(uuid.uuid4())
        child = await _session(
            db,
            generation=child_generation,
            metadata={"pid": 4242, "pid_create_time": 100.0},
        )
        parent_row = await db.get_session(parent)
        child_row = await db.get_session(child)
        new_generation = str(uuid.uuid4())
        guard = db.cancel_session_if_worker_generation
        calls = []

        async def resume_child_before_guard(session_id, generation, **kwargs):
            calls.append((session_id, generation))
            if session_id == child:
                await db.update_session(child, cli_worker_generation=new_generation)
            return await guard(session_id, generation, **kwargs)

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", resume_child_before_guard)

        @asynccontextmanager
        async def existing_db():
            yield db

        monkeypatch.setattr(db_mod, "StateDB", existing_db)
        monkeypatch.setattr(
            kill_mod, "_resolve_entity", AsyncMock(return_value=("sessions", "session", parent_row))
        )
        patched_list_running_children = AsyncMock(return_value=[("sessions", "session", child_row)])
        monkeypatch.setattr(
            kill_mod,
            "list_running_children",
            patched_list_running_children,
        )
        monkeypatch.setattr(
            kill_service,
            "list_running_children",
            patched_list_running_children,
        )
        monkeypatch.setattr(kill_mod, "_try_recorded_worker_cleanup", AsyncMock(return_value=None))

        rc = await kill_mod._do_kill(parent, recursive=True)

        output = capsys.readouterr()
        text = (output.out + output.err + caplog.text).lower()
        assert rc == 1
        assert child[:12] in text
        assert "child session" in text
        assert "signal=sigterm" in text
        assert "cancellation was not recorded" in text
        assert "changed" in text
        assert signals == [4242]
        assert (child, child_generation) in calls
        after = await db.get_session(child)
        assert after["status"] == "running"
        assert after["cli_worker_generation"] == new_generation
        assert not await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? AND source = 'admin'",
            (child,),
        )
