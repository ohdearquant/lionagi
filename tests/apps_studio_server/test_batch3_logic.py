# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Batch 3 logic fixes: #991 SSE done condition, #1013 update validation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# #991 — is_session_stream_done() gates on terminal status AND stale time
# ---------------------------------------------------------------------------


class TestIsSessionStreamDone:
    def test_running_status_returns_false(self):
        """A session with 'running' status must never trigger done, regardless of staleness."""
        from apps.studio.server.services.sessions import is_session_stream_done

        state = {"status": "running", "updated_at": 0.0}
        # now is very large — stale condition would fire if status were terminal
        assert not is_session_stream_done(state, now=9_999_999.0)

    def test_completed_but_fresh_returns_false(self):
        """Terminal status alone is not enough — updated_at must also be > 60s ago."""
        from apps.studio.server.services.sessions import (
            SESSION_DONE_STABLE_SECS,
            is_session_stream_done,
        )

        now = 1_000_000.0
        # updated_at is only 30s ago — not yet stable
        state = {"status": "completed", "updated_at": now - (SESSION_DONE_STABLE_SECS / 2)}
        assert not is_session_stream_done(state, now=now)

    def test_completed_and_stale_returns_true(self):
        """Both conditions met → done."""
        from apps.studio.server.services.sessions import (
            SESSION_DONE_STABLE_SECS,
            is_session_stream_done,
        )

        now = 1_000_000.0
        state = {"status": "completed", "updated_at": now - SESSION_DONE_STABLE_SECS - 1}
        assert is_session_stream_done(state, now=now)

    def test_failed_and_stale_returns_true(self):
        """'failed' is also a terminal status."""
        from apps.studio.server.services.sessions import (
            SESSION_DONE_STABLE_SECS,
            is_session_stream_done,
        )

        now = 1_000_000.0
        state = {"status": "failed", "updated_at": now - SESSION_DONE_STABLE_SECS - 1}
        assert is_session_stream_done(state, now=now)

    def test_aborted_and_stale_returns_true(self):
        """'aborted' is also a terminal status."""
        from apps.studio.server.services.sessions import (
            SESSION_DONE_STABLE_SECS,
            is_session_stream_done,
        )

        now = 1_000_000.0
        state = {"status": "aborted", "updated_at": now - SESSION_DONE_STABLE_SECS - 1}
        assert is_session_stream_done(state, now=now)

    def test_none_state_returns_false(self):
        """Missing/unknown session must keep the stream alive (not close it)."""
        from apps.studio.server.services.sessions import is_session_stream_done

        assert not is_session_stream_done(None, now=9_999_999.0)


class TestGetSessionStreamState:
    def _patch_db(self, monkeypatch, svc, db_path: Path):
        """Patch both the string path and the Path sentinel used by the exists() check."""
        monkeypatch.setattr(svc, "_DB", str(db_path))
        monkeypatch.setattr(svc, "DEFAULT_DB_PATH", db_path)

    def test_returns_none_when_db_missing(self, tmp_path, monkeypatch):
        """When the DB file does not exist, return None (keep stream alive)."""
        import apps.studio.server.services.sessions as svc

        self._patch_db(monkeypatch, svc, tmp_path / "nonexistent.db")
        result = _run(svc.get_session_stream_state("fake-id"))
        assert result is None

    def test_returns_none_for_unknown_session(self, tmp_path, monkeypatch):
        """Row not found → None (not an error)."""
        import apps.studio.server.services.sessions as svc

        db_path = tmp_path / "test.db"

        async def _setup():
            import aiosqlite as aio
            async with aio.connect(str(db_path)) as db:
                await db.execute(
                    "CREATE TABLE sessions (id TEXT PRIMARY KEY, updated_at REAL, status TEXT)"
                )
                await db.commit()

        _run(_setup())
        self._patch_db(monkeypatch, svc, db_path)

        result = _run(svc.get_session_stream_state("not-there"))
        assert result is None

    def test_returns_state_dict_for_known_session(self, tmp_path, monkeypatch):
        """Existing row returns {updated_at, status}."""
        import apps.studio.server.services.sessions as svc

        db_path = tmp_path / "test.db"

        async def _setup():
            import aiosqlite as aio
            async with aio.connect(str(db_path)) as db:
                await db.execute(
                    "CREATE TABLE sessions (id TEXT PRIMARY KEY, updated_at REAL, status TEXT)"
                )
                await db.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?)",
                    ("sess-1", 12345.0, "completed"),
                )
                await db.commit()

        _run(_setup())
        self._patch_db(monkeypatch, svc, db_path)

        result = _run(svc.get_session_stream_state("sess-1"))
        assert result is not None
        assert result["updated_at"] == 12345.0
        assert result["status"] == "completed"

    def test_null_status_becomes_completed(self, tmp_path, monkeypatch):
        """Legacy rows with NULL status must map to 'completed' (not None)."""
        import apps.studio.server.services.sessions as svc

        db_path = tmp_path / "test.db"

        async def _setup():
            import aiosqlite as aio
            async with aio.connect(str(db_path)) as db:
                await db.execute(
                    "CREATE TABLE sessions (id TEXT PRIMARY KEY, updated_at REAL, status TEXT)"
                )
                await db.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?)",
                    ("sess-legacy", 5000.0, None),
                )
                await db.commit()

        _run(_setup())
        self._patch_db(monkeypatch, svc, db_path)

        result = _run(svc.get_session_stream_state("sess-legacy"))
        assert result is not None
        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# #1013 — update_playbook() rejects invalid links via validate_playbook()
# ---------------------------------------------------------------------------


class TestUpdatePlaybookValidation:
    def _make_playbook(self, tmp_path: Path, name: str, content: str) -> Path:
        path = tmp_path / f"{name}.playbook.yaml"
        path.write_text(content)
        return path

    def test_valid_update_succeeds(self, tmp_path, monkeypatch):
        """A well-formed update (links reference existing steps) must not raise."""
        import apps.studio.server.services.playbooks as svc

        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path)
        self._make_playbook(
            tmp_path,
            "my-pb",
            "description: test\nsteps:\n  a: {}\n  b: {}\nlinks:\n  - {from: a, to: b}\n",
        )

        result = svc.update_playbook("my-pb", {"description": "updated"})
        assert result is not None
        assert result["data"]["description"] == "updated"

    def test_invalid_link_raises_value_error(self, tmp_path, monkeypatch):
        """Links that reference non-existent steps must raise ValueError."""
        import apps.studio.server.services.playbooks as svc

        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path)
        self._make_playbook(
            tmp_path,
            "my-pb2",
            "description: test\nsteps:\n  a: {}\n",
        )

        with pytest.raises(ValueError, match="unknown step"):
            svc.update_playbook(
                "my-pb2",
                {
                    "steps": {"a": {}},
                    "links": [{"from": "a", "to": "ghost"}],
                },
            )

    def test_router_returns_422_on_invalid_update(self, tmp_path, monkeypatch):
        """Router must convert ValueError from update_playbook() to HTTP 422."""
        import apps.studio.server.services.playbooks as svc

        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path)
        self._make_playbook(
            tmp_path,
            "my-pb3",
            "description: test\nsteps:\n  a: {}\n",
        )

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from apps.studio.server.routers.playbooks import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.put(
            "/playbooks/my-pb3",
            json={
                "steps": {"a": {}},
                "links": [{"from": "a", "to": "nowhere"}],
            },
        )
        assert resp.status_code == 422

    def test_update_does_not_write_on_validation_failure(self, tmp_path, monkeypatch):
        """File must not be written when validation fails."""
        import apps.studio.server.services.playbooks as svc

        monkeypatch.setattr(svc, "_PLAYBOOKS_ROOT", tmp_path)
        original_content = "description: original\nsteps:\n  a: {}\n"
        pb_path = self._make_playbook(tmp_path, "my-pb4", original_content)

        with pytest.raises(ValueError):
            svc.update_playbook(
                "my-pb4",
                {
                    "steps": {"a": {}},
                    "links": [{"from": "a", "to": "ghost"}],
                },
            )

        # File must be untouched
        assert pb_path.read_text() == original_content
