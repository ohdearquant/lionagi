# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Which code the daemon is running, reported on /api/admin/health.

A daemon imports lionagi once, at start. Under an editable install that import
resolves to a working tree, so the code being served is whatever commit that
checkout happens to sit on — and it keeps serving it after the tree moves,
after a branch switch, after the rest of the fleet has advanced. Nothing else
in the health response can see that: the version string is identical between a
current tree and one many commits behind, so every other field looks the same
either way.

These pin the commit and the behind-verdict onto the endpoint an operator or a
monitor already reads, and pin that an *unanswerable* reading is reported as
unknown rather than dropped — an absent key reads as "not checked", which is
indistinguishable from "checked and current" to anything scanning the response.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lionagi.state.db as state_db_mod

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402


def _make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    import lionagi.studio.app as app_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    app = app_mod.create_app()
    return TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765")


def _health_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    client = _make_client(monkeypatch, tmp_path)
    response = client.get("/api/admin/health")
    assert response.status_code == 200
    body = response.json()
    assert "code_identity" in body, "the health response must always carry the key"
    return body["code_identity"]


def _install_identity(monkeypatch: pytest.MonkeyPatch, identity: dict) -> None:
    """Stand in for the real reading, which shells out to git."""
    import lionagi.cli._code_identity as ci

    monkeypatch.setattr(ci, "code_identity", lambda: identity)


def test_a_checkout_behind_its_ref_is_reported_as_drift(monkeypatch, tmp_path):
    # The whole point: the daemon says so itself, on the endpoint already being
    # polled, instead of the operator having to go and rev-parse the tree.
    _install_identity(
        monkeypatch,
        {
            "version": "0.30.2",
            "package_path": "/srv/lionagi/lionagi",
            "git": {"status": "ok", "commit_short": "5ec3bd79cbb5", "behind": 15},
            "drift": {
                "status": "drift",
                "reasons": ["the loaded checkout is 15 commit(s) behind origin/main"],
                "unknown": [],
            },
        },
    )

    payload = _health_identity(monkeypatch, tmp_path)

    assert payload["drift"]["status"] == "drift"
    assert payload["git"]["behind"] == 15
    # The commit is what makes the verdict actionable — "behind" alone does not
    # tell anyone what is running.
    assert payload["git"]["commit_short"] == "5ec3bd79cbb5"


def test_a_current_checkout_is_reported_ok(monkeypatch, tmp_path):
    _install_identity(
        monkeypatch,
        {
            "version": "0.30.2",
            "package_path": "/srv/lionagi/lionagi",
            "git": {"status": "ok", "commit_short": "12411ddb3ab1", "behind": 0},
            "drift": {"status": "ok", "reasons": [], "unknown": []},
        },
    )

    payload = _health_identity(monkeypatch, tmp_path)

    assert payload["drift"]["status"] == "ok"
    assert payload["git"]["behind"] == 0


def test_an_unreadable_identity_is_unknown_and_still_present(monkeypatch, tmp_path):
    # The failure that matters: the reading raised. Dropping the key would leave
    # the response looking exactly like a healthy one.
    import lionagi.cli._code_identity as ci

    def _boom() -> dict:
        raise RuntimeError("git is not on PATH")

    monkeypatch.setattr(ci, "code_identity", _boom)

    payload = _health_identity(monkeypatch, tmp_path)

    assert payload["drift"]["status"] == "unknown"
    assert any("git is not on PATH" in u for u in payload["drift"]["unknown"])


def test_the_daemon_snapshots_its_position_before_it_starts_serving(monkeypatch):
    """The snapshot has to be taken at startup, not on the first health request.

    ``code_identity`` reads its git position once and keeps it, initialising
    lazily. Left to initialise on the first request, the reading describes
    whatever the tree had become by then — so a checkout moved after the daemon
    started would be reported as the code being *run*, with ``checkout_moved``
    false and a clean drift verdict, which is precisely the condition the field
    exists to detect.

    Asserting the snapshot exists by the time the scheduler starts is what
    distinguishes taking it at startup from taking it later: everything after
    that point can already cause, or observe, a request.
    """
    import lionagi.cli._code_identity as ci
    import lionagi.studio.app as app_mod
    from lionagi.studio.scheduler.engine import scheduler

    monkeypatch.setattr(ci, "_SNAPSHOT", None)
    seen: dict[str, object] = {}

    async def _record_start() -> None:
        seen["snapshot_at_scheduler_start"] = ci._SNAPSHOT

    async def _noop() -> None:
        return None

    monkeypatch.setattr(scheduler, "start", _record_start)
    monkeypatch.setattr(scheduler, "stop", _noop)
    monkeypatch.setattr(app_mod, "run_startup_reconciliation", _noop, raising=False)
    monkeypatch.setattr(app_mod, "_start_claude_mirror", lambda: (None, None))
    monkeypatch.setattr(app_mod, "_stop_claude_mirror", lambda *a: _noop())
    monkeypatch.setattr(app_mod, "_startup_warmup", _noop)
    monkeypatch.setattr(app_mod, "_finalize_warmup", lambda *a: _noop())

    async def _drive() -> None:
        async with app_mod.lifespan(None):
            seen["snapshot_while_serving"] = ci._SNAPSHOT

    import anyio

    anyio.run(_drive)

    assert seen.get("snapshot_at_scheduler_start") is not None, (
        "the position was not read before the scheduler started"
    )
    assert seen.get("snapshot_while_serving") is not None


def test_startup_snapshot_does_not_block_the_event_loop(monkeypatch):
    import threading

    import lionagi.cli._code_identity as ci
    import lionagi.studio.app as app_mod
    from lionagi.studio.scheduler.engine import scheduler

    seen: dict[str, int] = {}

    def _record_snapshot() -> None:
        seen["snapshot_thread"] = threading.get_ident()

    async def _noop() -> None:
        return None

    monkeypatch.setattr(ci, "snapshot_git_position", _record_snapshot)
    monkeypatch.setattr(scheduler, "start", _noop)
    monkeypatch.setattr(scheduler, "stop", _noop)
    monkeypatch.setattr(app_mod, "run_startup_reconciliation", _noop, raising=False)
    monkeypatch.setattr(app_mod, "_start_claude_mirror", lambda: (None, None))
    monkeypatch.setattr(app_mod, "_stop_claude_mirror", lambda *a: _noop())
    monkeypatch.setattr(app_mod, "_startup_warmup", _noop)
    monkeypatch.setattr(app_mod, "_finalize_warmup", lambda *a: _noop())

    async def _drive() -> None:
        seen["event_loop_thread"] = threading.get_ident()
        async with app_mod.lifespan(None):
            pass

    import anyio

    anyio.run(_drive)

    assert seen["snapshot_thread"] != seen["event_loop_thread"]


def test_the_reading_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """A health check must not stall the daemon it is reporting on.

    The reading shells out to git, which can take seconds against an unhealthy
    tree, so it has to run off the loop. Asserting it is a *different* thread
    than the one serving the request is what distinguishes an offloaded call
    from an inline one; timing would not, since a fast mock returns instantly
    either way.
    """
    import threading

    import lionagi.cli._code_identity as ci

    serving_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _record() -> dict:
        seen["thread"] = threading.get_ident()
        return {"drift": {"status": "ok", "reasons": [], "unknown": []}}

    monkeypatch.setattr(ci, "code_identity", _record)

    _health_identity(monkeypatch, tmp_path)

    assert "thread" in seen, "the reading never ran"
    assert seen["thread"] != serving_thread
