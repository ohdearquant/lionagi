# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The notify.on_terminal settings contract -- string form, mapping form,
invalid values, per-run override precedence, the explicit disabled state,
and the no-shell safety path."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from lionagi.state.lifecycle.callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)
from lionagi.state.lifecycle.notify_settings import (
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
    assert resolved == ResolvedNotifyHandler(argv=("notify-hook", "--flag", "value"))


def test_string_form_preserves_quoted_shell_metacharacters_as_literal_args():
    # A quoted "|" is a literal argument character, not shell syntax -- must
    # not be flagged.
    resolved = resolve_notify_config(override='notify-hook "a|b"')
    assert resolved is not None
    assert resolved.argv == ("notify-hook", "a|b")


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
    assert resolved is None
    assert any("shell features" in r.message for r in caplog.records)


def test_unparseable_string_resolves_disabled_with_diagnostic(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override='notify-hook "unbalanced')
    assert resolved is None
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
    assert resolved is None
    assert any("empty command" in r.message for r in caplog.records)


def test_empty_argv_via_settings_path_also_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(settings={"notify": {"on_terminal": "   "}})
    assert resolved is None
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
    assert resolved == ResolvedNotifyHandler(
        argv=("notify-hook", "--x"),
        filter_kinds=("invocation",),
        filter_ids=("inv-1",),
    )


def test_mapping_form_python_adapter():
    resolved = resolve_notify_config(
        override={"enabled": True, "adapter": {"kind": "python", "ref": "os.path:join"}}
    )
    assert resolved == ResolvedNotifyHandler(python_ref="os.path:join")


def test_mapping_form_explicit_enabled_false_is_disabled():
    resolved = resolve_notify_config(
        override={"enabled": False, "adapter": {"kind": "exec", "argv": ["should-not-run"]}}
    )
    assert resolved is None


def test_mapping_form_unknown_adapter_kind_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override={"adapter": {"kind": "carrier-pigeon"}})
    assert resolved is None
    assert any("must be 'exec' or 'python'" in r.message for r in caplog.records)


def test_malformed_settings_never_raises(caplog, monkeypatch):
    def _boom(project_dir=None):
        raise ValueError("malformed yaml")

    monkeypatch.setattr("lionagi.state.lifecycle.notify_settings.load_settings", _boom)
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config()  # must not raise
    assert resolved is None
    assert any("settings resolution failed" in r.message for r in caplog.records)


# ── Invalid top-level value / absent key ─────────────────────────────────────


def test_invalid_value_type_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_notify_config(override=12345)
    assert resolved is None
    assert any("must be a string or mapping" in r.message for r in caplog.records)


def test_absent_notify_key_is_disabled():
    assert resolve_notify_config(settings={}) is None
    assert resolve_notify_config(settings={"notify": {}}) is None


# ── Precedence: per-run override beats settings for its own scope only ───────


def test_per_run_override_replaces_settings_handler():
    settings = {"notify": {"on_terminal": "settings-cmd"}}
    resolved_no_override = resolve_notify_config(settings=settings)
    assert resolved_no_override is not None
    assert resolved_no_override.argv == ("settings-cmd",)

    resolved_with_override = resolve_notify_config(settings=settings, override="override-cmd")
    assert resolved_with_override is not None
    assert resolved_with_override.argv == ("override-cmd",)


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
