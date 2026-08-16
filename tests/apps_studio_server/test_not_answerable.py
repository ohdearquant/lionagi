# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The third state a data route needs when the configured store has no file.

``require_file_store`` distinguishes three conditions from ``state_db_file()``
alone: a store path that exists (answer the rows), one that does not exist yet
(answer empty, unchanged), and no path at all -- a server-backed or in-memory
store this SQLite-direct layer cannot open. Only the third one raises.

The suite below pins all three, plus the one thing a must-refuse-only suite
would miss: a route in front of an *existing* store must still answer with its
real rows, not get swept into the refusal by an over-eager guard.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod  # noqa: E402
from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services._db import (  # noqa: E402
    StoreNotAddressableError,
    require_file_store,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _configure(monkeypatch, *, default: Path, url: str | None) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", default)
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url}),
    )


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    from lionagi.studio.app import app

    return TestClient(
        app, base_url="http://127.0.0.1:8765", raise_server_exceptions=raise_server_exceptions
    )


# ── require_file_store(): the three states ──────────────────────────────────


def test_existing_store_path_does_not_raise(tmp_path, monkeypatch):
    """The accept arm: an existing file-backed store is left alone."""
    default = tmp_path / "state.db"
    _run(StateDB(default).open())
    _configure(monkeypatch, default=default, url=None)

    require_file_store()  # must not raise


def test_missing_store_file_does_not_raise(tmp_path, monkeypatch):
    """A store that simply has not been created yet is not the same as one
    this layer cannot reach -- the existing empty-result behavior stays."""
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url=None)
    assert not default.exists()

    require_file_store()  # must not raise


def test_server_backed_store_raises(tmp_path, monkeypatch):
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@host/db")

    with pytest.raises(StoreNotAddressableError) as exc_info:
        require_file_store()
    assert exc_info.value.backend == "postgresql"


def test_in_memory_store_raises(tmp_path, monkeypatch):
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url=":memory:")

    with pytest.raises(StoreNotAddressableError) as exc_info:
        require_file_store()
    assert "memory" in exc_info.value.backend


# ── The app-level 501 handler, on a representative route ────────────────────


def test_data_route_is_501_when_store_is_server_backed(tmp_path, monkeypatch):
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@host/db")

    r = _client().get("/api/projects/")

    assert r.status_code == 501
    body = r.json()
    assert body["route"] == "/api/projects/"
    assert body["backend"] == "postgresql"
    assert "detail" in body and "postgresql" in body["detail"]


def test_same_route_is_200_empty_when_store_file_is_merely_absent(tmp_path, monkeypatch):
    """Same route, same shape of "nothing here" -- but this time the store is
    a local file that just has not been created, so the pre-existing empty
    answer must survive untouched."""
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url=None)
    assert not default.exists()

    r = _client().get("/api/projects/")

    assert r.status_code == 200
    assert r.json() == {"projects": [], "unassigned_count": 0}


def test_same_route_still_answers_rows_against_an_existing_store(tmp_path, monkeypatch):
    """The over-refusal arm: a guard that fires on every request would pass
    every must-refuse test above and still be wrong. A route in front of a
    store that genuinely exists must keep answering with real rows."""
    default = tmp_path / "state.db"
    _run(StateDB(default).open())
    _configure(monkeypatch, default=default, url=None)

    client = _client()
    created = client.post("/api/projects/", json={"name": "demo-project"})
    assert created.status_code == 201

    r = client.get("/api/projects/")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()["projects"]]
    assert names == ["demo-project"]


@pytest.mark.parametrize(
    ("path", "expected_route"),
    (
        ("/api/shows/", "/api/shows/"),
        ("/api/stats", "/api/stats"),
    ),
)
def test_remaining_sqlite_direct_routes_are_501_when_store_is_server_backed(
    tmp_path, monkeypatch, path, expected_route
):
    """A partial Studio response from the fallback SQLite file is never valid.

    Shows and the aggregate stats route were the two HTTP paths left outside
    ``require_file_store``.  They must now use the same explicit refusal as
    the other SQLite-direct services.
    """
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@host/db")

    r = _client().get(path)

    assert r.status_code == 501
    body = r.json()
    assert body["route"] == expected_route
    assert body["backend"] == "postgresql"
    assert "secret" not in str(body)


def test_signal_reads_refuse_a_server_backed_store_before_fallback(tmp_path, monkeypatch):
    """The signal reader is internal, so pin its service-level refusal directly."""
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@host/db")

    from lionagi.studio.services.signals import get_signals_after

    with pytest.raises(StoreNotAddressableError) as exc_info:
        _run(get_signals_after("session-1", 0))
    assert exc_info.value.backend == "postgresql"


# ── Readiness stays 200 no matter what (it is asked about the store, not for rows) ──


def test_readiness_route_stays_200_when_store_is_server_backed(tmp_path, monkeypatch):
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@host/db")

    r = _client().get("/api/admin/readiness")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"unavailable", "healthy", "slow"}


# ── Startup must reach the routes that do the refusing ──────────────────────


@pytest.fixture
def fresh_operator():
    """A coordinator built from this test's configured store, not an earlier one.

    The coordinator is a process-global singleton holding a store instance, and
    a test that leaves it started leaves the next one reading whatever file it
    was constructed with. Found the hard way: the 501 test below passed on its
    own and answered 200 inside the full suite, from a store no assertion in it
    had ever named.
    """
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing

    _run(reset_operator_coordinator_for_testing())
    yield
    _run(reset_operator_coordinator_for_testing())


def test_the_daemon_starts_against_a_server_backed_store(tmp_path, monkeypatch, fresh_operator):
    """Every test above builds the client without entering the lifespan, so
    none of them exercises startup — and startup is where a subsystem that can
    only read a local file gets to abort the whole daemon before a single
    route is reachable. The Operator did exactly that: its store resolves the
    StateDB file eagerly during recovery, so a server-backed deployment never
    served anything, and the 501 those routes are supposed to answer was
    unreachable by construction.

    The port here is one nothing listens on, so the connection is refused
    rather than hanging. That is the point: the rest of startup tolerates a
    store it cannot reach, and the assertion is that the Operator does too.
    """
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@127.0.0.1:1/db")

    from lionagi.studio.app import app

    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        r = client.get("/api/operator/conversations")

    assert r.status_code == 501, (
        "the daemon started, so the route got to answer — and its answer is the "
        "permanent one, not a 503 inviting a retry that cannot help"
    )
    body = r.json()
    assert body["backend"] == "postgresql"
    assert "secret" not in str(body), "the refusal must not echo the store URL"


def test_operator_startup_recovers_nothing_rather_than_refusing(tmp_path, monkeypatch):
    """The startup half on its own, without a client: a store with no file
    holds no interrupted turns, so there is nothing to recover and no reason
    to raise. Asserted separately because the test above would also pass if
    the Operator were removed from the lifespan altogether."""
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@127.0.0.1:1/db")

    from lionagi.studio.services.operator import operator_startup

    assert _run(operator_startup()) == []


def test_operator_startup_still_recovers_against_a_file_store(
    tmp_path, monkeypatch, fresh_operator
):
    """The arm that stops the gate above from being a blanket disable: a real
    file-backed store still runs Operator recovery, which is what a guard
    keyed on the wrong condition would have silently switched off.

    The fixture matters here for a second reason: `_started` is what this
    asserts, and inheriting a coordinator some earlier test already started
    would satisfy it without this call doing anything.
    """
    default = tmp_path / "state.db"
    _run(StateDB(default).open())
    _configure(monkeypatch, default=default, url=None)

    from lionagi.studio.operator.coordinator import get_operator_coordinator
    from lionagi.studio.services.operator import operator_startup

    assert not get_operator_coordinator()._started, "the premise: nothing has started it yet"
    assert _run(operator_startup()) == []
    assert get_operator_coordinator()._started, (
        "the coordinator never started, so recovery was skipped on a store that has a file"
    )
