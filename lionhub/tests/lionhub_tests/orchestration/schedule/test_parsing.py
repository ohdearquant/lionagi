# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Schedule parser and URL construction."

from __future__ import annotations

import argparse

import pytest

from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._parsing_test_support import _port_of as _port_of


def test_schedule_list_subcommand_registered():
    """li schedule list must be a recognized subcommand."""
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "list"])
    assert args.schedule_action == "list"


def test_schedule_create_subcommand_args():
    """li schedule create parses name + optional flags."""
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    for action in ("enable", "disable", "trigger", "delete"):
        args = parser.parse_args(["schedule", action, "sched-123"])
        assert args.schedule_action == action
        assert args.id == "sched-123"


def test_schedule_disable_takes_an_optional_reason():
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    assert parser.parse_args(["schedule", "disable", "sched-123"]).reason is None
    args = parser.parse_args(
        ["schedule", "disable", "sched-123", "--reason", "planned maintenance"]
    )
    assert args.reason == "planned maintenance"


def test_schedule_limits_subcommand_registered():
    """li schedule limits is a recognized subcommand and takes no positional."""
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "limits"])
    assert args.schedule_action == "limits"


def test_schedule_runs_subcommand():
    """li schedule runs <id> parses correctly."""
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "run", "9c8f4d5a2b10", "--json"])
    assert args.schedule_action == "run"
    assert args.id == "9c8f4d5a2b10"
    assert args.as_json is True


def test_schedule_status_subcommand_wait_and_json():
    """li schedule status <id> [--wait] [--json] parses correctly."""
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "trigger", "sched-abc"])
    assert args.wait is False
    args = parser.parse_args(["schedule", "trigger", "sched-abc", "--wait"])
    assert args.wait is True


def test_base_url_default(monkeypatch):
    """_base_url returns the default when no env vars are set."""
    monkeypatch.delenv("LIONAGI_STUDIO_URL", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_PORT", raising=False)

    from lionhub.orchestration.schedule.parsing import _base_url

    assert _base_url() == "http://127.0.0.1:8765"


def test_base_url_respects_studio_port(monkeypatch):
    """_base_url reflects LIONAGI_STUDIO_PORT when set."""
    monkeypatch.delenv("LIONAGI_STUDIO_URL", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)
    monkeypatch.setenv("LIONAGI_STUDIO_PORT", "9000")

    from lionhub.orchestration.schedule.parsing import _base_url

    assert _base_url() == "http://127.0.0.1:9000"


def test_base_url_respects_studio_host(monkeypatch):
    """_base_url reflects LIONAGI_STUDIO_HOST when set."""
    monkeypatch.delenv("LIONAGI_STUDIO_URL", raising=False)
    monkeypatch.setenv("LIONAGI_STUDIO_HOST", "192.168.1.5")
    monkeypatch.setenv("LIONAGI_STUDIO_PORT", "8765")

    from lionhub.orchestration.schedule.parsing import _base_url

    assert _base_url() == "http://192.168.1.5:8765"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "[::1]:8765"], ids=repr)
def test_base_url_returns_a_value_carrying_a_port_as_the_name_it_is(monkeypatch, host):
    """An address and a port in one string is not an address, so `_url_authority`
    returns it as the name it is -- with no brackets invented around it -- and the
    URL built from it is not parsable. That value is not a bind host (`--host`
    refuses it, so the CLI never writes it), and the decided reading is that the
    normalizer returns a value it cannot parse as a name rather than guessing
    which half was the host.
    """
    monkeypatch.delenv("LIONAGI_STUDIO_URL", raising=False)
    monkeypatch.setenv("LIONAGI_STUDIO_HOST", host)

    from lionhub.orchestration.schedule.parsing import _base_url, _url_authority

    assert _url_authority(host) == host
    with pytest.raises(ValueError):
        _port_of(_base_url())


def test_base_url_studio_url_takes_precedence(monkeypatch):
    """LIONAGI_STUDIO_URL wins over host/port env vars."""
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "https://studio.example.com/")
    monkeypatch.setenv("LIONAGI_STUDIO_PORT", "9999")

    from lionhub.orchestration.schedule.parsing import _base_url

    assert _base_url() == "https://studio.example.com"


def test_warn_if_cron_far_out_warns_beyond_360_days(monkeypatch):
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionhub.orchestration.schedule.parsing as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    # A cron pinned to a date almost certainly >360 days from "now" in test runs:
    # Feb 29 only exists every 4 years, so worst case is ~3 years out — always far.
    sched_mod._warn_if_cron_far_out("0 0 29 2 *")

    assert warnings
    assert "days" in warnings[0]


def test_warn_if_cron_far_out_silent_when_near(monkeypatch):
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionhub.orchestration.schedule.parsing as sched_mod

    warnings: list[str] = []
    monkeypatch.setattr(sched_mod, "warn", warnings.append)

    # Fires hourly — always within a day.
    sched_mod._warn_if_cron_far_out("0 * * * *")

    assert not warnings


def test_warn_if_cron_far_out_no_croniter_is_noop(monkeypatch):
    """Missing the optional `studio` extra's croniter dep must never break create."""
    import builtins

    import lionhub.orchestration.schedule.parsing as sched_mod

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "croniter":
            raise ImportError("no croniter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    # Should not raise.
    sched_mod._warn_if_cron_far_out("0 0 29 2 *")


def test_suggest_schedule_flag_synonym_map():
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser, suggest_schedule_flag

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
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser, suggest_schedule_flag

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)

    assert suggest_schedule_flag("--corn") == "--cron"


def test_suggest_schedule_flag_no_match_returns_none():
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser, suggest_schedule_flag

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)

    assert suggest_schedule_flag("--completely-unrelated-xyz") is None


def test_schedule_create_github_flags_parse():
    """create accepts --trigger-type github/github_poll and the github flags."""
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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


def test_schedule_create_threshold_config_flag_parses():
    """create accepts --threshold-config as a raw string (JSON parsing happens in _cmd_create)."""
    from lionhub.orchestration.schedule.parsing import add_schedule_subparser

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
