# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li engine run` CLI subcommand (Phase C Move 2).

Coverage targets:
  - add_engine_subparser wires all expected engine kinds
  - Missing --test-cmd for 'coding' kind returns exit 1
  - Valid arg parsing for each kind
  - Engine execution dispatches to the correct class (mocked)
  - run_engine returns 0 on success and emits JSON result on stdout
  - run_engine returns 1 on engine failure
  - StateDB insert/update called on success and failure paths
  - --no-persist skips DB writes
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_args(**kwargs) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for engine run tests."""
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
        "no_persist": True,  # skip DB by default in unit tests
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def test_add_engine_subparser_registers_engine_command():
    """add_engine_subparser must register 'engine' as a valid subcommand."""
    from lionagi.cli.engine import add_engine_subparser

    top = argparse.ArgumentParser(prog="li")
    sub = top.add_subparsers(dest="command")
    add_engine_subparser(sub)

    # Should parse without error.
    args = top.parse_args(["engine", "run", "research", "What is GQA?"])
    assert args.command == "engine"
    assert args.kind == "research"
    assert args.spec == "What is GQA?"


def test_all_engine_kinds_are_valid():
    """Every expected kind is accepted by the parser."""
    from lionagi.cli.engine import _KIND_META, add_engine_subparser

    top = argparse.ArgumentParser(prog="li")
    sub = top.add_subparsers(dest="command")
    add_engine_subparser(sub)

    for kind in _KIND_META:
        args = top.parse_args(["engine", "run", kind, f"input for {kind}"])
        assert args.kind == kind, f"kind={kind} was not parsed correctly"


def test_invalid_kind_raises_parser_error():
    """An unknown kind must cause argparse to exit (SystemExit)."""
    from lionagi.cli.engine import add_engine_subparser

    top = argparse.ArgumentParser(prog="li")
    sub = top.add_subparsers(dest="command")
    add_engine_subparser(sub)

    with pytest.raises(SystemExit):
        top.parse_args(["engine", "run", "nonexistent-kind", "some input"])


def test_coding_kind_accepts_test_cmd():
    """--test-cmd is stored correctly on the parsed args."""
    from lionagi.cli.engine import add_engine_subparser

    top = argparse.ArgumentParser(prog="li")
    sub = top.add_subparsers(dest="command")
    add_engine_subparser(sub)

    args = top.parse_args(["engine", "run", "coding", "impl BFS", "--test-cmd", "pytest tests/"])
    assert args.kind == "coding"
    assert args.test_cmd == "pytest tests/"


def test_engine_run_accepts_model_and_depth_flags():
    """--model and --max-depth are stored correctly."""
    from lionagi.cli.engine import add_engine_subparser

    top = argparse.ArgumentParser(prog="li")
    sub = top.add_subparsers(dest="command")
    add_engine_subparser(sub)

    args = top.parse_args(
        [
            "engine",
            "run",
            "planning",
            "Build a REST API",
            "--model",
            "claude/sonnet",
            "--max-depth",
            "5",
        ]
    )
    assert args.model == "claude/sonnet"
    assert args.max_depth == 5


# ---------------------------------------------------------------------------
# Coding kind: missing --test-cmd → exit 1
# ---------------------------------------------------------------------------


async def test_coding_without_test_cmd_returns_1(monkeypatch):
    """'coding' kind without --test-cmd must return exit code 1."""
    import lionagi.cli._logging as log_mod

    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)

    from lionagi.cli.engine import _do_engine_run

    args = _build_args(kind="coding", spec="impl BFS", test_cmd=None, no_persist=True)
    result = await _do_engine_run(args)
    assert result == 1


# ---------------------------------------------------------------------------
# Successful engine run (mocked engine)
# ---------------------------------------------------------------------------


async def test_successful_engine_run_returns_0(monkeypatch, capsys):
    """A mocked engine that returns a string → exit 0 + JSON on stdout."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    # Patch _import_engine_class to return a mock engine class.
    async def _mock_run(spec, *, on_event=None, **kwargs):
        if on_event:
            on_event({"type": "thinking", "detail": "deep dive"})
        return "This is the research result."

    mock_engine = MagicMock()
    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    args = _build_args(kind="research", spec="What is GQA?", no_persist=True)
    result = await engine_mod._do_engine_run(args)

    assert result == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["result"] == "This is the research result."


async def test_engine_failure_returns_1(monkeypatch):
    """A mocked engine that raises → exit 1."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)

    async def _mock_run_fail(spec, *, on_event=None, **kwargs):
        raise RuntimeError("LLM unavailable")

    mock_engine = MagicMock()
    mock_engine.run = _mock_run_fail
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    args = _build_args(kind="planning", spec="Build something", no_persist=True)
    result = await engine_mod._do_engine_run(args)
    assert result == 1


# ---------------------------------------------------------------------------
# Pydantic model result — CodeResultRecorded real shape
# ---------------------------------------------------------------------------


async def test_code_result_recorded_shape_serialized(monkeypatch, capsys):
    """CodingEngine returns CodeResultRecorded (passed/measurements/caveats/
    experiment_ref/verdict_ref) — NOT a dict with export_dir.  Verify the CLI
    serialises that real shape and does NOT crash when export_dir is absent."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    # Simulate the real CodeResultRecorded.model_dump() output — no export_dir field.
    real_coding_dump = {
        "passed": True,
        "measurements": {"rounds": 1, "test_runs": 1, "returncode": 0},
        "caveats": [],
        "experiment_ref": "",
        "verdict_ref": "",
    }

    pydantic_result = MagicMock()
    pydantic_result.model_dump = MagicMock(return_value=real_coding_dump)

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return pydantic_result

    mock_engine = MagicMock()
    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    args = _build_args(
        kind="coding",
        spec="impl BFS",
        test_cmd="pytest",
        no_persist=True,
    )
    result = await engine_mod._do_engine_run(args)

    assert result == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["passed"] is True
    assert "measurements" in output
    # No export_dir in CodeResultRecorded, no args.export_dir → None in DB (verified
    # in export_dir persistence tests below).


# ---------------------------------------------------------------------------
# export_dir persistence — sourced from args, not result model
# ---------------------------------------------------------------------------


async def test_export_dir_persisted_from_args_coding(monkeypatch, capsys):
    """--export-dir must be written to the DB for 'coding' even though
    CodeResultRecorded has no export_dir field (confirmed real shape above)."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    # Real CodeResultRecorded dump — NO export_dir key.
    real_coding_dump = {
        "passed": True,
        "measurements": {"rounds": 1},
        "caveats": [],
        "experiment_ref": "",
        "verdict_ref": "",
    }
    pydantic_result = MagicMock()
    pydantic_result.model_dump = MagicMock(return_value=real_coding_dump)

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return pydantic_result

    mock_engine = MagicMock()
    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    update_calls: list[dict] = []

    class MockStateDB:
        async def open(self):
            pass

        async def close(self):
            pass

        async def insert_engine_run(self, *, run_id, kind, spec_json, started_at, session_id=None):
            pass

        async def update_engine_run(
            self, run_id, *, status, ended_at=None, export_dir=None, error=None
        ):
            update_calls.append({"status": status, "export_dir": export_dir})

    monkeypatch.setattr(db_mod, "StateDB", MockStateDB)

    args = _build_args(
        kind="coding",
        spec="impl BFS",
        test_cmd="pytest tests/",
        export_dir="/tmp/real-export",
        no_persist=False,
    )
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    completed = [c for c in update_calls if c["status"] == "completed"]
    assert completed, "no completed update call"
    assert completed[0]["export_dir"] == "/tmp/real-export", (
        f"expected /tmp/real-export, got {completed[0]['export_dir']!r}"
    )


async def test_export_dir_persisted_from_args_hypothesis(monkeypatch, capsys):
    """--export-dir must be written to the DB for 'hypothesis'; the hypothesis
    engine returns a plain string, not a Pydantic model."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    async def _mock_run(spec, *, on_event=None, **kwargs):
        # Hypothesis engine returns a plain string report.
        return "Hypothesis: X causes Y because Z."

    mock_engine = MagicMock()
    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    update_calls: list[dict] = []

    class MockStateDB:
        async def open(self):
            pass

        async def close(self):
            pass

        async def insert_engine_run(self, *, run_id, kind, spec_json, started_at, session_id=None):
            pass

        async def update_engine_run(
            self, run_id, *, status, ended_at=None, export_dir=None, error=None
        ):
            update_calls.append({"status": status, "export_dir": export_dir})

    monkeypatch.setattr(db_mod, "StateDB", MockStateDB)

    args = _build_args(
        kind="hypothesis",
        spec="Finding: X causes Y",
        export_dir="/tmp/hypo-export",
        no_persist=False,
    )
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    completed = [c for c in update_calls if c["status"] == "completed"]
    assert completed, "no completed update call"
    assert completed[0]["export_dir"] == "/tmp/hypo-export", (
        f"expected /tmp/hypo-export, got {completed[0]['export_dir']!r}"
    )


async def test_export_dir_none_when_not_passed(monkeypatch, capsys):
    """No --export-dir → export_dir=None in the DB update (not a stale string)."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return "Research findings."

    mock_engine = MagicMock()
    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    update_calls: list[dict] = []

    class MockStateDB:
        async def open(self):
            pass

        async def close(self):
            pass

        async def insert_engine_run(self, *, run_id, kind, spec_json, started_at, session_id=None):
            pass

        async def update_engine_run(
            self, run_id, *, status, ended_at=None, export_dir=None, error=None
        ):
            update_calls.append({"status": status, "export_dir": export_dir})

    monkeypatch.setattr(db_mod, "StateDB", MockStateDB)

    args = _build_args(kind="research", spec="GQA", no_persist=False, export_dir=None)
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    completed = [c for c in update_calls if c["status"] == "completed"]
    assert completed[0]["export_dir"] is None


# ---------------------------------------------------------------------------
# Cancellation handling — BaseException paths
# ---------------------------------------------------------------------------


async def test_cancelled_error_marks_row_cancelled(monkeypatch):
    """asyncio.CancelledError → row status='cancelled', db closed, error re-raised."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)

    async def _mock_run_cancel(spec, *, on_event=None, **kwargs):
        raise asyncio.CancelledError("test cancel")

    mock_engine = MagicMock()
    mock_engine.run = _mock_run_cancel
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    insert_calls: list[dict] = []
    update_calls: list[dict] = []
    close_count = [0]

    class MockStateDB:
        async def open(self):
            pass

        async def close(self):
            close_count[0] += 1

        async def insert_engine_run(self, *, run_id, kind, spec_json, started_at, session_id=None):
            insert_calls.append({"run_id": run_id, "kind": kind})

        async def update_engine_run(
            self, run_id, *, status, ended_at=None, export_dir=None, error=None
        ):
            update_calls.append({"run_id": run_id, "status": status, "error": error})

    monkeypatch.setattr(db_mod, "StateDB", MockStateDB)

    args = _build_args(kind="research", spec="test", no_persist=False)
    with pytest.raises(asyncio.CancelledError):
        await engine_mod._do_engine_run(args)

    # Row must be marked 'cancelled', not left as 'running'.
    assert len(insert_calls) == 1
    cancelled_updates = [c for c in update_calls if c["status"] == "cancelled"]
    assert cancelled_updates, f"expected a 'cancelled' update; got {update_calls}"
    assert cancelled_updates[0]["error"] is not None
    # DB must be closed.
    assert close_count[0] == 1


async def test_keyboard_interrupt_marks_row_cancelled(monkeypatch):
    """KeyboardInterrupt → row status='cancelled', db closed, re-raised."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)

    async def _mock_run_interrupt(spec, *, on_event=None, **kwargs):
        raise KeyboardInterrupt("SIGINT simulation")

    mock_engine = MagicMock()
    mock_engine.run = _mock_run_interrupt
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    update_calls: list[dict] = []
    close_count = [0]

    class MockStateDB:
        async def open(self):
            pass

        async def close(self):
            close_count[0] += 1

        async def insert_engine_run(self, *, run_id, kind, spec_json, started_at, session_id=None):
            pass

        async def update_engine_run(
            self, run_id, *, status, ended_at=None, export_dir=None, error=None
        ):
            update_calls.append({"status": status})

    monkeypatch.setattr(db_mod, "StateDB", MockStateDB)

    args = _build_args(kind="planning", spec="test", no_persist=False)
    with pytest.raises(KeyboardInterrupt):
        await engine_mod._do_engine_run(args)

    cancelled = [c for c in update_calls if c["status"] == "cancelled"]
    assert cancelled, f"expected 'cancelled' update; got {update_calls}"
    assert close_count[0] == 1


# ---------------------------------------------------------------------------
# StateDB persistence paths
# ---------------------------------------------------------------------------


async def test_db_insert_called_on_success(monkeypatch, capsys):
    """With no_persist=False, insert_engine_run and update_engine_run are called."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return "done"

    mock_engine = MagicMock()
    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    insert_calls: list[dict] = []
    update_calls: list[dict] = []

    class MockStateDB:
        async def open(self):
            pass

        async def close(self):
            pass

        async def insert_engine_run(self, *, run_id, kind, spec_json, started_at, session_id=None):
            insert_calls.append({"run_id": run_id, "kind": kind})

        async def update_engine_run(
            self, run_id, *, status, ended_at=None, export_dir=None, error=None
        ):
            update_calls.append({"run_id": run_id, "status": status})

    # Patch StateDB in the db module so the `from lionagi.state.db import StateDB`
    # inside _do_engine_run picks up the mock.
    monkeypatch.setattr(db_mod, "StateDB", MockStateDB)

    args = _build_args(kind="research", spec="test topic", no_persist=False)
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    assert len(insert_calls) == 1
    assert insert_calls[0]["kind"] == "research"
    assert any(c["status"] == "completed" for c in update_calls)


async def test_no_persist_skips_db(monkeypatch, capsys):
    """--no-persist flag means no DB calls are made."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)

    async def _mock_run(spec, *, on_event=None, **kwargs):
        return "no persist result"

    mock_engine = MagicMock()
    mock_engine.run = _mock_run
    MockEngineClass = MagicMock(return_value=mock_engine)
    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda m, n: MockEngineClass)

    db_opened = []

    class FailingStateDB:
        async def open(self):
            db_opened.append(True)
            raise AssertionError("StateDB should not be opened with --no-persist")

    monkeypatch.setattr(db_mod, "StateDB", FailingStateDB)

    args = _build_args(kind="research", spec="test", no_persist=True)
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    assert not db_opened, "StateDB.open() was called despite --no-persist"


# ---------------------------------------------------------------------------
# run_engine dispatch (synchronous entry point)
# ---------------------------------------------------------------------------


def test_run_engine_dispatches_run_subcommand(monkeypatch):
    """run_engine with engine_command='run' calls _do_engine_run via run_async."""
    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod

    called_with = []

    async def _mock_do_engine_run(args):
        called_with.append(args)
        return 0

    monkeypatch.setattr(engine_mod, "_do_engine_run", _mock_do_engine_run)
    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)

    # Patch run_async to execute the coroutine synchronously in a fresh event loop.
    import lionagi.ln.concurrency as conc_mod

    monkeypatch.setattr(conc_mod, "run_async", lambda coro: asyncio.run(coro))

    from lionagi.cli.engine import run_engine

    args = _build_args(kind="research", spec="test", no_persist=True)
    rc = run_engine(args)
    assert rc == 0
    assert len(called_with) == 1


def test_run_engine_unknown_subcommand_returns_1(monkeypatch):
    """Unknown engine subcommand → exit 1."""
    import lionagi.cli._logging as log_mod

    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)

    from lionagi.cli.engine import run_engine

    args = _build_args()
    args.engine_command = "unknown-cmd"

    rc = run_engine(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# Main entrypoint: 'engine' command is routed in main()
# ---------------------------------------------------------------------------


def test_main_routes_engine_command(monkeypatch):
    """main() routes 'engine run ...' to run_engine (not run_agent or others).

    Because `lionagi.cli` exports a `main` function that shadows the module
    name, we retrieve the actual main.py module via importlib and patch
    run_engine on that module object.
    """
    import lionagi.cli._logging as log_mod

    monkeypatch.setattr(log_mod, "configure_cli_logging", lambda *a: None)

    # Get the actual main.py module (not the main() function exported via __init__)
    main_module = importlib.import_module("lionagi.cli.main")

    run_engine_calls = []

    def _mock_run_engine(args):
        run_engine_calls.append(args)
        return 0

    monkeypatch.setattr(main_module, "run_engine", _mock_run_engine)

    # 'engine run research <spec>' — must be routed to run_engine.
    rc = main_module.main(["engine", "run", "research", "test topic"])
    assert rc == 0
    assert len(run_engine_calls) == 1
    assert run_engine_calls[0].command == "engine"
