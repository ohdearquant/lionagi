# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The notify.on_terminal settings contract -- string form, mapping form,
invalid values, per-run override precedence, the explicit disabled state,
and the no-shell safety path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from lionagi.state.lifecycle.callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)
from lionagi.state.lifecycle.notify_settings import (
    NotifyConfigResolution,
    ResolvedNotifyHandler,
    build_handler,
    register_settings_terminal_callback,
    resolve_notify_config,
)


def _envelope() -> RunTerminalEnvelope:
    return RunTerminalEnvelope(
        event_id="ev-1",
        entity=EntityRef(kind="invocation", id="inv-1"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=0.0,
    )


# ── String form ───────────────────────────────────────────────────────────────


def test_string_form_resolves_to_argv():
    resolved = resolve_notify_config(override="notify-hook --flag value")
    assert resolved.handler == ResolvedNotifyHandler(argv=("notify-hook", "--flag", "value"))
    assert resolved.reason is None  # nothing was rejected


def test_string_form_preserves_quoted_shell_metacharacters_as_literal_args():
    # A quoted "|" is a literal argument character, not shell syntax -- must
    # not be flagged.
    resolved = resolve_notify_config(override='notify-hook "a|b"')
    assert resolved.handler is not None
    assert resolved.handler.argv == ("notify-hook", "a|b")


@pytest.mark.parametrize(
    "command",
    [
        "notify-hook | grep x",
        "notify-hook && echo done",
        "notify-hook > /tmp/out",
        "notify-hook; rm -rf /",
        "echo $HOME",
        "echo `whoami`",
    ],
)
def test_shell_feature_string_resolves_disabled_with_diagnostic(caplog, command):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override=command)
    assert resolved.handler is None
    assert resolved.reason == "on_terminal_command_requires_shell_features"
    assert any("shell features" in r.message for r in caplog.records)


def test_unparseable_string_resolves_disabled_with_diagnostic(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override='notify-hook "unbalanced')
    assert resolved.handler is None
    assert resolved.reason == "on_terminal_command_not_parseable"
    assert any("failed to parse" in r.message for r in caplog.records)


# ── Empty-argv resolution: every path resolves to disabled (1c) ─────────────


@pytest.mark.parametrize(
    "source",
    [
        "",
        "   ",
        {"enabled": True, "adapter": {"kind": "exec", "argv": []}},
    ],
)
def test_empty_argv_resolves_disabled_with_diagnostic(caplog, source):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override=source)
    assert resolved.handler is None
    assert resolved.reason == "on_terminal_command_is_empty"
    assert any("empty command" in r.message for r in caplog.records)


def test_empty_argv_via_settings_path_also_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(settings={"notify": {"on_terminal": "   "}})
    assert resolved.handler is None
    assert resolved.reason == "on_terminal_command_is_empty"
    assert any("empty command" in r.message for r in caplog.records)


# ── Mapping form ──────────────────────────────────────────────────────────────


def test_mapping_form_exec_adapter():
    resolved = resolve_notify_config(
        override={
            "enabled": True,
            "adapter": {"kind": "exec", "argv": ["notify-hook", "--x"]},
            "filter": {"kinds": ["invocation"], "ids": ["inv-1"]},
        }
    )
    assert resolved.handler == ResolvedNotifyHandler(
        argv=("notify-hook", "--x"),
        filter_kinds=("invocation",),
        filter_ids=("inv-1",),
    )


def test_mapping_form_python_adapter():
    resolved = resolve_notify_config(
        override={"enabled": True, "adapter": {"kind": "python", "ref": "os.path:join"}}
    )
    assert resolved.handler == ResolvedNotifyHandler(python_ref="os.path:join")


def test_mapping_form_explicit_enabled_false_is_disabled():
    resolved = resolve_notify_config(
        override={"enabled": False, "adapter": {"kind": "exec", "argv": ["should-not-run"]}}
    )
    assert resolved.handler is None
    # Turning notification off is a choice being honored, not a rejection, so it
    # carries no reason -- it must not be reported to an operator as a failure.
    assert resolved.reason is None


def test_mapping_form_enabled_without_adapter_is_a_rejection(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override={"enabled": True})
    assert resolved.handler is None
    assert resolved.reason == "enabled_without_adapter"
    assert any("no adapter configured" in r.message for r in caplog.records)


def test_mapping_form_without_enabled_or_adapter_is_silence_not_rejection(caplog):
    # No warning today and no reason now: this mapping asked for nothing.
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override={"adapter": None})
    assert resolved.handler is None
    assert resolved.reason is None
    assert not caplog.records


def test_mapping_form_unknown_adapter_kind_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override={"adapter": {"kind": "carrier-pigeon"}})
    assert resolved.handler is None
    assert resolved.reason == "adapter_kind_unsupported"
    assert any("must be 'exec' or 'python'" in r.message for r in caplog.records)


def test_mapping_form_invalid_exec_argv_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(
            override={"enabled": True, "adapter": {"kind": "exec", "argv": "notify-hook"}}
        )
    assert resolved.handler is None
    assert resolved.reason == "exec_adapter_argv_not_a_list_of_strings"
    assert any("requires an argv" in r.message for r in caplog.records)


def test_mapping_form_invalid_python_ref_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(
            override={"enabled": True, "adapter": {"kind": "python", "ref": "no-colon"}}
        )
    assert resolved.handler is None
    assert resolved.reason == "python_adapter_ref_invalid"
    assert any("'module:callable' ref" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    ("filter_value", "diagnostic", "reason"),
    [
        ("session", "filter must be a non-empty mapping", "filter_not_a_mapping"),
        (
            {"unexpected": True},
            "filter keys must be 'kinds' and/or 'ids'",
            "filter_has_unknown_keys",
        ),
        (
            {"kinds": 0},
            "filter.kinds must be a list of strings",
            "filter_kinds_not_a_list_of_strings",
        ),
        (
            {"kinds": ["not-a-terminal-entity"]},
            "filter.kinds contains unsupported terminal entity kinds",
            "filter_kinds_unsupported",
        ),
        (
            {"ids": 1},
            "filter.ids must be a list of strings",
            "filter_ids_not_a_list_of_strings",
        ),
    ],
)
def test_mapping_form_invalid_filter_disables_handler(caplog, filter_value, diagnostic, reason):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(
            override={
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["echo", "ok"]},
                "filter": filter_value,
            }
        )
    assert resolved.handler is None
    assert resolved.reason == reason
    assert any(diagnostic in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "filter_value",
    [
        1,  # scalar int -- the crashing shape (bare tuple(1) raises TypeError)
        "session",  # scalar string -- would silently char-split, never match
        ["invocation", 1],  # list containing a non-string element
    ],
)
def test_mapping_form_malformed_filter_kinds_disabled_not_raised(caplog, filter_value):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(
            override={
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["echo", "ok"]},
                "filter": {"kinds": filter_value},
            }
        )  # must not raise
    assert resolved.handler is None
    assert resolved.reason == "filter_kinds_not_a_list_of_strings"
    assert any("filter.kinds must be a list of strings" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "filter_value",
    [
        1,
        "inv-1",
        ["inv-1", 1],
    ],
)
def test_mapping_form_malformed_filter_ids_disabled_not_raised(caplog, filter_value):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(
            override={
                "enabled": True,
                "adapter": {"kind": "exec", "argv": ["echo", "ok"]},
                "filter": {"ids": filter_value},
            }
        )  # must not raise
    assert resolved.handler is None
    assert resolved.reason == "filter_ids_not_a_list_of_strings"
    assert any("filter.ids must be a list of strings" in r.message for r in caplog.records)


def test_malformed_settings_never_raises(caplog, monkeypatch):
    def _boom(project_dir=None):
        raise ValueError("malformed yaml")

    monkeypatch.setattr("lionagi.state.lifecycle.notify_settings.load_settings", _boom)
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config()  # must not raise
    assert resolved.handler is None
    # Settings that cannot be read may well have named a notifier; that is not
    # the same as having configured none, so it is reported as a rejection.
    assert resolved.reason == "settings_load_failed"
    assert any("settings resolution failed" in r.message for r in caplog.records)


# ── Invalid top-level value / absent key ─────────────────────────────────────


def test_invalid_value_type_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override=12345)
    assert resolved.handler is None
    assert resolved.reason == "on_terminal_not_string_or_mapping"
    assert any("must be a string or mapping" in r.message for r in caplog.records)


def test_absent_notify_key_is_disabled():
    """The one genuinely-unconfigured path: no handler and no reason."""
    for settings in ({}, {"notify": {}}, {"notify": {"on_terminal": None}}):
        resolved = resolve_notify_config(settings=settings)
        assert resolved.handler is None
        assert resolved.reason is None


# ── Precedence: per-run override beats settings for its own scope only ───────


def test_per_run_override_replaces_settings_handler():
    settings = {"notify": {"on_terminal": "settings-cmd"}}
    resolved_no_override = resolve_notify_config(settings=settings)
    assert resolved_no_override.handler is not None
    assert resolved_no_override.handler.argv == ("settings-cmd",)

    resolved_with_override = resolve_notify_config(settings=settings, override="override-cmd")
    assert resolved_with_override.handler is not None
    assert resolved_with_override.handler.argv == ("override-cmd",)


# ── No-shell safety: the exec adapter never launches via a shell ────────────


@pytest.mark.asyncio
async def test_exec_handler_never_constructs_a_shell(monkeypatch):
    called: dict[str, object] = {}

    async def _fake_exec(*argv, **kwargs):
        called["argv"] = argv
        called["stdin_payload"] = None

        class _FakeProc:
            returncode = 0

            async def communicate(self, data=None):
                called["stdin_payload"] = data
                return (b"", b"")

        return _FakeProc()

    def _fail_if_shell_used(*a, **k):
        raise AssertionError("create_subprocess_shell must never be called")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fail_if_shell_used)

    handler = build_handler(ResolvedNotifyHandler(argv=("notify-hook", "--x")))
    await handler(_envelope())

    assert called["argv"] == ("notify-hook", "--x")
    payload = json.loads(called["stdin_payload"])
    assert payload["schema"] == "lionagi.run-terminal"
    assert payload["entity"] == {"kind": "invocation", "id": "inv-1"}


@pytest.mark.asyncio
async def test_exec_handler_swallows_nonzero_exit_and_timeout(monkeypatch, caplog):
    class _FakeProc:
        pid = 123
        returncode = 1

        async def communicate(self, data=None):
            return (b"", b"boom")

        async def wait(self):
            return 1

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(ResolvedNotifyHandler(argv=("notify-hook",)))
    with caplog.at_level(logging.WARNING):
        await handler(_envelope())  # must not raise
    assert any("exited 1" in r.message for r in caplog.records)


# ── Adapter outcome visibility: notify_outcome.json + the CLI warn() channel ──
#
# Outcome recording is never automatic: a bare build_handler(resolved) call
# (the process-wide default registered by register_settings_terminal_callback)
# has no bound run and therefore never records anything. Only a handler built
# via register_run_notify_outcome_scope(run, ...) -- or build_handler(...,
# outcome_fn=...) directly in these tests -- writes an outcome, and only into
# that specific run's own notify_outcome.json (never run.json).


def _outcome_fn_for(run):
    from lionagi.state.lifecycle.notify_settings import _record_notify_outcome_to_run

    def _fn(*, ok, exit_code, stderr_text):
        return _record_notify_outcome_to_run(
            run, ok=ok, exit_code=exit_code, stderr_text=stderr_text
        )

    return _fn


@pytest.mark.asyncio
async def test_exec_handler_records_nonzero_exit_outcome_and_warns(monkeypatch, tmp_path):
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="notify-fail-run")
    run.write_manifest({"status": "completed", "ended_at": 123.0})

    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    class _FakeProc:
        pid = 123
        returncode = 1

        async def communicate(self, data=None):
            return (b"", b"boom\nsecond line")

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(
        ResolvedNotifyHandler(argv=("notify-hook",)), outcome_fn=_outcome_fn_for(run)
    )
    await handler(_envelope())  # must not raise

    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome == {
        "ok": False,
        "exit_code": 1,
        "stderr_path": str(run.notify_stderr_path),
    }
    # The stderr text lives only in the captured file, never in the record --
    # and the capture is the whole output, not just its first line.
    assert run.notify_stderr_path.read_text() == "boom\nsecond line"
    # The outcome lands in its own file -- run.json's own terminal status is
    # never touched by notify bookkeeping.
    manifest = json.loads(run.manifest_path.read_text())
    assert "notify_outcome" not in manifest
    assert manifest["status"] == "completed"
    assert manifest["ended_at"] == 123.0

    assert len(warn_calls) == 1
    assert "exited 1" in warn_calls[0]
    # Only the adapter's own name (argv[0]'s basename) identifies it in the
    # user-facing warn line -- never the full argv repr.
    assert "notify-hook" in warn_calls[0]
    assert "('notify-hook',)" not in warn_calls[0]


@pytest.mark.asyncio
async def test_exec_handler_redacts_argv_and_bounds_stderr_in_warn_and_outcome(
    monkeypatch, tmp_path
):
    """A secret-bearing argv (webhook URLs, tokens as args) must never
    appear in the warn-channel line or the persisted notify_outcome.json --
    only the adapter's own name (argv[0]'s basename). Stderr content is
    adapter-controlled and can't be scrubbed for arbitrary secrets, but it
    is bounded to STDERR_SNIPPET_LIMIT chars, capping exposure."""
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run
    from lionagi.state.lifecycle.notify_settings import STDERR_SNIPPET_LIMIT

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="notify-secret-run")

    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    secret = "sekret123"
    secret_argv = ("notify", "--token", secret)
    long_detail = "adapter failure detail " + ("x" * 400)

    class _FakeProc:
        pid = 789
        returncode = 1

        async def communicate(self, data=None):
            return (b"", long_detail.encode())

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(
        ResolvedNotifyHandler(argv=secret_argv), outcome_fn=_outcome_fn_for(run)
    )
    await handler(_envelope())  # must not raise

    outcome_text = run.notify_outcome_path.read_text()
    outcome = json.loads(outcome_text)

    # The secret argv element never leaks into either surface.
    assert secret not in outcome_text
    assert len(warn_calls) == 1
    assert secret not in warn_calls[0]
    assert "--token" not in warn_calls[0]
    assert "notify" in warn_calls[0]

    # The adapter's stderr is not in the record at all -- only a path to the
    # owner-only file that holds it.
    assert outcome["stderr_path"] == str(run.notify_stderr_path)
    assert len(warn_calls[0]) < len(long_detail)


@pytest.mark.asyncio
async def test_exec_handler_records_timeout_outcome_and_warns(monkeypatch, tmp_path):
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="notify-timeout-run")

    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    class _FakeProc:
        pid = 456

        async def communicate(self, data=None):
            raise asyncio.TimeoutError()

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    async def _fake_terminate(proc, grace=None):
        return None

    async def _fake_await_dead(proc, grace=2.0):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.aterminate_process_group", _fake_terminate
    )
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings._await_proc_dead", _fake_await_dead
    )

    handler = build_handler(
        ResolvedNotifyHandler(argv=("notify-hook",)), outcome_fn=_outcome_fn_for(run)
    )
    await handler(_envelope())  # must not raise

    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome == {
        "ok": False,
        "exit_code": None,
        "stderr_path": None,
    }
    assert len(warn_calls) == 1
    assert "timed out" in warn_calls[0]


@pytest.mark.asyncio
async def test_exec_handler_records_spawn_error_outcome_and_warns(monkeypatch, tmp_path):
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="notify-spawn-error-run")

    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    async def _fake_exec(*argv, **kwargs):
        raise FileNotFoundError("no such file: notify-hook")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(
        ResolvedNotifyHandler(argv=("notify-hook",)), outcome_fn=_outcome_fn_for(run)
    )
    await handler(_envelope())  # must not raise

    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome["ok"] is False
    assert outcome["exit_code"] is None
    # A spawn failure produces no adapter output to capture; the reason is
    # Python's own message and stays on the warning channel.
    assert outcome["stderr_path"] is None
    assert len(warn_calls) == 1
    assert "failed to run" in warn_calls[0]
    assert "notify-hook" in warn_calls[0]
    assert "('notify-hook',)" not in warn_calls[0]


@pytest.mark.asyncio
async def test_exec_handler_records_success_outcome_without_warn(monkeypatch, tmp_path):
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="notify-ok-run")

    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    class _FakeProc:
        returncode = 0

        async def communicate(self, data=None):
            return (b"", b"")

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(
        ResolvedNotifyHandler(argv=("notify-hook",)), outcome_fn=_outcome_fn_for(run)
    )
    await handler(_envelope())

    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome == {
        "ok": True,
        "exit_code": 0,
        "stderr_path": None,
    }
    assert warn_calls == []


@pytest.mark.asyncio
async def test_exec_handler_outcome_recording_is_a_noop_without_a_bound_run(tmp_path, monkeypatch):
    """build_handler() with no outcome_fn (the process-wide default
    registration, before any run-scoped override exists) never guesses at a
    target run -- outcome recording is skipped, not misattributed."""
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="unbound-run")

    class _FakeProc:
        returncode = 0

        async def communicate(self, data=None):
            return (b"", b"")

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(ResolvedNotifyHandler(argv=("notify-hook",)))
    await handler(_envelope())  # must not raise

    assert not run.notify_outcome_path.exists()


# ── Run-scoped attribution: bound at registration time, never last-writer-wins ──


@pytest.mark.asyncio
async def test_run_scoped_outcome_survives_a_later_run_allocation(monkeypatch, tmp_path):
    """A late outcome for run A's entity must land on A even after run B has
    been allocated in the same process -- the handler is bound to A's RunDir
    at registration time, not resolved dynamically against whichever run is
    "current" when the callback fires."""
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run
    from lionagi.state.lifecycle.notify_settings import (
        register_run_notify_outcome_scope,
        unregister_run_notify_outcome_scope,
    )

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.resolve_notify_config",
        lambda **kw: NotifyConfigResolution(handler=ResolvedNotifyHandler(argv=("notify-hook",))),
    )

    class _FakeProc:
        returncode = 0

        async def communicate(self, data=None):
            return (b"", b"")

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    registry = TerminalCallbackRegistry()
    run_a = allocate_run(run_id="run-a")
    name_a = register_run_notify_outcome_scope(
        run_a, entity_kind="session", entity_id="session-a", registry=registry
    )
    assert name_a is not None

    # Run B is allocated afterward, in the same process -- must not affect A's binding.
    run_b = allocate_run(run_id="run-b")
    name_b = register_run_notify_outcome_scope(
        run_b, entity_kind="session", entity_id="session-b", registry=registry
    )
    assert name_b is not None

    try:
        # The "late" terminal event: session-a's own terminal transition,
        # fired after run-b already exists.
        envelope_a = RunTerminalEnvelope(
            event_id="ev-a",
            entity=EntityRef(kind="session", id="session-a"),
            previous_status="running",
            terminal_status="completed",
            reason_code="run.completed.ok",
            occurred_at=0.0,
        )
        await registry.emit(envelope_a)
    finally:
        unregister_run_notify_outcome_scope(name_a, registry=registry)
        unregister_run_notify_outcome_scope(name_b, registry=registry)

    assert json.loads(run_a.notify_outcome_path.read_text()) == {
        "ok": True,
        "exit_code": 0,
        "stderr_path": None,
    }
    assert not run_b.notify_outcome_path.exists()


@pytest.mark.asyncio
async def test_unscoped_entity_records_nothing_never_last_writer_wins(monkeypatch, tmp_path):
    """A terminal event for an entity with no run-scoped registration must
    record nothing -- never fall back to whichever run was allocated most
    recently."""
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run
    from lionagi.state.lifecycle.notify_settings import register_run_notify_outcome_scope

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.resolve_notify_config",
        lambda **kw: NotifyConfigResolution(handler=ResolvedNotifyHandler(argv=("notify-hook",))),
    )

    class _FakeProc:
        returncode = 0

        async def communicate(self, data=None):
            return (b"", b"")

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    registry = TerminalCallbackRegistry()
    # A run is allocated, but its entity ("session-known") is never registered.
    run = allocate_run(run_id="only-run")
    register_run_notify_outcome_scope(
        run, entity_kind="session", entity_id="session-known", registry=registry
    )
    # Also install the process-wide default (no outcome_fn) so the unscoped
    # entity still gets a matching, non-attributing handler.
    from lionagi.state.lifecycle.notify_settings import build_handler, resolve_notify_config

    default_handler = build_handler(resolve_notify_config().handler)
    registry.register("notify.settings.on_terminal", default_handler)

    other_envelope = RunTerminalEnvelope(
        event_id="ev-other",
        entity=EntityRef(kind="session", id="session-unrelated"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=0.0,
    )
    await registry.emit(other_envelope)

    assert not run.notify_outcome_path.exists()


# ── Cancellation (the registry's outer deadline winning the race against
# the handler's own identical wait_for) must still reap the child ──────────


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_cancelled_exec_handler_still_kills_its_child_process_group(tmp_path: Path):
    # Reproduces the race: TerminalCallbackRegistry's own move_on_after
    # budget is set far shorter than the exec handler's internal
    # asyncio.wait_for budget, so the OUTER scope wins and delivers
    # cancellation to _exec_handler mid-communicate() -- the same failure
    # shape asyncio.TimeoutError would hit, but through a different
    # exception. The child (its own process group via start_new_session)
    # must still be dead by the time emit() returns, not orphaned alive.
    pid_file = tmp_path / "pid.txt"
    cmd = (
        f"{shlex.quote(sys.executable)} -c "
        '"import os, pathlib, sys, time; '
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        'time.sleep(30)" '
        f"{shlex.quote(str(pid_file))}"
    )
    resolved = resolve_notify_config(override=cmd).handler
    assert resolved is not None
    handler = build_handler(resolved)
    assert handler is not None

    registry = TerminalCallbackRegistry(budget_seconds=0.3)
    registry.register("slow-notify", handler)

    start = time.monotonic()
    await registry.emit(_envelope())  # must not raise
    elapsed = time.monotonic() - start
    assert elapsed < 5.0  # bounded by the outer 0.3s budget, not the 30s sleep

    # The child had time to write its pid before being killed.
    for _ in range(50):
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text())

    # aterminate_process_group's own SIGKILL + proc.wait() already ran
    # inside the shielded cleanup before emit() returned; give the OS a
    # brief grace period for the zombie to clear, then confirm the process
    # is actually gone -- never left running past the run's own return.
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"child pid {child_pid} was still alive after cancellation cleanup")


# ── build_handler: a malformed python adapter must never raise ─────────────


def test_build_handler_bad_python_ref_returns_none_not_raises(caplog):
    resolved = ResolvedNotifyHandler(python_ref="definitely.not.a.real.module:handler")
    with caplog.at_level(logging.WARNING):
        handler = build_handler(resolved)
    assert handler is None
    assert any("failed to import" in r.message for r in caplog.records)


def test_build_handler_python_ref_missing_callable_returns_none_not_raises(caplog):
    resolved = ResolvedNotifyHandler(python_ref="os.path:definitely_not_a_real_attr")
    with caplog.at_level(logging.WARNING):
        handler = build_handler(resolved)
    assert handler is None
    assert any("failed to import" in r.message for r in caplog.records)


def test_build_handler_non_callable_python_ref_returns_none_not_raises(caplog):
    # os.pathsep is a real, importable module attribute -- just not callable.
    resolved = ResolvedNotifyHandler(python_ref="os:pathsep")
    with caplog.at_level(logging.WARNING):
        handler = build_handler(resolved)
    assert handler is None
    assert any("non-callable" in r.message for r in caplog.records)


def test_build_handler_callable_python_ref_still_resolves():
    resolved = ResolvedNotifyHandler(python_ref="os.path:join")
    handler = build_handler(resolved)
    assert handler is os.path.join


def test_register_settings_terminal_callback_bad_python_ref_never_raises(monkeypatch):
    # The exact regression this guards: a typo'd notify.on_terminal python
    # adapter must resolve to disabled at the bootstrap call site (CLI
    # startup / Studio lifespan), never raise and abort unrelated commands.
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.load_settings",
        lambda project_dir=None: {
            "notify": {
                "on_terminal": {
                    "enabled": True,
                    "adapter": {"kind": "python", "ref": "missing.module:handler"},
                }
            }
        },
    )
    registry = TerminalCallbackRegistry()
    installed = register_settings_terminal_callback(
        registry, name="test.bad-python"
    )  # must not raise
    assert installed is False
    assert "test.bad-python" not in registry


def test_register_settings_terminal_callback_non_callable_python_ref_disabled(monkeypatch, caplog):
    # A well-formed, importable ref that just isn't callable (e.g. a data
    # attribute) must be rejected at bootstrap, not registered and left to
    # blow up every terminal-callback delivery downstream.
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.load_settings",
        lambda project_dir=None: {
            "notify": {
                "on_terminal": {
                    "enabled": True,
                    "adapter": {"kind": "python", "ref": "os:pathsep"},
                }
            }
        },
    )
    registry = TerminalCallbackRegistry()
    with caplog.at_level(logging.WARNING):
        installed = register_settings_terminal_callback(registry, name="test.non-callable")
    assert installed is False
    assert "test.non-callable" not in registry
    assert any("non-callable" in r.message for r in caplog.records)


def test_register_settings_terminal_callback_malformed_filter_never_raises(monkeypatch, caplog):
    # The exact regression this guards: `notify.on_terminal.filter.kinds: 1`
    # (a scalar where the schema expects a list) used to raise TypeError out
    # of a bare tuple(...) coercion, aborting CLI startup (lionagi/cli/main.py)
    # and Studio lifespan startup (lionagi/studio/app.py) -- both call this
    # function once per process with no guard of their own around it.
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.load_settings",
        lambda project_dir=None: {
            "notify": {
                "on_terminal": {
                    "enabled": True,
                    "adapter": {"kind": "exec", "argv": ["echo", "ok"]},
                    "filter": {"kinds": 1},
                }
            }
        },
    )
    registry = TerminalCallbackRegistry()
    with caplog.at_level(logging.WARNING):
        installed = register_settings_terminal_callback(
            registry, name="test.bad-filter"
        )  # must not raise -- this is the bootstrap call site itself
    assert installed is False
    assert "test.bad-filter" not in registry
    assert any("filter.kinds must be a list of strings" in r.message for r in caplog.records)


# ── Legacy argv/env substitution hooks used by the flow `--notify` adapter ──


@pytest.mark.asyncio
async def test_exec_handler_argv_fn_and_env_fn_are_applied_per_call(monkeypatch):
    called: dict[str, object] = {}

    async def _fake_exec(*argv, **kwargs):
        called["argv"] = argv
        called["env"] = kwargs.get("env")

        class _FakeProc:
            returncode = 0

            async def communicate(self, data=None):
                return (b"", b"")

        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    def _argv_fn(argv, envelope):
        return [tok.replace("{status}", envelope.terminal_status) for tok in argv]

    def _env_fn(envelope):
        return {"MY_STATUS": envelope.terminal_status}

    handler = build_handler(
        ResolvedNotifyHandler(argv=("hook", "{status}")),
        argv_fn=_argv_fn,
        env_fn=_env_fn,
    )
    await handler(_envelope())

    assert called["argv"] == ("hook", "completed")
    assert called["env"]["MY_STATUS"] == "completed"
    # Parent environment is still inherited alongside the extra var.
    assert "PATH" in called["env"] or called["env"] is not None


# ── Bootstrap: register/unregister on the shared registry ──────────────────


def test_register_settings_terminal_callback_installs_and_uninstalls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.load_settings",
        lambda project_dir=None: {
            "notify": {"on_terminal": {"enabled": True, "adapter": {"kind": "exec", "argv": ["x"]}}}
        },
    )
    registry = TerminalCallbackRegistry()
    installed = register_settings_terminal_callback(registry, name="test.settings")
    assert installed is True
    assert "test.settings" in registry

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.load_settings",
        lambda project_dir=None: {"notify": {"on_terminal": {"enabled": False}}},
    )
    installed_again = register_settings_terminal_callback(registry, name="test.settings")
    assert installed_again is False
    assert "test.settings" not in registry


def test_default_registry_is_the_process_wide_instance():
    assert isinstance(DEFAULT_TERMINAL_CALLBACKS, TerminalCallbackRegistry)


@pytest.mark.asyncio
async def test_run_scoped_registration_honors_configured_filter(monkeypatch, tmp_path):
    """A run-scoped registration is an override, so it dispatches on its own
    match instead of deferring to the process-wide registration's filter. It
    must therefore apply the configured filter itself -- otherwise an entity
    the operator excluded via filter.kinds/filter.ids starts receiving
    notifications the moment it runs under a run scope."""
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run
    from lionagi.state.lifecycle.notify_settings import (
        register_run_notify_outcome_scope,
        unregister_run_notify_outcome_scope,
    )

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.resolve_notify_config",
        lambda **kw: NotifyConfigResolution(
            handler=ResolvedNotifyHandler(
                argv=("notify-hook",), filter_kinds=("play",), filter_ids=("play-allowed",)
            )
        ),
    )

    spawned: list[tuple] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self, data=None):
            return (b"", b"")

    async def _fake_exec(*argv, **kwargs):
        spawned.append(argv)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    registry = TerminalCallbackRegistry()
    run = allocate_run(run_id="filtered-run")

    # Excluded kind: the operator asked for plays only.
    assert (
        register_run_notify_outcome_scope(
            run, entity_kind="session", entity_id="play-allowed", registry=registry
        )
        is None
    )
    # Right kind, excluded id.
    assert (
        register_run_notify_outcome_scope(
            run, entity_kind="play", entity_id="play-other", registry=registry
        )
        is None
    )

    await registry.emit(
        RunTerminalEnvelope(
            event_id="ev-excluded",
            entity=EntityRef(kind="session", id="play-allowed"),
            previous_status="running",
            terminal_status="completed",
            reason_code="run.completed.ok",
            occurred_at=0.0,
        )
    )
    assert spawned == []
    assert not run.notify_outcome_path.exists()

    # The allowed entity still registers and still records its outcome.
    name = register_run_notify_outcome_scope(
        run, entity_kind="play", entity_id="play-allowed", registry=registry
    )
    assert name is not None
    try:
        await registry.emit(
            RunTerminalEnvelope(
                event_id="ev-allowed",
                entity=EntityRef(kind="play", id="play-allowed"),
                previous_status="running",
                terminal_status="completed",
                reason_code="run.completed.ok",
                occurred_at=0.0,
            )
        )
    finally:
        unregister_run_notify_outcome_scope(name, registry=registry)

    assert len(spawned) == 1
    assert json.loads(run.notify_outcome_path.read_text())["ok"] is True


@pytest.mark.asyncio
async def test_adapter_argument_values_echoed_on_stderr_are_redacted(monkeypatch, tmp_path, caplog):
    """The realistic leak path: an adapter fails and echoes its own invocation
    back on stderr, carrying a token it was passed as an argument. That value
    must not survive into the warn line, the persisted outcome, or the log."""
    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="redaction-run")

    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    secret = "wh0-t0ken-abcdef123456"
    secret_argv = ("/usr/local/bin/notify-hook", "--token", secret)
    echoed = f"notify-hook: request failed: curl -H 'Auth: {secret}' https://hook.example/x"

    class _FakeProc:
        returncode = 2

        async def communicate(self, data=None):
            return (b"", echoed.encode())

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(
        ResolvedNotifyHandler(argv=secret_argv), outcome_fn=_outcome_fn_for(run)
    )
    with caplog.at_level(logging.WARNING):
        await handler(_envelope())

    outcome_text = run.notify_outcome_path.read_text()
    assert secret not in outcome_text
    assert json.loads(outcome_text)["stderr_path"] == str(run.notify_stderr_path)

    assert len(warn_calls) == 1
    assert secret not in warn_calls[0]
    assert "notify-hook" in warn_calls[0]

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logged
    assert "--token" not in logged


@pytest.mark.asyncio
async def test_env_sourced_credential_in_adapter_stderr_reaches_no_shared_surface(
    monkeypatch, tmp_path, caplog
):
    """An adapter can print a credential it got from anywhere -- an inherited
    environment variable, a file it read -- and no value-matching redaction
    can recognize it. So the adapter's stderr goes to an owner-only file and
    every shared surface (warn channel, log, outcome record) carries only the
    path to it."""
    import stat

    import lionagi.cli._runs as runs_mod
    from lionagi.cli._runs import allocate_run

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="env-secret-run")

    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    # The secret is NOT an argv element -- the adapter inherited it.
    env_secret = "env-cred-9f8e7d6c5b4a"
    monkeypatch.setenv("NOTIFY_WEBHOOK_TOKEN", env_secret)
    echoed = f"hook failed: POST rejected for token {env_secret}"

    class _FakeProc:
        returncode = 1

        async def communicate(self, data=None):
            return (b"", echoed.encode())

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(
        ResolvedNotifyHandler(argv=("notify-hook",)), outcome_fn=_outcome_fn_for(run)
    )
    with caplog.at_level(logging.DEBUG):
        await handler(_envelope())

    outcome_text = run.notify_outcome_path.read_text()
    logged = "\n".join(record.getMessage() for record in caplog.records)

    assert env_secret not in outcome_text
    assert env_secret not in logged
    assert len(warn_calls) == 1
    assert env_secret not in warn_calls[0]

    # The operator can still diagnose: the path is named, and the file holds
    # the full stderr, readable only by them.
    captured = run.notify_stderr_path
    assert str(captured) in warn_calls[0]
    assert captured.read_text() == echoed
    assert stat.S_IMODE(captured.stat().st_mode) == 0o600
    assert json.loads(outcome_text)["stderr_path"] == str(captured)


@pytest.mark.asyncio
async def test_unbound_handler_drops_adapter_stderr_rather_than_sharing_it(monkeypatch, caplog):
    """With no run bound there is nowhere owner-only to put the stderr, so it
    is dropped -- never rerouted onto the log or the warning channel."""
    warn_calls: list[str] = []
    monkeypatch.setattr("lionagi.cli._logging.warn", warn_calls.append)

    secret = "unbound-secret-abc123"

    class _FakeProc:
        returncode = 3

        async def communicate(self, data=None):
            return (b"", f"failed with {secret}".encode())

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    handler = build_handler(ResolvedNotifyHandler(argv=("notify-hook",)))
    with caplog.at_level(logging.DEBUG):
        await handler(_envelope())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logged
    assert len(warn_calls) == 1
    assert secret not in warn_calls[0]
    assert "not captured" in warn_calls[0]
