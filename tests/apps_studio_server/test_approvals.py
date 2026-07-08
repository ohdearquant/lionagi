# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the server-side approval ledger backing operator-proposed actions."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.approvals as approvals_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(approvals_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(approvals_mod, "_DB", str(db_path))


def _make_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _patch_db(monkeypatch, db_path)
    # Pre-apply the full StateDB schema (sessions, approvals, ...) the same
    # way every other studio test does -- TestClient.post() outside a `with`
    # block does not run ASGI lifespan startup, so nothing else creates it.
    _run(_init_db(db_path))
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


async def _init_db(db_path: Path) -> None:
    async with StateDB(db_path):
        pass  # opens + applies schema (creates the approvals table too)


# ---------------------------------------------------------------------------
# Unit-level: service functions directly (no HTTP, no principal concerns)
# ---------------------------------------------------------------------------


def test_propose_returns_pending_row_with_hash(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )
    assert row["status"] == "pending"
    assert row["action_kind"] == "launch_playbook"
    assert row["params_hash"] == approvals_mod.compute_params_hash({"name": "demo"})
    assert row["granted_at"] is None
    assert row["consumed_at"] is None
    assert row["expires_at"] - row["proposed_at"] == pytest.approx(
        approvals_mod.APPROVAL_TTL_SECONDS, abs=1
    )


def test_params_hash_is_canonical_over_key_order():
    from lionagi.studio.services import approvals as approvals_mod

    a = approvals_mod.compute_params_hash({"b": 2, "a": 1})
    b = approvals_mod.compute_params_hash({"a": 1, "b": 2})
    assert a == b


def test_grant_then_consume_happy_path(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import approvals as approvals_mod

    params = {"name": "demo"}
    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params=params))
    granted = _run(approvals_mod.grant_approval(row["id"]))
    assert granted["status"] == "granted"
    assert granted["granted_at"] is not None

    consumed = _run(
        approvals_mod.require_approval(row["id"], action_kind="launch_playbook", params=params)
    )
    assert consumed["status"] == "consumed"
    assert consumed["consumed_at"] is not None


def test_double_consume_is_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import approvals as approvals_mod

    params = {"name": "demo"}
    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params=params))
    _run(approvals_mod.grant_approval(row["id"]))
    _run(approvals_mod.require_approval(row["id"], action_kind="launch_playbook", params=params))

    with pytest.raises(HTTPException) as exc_info:
        _run(
            approvals_mod.require_approval(row["id"], action_kind="launch_playbook", params=params)
        )
    assert exc_info.value.status_code == 409


def test_expiry_rejects_grant(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )

    async def _force_expired():
        from lionagi.studio.services._db import open_db

        async with open_db(str(db_path)) as db:
            await db.execute(
                "UPDATE approvals SET expires_at = ? WHERE id = ?",
                (time.time() - 1, row["id"]),
            )
            await db.commit()

    _run(_force_expired())

    with pytest.raises(HTTPException) as exc_info:
        _run(approvals_mod.grant_approval(row["id"]))
    assert exc_info.value.status_code == 409
    assert "not pending" in exc_info.value.detail

    refreshed = _run(approvals_mod.get_approval(row["id"]))
    assert refreshed["status"] == "expired"


def test_expiry_rejects_consume_of_granted_but_expired(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import approvals as approvals_mod

    params = {"name": "demo"}
    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params=params))
    _run(approvals_mod.grant_approval(row["id"]))

    async def _force_expired():
        from lionagi.studio.services._db import open_db

        async with open_db(str(db_path)) as db:
            await db.execute(
                "UPDATE approvals SET expires_at = ? WHERE id = ?",
                (time.time() - 1, row["id"]),
            )
            await db.commit()

    _run(_force_expired())

    with pytest.raises(HTTPException) as exc_info:
        _run(
            approvals_mod.require_approval(row["id"], action_kind="launch_playbook", params=params)
        )
    assert exc_info.value.status_code == 409

    refreshed = _run(approvals_mod.get_approval(row["id"]))
    assert refreshed["status"] == "expired"


def test_params_hash_mismatch_rejected_without_consuming(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )
    _run(approvals_mod.grant_approval(row["id"]))

    with pytest.raises(HTTPException) as exc_info:
        _run(
            approvals_mod.require_approval(
                row["id"], action_kind="launch_playbook", params={"name": "other"}
            )
        )
    assert exc_info.value.status_code == 422

    # A hash mismatch must not burn the single use -- the correct params can
    # still be presented afterward.
    still_granted = _run(approvals_mod.get_approval(row["id"]))
    assert still_granted["status"] == "granted"
    consumed = _run(
        approvals_mod.require_approval(
            row["id"], action_kind="launch_playbook", params={"name": "demo"}
        )
    )
    assert consumed["status"] == "consumed"


def test_action_kind_mismatch_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )
    _run(approvals_mod.grant_approval(row["id"]))

    with pytest.raises(HTTPException) as exc_info:
        _run(
            approvals_mod.require_approval(
                row["id"], action_kind="run_maintenance", params={"name": "demo"}
            )
        )
    assert exc_info.value.status_code == 422


def test_deny_transitions_pending_to_denied(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )
    denied = _run(approvals_mod.deny_approval(row["id"]))
    assert denied["status"] == "denied"

    # A denied approval can never be granted.
    with pytest.raises(HTTPException) as exc_info:
        _run(approvals_mod.grant_approval(row["id"]))
    assert exc_info.value.status_code == 409


def test_require_approval_on_never_granted_approval_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )
    with pytest.raises(HTTPException) as exc_info:
        _run(
            approvals_mod.require_approval(
                row["id"], action_kind="launch_playbook", params={"name": "demo"}
            )
        )
    assert exc_info.value.status_code == 409


def test_get_unknown_approval_returns_none(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import approvals as approvals_mod

    assert _run(approvals_mod.get_approval("does-not-exist")) is None


# ---------------------------------------------------------------------------
# HTTP-level: routes + the grant-route principal separation
# ---------------------------------------------------------------------------


def test_propose_route_creates_pending_approval(tmp_path, monkeypatch):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    resp = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["action_kind"] == "launch_playbook"


def test_grant_route_succeeds_for_human_browser_caller(tmp_path, monkeypatch):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/grant")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "granted"


def test_deny_route_succeeds_for_human_browser_caller(tmp_path, monkeypatch):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/deny")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "denied"


def test_grant_route_403_for_operator_service_principal(tmp_path, monkeypatch):
    """The security-critical case: a caller identifying itself as the
    operator/service context -- e.g. a compromised driver that learned an
    approval id and tries to self-approve -- is rejected before the row is
    touched, regardless of whatever bearer credential it also presents."""
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    ).json()["id"]

    resp = client.post(
        f"/api/approvals/{approval_id}/grant",
        headers={"X-Lionagi-Operator-Principal": "service"},
    )
    assert resp.status_code == 403, resp.text

    # The approval must still be pending -- the 403 short-circuits before
    # any state change.
    from lionagi.studio.services import approvals as approvals_mod

    row = _run(approvals_mod.get_approval(approval_id))
    assert row["status"] == "pending"


def test_deny_route_403_for_operator_service_principal(tmp_path, monkeypatch):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    ).json()["id"]

    resp = client.post(
        f"/api/approvals/{approval_id}/deny",
        headers={"X-Lionagi-Operator-Principal": "service"},
    )
    assert resp.status_code == 403, resp.text


def test_get_route_returns_current_state(tmp_path, monkeypatch):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    ).json()["id"]

    resp = client.get(f"/api/approvals/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_grant_route_404_for_unknown_id(tmp_path, monkeypatch):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    resp = client.post("/api/approvals/does-not-exist/grant")
    assert resp.status_code == 404
