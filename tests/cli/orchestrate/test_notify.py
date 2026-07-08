# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the terminal-notify hook: settings/flag resolution, payload shape, and
failure containment (nonzero exit / timeout must never propagate)."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from lionagi.cli.orchestrate import _notify
from lionagi.cli.orchestrate._notify import fire_terminal_notify


def _capture_command(out_file: Path) -> str:
    """A shell template that writes the substituted {payload} to *out_file*."""
    return (
        f"{shlex.quote(sys.executable)} -c "
        '"import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])" '
        f"{shlex.quote(str(out_file))} '{{payload}}'"
    )


def _write_project_settings(project_dir: Path, on_terminal: str) -> None:
    lionagi_dir = project_dir / ".lionagi"
    lionagi_dir.mkdir(parents=True, exist_ok=True)
    (lionagi_dir / "settings.yaml").write_text(yaml.dump({"notify": {"on_terminal": on_terminal}}))


async def test_settings_configured_hook_fires_with_correct_payload(tmp_path: Path):
    out_file = tmp_path / "captured.json"
    _write_project_settings(tmp_path, _capture_command(out_file))

    await fire_terminal_notify(
        invocation_id="inv-123",
        kind="flow",
        playbook=None,
        status="completed",
        save_dir="/tmp/saves",
        cwd="/repo",
        exit_class="success",
        started_at=1.0,
        ended_at=2.0,
        override_command=None,
        project_dir=str(tmp_path),
    )

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


async def test_status_and_invocation_id_substitution(tmp_path: Path):
    out_file = tmp_path / "captured.txt"
    template = (
        f"{shlex.quote(sys.executable)} -c "
        '"import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])" '
        f"{shlex.quote(str(out_file))} '{{status}}:{{invocation_id}}'"
    )
    _write_project_settings(tmp_path, template)

    await fire_terminal_notify(
        invocation_id="inv-xyz",
        kind="play",
        playbook="ship",
        status="failed",
        save_dir=None,
        cwd="/repo",
        exit_class="failure",
        started_at=1.0,
        ended_at=2.0,
        override_command=None,
        project_dir=str(tmp_path),
    )

    assert out_file.read_text() == "failed:inv-xyz"


async def test_notify_flag_overrides_settings(tmp_path: Path):
    settings_out = tmp_path / "from_settings.json"
    override_out = tmp_path / "from_override.json"
    _write_project_settings(tmp_path, _capture_command(settings_out))

    await fire_terminal_notify(
        invocation_id="inv-1",
        kind="flow",
        playbook=None,
        status="completed",
        save_dir=None,
        cwd="/repo",
        exit_class="success",
        started_at=0.0,
        ended_at=1.0,
        override_command=_capture_command(override_out),
        project_dir=str(tmp_path),
    )

    assert override_out.exists()
    assert not settings_out.exists()
    assert json.loads(override_out.read_text())["invocation_id"] == "inv-1"


async def test_no_hook_configured_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Isolate from any real ~/.lionagi/settings.yaml on the host so this
    # assertion holds regardless of the machine running the suite.
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    with patch("asyncio.create_subprocess_shell") as spawn:
        await fire_terminal_notify(
            invocation_id="inv-1",
            kind="flow",
            playbook=None,
            status="completed",
            save_dir=None,
            cwd="/repo",
            exit_class="success",
            started_at=0.0,
            ended_at=1.0,
            override_command=None,
            project_dir=str(tmp_path),
        )
    spawn.assert_not_called()


async def test_nonzero_exit_is_swallowed_and_logged(tmp_path: Path):
    cmd = f'{shlex.quote(sys.executable)} -c "import sys; sys.exit(3)"'

    with patch.object(_notify, "warn") as warn_mock:
        await fire_terminal_notify(
            invocation_id="inv-1",
            kind="flow",
            playbook=None,
            status="failed",
            save_dir=None,
            cwd="/repo",
            exit_class="failure",
            started_at=0.0,
            ended_at=1.0,
            override_command=cmd,
            project_dir=None,
        )

    warn_mock.assert_called_once()
    assert "exited 3" in warn_mock.call_args.args[0]


async def test_timeout_is_swallowed_and_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_notify, "_HOOK_TIMEOUT", 0.2)
    cmd = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(5)"'

    with patch.object(_notify, "warn") as warn_mock:
        await fire_terminal_notify(
            invocation_id="inv-1",
            kind="flow",
            playbook=None,
            status="timed_out",
            save_dir=None,
            cwd="/repo",
            exit_class="failure",
            started_at=0.0,
            ended_at=1.0,
            override_command=cmd,
            project_dir=None,
        )

    warn_mock.assert_called_once()
    assert "timed out" in warn_mock.call_args.args[0]
