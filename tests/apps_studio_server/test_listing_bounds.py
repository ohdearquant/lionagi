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
        """The probe gives up on a genuinely locked store and takes its thread with it.

        A slow verdict is a cancellation, so the code that closes the connection
        runs inside a scope that has already been cancelled. If that close is
        awaited unprotected it is cancelled too, and the connection's worker
        thread outlives the probe holding an open database — until the event
        loop closes underneath it and it raises from a thread nobody is
        watching. Nothing about the response body shows this, which is why the
        assertion is on the thread rather than on the verdict.

        The lock is a real one taken by another connection. A slow double can
        be made to hang, but only a real lock produces the real connection, the
        real worker thread and the real cleanup path that was wrong.
        """
        import asyncio
        import threading

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
            body = asyncio.run(admin_svc.store_probe(timeout_ms=50))
            assert body["status"] == "slow", body
            assert _workers() == before, "a connection worker outlived the probe"
        finally:
            blocker.rollback()
            blocker.close()

        # The real-thread assertion above passes the same way whether or not
        # the predicate checks is_alive() -- by the time it runs, a
        # correctly-cleaned-up worker has already dropped out of
        # threading.enumerate() entirely, so a target-only predicate and the
        # correct one agree by coincidence, not because either was exercised.
        # Force the discriminating case: splice a terminating-but-not-alive
        # stand-in into the real thread listing and confirm _workers() (the
        # same computation the assertion above relies on) does not count it.
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
        worker thread may still be finishing. Closing a loop closes the read
        end of its self-pipe before the write end, so a thread handing back a
        result in that window writes to a pipe whose peer is gone. Under the
        interpreter default that is an ``OSError`` asyncio already swallows.
        Under ``SIG_DFL`` the kernel kills the process first, and it dies with
        its buffered output still buffered: no traceback, no failing
        assertion, just a worker that stopped and a report blaming whichever
        test it was holding.

        The CLI sets ``SIG_DFL`` on entry, deliberately, because a command in
        a pipeline should die quietly when its reader leaves. ``signal.signal``
        is process-wide, so any test that drives the CLI in-process hands that
        to every test after it. A fixture in ``tests/conftest.py`` puts it
        back; this asserts the tests here actually got the benefit.

        Order-dependent by nature: it can only catch the leak when something
        that changes the policy ran earlier in this same process. Run this
        file after ``tests/cli`` with ``-n 0`` to see it fail without that
        fixture. Distributed across workers it may pass without proving
        anything, which is why it is written to never fail falsely.
        """
        import signal

        assert signal.getsignal(signal.SIGPIPE) is signal.SIG_IGN, (
            "an earlier test left SIGPIPE at SIG_DFL; a closed-loop wakeup "
            "here will kill the worker with no traceback"
        )
