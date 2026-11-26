# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Checkpoint reader scheduling at the CLI, Studio, and MCP boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event, Thread

import anyio
import pytest

from lionhub.orchestration.flow import checkpoint
from tests._checkpoint_reader_versions_test_support import _RUN_ID, _base, _write_fixture

pytestmark = pytest.mark.asyncio
_WAIT_SECONDS = 5


class _SyntheticState:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_sessions_for_run(self, run_id):
        assert run_id == _RUN_ID
        return [{"id": "synthetic-session", "updated_at": 1}]


def _checkpoint_fixture(tmp_path, monkeypatch):
    data = _base(2)
    data["plan"] = [{"agent_id": "worker"}]
    data["config"] = {"no_mcp_config": True}
    monkeypatch.setattr(checkpoint, "RUNS_ROOT", tmp_path)
    return _write_fixture(tmp_path, json.dumps(data)), data


async def _while_checkpoint_read_is_held(path, monkeypatch, call):
    loop = asyncio.get_running_loop()
    read_text = Path.read_text
    entered = Event()
    release = Event()
    callback_before_release = Event()
    reads = []

    def callback():
        if not release.is_set():
            callback_before_release.set()
        release.set()

    def watchdog():
        if entered.wait(_WAIT_SECONDS) and not release.is_set():
            loop.call_soon_threadsafe(callback)
            release.wait(_WAIT_SECONDS)
        release.set()

    def controlled_read(file, *args, **kwargs):
        if file == path:
            reads.append(file)
            entered.set()
            assert release.wait(2 * _WAIT_SECONDS), "checkpoint read was not released"
        return read_text(file, *args, **kwargs)

    # The watchdog can release an inline read even when that read blocks the loop.
    monitor = Thread(target=watchdog)
    monitor.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", controlled_read)
            result = await call()
    finally:
        release.set()
        entered.set()
        monitor.join(_WAIT_SECONDS)
    assert not monitor.is_alive()
    assert reads == [path]
    assert callback_before_release.is_set(), "checkpoint read blocked the event-loop callback"
    return result


async def test_cli_resume_allows_callback_during_checkpoint_read(tmp_path, monkeypatch):
    from lionhub.serve.cli.orchestrate import flow

    path, data = _checkpoint_fixture(tmp_path, monkeypatch)

    async def replay(**kwargs):
        assert kwargs["resume_checkpoint"] == data
        return "synthetic replay", "completed"

    monkeypatch.setattr(flow, "_run_flow", replay)
    result = await _while_checkpoint_read_is_held(
        path, monkeypatch, lambda: flow._resume_flow(_RUN_ID)
    )
    assert result == ("synthetic replay", "completed")


async def test_session_fallback_allows_callback_during_checkpoint_read(tmp_path, monkeypatch):
    from lionhub.orchestration import inspection
    from lionhub.state import db as state_db

    path, data = _checkpoint_fixture(tmp_path, monkeypatch)

    async def find_target(_db, target):
        assert target == "synthetic-session"
        return "session", {"id": target}

    async def find_session(_db, entity_type, row):
        assert entity_type == "session"
        return {**row, "node_metadata": {"run_id": _RUN_ID}}

    monkeypatch.setattr(state_db, "StateDB", _SyntheticState)
    monkeypatch.setattr(inspection, "resolve_any_target", find_target)
    monkeypatch.setattr(inspection, "resolve_primary_session", find_session)
    run_dir, result = await _while_checkpoint_read_is_held(
        path, monkeypatch, lambda: checkpoint.resolve_checkpoint_target("synthetic-session")
    )
    assert run_dir.run_id == _RUN_ID
    assert result == data


async def test_studio_preflight_allows_callback_during_checkpoint_read(tmp_path, monkeypatch):
    from lionhub.serve.daemon.services import run_resume

    path, data = _checkpoint_fixture(tmp_path, monkeypatch)
    run_dir, result = await _while_checkpoint_read_is_held(
        path, monkeypatch, lambda: run_resume._resolve_flow_checkpoint(_RUN_ID)
    )
    assert run_dir.run_id == _RUN_ID
    assert result == data


def _stub_mcp_session(tmp_path, monkeypatch):
    from lionagi import _paths
    from lionhub.serve.daemon.services import sessions
    from lionhub.state import db as state_db

    async def get_session(session_id):
        assert session_id == "synthetic-session"
        return {"graph": {"nodes": [{"id": "base-child"}]}, "branches": [], "segments": []}

    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(state_db, "StateDB", _SyntheticState)
    monkeypatch.setattr(sessions, "get_session", get_session)


async def test_mcp_run_detail_allows_callback_during_checkpoint_read(tmp_path, monkeypatch):
    from lionhub.serve.mcp import _run_detail

    path, _data = _checkpoint_fixture(tmp_path, monkeypatch)
    _stub_mcp_session(tmp_path, monkeypatch)
    detail = await _while_checkpoint_read_is_held(
        path, monkeypatch, lambda: _run_detail.build_run_detail(_RUN_ID)
    )
    assert detail["nodes"][0]["id"] == "base-child"
    assert detail["nodes"][0]["spawned_by"] == "base-parent"


async def test_checkpoint_cancellation_preserves_shared_resume_and_mcp_capacity(
    tmp_path, monkeypatch
):
    from lionhub.serve.mcp import _run_detail

    path, data = _checkpoint_fixture(tmp_path, monkeypatch)
    _stub_mcp_session(tmp_path, monkeypatch)
    for run_id in ("first", "second", "queued"):
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        (run_dir / "checkpoint.json").write_text(json.dumps(data), encoding="utf-8")

    loop = asyncio.get_running_loop()
    limiter = checkpoint.CHECKPOINT_READ_LIMIT
    assert limiter.total_tokens == 2
    assert limiter.borrowed_tokens == 0
    entered = {run_id: asyncio.Event() for run_id in ("first", "second")}
    release = {run_id: Event() for run_id in entered}
    first_settled = Event()
    queued_started = Event()
    mcp_reads_after_settlement = []
    tasks = []
    load_for_run = checkpoint._load_checkpoint_for_run
    read_text = Path.read_text

    def controlled_load(run_id):
        if run_id == "queued":
            queued_started.set()
            return load_for_run(run_id)
        loop.call_soon_threadsafe(entered[run_id].set)
        try:
            assert release[run_id].wait(2 * _WAIT_SECONDS), "checkpoint read was not released"
            return load_for_run(run_id)
        finally:
            if run_id == "first":
                first_settled.set()

    def observe_read(file, *args, **kwargs):
        if file == path:
            mcp_reads_after_settlement.append(first_settled.is_set())
        return read_text(file, *args, **kwargs)

    def start(call):
        task = asyncio.create_task(call)
        tasks.append(task)
        return task

    monkeypatch.setattr(checkpoint, "_load_checkpoint_for_run", controlled_load)
    monkeypatch.setattr(Path, "read_text", observe_read)
    try:
        with anyio.fail_after(_WAIT_SECONDS):
            first = start(checkpoint.resolve_checkpoint_target("first"))
            second = start(checkpoint.resolve_checkpoint_target("second"))
            await entered["first"].wait()
            await entered["second"].wait()
            assert limiter.borrowed_tokens == 2

            queued = start(checkpoint.resolve_checkpoint_target("queued"))
            await anyio.wait_all_tasks_blocked()
            assert limiter._lim.statistics().tasks_waiting == 1
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert limiter._lim.statistics().tasks_waiting == 0
            assert not queued_started.is_set()

            first.cancel()
            mcp = start(_run_detail.build_run_detail(_RUN_ID))
            await anyio.wait_all_tasks_blocked()
            assert limiter._lim.statistics().tasks_waiting == 1
            assert limiter.borrowed_tokens == 2
            assert not first.done()
            assert not first_settled.is_set()
            assert not mcp_reads_after_settlement

            release["first"].set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert first_settled.is_set()
            detail = await mcp
            assert mcp_reads_after_settlement == [True]
            assert detail["nodes"][0]["spawned_by"] == "base-parent"
            assert limiter._lim.statistics().tasks_waiting == 0
            assert limiter.borrowed_tokens == 1

            release["second"].set()
            run_dir, resolved = await second
            assert run_dir.run_id == "second"
            assert resolved == data
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert not queued_started.is_set()
    assert limiter.borrowed_tokens == 0
