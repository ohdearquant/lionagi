# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li schedule` CLI command: subcommands, argument parsing, and dispatch."""

from __future__ import annotations

import argparse
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
    assert "must be a positive number" in capsys.readouterr().err


def test_schedule_create_zero_max_cost_usd_errors(monkeypatch, capsys):
    """A zero --max-cost-usd returns 1 and never posts to the API."""
    outcome = _run_create_argv(monkeypatch, ["--max-cost-usd", "0"])
    assert outcome["result"] == 1
    assert outcome["called"] is False
    assert "must be a positive number" in capsys.readouterr().err


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
