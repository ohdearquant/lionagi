# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li schedule` CLI command: subcommands, argument parsing, and dispatch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_schedule_subparser_registered():
    """li schedule is wired into the main parser."""
    from lionagi.cli.main import main

    # Parsing li schedule list --help should not raise SystemExit with code != 0.
    try:
        main(["schedule", "--help"])
    except SystemExit as exc:
        assert exc.code == 0, f"Expected clean help exit, got {exc.code}"


def test_schedule_list_subcommand_registered():
    """li schedule list must be a recognized subcommand."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    assert args.schedule_action == "list"


def test_schedule_create_subcommand_args():
    """li schedule create parses name + optional flags."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        ["schedule", "create", "my-sched", "--cron", "0 * * * *", "--prompt", "ping"]
    )
    assert args.schedule_action == "create"
    assert args.name == "my-sched"
    assert args.cron == "0 * * * *"
    assert args.prompt == "ping"


def test_schedule_action_kind_choices_match_store_vocabulary():
    from lionagi.studio.cli import add_schedule_subparser
    from lionagi.studio.scheduler.subprocess import (
        _ALIAS_ACTION_KINDS,
        _VALID_ACTION_KINDS,
    )

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    schedule_parser = sub.choices["schedule"]
    action_subparsers = next(
        action
        for action in schedule_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    create_parser = action_subparsers.choices["create"]
    action_kind = next(action for action in create_parser._actions if action.dest == "action_kind")

    assert set(action_kind.choices) == _VALID_ACTION_KINDS | set(_ALIAS_ACTION_KINDS)


def test_schedule_create_normalizes_action_kind_alias(monkeypatch):
    import lionagi.studio.cli as schedule_cli

    captured = {}

    def fake_api(path, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"id": "schedule-id"}

    monkeypatch.setattr(schedule_cli, "_api", fake_api)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    schedule_cli.add_schedule_subparser(sub)
    args = parser.parse_args(
        [
            "schedule",
            "create",
            "nightly",
            "--cron",
            "0 0 * * *",
            "--action-kind",
            "playbook",
            "--playbook",
            "review",
        ]
    )

    assert schedule_cli.run_schedule(args) == 0
    assert captured["body"]["action_kind"] == "play"


def test_schedule_enable_disable_trigger_delete_accept_id():
    """enable/disable/trigger/delete all take an id positional arg."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    for action in ("enable", "disable", "trigger", "delete"):
        args = parser.parse_args(["schedule", action, "sched-123"])
        assert args.schedule_action == action
        assert args.id == "sched-123"


def test_schedule_limits_subcommand_registered():
    """li schedule limits is a recognized subcommand and takes no positional."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "limits"])
    assert args.schedule_action == "limits"


def test_schedule_limits_dispatches_to_api_and_prints_values(monkeypatch, capsys):
    """run_schedule limits calls _api('/limits') and prints cap + inflight."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "_api",
        lambda path, **kw: {"max_scheduled_concurrent": 4, "current_inflight": 1},
    )

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "limits"])
    result = run_schedule(args)

    assert result == 0
    out = capsys.readouterr().out
    assert "4" in out
    assert "1" in out


def test_schedule_limits_unlimited_display(monkeypatch, capsys):
    """A cap of 0 (unlimited) prints 'unlimited' rather than the digit 0."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "_api",
        lambda path, **kw: {"max_scheduled_concurrent": 0, "current_inflight": 2},
    )

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "limits"])
    result = run_schedule(args)

    assert result == 0
    out = capsys.readouterr().out
    assert "unlimited" in out


def test_schedule_limits_api_error_returns_1(monkeypatch):
    """When _api returns None (network error), run_schedule returns 1."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: None)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "limits"])
    result = run_schedule(args)
    assert result == 1


def test_schedule_runs_subcommand():
    """li schedule runs <id> parses correctly."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "runs", "sched-abc"])
    assert args.schedule_action == "runs"
    assert args.id == "sched-abc"
    assert args.limit == 20
    assert args.status is None
    assert args.as_json is False


def test_schedule_runs_subcommand_limit_status_json_flags():
    """--limit/--status (repeatable)/--json all parse onto `runs`."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        [
            "schedule",
            "runs",
            "sched-abc",
            "--limit",
            "5",
            "--status",
            "failed",
            "--status",
            "timed_out",
            "--json",
        ]
    )
    assert args.limit == 5
    assert args.status == ["failed", "timed_out"]
    assert args.as_json is True


def test_schedule_run_singular_subcommand():
    """li schedule run <run-id> [--json] parses correctly."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "run", "9c8f4d5a2b10", "--json"])
    assert args.schedule_action == "run"
    assert args.id == "9c8f4d5a2b10"
    assert args.as_json is True


def test_schedule_status_subcommand_wait_and_json():
    """li schedule status <id> [--wait] [--json] parses correctly."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc", "--wait", "--json"])
    assert args.schedule_action == "status"
    assert args.id == "sched-abc"
    assert args.wait is True
    assert args.as_json is True


def test_schedule_trigger_wait_flag_parses():
    """li schedule trigger <id> --wait parses correctly, default False."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc"])
    assert args.wait is False
    args = parser.parse_args(["schedule", "trigger", "sched-abc", "--wait"])
    assert args.wait is True


# ---------------------------------------------------------------------------
# run_schedule dispatch: runs/run/status/trigger --wait
# ---------------------------------------------------------------------------


def test_schedule_runs_limit_out_of_range_rejected(monkeypatch, capsys):
    """--limit outside [1, 200] is rejected before any API call."""
    import lionagi.studio.cli as sched_mod

    api_called = []
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "runs", "sched-abc", "--limit", "500"])
    result = run_schedule(args)

    assert result == 1
    assert not api_called
    assert "--limit" in capsys.readouterr().err


def test_schedule_runs_dispatches_with_limit_and_status_query(monkeypatch):
    """run_schedule runs builds the query string from --limit/--status."""
    import lionagi.studio.cli as sched_mod

    captured = {}

    def _fake_api(path, method="GET", body=None):
        captured["path"] = path
        return {"runs": [], "limit": 5, "offset": 0, "has_next": False}

    monkeypatch.setattr(sched_mod, "_api", _fake_api)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        ["schedule", "runs", "sched-abc", "--limit", "5", "--status", "failed"]
    )
    result = run_schedule(args)

    assert result == 0
    assert captured["path"] == "/sched-abc/runs?limit=5&status=failed"


def test_schedule_runs_empty_prints_no_runs(monkeypatch, capsys):
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: {"runs": []})

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "runs", "sched-abc"])
    result = run_schedule(args)

    assert result == 0
    assert "(no runs)" in capsys.readouterr().out


def test_schedule_runs_human_table_uses_fired_at_not_started_at(monkeypatch, capsys):
    """Regression: the human table must render `fired_at` (the real schedule_runs
    column), not the nonexistent `started_at` that used to print as '?'."""
    import lionagi.studio.cli as sched_mod

    fake_run = {
        "id": "run1",
        "status": "completed",
        "fired_at": 1_752_000_000.0,
        "ended_at": 1_752_000_010.0,
        "duration_ms": 10000,
        "outcome": {"code": "run.completed.ok", "summary": "completed: 1 artifact(s)"},
        "invocation_id": "inv1",
        "artifacts": ["/runs/inv1/artifacts"],
    }
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: {"runs": [fake_run]})

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "runs", "sched-abc"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "?" not in out.replace("(no runs)", "")
    assert "run1" in out
    assert "inv1" in out


def test_schedule_runs_json_emits_raw_api_response(monkeypatch, capsys):
    import lionagi.studio.cli as sched_mod

    api_response = {"runs": [{"id": "run1", "status": "completed"}], "limit": 20, "offset": 0}
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_response)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "runs", "sched-abc", "--json"])
    result = run_schedule(args)

    assert result == 0
    import json as _json

    assert _json.loads(capsys.readouterr().out) == api_response


def test_schedule_run_singular_dispatches_and_prints_table(monkeypatch, capsys):
    import lionagi.studio.cli as sched_mod

    fake_run = {
        "id": "run1",
        "status": "failed",
        "fired_at": 1_752_000_000.0,
        "ended_at": 1_752_000_010.0,
        "duration_ms": 10000,
        "outcome": {"code": "run.failed.failed", "summary": "tests failed"},
        "invocation_id": "inv1",
        "artifacts": [],
    }
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: fake_run)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "run", "run1"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "run1" in out and "tests faile" in out


def test_schedule_run_singular_not_found_returns_1(monkeypatch):
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: None)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "run", "nonexistent"])
    result = run_schedule(args)

    assert result == 1


def _status_response(**overrides) -> dict:
    base = {
        "schedule": {
            "id": "sched-abc",
            "name": "nightly",
            "enabled": True,
            "trigger_type": "cron",
            "cron_expr": "0 2 * * *",
            "interval_sec": None,
            "next_fire_at": 1_752_100_000.0,
        },
        "latest_run": {
            "id": "run1",
            "status": "failed",
            "exit_code": 1,
            "fired_at": 1_752_000_000.0,
            "ended_at": 1_752_000_010.0,
            "duration_ms": 10000,
            "outcome": {"code": "run.failed.failed", "summary": "tests failed"},
            "invocation_id": "inv1",
            "session_ids": ["sess1"],
            "artifacts": ["/runs/inv1/artifacts"],
        },
        "exit_code": 1,
    }
    base.update(overrides)
    return base


def test_schedule_status_human_output_includes_inspect_line(monkeypatch, capsys):
    """The status block must route to `li monitor <invocation>` for drill-in."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: _status_response())

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 1  # exit_code from the response, ordinary failure
    assert "inspect:     li monitor inv1" in out
    assert "outcome:     run.failed.failed" in out
    assert "session:     sess1" in out


def test_schedule_status_json_matches_human_fields(monkeypatch, capsys):
    """Human and JSON views must agree on the same underlying data."""
    import json as _json

    import lionagi.studio.cli as sched_mod

    response = _status_response()
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: response)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc", "--json"])
    result = run_schedule(args)

    parsed = _json.loads(capsys.readouterr().out)
    assert result == response["exit_code"]
    assert parsed == response


def test_schedule_status_no_runs_yet(monkeypatch, capsys):
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(
        sched_mod, "_api", lambda *a, **kw: _status_response(latest_run=None, exit_code=2)
    )

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 2
    assert "last run:    (none)" in out


def test_schedule_status_not_found_returns_1(monkeypatch):
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: None)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc"])
    result = run_schedule(args)

    assert result == 1


def test_schedule_status_wait_polls_until_terminal(monkeypatch):
    """--wait must keep polling while the latest run is still 'running'."""
    import lionagi.studio.cli as sched_mod

    responses = [
        _status_response(latest_run={"status": "running"}, exit_code=3),
        _status_response(latest_run={"status": "running"}, exit_code=3),
        _status_response(),
    ]
    calls = []

    def _fake_api(path, **kw):
        calls.append(path)
        return responses.pop(0)

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc", "--wait"])
    result = run_schedule(args)

    assert result == 1
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# trigger --wait: grace-period retry across the fire-now-before-insert race
# ---------------------------------------------------------------------------


def test_schedule_trigger_without_wait_unchanged(monkeypatch, capsys):
    """No --wait: trigger returns as soon as the run id comes back (existing behavior)."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: {"ok": True, "run_id": "run1"})

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "Run: run1" in out


def test_schedule_trigger_wait_retries_through_the_creation_race(monkeypatch, capsys):
    """The occurrence lookup 404s (returns None) a couple of times right after
    trigger — --wait must retry through the grace period rather than failing."""
    import lionagi.studio.cli as sched_mod

    calls = []

    def _fake_api(path, method="GET", body=None):
        calls.append(path)
        if path == "/sched-abc/trigger":
            return {"ok": True, "run_id": "run1"}
        assert path == "/runs/run1"
        # First two lookups race the not-yet-written row; third finds it running,
        # fourth finds it terminal.
        if len(calls) <= 3:
            return None if len(calls) <= 2 else {"status": "running", "outcome": None}
        return {"status": "completed", "outcome": {"code": "run.completed.ok", "summary": "ok"}}

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc", "--wait"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "status: completed" in out


def test_schedule_trigger_wait_never_appears_errors(monkeypatch, capsys):
    """The occurrence never shows up within the grace period: a clear error, not a hang."""
    import lionagi.studio.cli as sched_mod

    def _fake_api(path, method="GET", body=None):
        if path == "/sched-abc/trigger":
            return {"ok": True, "run_id": "run1"}
        return None

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(sched_mod, "_TRIGGER_WAIT_GRACE_SECONDS", 0.01)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc", "--wait"])
    result = run_schedule(args)

    assert result == 1
    assert "never appeared" in capsys.readouterr().err


def test_schedule_trigger_wait_treats_timed_out_as_terminal(monkeypatch, capsys):
    """A timed-out occurrence must stop the poll loop like any other terminal
    status, not spin until the wait deadline."""
    import lionagi.studio.cli as sched_mod

    calls = []

    def _fake_api(path, method="GET", body=None):
        calls.append(path)
        if path == "/sched-abc/trigger":
            return {"ok": True, "run_id": "run1"}
        return {"status": "timed_out", "outcome": {"code": "timed_out", "summary": "timed out"}}

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(sched_mod, "_TRIGGER_WAIT_MAX_SECONDS", 0.05)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc", "--wait"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 1
    assert "status: timed_out" in out
    assert calls.count("/runs/run1") == 1


def test_schedule_status_wait_treats_timed_out_as_terminal(monkeypatch):
    """--wait must stop polling once the latest run reaches 'timed_out', not
    just the completed/failed/cancelled/skipped subset it used to check."""
    import lionagi.studio.cli as sched_mod

    calls = []

    def _fake_api(path, **kw):
        calls.append(path)
        return _status_response(latest_run={"status": "timed_out"}, exit_code=124)

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(sched_mod, "_TRIGGER_WAIT_MAX_SECONDS", 0.05)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc", "--wait"])
    result = run_schedule(args)

    assert result == 124
    assert len(calls) == 1


def test_schedule_trigger_no_run_id_skips_wait(monkeypatch, capsys):
    """A trigger response without a run_id (shouldn't normally happen) must not
    crash --wait — it just returns after printing 'Triggered'."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: {"ok": True})

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc", "--wait"])
    result = run_schedule(args)

    assert result == 0


def test_schedule_list_dispatches_to_api(monkeypatch):
    """run_schedule list calls _api('/') and prints schedules."""
    import lionagi.studio.cli as sched_mod

    fake_schedules = [{"id": "s1", "name": "daily", "enabled": True, "trigger_type": "cron"}]
    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: {"schedules": fake_schedules})

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    result = run_schedule(args)
    assert result == 0


def test_schedule_list_api_error_returns_1(monkeypatch):
    """When _api returns None (network error), run_schedule returns 1."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: None)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    result = run_schedule(args)
    assert result == 1


def test_base_url_default(monkeypatch):
    """_base_url returns the default when no env vars are set."""
    monkeypatch.delenv("LIONAGI_STUDIO_URL", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_PORT", raising=False)

    from lionagi.studio.cli import _base_url

    assert _base_url() == "http://127.0.0.1:8765"


def test_base_url_respects_studio_port(monkeypatch):
    """_base_url reflects LIONAGI_STUDIO_PORT when set."""
    monkeypatch.delenv("LIONAGI_STUDIO_URL", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)
    monkeypatch.setenv("LIONAGI_STUDIO_PORT", "9000")

    from lionagi.studio.cli import _base_url

    assert _base_url() == "http://127.0.0.1:9000"


def test_base_url_respects_studio_host(monkeypatch):
    """_base_url reflects LIONAGI_STUDIO_HOST when set."""
    monkeypatch.delenv("LIONAGI_STUDIO_URL", raising=False)
    monkeypatch.setenv("LIONAGI_STUDIO_HOST", "0.0.0.0")
    monkeypatch.setenv("LIONAGI_STUDIO_PORT", "8765")

    from lionagi.studio.cli import _base_url

    assert _base_url() == "http://0.0.0.0:8765"


def test_base_url_studio_url_takes_precedence(monkeypatch):
    """LIONAGI_STUDIO_URL wins over host/port env vars."""
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "https://studio.example.com/")
    monkeypatch.setenv("LIONAGI_STUDIO_PORT", "9999")

    from lionagi.studio.cli import _base_url

    assert _base_url() == "https://studio.example.com"


@pytest.mark.parametrize(
    "env_url",
    [
        "http://127.0.0.1:8765/api",
        "http://127.0.0.1:8765/api/",
        "https://studio.example.com/api",
    ],
    ids=["api", "api-slash", "https-api"],
)
def test_base_url_strips_trailing_api_suffix(monkeypatch, caplog, env_url):
    """A base URL already carrying /api must not double-prefix requests:
    endpoint paths add /api themselves, so _base_url strips a trailing one
    and warns (once) so intentional reverse-proxy layouts can diagnose it."""
    import logging

    import lionagi.studio.cli as sched_mod

    monkeypatch.setenv("LIONAGI_STUDIO_URL", env_url)
    monkeypatch.setattr(sched_mod, "_warned_api_suffix", False)

    with caplog.at_level(logging.WARNING):
        first = sched_mod._base_url()
        second = sched_mod._base_url()

    assert not first.endswith("/api")
    assert first == env_url.rstrip("/").removesuffix("/api")
    assert second == first
    warnings = [r for r in caplog.records if "LIONAGI_STUDIO_URL ends with /api" in r.message]
    assert len(warnings) == 1, "strip must warn exactly once per process"


def test_base_url_no_warning_without_api_suffix(monkeypatch, caplog):
    """A clean root URL is returned untouched with no warning."""
    import logging

    import lionagi.studio.cli as sched_mod

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "https://studio.example.com")
    monkeypatch.setattr(sched_mod, "_warned_api_suffix", False)

    with caplog.at_level(logging.WARNING):
        assert sched_mod._base_url() == "https://studio.example.com"

    assert not [r for r in caplog.records if "LIONAGI_STUDIO_URL" in r.message]


# ---------------------------------------------------------------------------
# _cmd_create: --project auto-detect from cwd (scheduler spawn-cwd fix)
# ---------------------------------------------------------------------------


def _run_create(monkeypatch, extra_args: list[str], api_response: dict | None = None) -> dict:
    """Run `li schedule create ...`, capturing the JSON body posted to _api."""
    import lionagi.studio.cli as sched_mod

    captured_body: dict = {}

    def _fake_api(path, method="GET", body=None):
        captured_body.update(body or {})
        return api_response if api_response is not None else {"id": "sched-1", "name": "n"}

    monkeypatch.setattr(sched_mod, "_api", _fake_api)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "create", "my-sched", "--cron", "0 * * * *", *extra_args])
    result = run_schedule(args)
    return {"result": result, "body": captured_body}


def test_schedule_create_explicit_project_skips_auto_detect(monkeypatch):
    """--project given: used as-is, detect_project is never consulted."""
    detect_calls = []
    monkeypatch.setattr(
        "lionagi.cli._project.detect_project",
        lambda cwd=None: detect_calls.append(cwd) or ("should-not-be-used", "git_remote"),
    )

    outcome = _run_create(monkeypatch, ["--project", "explicit-proj"])

    assert outcome["result"] == 0
    assert outcome["body"]["action_project"] == "explicit-proj"
    assert detect_calls == []


def test_schedule_create_without_project_auto_detects_valid(monkeypatch):
    """No --project: a valid detect_project() result populates action_project."""
    monkeypatch.setattr(
        "lionagi.cli._project.detect_project",
        lambda cwd=None: ("lionagi/lionagi", "git_remote"),
    )

    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert outcome["body"]["action_project"] == "lionagi/lionagi"


def test_schedule_create_without_project_no_detection_omits_field(monkeypatch):
    """No --project and detect_project finds nothing: action_project is simply absent."""
    monkeypatch.setattr("lionagi.cli._project.detect_project", lambda cwd=None: (None, None))

    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert "action_project" not in outcome["body"]


def test_schedule_create_auto_detect_invalid_identifier_silently_skipped(monkeypatch):
    """A detected name that fails identifier validation (leading '-') must never
    break `create` — it is silently dropped, not surfaced as an error."""
    monkeypatch.setattr(
        "lionagi.cli._project.detect_project",
        lambda cwd=None: ("-not-a-valid-flag-name", "git_remote"),
    )

    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert "action_project" not in outcome["body"]


def test_schedule_create_auto_detect_exception_silently_skipped(monkeypatch):
    """detect_project() raising must never break `create`."""

    def _boom(cwd=None):
        raise RuntimeError("cwd detection blew up")

    monkeypatch.setattr("lionagi.cli._project.detect_project", _boom)

    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert "action_project" not in outcome["body"]


# ---------------------------------------------------------------------------
# _cmd_create: --cwd (ADR-0070 delta 1 -- persisted execution root)
# ---------------------------------------------------------------------------


def test_schedule_create_explicit_cwd_used_as_is(monkeypatch, tmp_path):
    """--cwd given: resolved and sent as action_cwd, action_project auto-
    detection is skipped entirely because it takes priority."""
    monkeypatch.setattr(
        "lionagi.cli._project.detect_project",
        lambda cwd=None: ("should-not-be-used", "git_remote"),
    )

    outcome = _run_create(monkeypatch, ["--cwd", str(tmp_path)])

    assert outcome["result"] == 0
    assert outcome["body"]["action_cwd"] == str(tmp_path)
    assert outcome["body"]["action_project"] == "should-not-be-used"


def test_schedule_create_cwd_nonexistent_directory_errors(monkeypatch, capsys):
    """--cwd pointing at a directory that doesn't exist is rejected before
    any API call is made."""
    outcome = _run_create(monkeypatch, ["--cwd", "/no/such/directory/at/all"])

    assert outcome["result"] == 1
    assert "action_cwd" not in outcome["body"]
    captured = capsys.readouterr()
    assert "--cwd" in captured.err


def test_schedule_create_without_cwd_or_project_falls_back_to_cli_cwd(monkeypatch):
    """Neither --cwd nor a resolvable action_project: the CLI's own
    invocation directory is sent as action_cwd so the schedule still gets a
    stable execution root."""
    monkeypatch.setattr("lionagi.cli._project.detect_project", lambda cwd=None: (None, None))

    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert "action_project" not in outcome["body"]
    assert outcome["body"]["action_cwd"] == str(Path.cwd())


def test_schedule_create_with_resolved_project_omits_cwd_fallback(monkeypatch):
    """A resolvable action_project (explicit or auto-detected) means the CLI
    cwd fallback never fires -- action_project's registered path wins at
    creation time (see services/schedules.create_schedule)."""
    monkeypatch.setattr(
        "lionagi.cli._project.detect_project",
        lambda cwd=None: ("lionagi/lionagi", "git_remote"),
    )

    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert outcome["body"]["action_project"] == "lionagi/lionagi"
    assert "action_cwd" not in outcome["body"]


# ---------------------------------------------------------------------------
# _cmd_create: --on-success / --on-fail chain flags
# ---------------------------------------------------------------------------


def test_schedule_create_on_success_round_trips_to_body(monkeypatch):
    """--on-success JSON parses and lands on the posted body as a dict."""
    outcome = _run_create(
        monkeypatch, ["--on-success", '{"prompt": "notify done", "on_success": null}']
    )

    assert outcome["result"] == 0
    assert outcome["body"]["on_success"] == {"prompt": "notify done", "on_success": None}


def test_schedule_create_on_fail_round_trips_to_body(monkeypatch):
    """--on-fail JSON parses and lands on the posted body as a dict."""
    outcome = _run_create(
        monkeypatch, ["--on-fail", '{"prompt": "alert on-call", "on_fail": null}']
    )

    assert outcome["result"] == 0
    assert outcome["body"]["on_fail"] == {"prompt": "alert on-call", "on_fail": None}


def test_schedule_create_on_success_and_on_fail_together(monkeypatch):
    """Both chain flags can be set on the same create call."""
    outcome = _run_create(
        monkeypatch,
        [
            "--on-success",
            '{"agent": "notifier", "on_success": null}',
            "--on-fail",
            '{"agent": "pager", "on_fail": null}',
        ],
    )

    assert outcome["result"] == 0
    assert outcome["body"]["on_success"] == {"agent": "notifier", "on_success": None}
    assert outcome["body"]["on_fail"] == {"agent": "pager", "on_fail": None}


def test_schedule_create_on_success_malformed_json_errors_cleanly(monkeypatch, capsys):
    """Malformed --on-success JSON returns 1 with a clear stderr message, no API call."""
    api_called = []
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        ["schedule", "create", "my-sched", "--cron", "0 * * * *", "--on-success", "{not json"]
    )
    result = run_schedule(args)

    assert result == 1
    assert api_called == []
    captured = capsys.readouterr()
    assert "--on-success" in captured.err
    assert "invalid JSON" in captured.err


def test_schedule_create_on_fail_malformed_json_errors_cleanly(monkeypatch, capsys):
    """Malformed --on-fail JSON returns 1 with a clear stderr message, no API call."""
    api_called = []
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        ["schedule", "create", "my-sched", "--cron", "0 * * * *", "--on-fail", "[1, 2"]
    )
    result = run_schedule(args)

    assert result == 1
    assert api_called == []
    captured = capsys.readouterr()
    assert "--on-fail" in captured.err
    assert "invalid JSON" in captured.err


def test_schedule_create_on_success_non_object_json_rejected(monkeypatch, capsys):
    """--on-success must be a JSON object, not e.g. a bare list or string."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: (_ for _ in ()).throw(AssertionError))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        ["schedule", "create", "my-sched", "--cron", "0 * * * *", "--on-success", "[1, 2, 3]"]
    )
    result = run_schedule(args)

    assert result == 1
    captured = capsys.readouterr()
    assert "must be a JSON object" in captured.err


def test_schedule_create_on_success_unknown_key_rejected(monkeypatch, capsys):
    """A chain_action key the engine's merge doesn't understand is rejected up front,
    since it would otherwise silently clobber an unrelated schedule column via the
    shallow merge in scheduler/engine.py."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: (_ for _ in ()).throw(AssertionError))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        [
            "schedule",
            "create",
            "my-sched",
            "--cron",
            "0 * * * *",
            "--on-success",
            '{"trigger_type": "interval"}',
        ]
    )
    result = run_schedule(args)

    assert result == 1
    captured = capsys.readouterr()
    assert "unknown key" in captured.err
    assert "trigger_type" in captured.err


def test_schedule_create_on_success_missing_explicit_key_warns(monkeypatch):
    """A nested chain_action that omits its own on_success key triggers the
    re-fire warning (shallow-merge inheritance gotcha)."""
    import lionagi.studio.cli as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    outcome = _run_create(monkeypatch, ["--on-success", '{"prompt": "notify done"}'])

    assert outcome["result"] == 0
    assert len(warnings) == 1
    assert "on_success" in warnings[0]
    assert "re-fire" in warnings[0]


def test_schedule_create_on_fail_missing_explicit_key_warns(monkeypatch):
    """A nested chain_action that omits its own on_fail key triggers the
    re-fire warning (shallow-merge inheritance gotcha)."""
    import lionagi.studio.cli as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    outcome = _run_create(monkeypatch, ["--on-fail", '{"prompt": "alert on-call"}'])

    assert outcome["result"] == 0
    assert len(warnings) == 1
    assert "on_fail" in warnings[0]
    assert "re-fire" in warnings[0]


def test_schedule_create_on_success_explicit_null_no_warning(monkeypatch):
    """Explicitly setting on_success: null in the chain_action suppresses the warning."""
    import lionagi.studio.cli as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    outcome = _run_create(
        monkeypatch, ["--on-success", '{"prompt": "notify done", "on_success": null}']
    )

    assert outcome["result"] == 0
    assert warnings == []


def test_schedule_create_without_chain_flags_omits_fields(monkeypatch):
    """No --on-success/--on-fail: neither key is posted to the API."""
    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert "on_success" not in outcome["body"]
    assert "on_fail" not in outcome["body"]


# ---------------------------------------------------------------------------
# _cmd_create: nested chain_action validation (recursive)
#
# The engine fires a chain_action via a shallow merge (`{**schedule,
# **chain_action}` in scheduler/engine.py), so a chain_action nested inside
# on_success/on_fail rides the exact same merge one level deeper when its
# parent's run completes. Validation must recurse into those nested actions,
# not just check the top level.
# ---------------------------------------------------------------------------


def test_schedule_create_nested_on_success_unknown_key_rejected(monkeypatch, capsys):
    """A nested on_success dict with a key outside the allowed set is
    rejected, even though the top-level chain_action is clean — an unknown
    key at any depth would otherwise clobber an unrelated schedule column
    (e.g. trigger_type) via the engine's shallow merge one level down."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: (_ for _ in ()).throw(AssertionError))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        [
            "schedule",
            "create",
            "my-sched",
            "--cron",
            "0 * * * *",
            "--on-success",
            '{"prompt": "step 1", "on_success": {"trigger_type": "interval", "prompt": "step 2"}}',
        ]
    )
    result = run_schedule(args)

    assert result == 1
    captured = capsys.readouterr()
    assert "unknown key" in captured.err
    assert "trigger_type" in captured.err
    assert "--on-success.on_success" in captured.err


def test_schedule_create_nested_on_success_non_dict_rejected(monkeypatch, capsys):
    """A nested on_success value that isn't a JSON object (e.g. a list) is
    rejected up front — left unchecked, the engine would treat it as a truthy
    chain_action on the child's successful run and blow up on
    `{**schedule, **chain_action}` after that child action already ran."""
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: (_ for _ in ()).throw(AssertionError))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        [
            "schedule",
            "create",
            "my-sched",
            "--cron",
            "0 * * * *",
            "--on-success",
            '{"prompt": "step 1", "on_success": [1, 2, 3]}',
        ]
    )
    result = run_schedule(args)

    assert result == 1
    captured = capsys.readouterr()
    assert "must be a JSON object" in captured.err
    assert "--on-success.on_success" in captured.err


def test_schedule_create_nested_chain_explicit_null_accepted_no_warning(monkeypatch):
    """A valid 2-level chain where the innermost level explicitly nulls out
    its own on_success is accepted with no warnings at any depth."""
    import lionagi.studio.cli as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    outcome = _run_create(
        monkeypatch,
        [
            "--on-success",
            '{"prompt": "step 1", "on_success": {"prompt": "step 2", "on_success": null}}',
        ],
    )

    assert outcome["result"] == 0
    assert warnings == []
    assert outcome["body"]["on_success"] == {
        "prompt": "step 1",
        "on_success": {"prompt": "step 2", "on_success": None},
    }


def test_schedule_create_nested_chain_missing_on_success_warns(monkeypatch):
    """The top level sets its own on_success (to the nested dict), so it does
    not warn — but the nested dict itself omits its own on_success key, which
    does trigger the re-fire warning, scoped to that nested path."""
    import lionagi.studio.cli as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    outcome = _run_create(
        monkeypatch,
        ["--on-success", '{"prompt": "step 1", "on_success": {"prompt": "step 2"}}'],
    )

    assert outcome["result"] == 0
    assert len(warnings) == 1
    assert "--on-success.on_success" in warnings[0]
    assert "re-fire" in warnings[0]


# ---------------------------------------------------------------------------
# _cmd_create: --once / --max-runs (one-shot semantics)
# ---------------------------------------------------------------------------


def test_schedule_create_once_maps_to_max_runs_1(monkeypatch):
    """--once is sugar for --max-runs 1."""
    outcome = _run_create(monkeypatch, ["--once"])

    assert outcome["result"] == 0
    assert outcome["body"]["max_runs"] == 1


def test_schedule_create_max_runs_explicit(monkeypatch):
    """--max-runs N is passed through to the request body as-is."""
    outcome = _run_create(monkeypatch, ["--max-runs", "5"])

    assert outcome["result"] == 0
    assert outcome["body"]["max_runs"] == 5


def test_schedule_create_without_max_runs_or_once_omits_field(monkeypatch):
    """Neither flag given: max_runs is absent from the body (unlimited)."""
    outcome = _run_create(monkeypatch, [])

    assert outcome["result"] == 0
    assert "max_runs" not in outcome["body"]


def test_schedule_create_once_and_max_runs_together_rejected(monkeypatch, capsys):
    """--once and --max-runs are mutually exclusive."""
    api_called = []
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        ["schedule", "create", "my-sched", "--cron", "0 * * * *", "--once", "--max-runs", "2"]
    )
    result = run_schedule(args)

    assert result == 1
    assert not api_called
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.parametrize("bad_value", [0, -1])
def test_schedule_create_max_runs_rejects_non_positive(monkeypatch, capsys, bad_value):
    """--max-runs 0 or a negative integer is rejected before hitting the API."""
    api_called = []
    import lionagi.studio.cli as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        ["schedule", "create", "my-sched", "--cron", "0 * * * *", "--max-runs", str(bad_value)]
    )
    result = run_schedule(args)

    assert result == 1
    assert not api_called
    assert "positive integer" in capsys.readouterr().err


def test_schedule_list_shows_remaining_runs(monkeypatch, capsys):
    """`li schedule list` prints remaining-runs info when max_runs is set."""
    import lionagi.studio.cli as sched_mod

    fake_schedules = [
        {
            "id": "s1",
            "name": "one-shot",
            "enabled": True,
            "trigger_type": "cron",
            "max_runs": 3,
            "remaining_runs": 1,
        },
        {
            "id": "s2",
            "name": "unlimited",
            "enabled": True,
            "trigger_type": "interval",
        },
    ]
    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: {"schedules": fake_schedules})

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    lines = out.splitlines()
    unlimited_line = next(line for line in lines if "unlimited" in line)

    assert result == 0
    assert "runs left: 1/3" in out
    assert "runs left" not in unlimited_line


# ---------------------------------------------------------------------------
# Cron far-out warning (date-pinned one-shot footgun)
# ---------------------------------------------------------------------------


def test_warn_if_cron_far_out_warns_beyond_360_days(monkeypatch):
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.cli as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    # A cron pinned to a date almost certainly >360 days from "now" in test runs:
    # Feb 29 only exists every 4 years, so worst case is ~3 years out — always far.
    sched_mod._warn_if_cron_far_out("0 0 29 2 *")

    assert warnings
    assert "days" in warnings[0]


def test_warn_if_cron_far_out_silent_when_near(monkeypatch):
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.cli as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    # Fires hourly — always within a day.
    sched_mod._warn_if_cron_far_out("0 * * * *")

    assert not warnings


def test_warn_if_cron_far_out_no_croniter_is_noop(monkeypatch):
    """Missing the optional `studio` extra's croniter dep must never break create."""
    import builtins

    import lionagi.studio.cli as sched_mod

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "croniter":
            raise ImportError("no croniter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    # Should not raise.
    sched_mod._warn_if_cron_far_out("0 0 29 2 *")


# ---------------------------------------------------------------------------
# Near-miss flag suggestions (li schedule ...)
# ---------------------------------------------------------------------------


def test_suggest_schedule_flag_synonym_map():
    from lionagi.studio.cli import add_schedule_subparser, suggest_schedule_flag

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)  # populates _ALL_SCHEDULE_FLAGS

    assert suggest_schedule_flag("--every") == "--interval"
    assert suggest_schedule_flag("--at") == "--cron"
    assert suggest_schedule_flag("--action") == "--action-kind"
    assert suggest_schedule_flag("--on_success") == "--on-success"
    assert suggest_schedule_flag("--on_fail") == "--on-fail"
    assert suggest_schedule_flag("--max_runs") == "--max-runs"


def test_suggest_schedule_flag_fuzzy_typo():
    from lionagi.studio.cli import add_schedule_subparser, suggest_schedule_flag

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)

    assert suggest_schedule_flag("--corn") == "--cron"


def test_suggest_schedule_flag_no_match_returns_none():
    from lionagi.studio.cli import add_schedule_subparser, suggest_schedule_flag

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)

    assert suggest_schedule_flag("--completely-unrelated-xyz") is None


def test_li_schedule_unrecognized_flag_suggests_correction(monkeypatch, capsys):
    """`li schedule create ... --every 60` produces a did-you-mean, not argparse noise."""
    from lionagi.cli.main import main

    rc = main(["schedule", "create", "my-sched", "--every", "60"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--every" in err
    assert "--interval" in err


def test_li_schedule_unrecognized_underscore_on_success_suggests_dash(monkeypatch, capsys):
    from lionagi.cli.main import main

    rc = main(
        [
            "schedule",
            "create",
            "my-sched",
            "--cron",
            "0 * * * *",
            "--on_success",
            '{"prompt": "x"}',
        ]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "--on_success" in err
    assert "--on-success" in err


def test_li_schedule_unrecognized_flag_with_no_suggestion(monkeypatch, capsys):
    from lionagi.cli.main import main

    rc = main(["schedule", "create", "my-sched", "--totally-bogus-flag", "x"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "unrecognized argument" in err


def test_li_schedule_recognized_flags_still_dispatch(monkeypatch):
    """Sanity check: the new interception path does not break normal dispatch."""
    import lionagi.studio.cli as sched_mod
    from lionagi.cli.main import main

    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: {"schedules": []})

    rc = main(["schedule", "list"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Help epilogs render for every subcommand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand", ["list", "get", "create", "enable", "disable", "trigger", "delete", "runs"]
)
def test_schedule_subcommand_help_has_example_epilog(capsys, subcommand):
    from lionagi.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main(["schedule", subcommand, "--help"])
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "Example" in out


def test_schedule_create_help_repeats_shallow_merge_caveat(capsys):
    from lionagi.cli.main import main

    with pytest.raises(SystemExit):
        main(["schedule", "create", "--help"])

    out = capsys.readouterr().out
    assert "shallow-merge" in out or "shallow merge" in out


# ---------------------------------------------------------------------------
# li schedule: surplus non-flag arguments must error (rc=2), not be ignored
# ---------------------------------------------------------------------------


def test_li_schedule_surplus_non_flag_argument_errors(monkeypatch, capsys):
    """`li schedule list unexpected-extra` must not silently exit 0 and drop
    the extra token — argparse would reject the equivalent direct parse."""
    import lionagi.studio.cli as sched_mod
    from lionagi.cli.main import main

    api_called = []
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    rc = main(["schedule", "list", "unexpected-extra"])

    assert rc == 2
    assert not api_called
    err = capsys.readouterr().err
    assert "unrecognized argument" in err
    assert "unexpected-extra" in err


def test_li_schedule_surplus_extra_after_required_positional_errors(monkeypatch, capsys):
    """A surplus positional after a subcommand's own required id (e.g.
    `delete <id> <extra>`) must also error rather than silently execute."""
    import lionagi.studio.cli as sched_mod
    from lionagi.cli.main import main

    api_called = []
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    rc = main(["schedule", "delete", "sched-123", "accidental-extra"])

    assert rc == 2
    assert not api_called
    err = capsys.readouterr().err
    assert "unrecognized argument" in err
    assert "accidental-extra" in err


def test_li_schedule_mixed_dash_and_bare_extras_both_reported(monkeypatch, capsys):
    """A dash-prefixed unknown flag alongside a bare surplus token: the flag
    gets a did-you-mean, the bare token gets a plain unrecognized-argument
    error — neither is silently dropped."""
    from lionagi.cli.main import main

    rc = main(["schedule", "create", "my-sched", "--every", "60", "stray-token"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--every" in err and "--interval" in err
    assert "stray-token" in err


# ---------------------------------------------------------------------------
# _cmd_create: github / github_poll trigger authoring
# ---------------------------------------------------------------------------


def _run_create_argv(monkeypatch, argv_tail: list[str], api_response: dict | None = None) -> dict:
    """Run `li schedule create my-sched <argv_tail>` with NO forced --cron, capturing
    the JSON body posted to _api (returns {} when _api is never called)."""
    import lionagi.studio.cli as sched_mod

    captured: dict = {"called": False, "body": {}}

    def _fake_api(path, method="GET", body=None):
        captured["called"] = True
        captured["body"] = body or {}
        return api_response if api_response is not None else {"id": "sched-1", "name": "n"}

    monkeypatch.setattr(sched_mod, "_api", _fake_api)

    from lionagi.studio.cli import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "create", "my-sched", *argv_tail])
    result = run_schedule(args)
    return {"result": result, "called": captured["called"], "body": captured["body"]}


def test_schedule_create_github_flags_parse():
    """create accepts --trigger-type github/github_poll and the github flags."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        [
            "schedule",
            "create",
            "gh-sched",
            "--trigger-type",
            "github",
            "--github-repo",
            "owner/name",
            "--github-filter",
            '{"state": "open"}',
            "--poll-interval",
            "300",
        ]
    )
    assert args.trigger_type == "github"
    assert args.github_repo == "owner/name"
    assert args.github_filter == '{"state": "open"}'
    assert args.poll_interval == 300


def test_schedule_create_normalizes_github_alias_to_github_poll(monkeypatch):
    """--trigger-type github is stored as the canonical github_poll token."""
    outcome = _run_create_argv(
        monkeypatch,
        ["--trigger-type", "github", "--github-repo", "owner/name", "--prompt", "review"],
    )
    assert outcome["result"] == 0
    assert outcome["body"]["trigger_type"] == "github_poll"
    assert outcome["body"]["github_repo"] == "owner/name"


def test_schedule_create_github_poll_token_passthrough(monkeypatch):
    """The canonical --trigger-type github_poll is preserved verbatim."""
    outcome = _run_create_argv(
        monkeypatch,
        ["--trigger-type", "github_poll", "--github-repo", "owner/name", "--prompt", "review"],
    )
    assert outcome["result"] == 0
    assert outcome["body"]["trigger_type"] == "github_poll"


def test_schedule_create_github_filter_and_poll_interval_wired(monkeypatch):
    """--github-filter parses to a dict and --poll-interval to poll_interval_sec."""
    outcome = _run_create_argv(
        monkeypatch,
        [
            "--trigger-type",
            "github",
            "--github-repo",
            "owner/name",
            "--github-filter",
            '{"state": "open", "base": "main"}',
            "--poll-interval",
            "600",
            "--prompt",
            "review",
        ],
    )
    assert outcome["result"] == 0
    assert outcome["body"]["github_filter"] == {"state": "open", "base": "main"}
    assert outcome["body"]["poll_interval_sec"] == 600


def test_schedule_create_github_filter_invalid_json_errors(monkeypatch, capsys):
    """A non-JSON --github-filter returns 1 and never posts to the API."""
    outcome = _run_create_argv(
        monkeypatch,
        ["--trigger-type", "github", "--github-repo", "owner/name", "--github-filter", "{not json"],
    )
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be valid JSON" in capsys.readouterr().err


def test_schedule_create_github_filter_non_object_errors(monkeypatch, capsys):
    """A JSON --github-filter that isn't an object returns 1 and never posts."""
    outcome = _run_create_argv(
        monkeypatch,
        ["--trigger-type", "github", "--github-repo", "owner/name", "--github-filter", "[1, 2]"],
    )
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be a JSON object" in capsys.readouterr().err


def test_schedule_create_negative_poll_interval_errors(monkeypatch, capsys):
    """A negative --poll-interval returns 1 and never posts to the API."""
    outcome = _run_create_argv(
        monkeypatch,
        ["--trigger-type", "github", "--github-repo", "owner/name", "--poll-interval", "-5"],
    )
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be a positive integer" in capsys.readouterr().err


def test_schedule_create_zero_poll_interval_errors(monkeypatch, capsys):
    """A zero --poll-interval returns 1 and never posts to the API."""
    outcome = _run_create_argv(
        monkeypatch,
        ["--trigger-type", "github", "--github-repo", "owner/name", "--poll-interval", "0"],
    )
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be a positive integer" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _cmd_create: --max-cost-usd / --max-tokens spend-budget flags
# ---------------------------------------------------------------------------


def test_schedule_create_max_cost_usd_wired_to_budget_usd(monkeypatch):
    """--max-cost-usd parses to a float and wires body['budget_usd']."""
    outcome = _run_create_argv(monkeypatch, ["--max-cost-usd", "12.5"])
    assert outcome["result"] == 0
    assert outcome["body"]["budget_usd"] == 12.5


def test_schedule_create_max_tokens_wired_to_budget_tokens(monkeypatch):
    """--max-tokens parses to an int and wires body['budget_tokens']."""
    outcome = _run_create_argv(monkeypatch, ["--max-tokens", "50000"])
    assert outcome["result"] == 0
    assert outcome["body"]["budget_tokens"] == 50000


def test_schedule_create_max_cost_usd_and_max_tokens_together(monkeypatch):
    """Both flags can be set on the same schedule; either bound trips the gate."""
    outcome = _run_create_argv(monkeypatch, ["--max-cost-usd", "5", "--max-tokens", "10000"])
    assert outcome["result"] == 0
    assert outcome["body"]["budget_usd"] == 5.0
    assert outcome["body"]["budget_tokens"] == 10000


def test_schedule_create_negative_max_cost_usd_errors(monkeypatch, capsys):
    """A negative --max-cost-usd returns 1 and never posts to the API."""
    outcome = _run_create_argv(monkeypatch, ["--max-cost-usd", "-1"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "finite positive number" in capsys.readouterr().err


def test_schedule_create_zero_max_cost_usd_errors(monkeypatch, capsys):
    """A zero --max-cost-usd returns 1 and never posts to the API."""
    outcome = _run_create_argv(monkeypatch, ["--max-cost-usd", "0"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "finite positive number" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _cmd_create: --threshold-config metric threshold alert flag
# ---------------------------------------------------------------------------


def test_schedule_create_threshold_config_flag_parses():
    """create accepts --threshold-config as a raw string (JSON parsing happens in _cmd_create)."""
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(
        [
            "schedule",
            "create",
            "watch-failures",
            "--interval",
            "300",
            "--prompt",
            "alert on-call",
            "--threshold-config",
            '{"metric": "failed_sessions", "op": "gt", "value": 5, "window_minutes": 60}',
        ]
    )
    assert (
        args.threshold_config
        == '{"metric": "failed_sessions", "op": "gt", "value": 5, "window_minutes": 60}'
    )


def test_schedule_create_threshold_config_wired_to_body(monkeypatch):
    """--threshold-config parses to a dict and wires body['threshold_config']."""
    outcome = _run_create_argv(
        monkeypatch,
        [
            "--interval",
            "300",
            "--prompt",
            "alert on-call",
            "--threshold-config",
            '{"metric": "total_cost_usd", "op": "gte", "value": 10.5, "window_minutes": 15}',
        ],
    )
    assert outcome["result"] == 0
    assert outcome["body"]["threshold_config"] == {
        "metric": "total_cost_usd",
        "op": "gte",
        "value": 10.5,
        "window_minutes": 15,
    }


def test_schedule_create_threshold_config_invalid_json_errors(monkeypatch, capsys):
    """A non-JSON --threshold-config returns 1 and never posts to the API."""
    outcome = _run_create_argv(monkeypatch, ["--threshold-config", "{not json"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be valid JSON" in capsys.readouterr().err


def test_schedule_create_threshold_config_non_object_errors(monkeypatch, capsys):
    """A JSON --threshold-config that isn't an object returns 1 and never posts."""
    outcome = _run_create_argv(monkeypatch, ["--threshold-config", "[1, 2]"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be a JSON object" in capsys.readouterr().err


def test_schedule_create_threshold_config_omitted_by_default(monkeypatch):
    """No --threshold-config means the body never carries the key at all."""
    outcome = _run_create_argv(monkeypatch, ["--interval", "300", "--prompt", "ping"])
    assert outcome["result"] == 0
    assert "threshold_config" not in outcome["body"]


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf"])
def test_schedule_create_non_finite_max_cost_usd_errors(monkeypatch, capsys, bad_value):
    """A non-finite --max-cost-usd (nan/inf/-inf) is rejected before hitting the API.

    argparse's float() accepts these, and a plain ``<= 0`` check lets nan/inf slip
    through — nan would round-trip to NULL in SQLite and silently unbound the gate.
    """
    # Use the --flag=value form: a bare "-inf" leading dash would be parsed as an
    # option by argparse. The equals form passes it through as the value so our
    # own finite-check is what rejects it.
    outcome = _run_create_argv(monkeypatch, [f"--max-cost-usd={bad_value}"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "finite positive number" in capsys.readouterr().err


def test_schedule_create_negative_max_tokens_errors(monkeypatch, capsys):
    """A negative --max-tokens returns 1 and never posts to the API."""
    outcome = _run_create_argv(monkeypatch, ["--max-tokens", "-5"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be a positive integer" in capsys.readouterr().err


def test_schedule_create_zero_max_tokens_errors(monkeypatch, capsys):
    """A zero --max-tokens returns 1 and never posts to the API."""
    outcome = _run_create_argv(monkeypatch, ["--max-tokens", "0"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be a positive integer" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# `li schedule validate` / `li schedule apply` — declarative ScheduleSet CLI
# ---------------------------------------------------------------------------


def _parse_schedule_args(argv_tail: list[str]):
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    return parser.parse_args(["schedule", *argv_tail])


def test_schedule_validate_and_apply_registered():
    args = _parse_schedule_args(["validate", "schedules.yaml"])
    assert args.schedule_action == "validate"
    assert args.file == "schedules.yaml"

    args = _parse_schedule_args(["apply", "schedules.yaml", "--dry-run", "--adopt", "--json"])
    assert args.schedule_action == "apply"
    assert args.dry_run is True
    assert args.adopt is True
    assert args.as_json is True


@pytest.fixture
def agent_profile_cwd(tmp_path, monkeypatch):
    import lionagi.cli._providers as providers_mod

    agents_dir = tmp_path / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(providers_mod, "_find_lionagi_dirs", lambda: [agents_dir.parent])
    return tmp_path


@pytest.fixture
def temp_state_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _write_manifest(cwd) -> Path:
    manifest = Path(cwd) / "schedules.yaml"
    manifest.write_text(
        f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  nightly-review:
    trigger:
      cron:
        expression: "0 2 * * *"
        timezone: America/New_York
    target:
      kind: agent
      profile: reviewer
      prompt: "check things"
    execution:
      cwd: {cwd}
"""
    )
    return manifest


def test_schedule_validate_valid_file_exits_zero(agent_profile_cwd, capsys):
    from lionagi.studio.cli import run_schedule

    manifest = _write_manifest(agent_profile_cwd)
    args = _parse_schedule_args(["validate", str(manifest)])
    result = run_schedule(args)
    assert result == 0
    assert "VALID" in capsys.readouterr().out


def test_schedule_validate_invalid_file_exits_nonzero(agent_profile_cwd, capsys):
    from lionagi.studio.cli import run_schedule

    manifest = _write_manifest(agent_profile_cwd)
    manifest.write_text(manifest.read_text().replace("reviewer", "ghost-profile"))
    args = _parse_schedule_args(["validate", str(manifest)])
    result = run_schedule(args)
    assert result == 1
    assert "does not exist" in capsys.readouterr().err


def test_schedule_validate_missing_file_exits_nonzero(capsys):
    from lionagi.studio.cli import run_schedule

    args = _parse_schedule_args(["validate", "/no/such/schedules.yaml"])
    result = run_schedule(args)
    assert result == 1
    assert "not found" in capsys.readouterr().err


def test_schedule_apply_dry_run_never_writes(agent_profile_cwd, temp_state_db, capsys):
    import asyncio

    from lionagi.state.db import StateDB
    from lionagi.studio.cli import run_schedule

    manifest = _write_manifest(agent_profile_cwd)
    args = _parse_schedule_args(["apply", str(manifest), "--dry-run"])
    result = run_schedule(args)
    out = capsys.readouterr().out
    assert result == 0
    assert "CREATE" in out
    assert "Plan: 1 create" in out

    async def _rows():
        async with StateDB() as db:
            return await db.list_schedules()

    assert asyncio.run(_rows()) == []


def test_schedule_apply_then_idempotent_reapply(agent_profile_cwd, temp_state_db, capsys):
    from lionagi.studio.cli import run_schedule

    manifest = _write_manifest(agent_profile_cwd)
    args = _parse_schedule_args(["apply", str(manifest)])
    result = run_schedule(args)
    out = capsys.readouterr().out
    assert result == 0
    assert "Applied atomically: 1 created" in out

    args2 = _parse_schedule_args(["apply", str(manifest)])
    result2 = run_schedule(args2)
    out2 = capsys.readouterr().out
    assert result2 == 0
    assert "Applied atomically: 0 created, 0 updated, 1 unchanged" in out2


def test_schedule_apply_json_output_shape(agent_profile_cwd, temp_state_db, capsys):
    from lionagi.studio.cli import run_schedule

    manifest = _write_manifest(agent_profile_cwd)
    args = _parse_schedule_args(["apply", str(manifest), "--dry-run", "--json"])
    result = run_schedule(args)
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["dry_run"] is True
    assert payload["plan"] == [{"name": "demo/nightly-review", "action": "CREATE", "detail": None}]
