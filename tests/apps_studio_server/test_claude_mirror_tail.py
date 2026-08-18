# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for studio's in-process Claude Code mirror tail (mirror_forever + lifespan wiring)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.claude_mirror import session_db_id  # noqa: E402
from lionagi.state.db import StateDB  # noqa: E402

from ._helpers import run_async  # noqa: E402


def _write_transcript(
    root: Path,
    uid: str,
    *,
    cwd: str,
    base_ts: float,
    prompt: str = "hello from the mirror tail test",
) -> Path:
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
            "message": {"role": "user", "content": prompt},
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


def _write_codex_transcript(
    root: Path,
    uid: str,
    *,
    cwd: str,
    prompt: str = "codex mirror isolation decoy",
) -> Path:
    """Write one interactive Codex rollout under the sessions root."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-{uid}.jsonl"
    records = [
        {
            "type": "session_meta",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "payload": {
                "id": uid,
                "session_id": uid,
                "cwd": cwd,
                "originator": "Codex Desktop",
            },
        },
        {
            "type": "response_item",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "payload": {
                "type": "message",
                "role": "user",
                "id": "m1",
                "content": [{"text": prompt}],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


async def _poll_session_count(db, expected: int) -> list[dict]:
    """Poll an already-open mirror DB until it reaches ``expected`` rows or settles."""
    rows: list[dict] = []
    for _ in range(300):
        rows = await db.list_sessions(limit=20)
        if len(rows) >= expected:
            break
        await asyncio.sleep(0.01)
    return rows


async def _wait_for_session_count(db_path: Path, expected: int) -> list[dict]:
    """Poll a fresh mirror DB until it reaches ``expected`` rows or settles."""
    async with StateDB(db_path) as db:
        return await _poll_session_count(db, expected)


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


def test_scoping_the_claude_root_does_not_reach_the_real_codex_tree(tmp_path, monkeypatch):
    """A caller that scopes ``root`` must not silently acquire the home codex tree.

    ``codex_root`` defaults to ~/.codex/sessions, so without the explicit source
    selector a test (or any embedder) that carefully points the mirror at its own
    directory would still walk the machine's whole codex rollout corpus.
    """
    import lionagi.cli.mirror as mirror_mod
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(mirror_mod, "_OFFSETS_PATH", tmp_path / "offsets.json")

    visited: list[str] = []
    claude_passes = 0

    async def _spy_codex_pass(db, root, states, offsets, **kw):
        visited.append(str(root))
        return 0

    async def _spy_claude_pass(db, root, states, offsets, **kw):
        nonlocal claude_passes
        claude_passes += 1
        return 0

    monkeypatch.setattr(mirror_mod, "_codex_pass", _spy_codex_pass)
    monkeypatch.setattr(mirror_mod, "_one_pass", _spy_claude_pass)

    root = tmp_path / "claude_projects"
    _write_transcript(
        root, "aaaaaaaa-2222-3333-4444-555555555555", cwd=str(tmp_path), base_ts=time.time()
    )

    async def _run_until(predicate, what: str, **kwargs) -> None:
        """Run the tail until *predicate* holds, rather than for a fixed span.

        A wall-clock window says nothing about how many polls happened inside
        it: on a loaded machine the loop can complete none, and then an
        assertion that the codex root was never reached passes because nothing
        ran at all. Waiting on the thing being measured makes the loop's own
        progress the precondition instead of an assumption about timing.
        """
        stop = asyncio.Event()
        task = asyncio.create_task(
            mirror_mod.mirror_forever(stop, root=root, since=None, interval=0.02, **kwargs)
        )
        failure: str | None = None
        try:
            for _ in range(500):
                if predicate():
                    break
                await asyncio.sleep(0.01)
            else:
                failure = f"tail never {what} within the wait budget"
        finally:
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
                # A tail that will not stop is usually the same tail that never
                # made progress, so raising from here would replace the useful
                # diagnosis with a generic cleanup timeout. The first failure
                # observed is the one worth reporting.
                if failure is None:
                    failure = "tail did not stop within the cleanup budget"
        if failure is not None:
            raise AssertionError(failure)

    # The precondition for the negative assertion below: the loop demonstrably
    # ran a pass, so an empty `visited` means the codex root was not reached
    # rather than that nothing was polled.
    run_async(_run_until(lambda: claude_passes > 0, "completed a claude pass"))
    assert claude_passes > 0
    assert visited == [], f"default source reached codex roots: {visited}"

    # The codex tree is read only when the caller asks for it, and then it is the
    # one the caller named — never the home default. The tail polls repeatedly in
    # the window, so it is the set of roots that is under test, not the count.
    codex_root = tmp_path / "codex_sessions"
    codex_root.mkdir()
    run_async(
        _run_until(
            lambda: bool(visited),
            "ran a codex pass under source=both",
            source="both",
            codex_root=codex_root,
        )
    )
    assert set(visited) == {str(codex_root)}


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
    monkeypatch.setattr(config_mod, "MIRROR_IMPORT_AMBIENT", True)
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


def test_studio_autostart_isolated_profile_does_not_import_ambient_home(tmp_path, monkeypatch):
    """A selected LIONAGI_HOME must not make Studio ingest the real-home trees.

    This drives ``_start_claude_mirror`` itself. Reverting only its root wiring
    makes both decoys land in the fresh profile DB, even though the lower-level
    mirror already supports scoped roots.
    """
    import lionagi.cli.mirror as mirror_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.app as app_mod
    import lionagi.studio.config as config_mod

    isolated_home = tmp_path / "isolated-profile"
    db_path = isolated_home / "state.db"
    ambient_claude = tmp_path / "real-home" / ".claude" / "projects"
    ambient_codex = tmp_path / "real-home" / ".codex" / "sessions"
    _write_transcript(
        ambient_claude,
        "aaaaaaaa-1111-2222-3333-444444444444",
        cwd=str(tmp_path),
        base_ts=time.time(),
    )
    _write_codex_transcript(
        ambient_codex,
        "019c1111-2222-7333-8444-555555555555",
        cwd=str(tmp_path),
    )

    monkeypatch.setenv("LIONAGI_HOME", str(isolated_home))
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(mirror_mod, "_OFFSETS_PATH", isolated_home / "mirror" / "offsets.json")
    monkeypatch.setattr(mirror_mod, "CLAUDE_PROJECTS_DIR", ambient_claude)
    monkeypatch.setattr(mirror_mod, "CODEX_SESSIONS_DIR", ambient_codex)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", True)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_SINCE", None)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_INTERVAL", 0.01)
    monkeypatch.setattr(config_mod, "MIRROR_SOURCE", "both")
    assert config_mod._mirror_import_ambient_default() is False
    monkeypatch.setattr(config_mod, "MIRROR_IMPORT_AMBIENT", False, raising=False)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ROOT", None, raising=False)
    monkeypatch.setattr(config_mod, "MIRROR_CODEX_ROOT", None, raising=False)

    async def _body() -> tuple[object, object, list[dict]]:
        stop, task = app_mod._start_claude_mirror()
        rows: list[dict] = []
        try:
            if task is not None:
                rows = await _wait_for_session_count(db_path, 2)
        finally:
            await app_mod._stop_claude_mirror(stop, task)
        return stop, task, rows

    stop, task, rows = run_async(_body())
    assert stop is None and task is None
    assert rows == []


def test_studio_autostart_uses_explicit_custom_roots(tmp_path, monkeypatch):
    """Custom Claude and Codex roots remain an explicit isolated-profile opt-in."""
    import lionagi.cli.mirror as mirror_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.app as app_mod
    import lionagi.studio.config as config_mod

    isolated_home = tmp_path / "isolated-profile"
    db_path = isolated_home / "state.db"
    custom_claude = tmp_path / "fixtures" / "claude"
    custom_codex = tmp_path / "fixtures" / "codex"
    ambient_claude = tmp_path / "real-home" / ".claude" / "projects"
    ambient_codex = tmp_path / "real-home" / ".codex" / "sessions"
    custom_claude_uid = "bbbbbbbb-1111-2222-3333-444444444444"
    custom_codex_uid = "019c2222-2222-7333-8444-555555555555"
    _write_transcript(
        custom_claude,
        custom_claude_uid,
        cwd=str(tmp_path),
        base_ts=time.time(),
        prompt="custom claude root",
    )
    _write_codex_transcript(
        custom_codex,
        custom_codex_uid,
        cwd=str(tmp_path),
        prompt="custom codex root",
    )
    _write_transcript(
        ambient_claude,
        "cccccccc-1111-2222-3333-444444444444",
        cwd=str(tmp_path),
        base_ts=time.time(),
        prompt="ambient claude decoy",
    )
    _write_codex_transcript(
        ambient_codex,
        "019c3333-2222-7333-8444-555555555555",
        cwd=str(tmp_path),
        prompt="ambient codex decoy",
    )

    monkeypatch.setenv("LIONAGI_HOME", str(isolated_home))
    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_CLAUDE_ROOT", str(custom_claude))
    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_CODEX_ROOT", str(custom_codex))
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(mirror_mod, "_OFFSETS_PATH", isolated_home / "mirror" / "offsets.json")
    monkeypatch.setattr(mirror_mod, "CLAUDE_PROJECTS_DIR", ambient_claude)
    monkeypatch.setattr(mirror_mod, "CODEX_SESSIONS_DIR", ambient_codex)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", True)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_SINCE", None)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_INTERVAL", 0.01)
    monkeypatch.setattr(config_mod, "MIRROR_SOURCE", "both")
    monkeypatch.setattr(config_mod, "MIRROR_IMPORT_AMBIENT", False, raising=False)
    claude_root = config_mod._optional_mirror_root("LIONAGI_STUDIO_MIRROR_CLAUDE_ROOT")
    codex_root = config_mod._optional_mirror_root("LIONAGI_STUDIO_MIRROR_CODEX_ROOT")
    assert claude_root == custom_claude.resolve()
    assert codex_root == custom_codex.resolve()
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ROOT", claude_root, raising=False)
    monkeypatch.setattr(config_mod, "MIRROR_CODEX_ROOT", codex_root, raising=False)

    async def _body() -> list[dict]:
        # Open the poll connection before the tail, so it does the one-time WAL
        # and schema init alone. A cold open racing the tail's writes is what
        # locks the store up under load.
        async with StateDB(db_path) as db:
            stop, task = app_mod._start_claude_mirror()
            assert stop is not None and task is not None
            try:
                return await _poll_session_count(db, 2)
            finally:
                await app_mod._stop_claude_mirror(stop, task)

    rows = run_async(_body())
    names = {row["name"] for row in rows}
    assert len(rows) == 2
    assert names == {"custom claude root", "custom codex root"}


def test_studio_mirror_startup_error_does_not_expose_transcript_data(tmp_path, monkeypatch, caplog):
    """Unexpected autostart failures stay useful without echoing paths/content."""
    import lionagi.studio.app as app_mod
    import lionagi.studio.config as config_mod

    secret_path = tmp_path / "private" / "rollout-secret.jsonl"
    secret_content = "interview transcript content"

    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", True)
    monkeypatch.setattr(config_mod, "MIRROR_IMPORT_AMBIENT", False, raising=False)
    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ROOT", secret_path.parent, raising=False)
    monkeypatch.setattr(config_mod, "MIRROR_CODEX_ROOT", None, raising=False)
    monkeypatch.setattr(config_mod, "MIRROR_SOURCE", "claude")

    async def _boom(stop, **kwargs):
        raise RuntimeError(f"{secret_path}: {secret_content}")

    monkeypatch.setattr("lionagi.cli.mirror.mirror_forever", _boom)

    async def _body() -> None:
        stop, task = app_mod._start_claude_mirror()
        assert stop is not None and task is not None
        try:
            await asyncio.wait_for(task, timeout=2)
        except RuntimeError:
            pass

    with caplog.at_level("ERROR", logger="lionagi.studio.app"):
        run_async(_body())

    assert secret_path.as_posix() not in caplog.text
    assert secret_content not in caplog.text
