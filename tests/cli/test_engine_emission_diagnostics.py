# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for engine emission-missing diagnostics written to engine_runs DB (issue #1397).

Covers:
  - Zero-emission run → engine_runs row has status='completed' AND
    error containing 'emission_missing'
  - Normal run (no emission failures) → error column stays NULL
  - EngineRun._emission_failures accumulates correctly
  - EngineRun.operate_with_repair notifies and records emission_missing
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_args(**kwargs) -> argparse.Namespace:
    defaults = {
        "command": "engine",
        "engine_command": "run",
        "kind": "research",
        "spec": "What is GQA?",
        "test_cmd": None,
        "export_dir": None,
        "model": None,
        "max_depth": None,
        "max_agents": None,
        "session_id": None,
        "no_persist": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class MockStateDB:
    """Minimal StateDB mock that records insert and update calls."""

    def __init__(self):
        self.insert_calls: list[dict] = []
        self.update_calls: list[dict] = []

    async def open(self):
        pass

    async def close(self):
        pass

    async def insert_engine_run(self, *, run_id, kind, spec_json, started_at, session_id=None):
        self.insert_calls.append({"run_id": run_id, "kind": kind})

    async def update_engine_run(
        self, run_id, *, status, ended_at=None, export_dir=None, error=None
    ):
        self.update_calls.append({"run_id": run_id, "status": status, "error": error})


# ---------------------------------------------------------------------------
# Engine CLI: emission_missing events → error column on completed row
# ---------------------------------------------------------------------------


async def test_emission_failures_written_to_db_error_on_completed(monkeypatch):
    """When an engine run completes but emission failures occurred, the DB row
    must have status='completed' AND a non-None error with 'emission_missing'."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    # Engine that returns a result but has _emission_failures populated.
    mock_engine = MagicMock()
    mock_engine._emission_failures = ["planner x2", "summariser x1"]

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return "partial result despite missing emissions"

    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    mock_db = MockStateDB()
    monkeypatch.setattr(db_mod, "StateDB", lambda: mock_db)

    args = _build_args(kind="research", spec="GQA", no_persist=False)
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    completed = [c for c in mock_db.update_calls if c["status"] == "completed"]
    assert completed, f"no completed update; calls={mock_db.update_calls}"
    error_val = completed[0]["error"]
    assert error_val is not None, "emission failures must be written to error column"
    assert "emission_missing" in error_val, (
        f"error column must contain 'emission_missing'; got: {error_val!r}"
    )
    assert "planner" in error_val or "summariser" in error_val, (
        f"error column must name the failing agents; got: {error_val!r}"
    )


async def test_no_emission_failures_error_column_stays_null(monkeypatch):
    """A clean run with no emission failures must leave error=None in the DB."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    mock_engine = MagicMock()
    mock_engine._emission_failures = []  # no failures

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return "clean result"

    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    mock_db = MockStateDB()
    monkeypatch.setattr(db_mod, "StateDB", lambda: mock_db)

    args = _build_args(kind="research", spec="GQA", no_persist=False)
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    completed = [c for c in mock_db.update_calls if c["status"] == "completed"]
    assert completed, f"no completed update; calls={mock_db.update_calls}"
    assert completed[0]["error"] is None, (
        f"clean run must have error=None; got: {completed[0]['error']!r}"
    )


async def test_engine_without_emission_failures_attr_error_column_stays_null(monkeypatch):
    """Engine objects without _emission_failures attr (older subclass) must not crash."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    mock_engine = MagicMock(spec=[])  # spec=[] means no _emission_failures attr

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return "result"

    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    mock_db = MockStateDB()
    monkeypatch.setattr(db_mod, "StateDB", lambda: mock_db)

    args = _build_args(kind="research", spec="GQA", no_persist=False)
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    completed = [c for c in mock_db.update_calls if c["status"] == "completed"]
    assert completed
    assert completed[0]["error"] is None


# ---------------------------------------------------------------------------
# EngineRun._emission_failures accumulation
# ---------------------------------------------------------------------------


def _make_minimal_engine_run() -> Any:
    """Build a minimal EngineRun with a no-op on_event."""
    from unittest.mock import MagicMock

    from lionagi.engines.engine import EngineRun

    engine = MagicMock()
    engine.max_concurrent = 1
    engine.max_agents = 100
    engine.deadline_s = None
    engine.model = None
    run = EngineRun(engine, on_event=None)
    return run


def test_engine_run_emission_failures_starts_empty():
    er = _make_minimal_engine_run()
    assert er._emission_failures == []


async def test_operate_with_repair_records_emission_failure(monkeypatch):
    """When arrived() never returns True and retries are exhausted,
    _emission_failures must contain a descriptive entry."""
    from lionagi.engines.engine import EngineRun

    engine = MagicMock()
    engine.max_concurrent = 1
    engine.max_agents = 100
    engine.deadline_s = None
    engine.model = None

    er = EngineRun(engine, on_event=None)

    # Branch mock: operate always returns None-ish
    branch = MagicMock()
    branch.name = "planner"
    branch.chat_model = MagicMock()
    branch.chat_model.is_cli = False

    async def fake_operate(instruction=None, **kw):
        return None

    branch.operate = fake_operate

    arrived_count = [0]

    def never_arrived():
        return False

    await er.operate_with_repair(
        branch,
        "do work",
        arrived=never_arrived,
        emits=(),
        retries=2,
    )

    assert len(er._emission_failures) == 1, (
        f"expected 1 emission_failure entry, got {er._emission_failures}"
    )
    entry = er._emission_failures[0]
    assert "planner" in entry, f"agent name must be in entry; got: {entry!r}"
    assert "x" in entry, f"attempt count must be in entry; got: {entry!r}"


async def test_operate_with_repair_no_failure_when_arrived(monkeypatch):
    """When arrived() returns True on the first try, no failure is recorded."""
    from lionagi.engines.engine import EngineRun

    engine = MagicMock()
    engine.max_concurrent = 1
    engine.max_agents = 100
    engine.deadline_s = None
    engine.model = None

    er = EngineRun(engine, on_event=None)

    branch = MagicMock()
    branch.name = "worker"
    branch.chat_model = MagicMock()
    branch.chat_model.is_cli = False

    async def fake_operate(instruction=None, **kw):
        return "emission ok"

    branch.operate = fake_operate

    def always_arrived():
        return True

    await er.operate_with_repair(
        branch,
        "do work",
        arrived=always_arrived,
        emits=(),
        retries=2,
    )

    assert er._emission_failures == [], (
        f"no failure should be recorded when arrived=True; got: {er._emission_failures}"
    )


# ---------------------------------------------------------------------------
# Real StateDB: completed + error coexist in engine_runs table
# ---------------------------------------------------------------------------


async def test_completed_run_with_error_column_in_real_db(tmp_path):
    """Verify the real StateDB allows writing error='emission_missing: x2' with
    status='completed' — confirms no DB-level constraint prevents it."""
    aiosqlite = pytest.importorskip("aiosqlite")

    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    import uuid

    rid = uuid.uuid4().hex

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind="research",
            spec_json={"topic": "test"},
            started_at=1000.0,
        )
        await db.update_engine_run(
            rid,
            status="completed",
            ended_at=1100.0,
            error="emission_missing: planner x2; summariser x1",
        )
        row = await db.get_engine_run(rid)

    assert row is not None
    assert row["status"] == "completed"
    assert row["error"] == "emission_missing: planner x2; summariser x1", (
        f"error column must persist alongside 'completed' status; got: {row['error']!r}"
    )
