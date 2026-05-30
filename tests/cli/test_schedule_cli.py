# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li schedule` CLI command (#1165)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch


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
    from lionagi.cli.schedule import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    assert args.schedule_action == "list"


def test_schedule_create_subcommand_args():
    """li schedule create parses name + optional flags."""
    from lionagi.cli.schedule import add_schedule_subparser

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


def test_schedule_enable_disable_trigger_delete_accept_id():
    """enable/disable/trigger/delete all take an id positional arg."""
    from lionagi.cli.schedule import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    for action in ("enable", "disable", "trigger", "delete"):
        args = parser.parse_args(["schedule", action, "sched-123"])
        assert args.schedule_action == action
        assert args.id == "sched-123"


def test_schedule_runs_subcommand():
    """li schedule runs <id> parses correctly."""
    from lionagi.cli.schedule import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "runs", "sched-abc"])
    assert args.schedule_action == "runs"
    assert args.id == "sched-abc"


def test_schedule_list_dispatches_to_api(monkeypatch):
    """run_schedule list calls _api('/') and prints schedules."""
    import lionagi.cli.schedule as sched_mod

    fake_schedules = [{"id": "s1", "name": "daily", "enabled": True, "trigger_type": "cron"}]
    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: {"schedules": fake_schedules})

    from lionagi.cli.schedule import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    result = run_schedule(args)
    assert result == 0


def test_schedule_list_api_error_returns_1(monkeypatch):
    """When _api returns None (network error), run_schedule returns 1."""
    import lionagi.cli.schedule as sched_mod

    monkeypatch.setattr(sched_mod, "_api", lambda path, **kw: None)

    from lionagi.cli.schedule import add_schedule_subparser, run_schedule

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    result = run_schedule(args)
    assert result == 1
