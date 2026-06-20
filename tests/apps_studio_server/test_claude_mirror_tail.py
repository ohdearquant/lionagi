# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for studio's in-process Claude Code mirror tail (mirror_forever + lifespan wiring)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from lionagi.state.claude_mirror import session_db_id
from lionagi.state.db import StateDB

from ._helpers import run_async


def _write_transcript(root: Path, uid: str, *, cwd: str, base_ts: float) -> Path:
    """Write a minimal two-event Claude transcript under root/<proj>/<uid>.jsonl."""
    proj_dir = root / "-Users-someone-proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{uid}.jsonl"
    t0 = datetime.fromtimestamp(base_ts, tz=timezone.utc).isoformat()
    t1 = datetime.fromtimestamp(base_ts + 1, tz=timezone.utc).isoformat()
    events = [
        {
            "type": "user",
            "sessionId": uid,
            "uuid": "u1",
            "timestamp": t0,
            "cwd": cwd,
            "message": {"role": "user", "content": "hello from the mirror tail test"},
        },
        {
            "type": "assistant",
            "sessionId": uid,
            "uuid": "a1",
            "timestamp": t1,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "hi"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_mirror_forever_writes_session_then_stops(tmp_path, monkeypatch):
    """A fresh transcript is mirrored to a live (running) session; stop ends the loop."""
    import lionagi.cli.mirror as mirror_mod
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    root = tmp_path / "claude_projects"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(mirror_mod, "_OFFSETS_PATH", tmp_path / "offsets.json")

    uid = "11111111-2222-3333-4444-555555555555"
    # Near-now timestamps so the session reconciles as live (running), not idle.
    _write_transcript(root, uid, cwd=str(tmp_path), base_ts=time.time())
    sid = session_db_id(uid)

    async def _body() -> dict | None:
        # Open one poll connection up front so it performs the one-time WAL +
        # schema init alone, then start the tail against the already-initialised
        # file. This mirrors studio (a shared connection established at startup,
        # the mirror tail joining later) and avoids two cold connections racing
        # the WAL-mode promotion. Re-polling the same connection still observes
        # the tail's commits — each get_session opens a fresh read transaction.
        async with StateDB(db_path) as db:
            stop = asyncio.Event()
            task = asyncio.create_task(
                mirror_mod.mirror_forever(stop, root=root, since=None, interval=0.02)
            )
            row = None
            try:
                for _ in range(300):
                    row = await db.get_session(sid)
                    if row is not None:
                        break
                    await asyncio.sleep(0.01)
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=5)
        return row

    row = run_async(_body())
    assert row is not None, "mirror_forever did not write the session"
    assert row["status"] == "running"
    assert row["agent_name"] == "claude-code"
    assert "mirror tail test" in (row["name"] or "")


def test_mirror_forever_missing_root_is_noop(tmp_path, monkeypatch):
    """A missing Claude projects dir returns immediately and writes nothing."""
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    from lionagi.cli.mirror import mirror_forever

    async def _body() -> None:
        stop = asyncio.Event()
        await asyncio.wait_for(
            mirror_forever(stop, root=tmp_path / "does-not-exist", since=None),
            timeout=2,
        )

    run_async(_body())  # returns without spinning the loop
    assert not db_path.exists()


def test_start_claude_mirror_respects_flag(monkeypatch):
    """_start_claude_mirror no-ops when off, spawns and threads config when on."""
    import lionagi.studio.app as app_mod
    import lionagi.studio.config as config_mod

    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", False)

    async def _off() -> None:
        stop, task = app_mod._start_claude_mirror()
        assert stop is None and task is None
        await app_mod._stop_claude_mirror(stop, task)  # tolerates (None, None)

    run_async(_off())

    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", True)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_SINCE", "12h")
    seen: dict[str, object] = {}

    async def _fake_forever(stop, **kwargs):
        seen.update(kwargs)
        await stop.wait()

    monkeypatch.setattr("lionagi.cli.mirror.mirror_forever", _fake_forever)

    async def _on() -> None:
        stop, task = app_mod._start_claude_mirror()
        assert stop is not None and task is not None
        await app_mod._stop_claude_mirror(stop, task)
        assert task.done()

    run_async(_on())
    assert seen.get("since") == "12h"
