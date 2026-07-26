# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The listing endpoints must do work proportional to the page, must refuse
rather than serve an unbounded page, must say so when an answer is bounded,
and the store probe must be able to go red."""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from lionagi.studio.app import create_app


def _seed(db_path, *, sessions: int, branches_per_session: int = 1) -> list[str]:
    from lionagi.state.db import _SCHEMA_PATH

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_PATH.read_text())
    now = time.time()
    ids = []
    for i in range(sessions):
        sid = str(uuid.uuid4())
        prog = str(uuid.uuid4())
        ids.append(sid)
        conn.execute(
            "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, '[]')",
            (prog, now),
        )
        conn.execute(
            """INSERT INTO sessions
               (id, created_at, progression_id, updated_at, name, status,
                playbook_name, project)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                sid,
                now - i,
                prog,
                now - i,
                f"run-{i}",
                "completed" if i % 2 else "running",
                f"book-{i % 3}",
                "alpha" if i % 2 else None,
            ),
        )
        for b in range(branches_per_session):
            bprog = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
                (bprog, now, '["a","b","c"]'),
            )
            conn.execute(
                """INSERT INTO branches (id, created_at, session_id, progression_id, name)
                   VALUES (?,?,?,?,?)""",
                (str(uuid.uuid4()), now, sid, bprog, f"b{b}"),
            )
    conn.commit()
    conn.close()
    return ids


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ids = _seed(db_path, sessions=25)
    for mod in ("sessions", "runs", "admin", "run_tags", "stats"):
        module = pytest.importorskip(f"lionagi.studio.services.{mod}")
        monkeypatch.setattr(module, "_DB", str(db_path), raising=False)
    import lionagi.state.db as db_mod

    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    for mod in ("sessions", "runs", "admin", "run_tags"):
        module = pytest.importorskip(f"lionagi.studio.services.{mod}")
        if hasattr(module, "DEFAULT_DB_PATH"):
            monkeypatch.setattr(module, "DEFAULT_DB_PATH", db_path)
    return db_path, ids


@pytest.fixture
def client(seeded):
    # The daemon rejects Host headers it doesn't recognise, and TestClient's
    # default ("testserver") is not one of them.
    with TestClient(create_app(), base_url="http://localhost") as c:
        yield c


class TestPageBoundsTheWork:
    def test_rows_outside_the_page_are_never_examined(self, tmp_path, monkeypatch):
        """A page size bounds rows *returned*; the defect was that it bounded
        nothing *examined*. Poison every progression outside the first page with
        JSON the aggregate cannot parse: a listing that reads the whole store
        raises, one that reads only its page does not."""
        import asyncio

        db_path = tmp_path / "state.db"
        ids = _seed(db_path, sessions=20)
        conn = sqlite3.connect(str(db_path))
        # Oldest 15 sessions -- outside a 5-row newest-first page.
        for sid in ids[5:]:
            conn.execute(
                """UPDATE progressions SET collection = 'not json'
                   WHERE id IN (SELECT progression_id FROM branches WHERE session_id = ?)""",
                (sid,),
            )
        conn.commit()
        conn.close()

        import lionagi.state.db as db_mod
        from lionagi.studio.services import sessions as sessions_svc

        monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
        monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
        monkeypatch.setattr(sessions_svc, "_DB", str(db_path))

        rows = asyncio.run(sessions_svc.list_sessions(limit=5))
        assert [r["id"] for r in rows] == ids[:5]
        assert all(r["message_count"] == 3 for r in rows)

    def test_second_page_is_disjoint_and_ordered(self, seeded):
        import asyncio

        from lionagi.studio.services import sessions as sessions_svc

        first = asyncio.run(sessions_svc.list_sessions(limit=5, offset=0))
        second = asyncio.run(sessions_svc.list_sessions(limit=5, offset=5))
        assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})
        assert [r["updated_at"] for r in first] == sorted(
            (r["updated_at"] for r in first), reverse=True
        )
        assert first[-1]["updated_at"] >= second[0]["updated_at"]

    def test_limit_is_clamped_not_honoured_unbounded(self, seeded):
        import asyncio

        from lionagi.studio.services import sessions as sessions_svc

        rows = asyncio.run(sessions_svc.list_sessions(limit=10_000))
        assert len(rows) <= sessions_svc.MAX_SESSION_PAGE

    def test_message_count_still_aggregates_over_branches(self, seeded):
        import asyncio

        from lionagi.studio.services import sessions as sessions_svc

        rows = asyncio.run(sessions_svc.list_sessions(limit=3))
        assert all(r["branch_count"] == 1 for r in rows)
        assert all(r["message_count"] == 3 for r in rows)


class TestFiltersApplyInSql:
    def test_status_filter_matches_python_semantics(self, client):
        r = client.get("/api/runs/", params={"status": "running", "per_page": 100})
        assert r.status_code == 200
        body = r.json()
        assert body["runs"]
        assert all(run["status"] == "running" for run in body["runs"])
        assert body["total"] == len(body["runs"])

    def test_status_alias_expands(self, client):
        aliased = client.get("/api/runs/", params={"status": "done", "per_page": 100}).json()
        direct = client.get("/api/runs/", params={"status": "completed", "per_page": 100}).json()
        assert aliased["total"] == direct["total"]

    def test_project_null_filter(self, client):
        body = client.get("/api/runs/", params={"project_null": True, "per_page": 100}).json()
        assert body["runs"]
        assert all(run["project"] is None for run in body["runs"])

    def test_project_exact_filter(self, client):
        body = client.get("/api/runs/", params={"project": "alpha", "per_page": 100}).json()
        assert body["runs"]
        assert all(run["project"] == "alpha" for run in body["runs"])

    def test_playbook_filter_is_case_insensitive_contains(self, client):
        body = client.get("/api/runs/", params={"playbook": "BOOK-1", "per_page": 100}).json()
        assert body["runs"]
        assert all("book-1" in run["playbook_name"] for run in body["runs"])

    def test_tag_filter_and_composes(self, seeded, client):
        _, ids = seeded
        client.post(f"/api/sessions/{ids[0]}/tags", json={"tag": "keep"})
        client.post(f"/api/sessions/{ids[0]}/tags", json={"tag": "urgent"})
        client.post(f"/api/sessions/{ids[1]}/tags", json={"tag": "keep"})

        one = client.get("/api/runs/", params={"tag": ["keep"], "per_page": 100}).json()
        both = client.get("/api/runs/", params={"tag": ["keep", "urgent"], "per_page": 100}).json()
        assert one["total"] == 2
        assert both["total"] == 1
        assert both["runs"][0]["run_id"] == ids[0]

    def test_tag_filter_on_never_tagged_store(self, client):
        """run_tags is created on first tag write; filtering before that must
        return nothing, not raise."""
        r = client.get("/api/runs/", params={"tag": ["nope"], "per_page": 100})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_total_counts_the_filtered_set_not_the_page(self, client):
        body = client.get("/api/runs/", params={"per_page": 5}).json()
        assert len(body["runs"]) == 5
        assert body["total"] == 25
        assert body["total_pages"] == 5
        assert body["has_next"] is True


class TestBoundedAnswersSaySo:
    def test_runs_pagination_envelope_is_honest(self, client):
        body = client.get("/api/runs/", params={"page": 2, "per_page": 10}).json()
        assert body["page"] == 2
        assert body["total"] == 25
        assert body["has_prev"] is True
        assert body["has_next"] is True

    def test_sessions_listing_reports_truncation(self, client, monkeypatch):
        body = client.get("/api/sessions/", params={"limit": 10}).json()
        assert len(body["sessions"]) == 10
        assert body["total"] == 25
        assert body["truncated"] is True

    def test_sessions_listing_not_truncated_when_complete(self, client):
        body = client.get("/api/sessions/", params={"limit": 100}).json()
        assert len(body["sessions"]) == 25
        assert body["truncated"] is False

    def test_admin_health_reports_scan_coverage(self, client):
        body = client.get("/api/admin/health").json()
        assert body["sessions"]["total"] == 25
        assert body["sessions"]["scanned"] == 25
        assert body["sessions"]["truncated"] is False


class TestOversizedPageIsRefused:
    def test_per_page_above_cap_is_refused(self, client):
        from lionagi.studio.services import sessions as sessions_svc

        r = client.get("/api/runs/", params={"per_page": sessions_svc.MAX_SESSION_PAGE + 1})
        assert r.status_code == 422

    def test_per_page_at_cap_is_served(self, client):
        from lionagi.studio.services import sessions as sessions_svc

        r = client.get("/api/runs/", params={"per_page": sessions_svc.MAX_SESSION_PAGE})
        assert r.status_code == 200

    def test_sessions_limit_above_cap_is_refused(self, client):
        from lionagi.studio.services import sessions as sessions_svc

        r = client.get("/api/sessions/", params={"limit": sessions_svc.MAX_SESSION_PAGE + 1})
        assert r.status_code == 422


class TestStoreProbe:
    def test_healthy_store_reports_healthy(self, client):
        body = client.get("/api/admin/readiness").json()
        assert body["status"] == "healthy"
        assert body["store_present"] is True
        assert body["latency_ms"] >= 0

    def test_missing_store_reports_unavailable_not_healthy(self, client, tmp_path, monkeypatch):
        from lionagi.studio.services import admin as admin_svc

        monkeypatch.setattr(admin_svc, "DEFAULT_DB_PATH", tmp_path / "gone.db")
        body = client.get("/api/admin/readiness").json()
        assert body["status"] == "unavailable"
        assert body["store_present"] is False

    def test_slow_store_reports_slow_not_unavailable(self, client, monkeypatch):
        """A store that answers, but not inside the probe's deadline, is a
        distinct verdict from one that cannot be reached at all."""
        import anyio

        class _SlowConnection:
            async def __aenter__(self):
                await anyio.sleep(5)
                raise AssertionError("probe should have given up before this")

            async def __aexit__(self, *exc):
                return False

        import aiosqlite

        monkeypatch.setattr(aiosqlite, "connect", lambda *a, **kw: _SlowConnection())
        body = client.get("/api/admin/readiness", params={"timeout_ms": 100}).json()
        assert body["status"] == "slow"
        assert body["timeout_ms"] == 100
        assert body["store_present"] is True
        assert "did not answer" in body["detail"]

    def test_probe_never_returns_5xx(self, client, monkeypatch):
        import aiosqlite

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("database disk image is malformed")

        monkeypatch.setattr(aiosqlite, "connect", _boom)
        r = client.get("/api/admin/readiness")
        assert r.status_code == 200
        assert r.json()["status"] == "unavailable"
        assert "malformed" in r.json()["detail"]

    def test_liveness_endpoint_is_unchanged(self, client):
        """Callers depending on /health must see exactly what they saw before."""
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
