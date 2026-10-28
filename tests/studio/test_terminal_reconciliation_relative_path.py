# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Project notification settings must not resolve relative to the daemon cwd."""

from __future__ import annotations

import builtins
import sys
import types
import uuid
from pathlib import Path

import pytest

import lionagi._settings as settings_module
from lionagi.state.db import StateDB
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import is_acknowledged
from lionagi.state.lifecycle.reconciler import TerminalReconciler
from lionagi.studio.services import terminal_reconciliation as terminal


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute-control"])
@pytest.mark.asyncio
async def test_project_path_must_be_absolute_for_settings(tmp_path, monkeypatch, absolute):
    home = tmp_path / "home"
    daemon = tmp_path / "daemon"
    project = daemon / "project"
    for directory in (home / ".lionagi", project / ".lionagi"):
        directory.mkdir(parents=True)
    global_file = home / ".lionagi" / "settings.yaml"
    project_file = project / ".lionagi" / "settings.yaml"
    for file, name in ((global_file, "global"), (project_file, "project")):
        file.write_text(
            "notify:\n  on_terminal:\n    enabled: true\n    adapter:\n"
            f"      kind: python\n      ref: terminal_path_{name}:handle\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(daemon)
    seen = []
    for name in ("global", "project"):
        adapter = types.ModuleType(f"terminal_path_{name}")
        adapter.handle = lambda envelope, name=name: seen.append((name, envelope.event_id))
        monkeypatch.setitem(sys.modules, adapter.__name__, adapter)

    opened = []

    def recording_open(file, *args, **kwargs):
        opened.append(Path(file).resolve())
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(settings_module, "open", recording_open, raising=False)
    consumer = "relative-path-settings-test"
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_project("alpha", path=str(project) if absolute else "project")
        session_id, progression_id = str(uuid.uuid4()), str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression_id,
                "status": "running",
                "project": "alpha",
            }
        )
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        assert await db.update_status(
            "session", session_id, new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
            ("session", session_id, "failed"),
        )
        assert row is not None
        reconciler = TerminalReconciler(consumer, terminal._prepare_settings_delivery)
        summary = await reconciler.run_pass(db)
        assert summary["attempted"] == summary["acknowledged"] == 1
        assert await is_acknowledged(db, row["id"], consumer)

    assert global_file in opened
    assert (project_file in opened) is absolute
    assert seen == [("project" if absolute else "global", row["id"])]
