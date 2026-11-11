# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule command adapter dispatch and output behavior."""

from __future__ import annotations

import argparse

import pytest


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


def test_schedule_action_kind_choices_match_store_vocabulary():
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser
    from lionhub.orchestration.scheduler.subprocess import (
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
    import lionhub.cli.schedule as schedule_cli

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


def test_schedule_disable_sends_its_reason_and_rejects_a_blank_one(monkeypatch, capsys):
    import lionhub.cli.schedule as schedule_cli

    captured: dict = {}

    def fake_api(path, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"ok": True}

    monkeypatch.setattr(schedule_cli, "_api", fake_api)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    schedule_cli.add_schedule_subparser(sub)

    args = parser.parse_args(
        ["schedule", "disable", "sched-123", "--reason", " planned maintenance "]
    )
    assert schedule_cli.run_schedule(args) == 0
    assert captured == {
        "path": "/sched-123/disable",
        "method": "POST",
        "body": {"reason": "planned maintenance"},
    }

    blank = parser.parse_args(["schedule", "disable", "sched-123", "--reason", "   "])
    captured.clear()
    assert schedule_cli.run_schedule(blank) == 1
    assert "reason" in capsys.readouterr().err
    assert captured == {}

    bare = parser.parse_args(["schedule", "disable", "sched-123"])
    assert schedule_cli.run_schedule(bare) == 0
    assert captured == {"path": "/sched-123/disable", "method": "POST", "body": None}


def test_schedule_limits_dispatches_to_api_and_prints_values(monkeypatch, capsys):
    """run_schedule limits calls _api('/limits') and prints cap + inflight
    for both the scheduled and the ad-hoc lane."""
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "_api",
        lambda path, **kw: {
            "max_scheduled_concurrent": 4,
            "current_inflight": 1,
            "max_adhoc_concurrent": 6,
            "current_adhoc_inflight": 2,
        },
    )

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "limits"])
    result = run_schedule(args)

    assert result == 0
    out = capsys.readouterr().out
    assert "4" in out
    assert "1" in out
    assert "6" in out
    assert "2" in out


def test_schedule_limits_unlimited_display(monkeypatch, capsys):
    """A cap of 0 (unlimited) prints 'unlimited' rather than the digit 0."""
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "_api",
        lambda path, **kw: {"max_scheduled_concurrent": 0, "current_inflight": 2},
    )

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: None)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "limits"])
    result = run_schedule(args)
    assert result == 1


def test_schedule_runs_limit_out_of_range_rejected(monkeypatch, capsys):
    """--limit outside [1, 200] is rejected before any API call."""
    import lionhub.cli.schedule as sched_mod

    api_called = []
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_called.append(1))

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    captured = {}

    def _fake_api(path, method="GET", body=None):
        captured["path"] = path
        return {"runs": [], "limit": 5, "offset": 0, "has_next": False}

    monkeypatch.setattr(sched_mod, "_api", _fake_api)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: {"runs": []})

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

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

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    api_response = {"runs": [{"id": "run1", "status": "completed"}], "limit": 20, "offset": 0}
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: api_response)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "runs", "sched-abc", "--json"])
    result = run_schedule(args)

    assert result == 0
    import json as _json

    assert _json.loads(capsys.readouterr().out) == api_response


def test_schedule_run_singular_dispatches_and_prints_table(monkeypatch, capsys):
    import lionhub.cli.schedule as sched_mod

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

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "run", "run1"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "run1" in out and "tests faile" in out


def test_schedule_run_singular_not_found_returns_1(monkeypatch):
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: None)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "run", "nonexistent"])
    result = run_schedule(args)

    assert result == 1


def test_schedule_status_human_output_includes_inspect_line(monkeypatch, capsys):
    """The status block must route to `li monitor <invocation>` for drill-in."""
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: _status_response())

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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

    import lionhub.cli.schedule as sched_mod

    response = _status_response()
    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: response)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc", "--json"])
    result = run_schedule(args)

    parsed = _json.loads(capsys.readouterr().out)
    assert result == response["exit_code"]
    assert parsed == response


def test_schedule_status_no_runs_yet(monkeypatch, capsys):
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(
        sched_mod, "_api", lambda *a, **kw: _status_response(latest_run=None, exit_code=2)
    )

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc"])
    result = run_schedule(args)

    out = capsys.readouterr().out
    assert result == 2
    assert "last run:    (none)" in out


def test_schedule_status_not_found_returns_1(monkeypatch):
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: None)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc"])
    result = run_schedule(args)

    assert result == 1


def test_schedule_status_wait_polls_until_terminal(monkeypatch):
    """--wait must keep polling while the latest run is still 'running'."""
    import lionhub.cli.schedule as sched_mod

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

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "status", "sched-abc", "--wait"])
    result = run_schedule(args)

    assert result == 1
    assert len(calls) == 3


def test_schedule_trigger_without_wait_unchanged(monkeypatch, capsys):
    """No --wait: trigger returns as soon as the run id comes back (existing behavior)."""
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: {"ok": True, "run_id": "run1"})

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

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

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    def _fake_api(path, method="GET", body=None):
        if path == "/sched-abc/trigger":
            return {"ok": True, "run_id": "run1"}
        return None

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(sched_mod, "_TRIGGER_WAIT_GRACE_SECONDS", 0.01)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    calls = []

    def _fake_api(path, method="GET", body=None):
        calls.append(path)
        if path == "/sched-abc/trigger":
            return {"ok": True, "run_id": "run1"}
        return {"status": "timed_out", "outcome": {"code": "timed_out", "summary": "timed out"}}

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(sched_mod, "_TRIGGER_WAIT_MAX_SECONDS", 0.05)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    calls = []

    def _fake_api(path, **kw):
        calls.append(path)
        return _status_response(latest_run={"status": "timed_out"}, exit_code=124)

    monkeypatch.setattr(sched_mod, "_api", _fake_api)
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(sched_mod, "_TRIGGER_WAIT_MAX_SECONDS", 0.05)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda *a, **kw: {"ok": True})

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc", "--wait"])
    result = run_schedule(args)

    assert result == 0


def test_schedule_list_dispatches_to_api(monkeypatch):
    """run_schedule list calls _api('/') and prints schedules."""
    import lionhub.cli.schedule as sched_mod

    fake_schedules = [{"id": "s1", "name": "daily", "enabled": True, "trigger_type": "cron"}]
    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: {"schedules": fake_schedules})

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    result = run_schedule(args)
    assert result == 0


def test_schedule_list_api_error_returns_1(monkeypatch):
    """When _api returns None (network error), run_schedule returns 1."""
    import lionhub.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: None)

    from lionhub.cli.schedule import run_schedule
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    result = run_schedule(args)
    assert result == 1
