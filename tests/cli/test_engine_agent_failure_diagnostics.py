# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the CLI half of total sub-agent failure diagnostics: a run where
every agent terminally errored (e.g. missing API key) must write status
'failed' to engine_runs, with the agent errors folded into the error column —
not silently report 'completed'."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/cli/test_engine_emission_diagnostics.py)
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
# Engine CLI: _total_agent_failure → status='failed', errors in error column
# ---------------------------------------------------------------------------


async def test_total_agent_failure_written_to_db_as_failed(monkeypatch):
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    mock_engine = MagicMock()
    mock_engine._emission_failures = []
    mock_engine._total_agent_failure = True
    mock_engine._agent_errors = [
        "worker-1: API key is required",
        "worker-2: API key is required",
    ]

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return ""

    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    mock_db = MockStateDB()
    monkeypatch.setattr(db_mod, "StateDB", lambda: mock_db)

    args = _build_args(kind="research", spec="GQA", no_persist=False)
    await engine_mod._do_engine_run(args)

    failed = [c for c in mock_db.update_calls if c["status"] == "failed"]
    assert failed, f"no failed update; calls={mock_db.update_calls}"
    assert not [c for c in mock_db.update_calls if c["status"] == "completed"], (
        "total agent failure must not also write status='completed'"
    )
    error_val = failed[0]["error"]
    assert error_val is not None
    assert "all sub-agents failed" in error_val
    assert "worker-1" in error_val and "worker-2" in error_val


async def test_total_agent_failure_folds_with_emission_failures(monkeypatch):
    """Both diagnostics (emission_missing + agent errors) must land in the
    same error column when a run hits both."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    mock_engine = MagicMock()
    mock_engine._emission_failures = ["synthesizer x1"]
    mock_engine._total_agent_failure = True
    mock_engine._agent_errors = ["worker-1: API key is required"]

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return ""

    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    mock_db = MockStateDB()
    monkeypatch.setattr(db_mod, "StateDB", lambda: mock_db)

    args = _build_args(kind="research", spec="GQA", no_persist=False)
    await engine_mod._do_engine_run(args)

    failed = [c for c in mock_db.update_calls if c["status"] == "failed"]
    assert failed
    error_val = failed[0]["error"]
    assert "emission_missing" in error_val
    assert "all sub-agents failed" in error_val
    assert "synthesizer" in error_val
    assert "worker-1" in error_val


async def test_no_total_agent_failure_status_stays_completed(monkeypatch):
    """A run with some successful agents (no _total_agent_failure) must still
    report 'completed' — this diagnostic only fires on total failure."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    mock_engine = MagicMock()
    mock_engine._emission_failures = []
    mock_engine._total_agent_failure = False
    mock_engine._agent_errors = []

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
    assert completed
    assert completed[0]["error"] is None


async def test_engine_without_total_agent_failure_attr_stays_completed(monkeypatch):
    """Engine objects without the _total_agent_failure attr (older subclass,
    or an engine that never ran make_agent) must not crash and must not be
    treated as a total failure."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    mock_engine = MagicMock(spec=[])  # no _total_agent_failure / _agent_errors attrs

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
# INTEGRATION: real Engine subclass whose _run() drives every agent to error
# — verifies the full Engine.run() → CLI read-site handoff for the total
# agent-failure signal (the same integration shape as the emission-failure
# test in test_engine_emission_diagnostics.py).
# ---------------------------------------------------------------------------


async def _build_all_agents_failed_engine():
    from lionagi.engines.engine import Engine, EngineRun

    class AllAgentsFailedEngine(Engine):
        async def _run(self, run: EngineRun, spec: str, **kwargs) -> str:  # type: ignore[override]
            run.agents_made = 2
            run.notify("agent_error", agent="planner", error="API key is required")
            run.notify("agent_error", agent="worker", error="API key is required")
            return ""

    return AllAgentsFailedEngine(max_agents=5)


async def test_real_engine_total_agent_failure_propagates_to_cli(monkeypatch):
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    real_engine = await _build_all_agents_failed_engine()
    MockEngineClass = MagicMock(return_value=real_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    mock_db = MockStateDB()
    monkeypatch.setattr(db_mod, "StateDB", lambda: mock_db)

    args = _build_args(kind="research", spec="test topic", no_persist=False)
    rc = await engine_mod._do_engine_run(args)

    # Total agent failure must exit non-zero so shell/CI callers see it, not
    # just the persisted DB status.
    assert rc == 1
    failed = [c for c in mock_db.update_calls if c["status"] == "failed"]
    assert failed, f"no failed update; all calls: {mock_db.update_calls}"
    error_val = failed[0]["error"]
    assert error_val is not None
    assert "all sub-agents failed" in error_val
    assert "planner" in error_val and "worker" in error_val
