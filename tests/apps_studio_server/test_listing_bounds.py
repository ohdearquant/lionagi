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


def _is_live_connection_worker(t, target) -> bool:
    """True for a thread object that is both aiosqlite's connection worker
    *and* still actually running.

    aiosqlite's worker schedules its close future's result via
    ``call_soon_threadsafe`` and only then breaks out of its loop, so the
    event loop can observe the future as done -- and an awaiting ``close()``
    can return -- a moment before the OS thread has finished tearing itself
    down and dropped out of ``threading.enumerate()``'s backing registry.
    Matching by ``_target`` alone counts that terminating-but-not-yet-reaped
    thread object as a live leak; ``is_alive()`` does not.
    """
    return getattr(t, "_target", None) is target and t.is_alive()


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


def _hold_the_write_lock(db_path):
    """A connection holding the store's write lock against every reader.

    The store is created in WAL, where an ordinary writer deliberately does not
    block readers, so a plain transaction cannot produce the condition these
    tests need. The obvious lever, `PRAGMA locking_mode = EXCLUSIVE`, is the
    wrong one: a second connection to the same WAL database in the same process
    while another holds the shared-memory region exclusively is not a
    configuration SQLite supports, and it can end the process rather than
    return an error — a harness that aborts its own worker, with no traceback
    to say why.

    Moving the file to a rollback journal gets there within supported
    behaviour: there a writer holding a transaction does block readers, which
    is exactly the "store will not answer" the probe has to report as slow.
    """
    blocker = sqlite3.connect(str(db_path), isolation_level=None)
    blocker.execute("PRAGMA journal_mode = DELETE")
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.execute("UPDATE sessions SET name = name")
    return blocker


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    ids = _seed(db_path, sessions=25)
    import lionagi.state.db as db_mod

    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
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
    def test_sparse_status_filter_keeps_the_indexed_column_sargable(self):
        from lionagi.studio.services import sessions as sessions_svc

        clause, params = sessions_svc.SessionFilter(statuses={"running"}).where()

        assert "COALESCE" not in clause
        assert "s.status IN (?)" in clause
        assert "s.status IS NULL" not in clause
        assert params == ["running"]

        completed_clause, _ = sessions_svc.SessionFilter(statuses={"completed"}).where()
        assert "s.status IN (?)" in completed_clause
        assert "s.status IS NULL" in completed_clause

    def test_recent_status_page_plan_seeks_without_temp_sort(self, seeded):
        from lionagi.studio.services import sessions as sessions_svc

        db_path, _ = seeded
        clause, params = sessions_svc.SessionFilter(statuses={"running"}).where()
        # The migrated spelling, not the legacy constant: a real column is the
        # form that could cost the plan its index, so it is the one to measure.
        sql = sessions_svc._session_page_sql(
            clause, sessions_svc._SESSION_SORTS["recent"], "s.ended_at_is_approximate"
        )
        conn = sqlite3.connect(str(db_path))
        try:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN " + sql,
                [*params, 21, 0],
            ).fetchall()
        finally:
            conn.close()
        details = [str(row[3]) for row in plan]

        assert any("SEARCH s USING INDEX idx_sessions_status_updated_id" in row for row in details)
        assert not any("USE TEMP B-TREE" in row for row in details), details

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

    def test_contains_search_discloses_and_applies_a_candidate_cap(self, client):
        from lionagi.studio.services import sessions as sessions_svc

        clause, params = sessions_svc.SessionFilter(search="run").where()
        assert "ORDER BY recent.updated_at DESC, recent.id DESC LIMIT ?" in clause
        assert params[0] == sessions_svc.MAX_SESSION_SEARCH_CANDIDATES

        body = client.get("/api/runs/", params={"search": "run", "per_page": 5}).json()
        assert body["search"] == {
            "mode": "bounded_contains",
            "bounded_slow_path": True,
            "candidate_limit": sessions_svc.MAX_SESSION_SEARCH_CANDIDATES,
        }

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

    def test_sessions_listing_can_skip_exact_total_and_discloses_search_cost(self, client):
        body = client.get(
            "/api/sessions/",
            params={"limit": 5, "include_total": False},
        ).json()

        assert body["total"] is None
        assert body["total_exact"] is False
        assert body["search"] == {"mode": "none", "bounded_slow_path": False}


class TestSeekableSessionCursor:
    def test_cursor_page_is_stable_when_a_newer_row_is_inserted(self, seeded):
        import asyncio

        from lionagi.studio.services import sessions as sessions_svc

        db_path, _ = seeded
        first = asyncio.run(sessions_svc.list_sessions_page(limit=5))
        assert first.next_cursor

        conn = sqlite3.connect(str(db_path))
        now = time.time() + 10_000
        progression_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, '[]')",
            (progression_id, now),
        )
        conn.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, now, progression_id, now, "newer", "running"),
        )
        conn.commit()
        conn.close()

        second = asyncio.run(sessions_svc.list_sessions_page(limit=5, cursor=first.next_cursor))

        first_ids = {row["id"] for row in first.items}
        second_ids = {row["id"] for row in second.items}
        assert first_ids.isdisjoint(second_ids)
        assert session_id not in second_ids
        assert second.items[0]["updated_at"] <= first.items[-1]["updated_at"]

    def test_cursor_is_bound_to_its_filter_contract(self, seeded):
        import asyncio

        from lionagi.studio.services import sessions as sessions_svc

        first = asyncio.run(
            sessions_svc.list_sessions_page(
                limit=2,
                where=sessions_svc.SessionFilter(statuses={"running"}),
            )
        )
        assert first.next_cursor

        with pytest.raises(sessions_svc.SessionListCursorError):
            asyncio.run(
                sessions_svc.list_sessions_page(
                    limit=2,
                    cursor=first.next_cursor,
                    where=sessions_svc.SessionFilter(statuses={"completed"}),
                )
            )

    def test_deep_offset_compatibility_path_is_refused(self, client):
        from lionagi.studio.services import sessions as sessions_svc

        response = client.get(
            "/api/sessions/",
            params={"offset": sessions_svc.MAX_SESSION_OFFSET + 1},
        )
        assert response.status_code == 422

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
        import lionagi.state.db as db_mod

        monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", tmp_path / "gone.db")
        body = client.get("/api/admin/readiness").json()
        assert body["status"] == "unavailable"
        assert body["store_present"] is False

    def test_slow_store_reports_slow_not_unavailable(self, client, monkeypatch):
        """A store that answers, but not inside the probe's deadline, is a
        distinct verdict from one that cannot be reached at all.

        The double stalls on the first statement rather than on the connect,
        because that is where a real store stalls: opening a SQLite file takes
        no database lock, so a connect against a store held by someone else
        still succeeds and it is the statement afterwards that waits. A double
        that hung at the connect would be testing a deadline over a call that
        cannot be slow for the reason this verdict exists.
        """
        import anyio

        class _SlowConnection:
            # Shaped like the real connection object, which is awaited to
            # connect and closed explicitly. A double that only implemented
            # `async with` would still pass while the code under test stopped
            # using it.
            def __await__(self):
                async def _connect():
                    return self

                return _connect().__await__()

            async def execute(self, *args):
                await anyio.sleep(5)
                raise AssertionError("probe should have given up before this")

            async def close(self):
                return None

        import aiosqlite

        monkeypatch.setattr(aiosqlite, "connect", lambda *a, **kw: _SlowConnection())
        body = client.get("/api/admin/readiness", params={"timeout_ms": 100}).json()
        assert body["status"] == "slow"
        assert body["timeout_ms"] == 100
        assert body["store_present"] is True
        assert "did not answer" in body["detail"]

    def test_a_real_lock_leaves_no_connection_running_behind_the_timeout(self, seeded, monkeypatch):
        """The probe gives up on a genuinely locked store and closes what it opened.

        A slow verdict is a cancellation, so the code that closes the connection
        runs inside a scope that has already been cancelled. If that close is
        awaited unprotected it is cancelled too, and the connection's worker
        thread outlives the probe holding an open database — until the event
        loop closes underneath it and it raises from a thread nobody is
        watching. Nothing about the response body shows this, which is why the
        assertion is on the connection rather than on the verdict.

        The lock is a real one taken by another connection. A slow double can
        be made to hang, but only a real lock produces the real connection, the
        real worker thread and the real cleanup path that was wrong.
        """
        import asyncio

        import aiosqlite

        from lionagi.studio.services import admin as admin_svc

        db_path, _ = seeded

        # Hold every connection the probe opens. Keeping the reference is part
        # of the measurement rather than bookkeeping: dropping it runs
        # aiosqlite's finalizer, which stops the worker on its own and would
        # hide a connection the probe failed to close.
        opened = []
        real_connect = aiosqlite.connect

        def _recording_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        monkeypatch.setattr(aiosqlite, "connect", _recording_connect)

        blocker = _hold_the_write_lock(db_path)
        try:
            body = asyncio.run(admin_svc.store_probe(timeout_ms=50))
            assert body["status"] == "slow", body
            assert len(opened) == 1, opened
            # Asked of the connection, not of the thread listing. Counting live
            # workers cannot answer this in either direction: on the correct
            # path the worker resolves the close future and only then leaves
            # its loop, so it is still alive for a moment after close()
            # returns; and a genuinely leaked worker stays visible only until
            # its blocked statement gives up, which the probe caps at its own
            # deadline. Both windows are that same handful of milliseconds and
            # only timing separates them, so an immediate count is a race --
            # it is what reddened an unrelated PR's CI on a loaded runner --
            # while any settling window simply waits out the leak it is meant
            # to catch. Closing sets both of the below, and neither depends on
            # when the worker thread happens to be scheduled.
            conn = opened[0]
            assert conn._connection is None and not conn._running, (
                "the probe returned without closing the connection it opened"
            )
        finally:
            blocker.rollback()
            blocker.close()

    def test_a_caller_that_gives_up_first_also_leaves_nothing_running(self, seeded, monkeypatch):
        """The same cleanup, with the cancellation coming from outside.

        Readiness is served inside a request, and a request can be abandoned:
        the client disconnects, the daemon shuts down, an outer deadline fires.
        Then the probe's own cleanup runs with a cancellation already active
        around it, and an unprotected close is cancelled before it reaches the
        connection. The previous test cannot see this — there the probe absorbs
        its own timeout, so by the time cleanup runs nothing is cancelling any
        more and the close completes either way.

        The assertion is deliberately immediate. Left alone the thread does go
        away eventually: dropping the last reference to the connection runs a
        finalizer that stops the worker once its statement gives up. Waiting for
        that is what makes the difference invisible, and it is not the behaviour
        being asked for — a finalizer completing a close against an event loop
        that has since closed is the failure, not the recovery. What the probe
        owes its caller is a connection already closed by the time it returns.
        """
        import threading

        import anyio
        from aiosqlite.core import _connection_worker_thread

        from lionagi.studio.services import admin as admin_svc

        db_path, _ = seeded

        def _workers() -> int:
            return sum(
                1
                for t in threading.enumerate()
                if _is_live_connection_worker(t, _connection_worker_thread)
            )

        blocker = _hold_the_write_lock(db_path)
        try:
            before = _workers()

            async def _abandon():
                with anyio.move_on_after(0.05):
                    # Long enough that the caller's deadline is the one that
                    # fires, short enough that the cleanup's own bounded wait
                    # for the locked read to give up does not dominate the test.
                    await admin_svc.store_probe(timeout_ms=1000)

            anyio.run(_abandon)
            # Read immediately, and deliberately so -- see this test's
            # docstring. What the probe owes an abandoned caller is a
            # connection already closed by the time it returns, so any
            # settling window here would accept the exact regression the
            # test exists to catch: an unshielded close that lets the worker
            # outlive the probe and finish later on its own.
            assert _workers() == before, "a connection worker outlived an abandoned probe"
        finally:
            blocker.rollback()
            blocker.close()

        # Same coincidence as the previous test: by assertion time a
        # correctly-cleaned-up worker is already gone from
        # threading.enumerate(), so this passes whether or not the predicate
        # checks is_alive(). Splice in a terminating-but-not-alive stand-in
        # and confirm _workers() -- the same computation the assertion above
        # relies on -- does not count it as live.
        real_enumerate = threading.enumerate

        class _TerminatingWorker:
            def __init__(self) -> None:
                # Set on the instance, not the class -- a plain function
                # assigned as a class attribute binds as a method on access
                # (breaking `is target` identity below).
                self._target = _connection_worker_thread

            def is_alive(self) -> bool:
                return False

        monkeypatch.setattr(
            threading, "enumerate", lambda: [*real_enumerate(), _TerminatingWorker()]
        )
        assert _workers() == before, (
            "a terminating-but-not-alive worker must not be counted as live"
        )

    def test_live_connection_worker_predicate_excludes_a_reaped_thread(self):
        """Both arms of the worker-count predicate: a thread still actually
        running is counted, and a thread object whose target has already
        returned (is_alive() is False) is not, even though it still shares
        the same ``_target`` and has not yet dropped out of
        ``threading.enumerate()``'s backing registry."""

        def _target():
            pass

        class _FakeThread:
            def __init__(self, target, alive: bool):
                self._target = target
                self._alive = alive

            def is_alive(self) -> bool:
                return self._alive

        running = _FakeThread(_target, alive=True)
        terminating = _FakeThread(_target, alive=False)
        other = _FakeThread(lambda: None, alive=True)

        assert _is_live_connection_worker(running, _target) is True
        assert _is_live_connection_worker(terminating, _target) is False
        assert _is_live_connection_worker(other, _target) is False

    def test_a_connect_is_never_abandoned_partway(self, seeded, monkeypatch):
        """Whatever else is cancelled, the connect runs to completion.

        This is the leak the two tests above cannot see. A connection is closed
        through the object the connect returns, so a connect cancelled midway
        leaves the driver's worker thread running with nothing left to reach it
        by: the close that follows finds no connection and returns at once,
        reporting success while the thread is still there. Later it completes
        against an event loop that has closed, from a thread nobody is
        watching.

        Asserted on the connect rather than on a thread count because the
        window is a race — it needs the caller to give up during the connect
        specifically, which no test can schedule reliably against a real
        connection. What can be pinned is the property that closes the window.
        """
        import anyio

        finished = []

        class _SlowToConnect:
            def __await__(self):
                async def _connect():
                    await anyio.sleep(0.2)
                    finished.append(True)
                    return self

                return _connect().__await__()

            async def execute(self, *args):
                return self

            async def fetchone(self):
                return (0,)

            async def close(self):
                return None

        import aiosqlite

        from lionagi.studio.services import admin as admin_svc

        monkeypatch.setattr(aiosqlite, "connect", lambda *a, **kw: _SlowToConnect())

        async def _abandon():
            # Fires while the connect is still in flight.
            with anyio.move_on_after(0.05):
                await admin_svc.store_probe(timeout_ms=5000)

        anyio.run(_abandon)
        assert finished == [True], "the caller's cancellation reached the connect"

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

    def test_this_file_runs_under_the_interpreter_default_sigpipe(self):
        """The tests above must not inherit a ``SIG_DFL`` from an earlier test.

        Several of them close an event loop while a database connection's
        worker thread may still be finishing; closing a loop closes the read
        end of its self-pipe before the write end, so a thread handing back
        a result in that window writes to a pipe whose peer is gone. Under
        the interpreter default that's an ``OSError`` asyncio swallows.
        Under ``SIG_DFL`` the kernel kills the process first, with buffered
        output still buffered: no traceback, no failing assertion, just a
        worker that stopped.

        The CLI sets ``SIG_DFL`` on entry deliberately, so a command in a
        pipeline dies quietly when its reader leaves. ``signal.signal`` is
        process-wide, so any test that drives the CLI in-process leaks that
        policy to every test after it; a fixture in ``tests/conftest.py``
        restores it, and this asserts the restore actually happened.

        Order-dependent by nature -- it only catches the leak when something
        that changed the policy ran earlier in this same process (run this
        file after ``tests/cli`` with ``-n 0`` to see it fail without the
        fixture). Distributed across workers it may pass without proving
        anything, so it is written to never fail falsely.
        """
        import signal

        assert signal.getsignal(signal.SIGPIPE) is signal.SIG_IGN, (
            "an earlier test left SIGPIPE at SIG_DFL; a closed-loop wakeup "
            "here will kill the worker with no traceback"
        )
