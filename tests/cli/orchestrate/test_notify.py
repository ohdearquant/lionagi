# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `--notify` scoped compatibility sugar: legacy
payload shape, entity-scoped filtering, no-shell safety, and failure
containment (nonzero exit / timeout must never propagate)."""

from __future__ import annotations

import json
import logging
import shlex
import sys
import time
from pathlib import Path

import pytest

from lionagi.cli.orchestrate._notify import (
    register_flow_notify_scope,
    unregister_flow_notify_scope,
)
from lionagi.state.lifecycle.callbacks import (
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)
from lionagi.state.lifecycle.notify_settings import ResolvedNotifyHandler, build_handler


def _capture_command(out_file: Path) -> str:
    """An argv command (no shell) that writes its stdin JSON to *out_file*."""
    return (
        f"{shlex.quote(sys.executable)} -c "
        '"import pathlib, sys, json; '
        "data = sys.stdin.read(); "
        'pathlib.Path(sys.argv[1]).write_text(data)" '
        f"{shlex.quote(str(out_file))}"
    )


def _envelope(*, kind: str = "invocation", eid: str = "inv-123", status: str = "completed"):
    return RunTerminalEnvelope(
        event_id="ev-1",
        entity=EntityRef(kind=kind, id=eid),
        previous_status="running",
        terminal_status=status,
        reason_code="run.completed.ok",
        occurred_at=2.0,
    )


async def test_registered_scope_fires_with_legacy_payload_shape(tmp_path: Path):
    out_file = tmp_path / "captured.json"
    registry = TerminalCallbackRegistry()

    name = register_flow_notify_scope(
        registry,
        override=_capture_command(out_file),
        entity_kind="invocation",
        entity_id="inv-123",
        invocation_id="inv-123",
        flow_kind="flow",
        playbook=None,
        save_dir="/tmp/saves",
        cwd="/repo",
        started_at=1.0,
    )
    assert name is not None

    await registry.emit(_envelope(status="completed"))

    payload = json.loads(out_file.read_text())
    assert payload == {
        "invocation_id": "inv-123",
        "kind": "flow",
        "playbook": None,
        "status": "completed",
        "save_dir": "/tmp/saves",
        "cwd": "/repo",
        "exit_class": "success",
        "started_at": 1.0,
        "ended_at": 2.0,
    }

    unregister_flow_notify_scope(name, registry)
    assert name not in registry


async def test_scope_only_fires_for_its_own_entity(tmp_path: Path):
    out_file = tmp_path / "captured.json"
    registry = TerminalCallbackRegistry()
    register_flow_notify_scope(
        registry,
        override=_capture_command(out_file),
        entity_kind="invocation",
        entity_id="inv-123",
        invocation_id="inv-123",
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )

    # A different invocation's terminal event must not fire this scope.
    await registry.emit(_envelope(kind="invocation", eid="some-other-invocation"))
    assert not out_file.exists()

    # A session-kind envelope with a coincidentally equal id must not fire
    # either -- the scope filters on kind too.
    await registry.emit(_envelope(kind="session", eid="inv-123"))
    assert not out_file.exists()

    await registry.emit(_envelope(kind="invocation", eid="inv-123"))
    assert out_file.exists()


async def test_fires_with_null_invocation_id_for_session_scoped_run(tmp_path: Path):
    """An invocation-less run scopes to its session id; the legacy payload's
    own invocation_id field stays null."""
    out_file = tmp_path / "captured.json"
    registry = TerminalCallbackRegistry()
    register_flow_notify_scope(
        registry,
        override=_capture_command(out_file),
        entity_kind="session",
        entity_id="sess-1",
        invocation_id=None,
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )

    await registry.emit(_envelope(kind="session", eid="sess-1", status="completed"))

    payload = json.loads(out_file.read_text())
    assert payload["invocation_id"] is None
    assert payload["status"] == "completed"


async def test_invalid_override_registers_nothing(caplog):
    registry = TerminalCallbackRegistry()
    with caplog.at_level(logging.WARNING):
        name = register_flow_notify_scope(
            registry,
            override="",
            entity_kind="invocation",
            entity_id="inv-1",
            invocation_id="inv-1",
            flow_kind="flow",
            playbook=None,
            save_dir=None,
            cwd="/repo",
            started_at=0.0,
        )
    assert name is None
    assert len(registry._registrations) == 0


async def test_shell_feature_override_registers_nothing(caplog):
    registry = TerminalCallbackRegistry()
    with caplog.at_level(logging.WARNING):
        name = register_flow_notify_scope(
            registry,
            override="echo hi | grep x",
            entity_kind="invocation",
            entity_id="inv-1",
            invocation_id="inv-1",
            flow_kind="flow",
            playbook=None,
            save_dir=None,
            cwd="/repo",
            started_at=0.0,
        )
    assert name is None
    assert any("shell features" in r.message for r in caplog.records)


async def test_nonzero_exit_is_swallowed_and_logged(caplog):
    registry = TerminalCallbackRegistry()
    cmd = f'{shlex.quote(sys.executable)} -c "import sys; sys.exit(3)"'
    register_flow_notify_scope(
        registry,
        override=cmd,
        entity_kind="invocation",
        entity_id="inv-1",
        invocation_id="inv-1",
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )
    with caplog.at_level(logging.WARNING):
        await registry.emit(_envelope(eid="inv-1"))  # must not raise
    assert any("exited 3" in r.message for r in caplog.records)


@pytest.mark.slow_timing
async def test_timeout_is_swallowed_and_logged(caplog, monkeypatch: pytest.MonkeyPatch):
    # The exec adapter's own internal timeout (patched short here) fires
    # before the registry's outer per-emit budget (left at the default 10s)
    # so this exercises the handler's own "timed out" diagnostic, not just
    # the registry's generic external cancellation.
    import lionagi.state.lifecycle.notify_settings as notify_settings_mod

    monkeypatch.setattr(notify_settings_mod, "HANDLER_BUDGET_SECONDS", 0.2)

    registry = TerminalCallbackRegistry()
    slow_cmd = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(5)"'
    register_flow_notify_scope(
        registry,
        override=slow_cmd,
        entity_kind="invocation",
        entity_id="inv-1",
        invocation_id="inv-1",
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )
    start = time.monotonic()
    with caplog.at_level(logging.WARNING):
        await registry.emit(_envelope(eid="inv-1"))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0
    assert any("timed out" in r.message for r in caplog.records)


def _capture_env_and_argv_command(out_file: Path) -> str:
    """An argv command that writes both its argv (post-substitution) and
    the three legacy env vars to *out_file* as JSON, for asserting
    backward-compatible placeholder/env delivery."""
    return (
        f"{shlex.quote(sys.executable)} -c "
        '"import json, os, pathlib, sys; '
        "data = json.dumps({"
        "'argv': sys.argv[1:-1], "
        "'env': {k: os.environ.get(k) for k in "
        "('LIONAGI_NOTIFY_PAYLOAD', 'LIONAGI_NOTIFY_STATUS', 'LIONAGI_NOTIFY_INVOCATION_ID')}"
        "}); "
        'pathlib.Path(sys.argv[-1]).write_text(data)" '
        "{status} {invocation_id} "
        f"{shlex.quote(str(out_file))}"
    )


async def test_notify_substitutes_legacy_placeholders_into_argv_tokens(tmp_path: Path):
    out_file = tmp_path / "captured.json"
    registry = TerminalCallbackRegistry()
    register_flow_notify_scope(
        registry,
        override=_capture_env_and_argv_command(out_file),
        entity_kind="invocation",
        entity_id="inv-1",
        invocation_id="inv-42",
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )
    await registry.emit(_envelope(eid="inv-1", status="failed"))

    captured = json.loads(out_file.read_text())
    # {status}/{invocation_id} were substituted directly into argv tokens --
    # never re-parsed by a shell, so this is exactly one argv element each.
    assert captured["argv"] == ["failed", "inv-42"]


async def test_notify_sets_legacy_env_vars_on_child_process(tmp_path: Path):
    out_file = tmp_path / "captured.json"
    registry = TerminalCallbackRegistry()
    register_flow_notify_scope(
        registry,
        override=_capture_env_and_argv_command(out_file),
        entity_kind="invocation",
        entity_id="inv-1",
        invocation_id="inv-42",
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )
    await registry.emit(_envelope(eid="inv-1", status="failed"))

    captured = json.loads(out_file.read_text())
    assert captured["env"]["LIONAGI_NOTIFY_STATUS"] == "failed"
    assert captured["env"]["LIONAGI_NOTIFY_INVOCATION_ID"] == "inv-42"
    payload = json.loads(captured["env"]["LIONAGI_NOTIFY_PAYLOAD"])
    assert payload["invocation_id"] == "inv-42"
    assert payload["status"] == "failed"


async def test_notify_override_replaces_settings_handler_for_its_own_scope(tmp_path: Path):
    # The P1 fix: an already-registered unscoped settings-level handler must
    # NOT also fire for the entity `--notify` is scoped to -- the override
    # replaces it for that one entity, but leaves the settings handler
    # active for every other entity.
    settings_out = tmp_path / "settings.json"
    override_out = tmp_path / "override.json"
    registry = TerminalCallbackRegistry()
    registry.register(
        "notify.settings.on_terminal",
        build_handler(
            ResolvedNotifyHandler(argv=tuple(shlex.split(_capture_command(settings_out))))
        ),
    )
    register_flow_notify_scope(
        registry,
        override=_capture_command(override_out),
        entity_kind="invocation",
        entity_id="inv-scoped",
        invocation_id="inv-scoped",
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )

    await registry.emit(_envelope(kind="invocation", eid="inv-scoped"))
    assert override_out.exists()
    assert not settings_out.exists()  # settings handler suppressed for this entity

    await registry.emit(_envelope(kind="invocation", eid="some-other-invocation"))
    assert settings_out.exists()  # unaffected for a different entity


async def test_no_shell_ever_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import asyncio

    def _fail(*a, **k):
        raise AssertionError("create_subprocess_shell must never be called")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fail)

    out_file = tmp_path / "captured.json"
    registry = TerminalCallbackRegistry()
    register_flow_notify_scope(
        registry,
        override=_capture_command(out_file),
        entity_kind="invocation",
        entity_id="inv-1",
        invocation_id="inv-1",
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd="/repo",
        started_at=0.0,
    )
    await registry.emit(_envelope(eid="inv-1"))
    assert out_file.exists()  # ran fine via create_subprocess_exec, never the shell
