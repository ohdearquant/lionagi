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

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _patch_db(monkeypatch, db_path)
    # Pre-apply the full StateDB schema (sessions, approvals, ...) the same
    # way every other studio test does -- TestClient.post() outside a `with`
    # block does not run ASGI lifespan startup, so nothing else creates it.
    _run(_init_db(db_path))
    from lionagi_studio.app import app

    return TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        headers={"Content-Type": "application/json"},
    )


async def _init_db(db_path: Path) -> None:
    async with StateDB(db_path):
        pass  # opens + applies schema (creates the approvals table too)


# Unit-level: service functions directly (no HTTP, no principal concerns)


def test_propose_returns_pending_row_with_hash(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

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
    from lionagi_studio.services import approvals as approvals_mod

    a = approvals_mod.compute_params_hash({"b": 2, "a": 1})
    b = approvals_mod.compute_params_hash({"a": 1, "b": 2})
    assert a == b


def test_grant_then_consume_happy_path(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

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

    from lionagi_studio.services import approvals as approvals_mod

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

    from lionagi_studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )

    async def _force_expired():
        from lionhub.orchestration.state_store import open_db

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

    from lionagi_studio.services import approvals as approvals_mod

    params = {"name": "demo"}
    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params=params))
    _run(approvals_mod.grant_approval(row["id"]))

    async def _force_expired():
        from lionhub.orchestration.state_store import open_db

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

    from lionagi_studio.services import approvals as approvals_mod

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

    from lionagi_studio.services import approvals as approvals_mod

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

    from lionagi_studio.services import approvals as approvals_mod

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

    from lionagi_studio.services import approvals as approvals_mod

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

    from lionagi_studio.services import approvals as approvals_mod

    assert _run(approvals_mod.get_approval("does-not-exist")) is None


# HTTP-level: routes + the grant-route principal separation


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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_grant_route_succeeds_for_human_browser_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/grant", headers=_auth("test-token"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "granted"


def test_deny_route_succeeds_for_human_browser_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/deny", headers=_auth("test-token"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "denied"


def test_grant_route_403_when_no_auth_token_configured(tmp_path, monkeypatch):
    """With no token configured there is no human credential to distinguish,
    so granting fails closed rather than being open to any local caller."""
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/grant")
    assert resp.status_code == 403, resp.text
    check = client.get(f"/api/approvals/{approval_id}")
    assert check.json()["status"] == "pending"


def test_grant_route_rejected_without_correct_bearer(tmp_path, monkeypatch):
    """A caller that simply omits the service marker still cannot grant: the
    route positively requires the configured bearer credential."""
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/grant", headers=_auth("wrong-token"))
    assert resp.status_code in (401, 403), resp.text
    check = client.get(f"/api/approvals/{approval_id}", headers=_auth("test-token"))
    assert check.json()["status"] == "pending"


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
    from lionagi_studio.services import approvals as approvals_mod

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
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    resp = client.post("/api/approvals/does-not-exist/grant", headers=_auth("test-token"))
    assert resp.status_code == 404


# Evidence chain: hash-chained audit trail on the ledger


def _evidence_rows(db_path: Path) -> list[dict]:
    async def _fetch():
        from lionhub.orchestration.state_store import open_db

        async with open_db(str(db_path)) as db:
            cur = await db.execute("SELECT * FROM approval_evidence ORDER BY sequence ASC")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    return _run(_fetch())


def test_full_lifecycle_produces_intact_chain(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

    params = {"name": "demo"}
    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params=params))
    _run(approvals_mod.grant_approval(row["id"]))
    _run(approvals_mod.require_approval(row["id"], action_kind="launch_playbook", params=params))

    rows = _evidence_rows(db_path)
    assert [r["event_type"] for r in rows] == ["proposed", "granted", "consumed"]
    assert [r["sequence"] for r in rows] == [1, 2, 3]

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is True
    assert verdict["total_entries"] == 3
    assert verdict["errors"] == []


def test_genesis_previous_hash_is_64_zero_chars(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

    _run(approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"}))

    rows = _evidence_rows(db_path)
    assert rows[0]["previous_hash"] == "0" * 64
    assert rows[0]["sequence"] == 1
    # chain_hash = sha256(content_hash + previous_hash)
    expected_content = approvals_mod._compute_content_hash(
        {
            "id": rows[0]["id"],
            "sequence": rows[0]["sequence"],
            "event_type": rows[0]["event_type"],
            "approval_id": rows[0]["approval_id"],
            "action_kind": rows[0]["action_kind"],
            "status_from": rows[0]["status_from"],
            "status_to": rows[0]["status_to"],
            "params_hash": rows[0]["params_hash"],
            "justification_class": rows[0]["justification_class"],
            "justification_reason": rows[0]["justification_reason"],
            "created_at": rows[0]["created_at"],
        }
    )
    assert rows[0]["content_hash"] == expected_content
    assert rows[0]["chain_hash"] == approvals_mod._compute_chain_hash(expected_content, "0" * 64)


def test_tamper_detected_by_verify(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    params = {"name": "demo"}
    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params=params))
    _run(approvals_mod.grant_approval(row["id"]))

    async def _tamper():
        async with open_db(str(db_path)) as db:
            await db.execute(
                "UPDATE approval_evidence SET action_kind = 'evil_action' WHERE sequence = 1"
            )
            await db.commit()

    _run(_tamper())

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is False
    assert verdict["total_entries"] == 2
    assert any("hash mismatch" in e for e in verdict["errors"])


def test_hmac_signing_off_by_default(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.delenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY", raising=False)
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

    _run(approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"}))
    rows = _evidence_rows(db_path)
    assert rows[0]["hmac_sig"] is None

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is True


def test_hmac_signing_when_key_configured(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY", "secret-key")
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

    _run(approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"}))
    rows = _evidence_rows(db_path)
    assert rows[0]["hmac_sig"] is not None
    assert rows[0]["hmac_sig"] == approvals_mod._compute_hmac_sig(
        rows[0]["chain_hash"], rows[0]["created_at"], "secret-key"
    )

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is True


def test_hmac_tamper_detected_when_key_configured(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY", "secret-key")
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    _run(approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"}))

    async def _tamper_sig():
        async with open_db(str(db_path)) as db:
            await db.execute(
                "UPDATE approval_evidence SET hmac_sig = 'forged-signature-value' WHERE sequence = 1"
            )
            await db.commit()

    _run(_tamper_sig())

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is False
    assert any("hmac signature mismatch" in e for e in verdict["errors"])


def test_hmac_stripped_signature_fails_verification(tmp_path, monkeypatch):
    """Nulling hmac_sig on a tampered row must not downgrade to chain-only checks."""
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY", "secret-key")
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    _run(approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"}))
    rows = _evidence_rows(db_path)
    row = rows[0]

    forged_payload = {
        "id": row["id"],
        "sequence": row["sequence"],
        "event_type": row["event_type"],
        "approval_id": row["approval_id"],
        "action_kind": "evil_action",
        "status_from": row["status_from"],
        "status_to": row["status_to"],
        "params_hash": row["params_hash"],
        "justification_class": row["justification_class"],
        "justification_reason": row["justification_reason"],
        "created_at": row["created_at"],
    }
    forged_content = approvals_mod._compute_content_hash(forged_payload)
    forged_chain = approvals_mod._compute_chain_hash(forged_content, row["previous_hash"])

    async def _forge():
        async with open_db(str(db_path)) as db:
            await db.execute(
                "UPDATE approval_evidence SET action_kind = 'evil_action', "
                "content_hash = ?, chain_hash = ?, hmac_sig = NULL WHERE sequence = 1",
                (forged_content, forged_chain),
            )
            await db.commit()

    _run(_forge())

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is False
    assert any("hmac signature missing" in e for e in verdict["errors"])


def test_renumbered_sequence_detected_by_verify(tmp_path, monkeypatch):
    """Renumbering every row by the same offset must break verification.

    Before the fix, the evidence hash/signature did not cover `sequence` and
    verification never checked sequence contiguity, so shifting every row's
    sequence by a constant offset (e.g. 1,2 -> 101,102) left the chain
    reporting valid=True even though the ledger numbering had changed.
    """
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )
    _run(approvals_mod.grant_approval(row["id"]))

    rows = _evidence_rows(db_path)
    assert [r["sequence"] for r in rows] == [1, 2]

    before = _run(approvals_mod.verify_evidence_chain())
    assert before["valid"] is True

    async def _renumber():
        async with open_db(str(db_path)) as db:
            # Shift high->low to avoid a transient UNIQUE collision on sequence.
            await db.execute("UPDATE approval_evidence SET sequence = 102 WHERE sequence = 2")
            await db.execute("UPDATE approval_evidence SET sequence = 101 WHERE sequence = 1")
            await db.commit()

    _run(_renumber())

    after = _run(approvals_mod.verify_evidence_chain())
    assert after["valid"] is False


def test_deny_with_justification_lands_in_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )
    _run(
        approvals_mod.deny_approval(
            row["id"], justification_class="policy", justification_reason="not allowed here"
        )
    )

    rows = _evidence_rows(db_path)
    denied = [r for r in rows if r["event_type"] == "denied"][0]
    assert denied["justification_class"] == "policy"
    assert denied["justification_reason"] == "not allowed here"


def test_deny_route_with_justification_body(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(
        f"/api/approvals/{approval_id}/deny",
        json={"reason_class": "security", "reason": "unsafe action"},
        headers=_auth("test-token"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "denied"


def test_deny_route_rejects_blank_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(
        f"/api/approvals/{approval_id}/deny",
        json={"reason_class": "security", "reason": "   "},
        headers=_auth("test-token"),
    )
    assert resp.status_code == 422, resp.text

    # Rejected validation must not consume the pending approval.
    check = client.get(f"/api/approvals/{approval_id}", headers=_auth("test-token"))
    assert check.json()["status"] == "pending"


def test_deny_route_rejects_invalid_reason_class(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(
        f"/api/approvals/{approval_id}/deny",
        json={"reason_class": "not-a-real-class", "reason": "whatever"},
        headers=_auth("test-token"),
    )
    assert resp.status_code == 422, resp.text


def test_grant_needs_no_justification(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/grant", headers=_auth("test-token"))
    assert resp.status_code == 200, resp.text


def test_raw_params_never_appear_in_evidence_rows(tmp_path, monkeypatch):
    """R2: evidence rows carry hashes/kinds/statuses/timestamps/justification
    only -- never the raw action params (which may be sensitive)."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

    secret_params = {"api_key": "sk-super-secret-value", "target": "prod-db"}
    row = _run(approvals_mod.create_approval(action_kind="run_maintenance", params=secret_params))
    _run(approvals_mod.grant_approval(row["id"]))
    _run(
        approvals_mod.require_approval(
            row["id"], action_kind="run_maintenance", params=secret_params
        )
    )
    _run(
        approvals_mod.deny_approval(
            _run(
                approvals_mod.create_approval(action_kind="run_maintenance", params=secret_params)
            )["id"],
            justification_class="security",
            justification_reason="not allowed for this session",
        )
    )

    rows = _evidence_rows(db_path)
    serialized = str(rows)
    assert "sk-super-secret-value" not in serialized
    assert "prod-db" not in serialized
    # The params_hash is present instead of the raw params.
    assert all(r["params_hash"] == approvals_mod.compute_params_hash(secret_params) for r in rows)


def _fake_request(headers: dict[str, str]):
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return StarletteRequest(scope)


def test_require_human_principal_uses_compare_digest(monkeypatch):
    """The timing-safe bearer comparison (hmac.compare_digest) still accepts
    the correct token and rejects a wrong one, exercised directly against
    `_require_human_principal` (bypassing the app-level auth middleware so
    this isolates the function's own comparison logic)."""
    from fastapi import HTTPException

    from lionagi_studio.services import approvals as approvals_mod

    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")

    # Correct bearer: no exception.
    approvals_mod._require_human_principal(_fake_request({"authorization": "Bearer test-token"}))

    # Wrong bearer: 403.
    with pytest.raises(HTTPException) as exc_info:
        approvals_mod._require_human_principal(
            _fake_request({"authorization": "Bearer wrong-token"})
        )
    assert exc_info.value.status_code == 403

    # No header at all: 403 (fails closed).
    with pytest.raises(HTTPException) as exc_info:
        approvals_mod._require_human_principal(_fake_request({}))
    assert exc_info.value.status_code == 403


def test_grant_route_rejected_without_correct_bearer_at_http_level(tmp_path, monkeypatch):
    """End to end: the app-level auth middleware rejects a wrong/missing
    bearer before the route even runs, whenever a token is configured."""
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(f"/api/approvals/{approval_id}/grant", headers=_auth("wrong-token"))
    assert resp.status_code in (401, 403), resp.text

    resp_no_header = client.post(f"/api/approvals/{approval_id}/grant")
    assert resp_no_header.status_code in (401, 403), resp_no_header.text


def test_verify_route_returns_valid_true_for_intact_chain(tmp_path, monkeypatch):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
    )

    resp = client.get("/api/approvals/evidence/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["total_entries"] == 1
    assert body["errors"] == []


def test_lazy_expire_writes_expired_evidence_row_with_intact_chain(tmp_path, monkeypatch):
    """Reading a TTL-expired approval triggers _lazy_expire, which must append
    an 'expired' evidence row (same approval_id, chained from 'proposed') and
    keep the overall chain verifiable."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )

    async def _force_expired():
        async with open_db(str(db_path)) as db:
            await db.execute(
                "UPDATE approvals SET expires_at = ? WHERE id = ?",
                (time.time() - 1, row["id"]),
            )
            await db.commit()

    _run(_force_expired())

    refreshed = _run(approvals_mod.get_approval(row["id"]))
    assert refreshed["status"] == "expired"

    rows = _evidence_rows(db_path)
    expired_rows = [r for r in rows if r["event_type"] == "expired"]
    assert len(expired_rows) == 1
    assert expired_rows[0]["approval_id"] == row["id"]
    assert expired_rows[0]["status_from"] == "pending"
    assert expired_rows[0]["status_to"] == "expired"

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is True
    assert verdict["total_entries"] == 2  # proposed + expired
    assert verdict["errors"] == []


def test_double_expire_read_race_writes_only_one_evidence_row(tmp_path, monkeypatch):
    """Two reads of an expired approval (simulating a race between concurrent
    readers) must only append one 'expired' evidence row -- the CAS in
    `_lazy_expire` (`WHERE status = ?`) makes the second read a no-op."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    row = _run(
        approvals_mod.create_approval(action_kind="launch_playbook", params={"name": "demo"})
    )

    async def _force_expired():
        async with open_db(str(db_path)) as db:
            await db.execute(
                "UPDATE approvals SET expires_at = ? WHERE id = ?",
                (time.time() - 1, row["id"]),
            )
            await db.commit()

    _run(_force_expired())

    first = _run(approvals_mod.get_approval(row["id"]))
    second = _run(approvals_mod.get_approval(row["id"]))
    assert first["status"] == "expired"
    assert second["status"] == "expired"

    rows = _evidence_rows(db_path)
    expired_rows = [r for r in rows if r["event_type"] == "expired"]
    assert len(expired_rows) == 1

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is True
    assert verdict["errors"] == []


def test_fallback_ensure_table_creates_status_and_session_indexes(tmp_path):
    """The defensive `_ensure_table` fallback (used by direct module callers
    outside the studio lifespan, which never applies the full StateDB schema)
    must create the same partial indexes the canonical schema defines, so the
    ledger is not left un-indexed on the status/session read paths."""
    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    db_path = tmp_path / "bare.db"

    async def _indexes() -> set[str]:
        async with open_db(str(db_path)) as db:
            await approvals_mod._ensure_table(db)
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'approvals'"
            )
            return {r["name"] for r in await cur.fetchall()}

    names = _run(_indexes())
    assert "idx_approvals_status" in names
    assert "idx_approvals_session" in names


# Consuming over HTTP and the configurable lifetime


def _granted_over_http(client: TestClient, params: dict, **create) -> str:
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": params, **create},
        headers=_auth("test-token"),
    ).json()["id"]
    resp = client.post(f"/api/approvals/{approval_id}/grant", headers=_auth("test-token"))
    assert resp.status_code == 200, resp.text
    return approval_id


def test_consume_route_uses_a_granted_approval_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = _granted_over_http(client, {"name": "demo"})
    body = {"action_kind": "launch_playbook", "params": {"name": "demo"}}

    first = client.post(
        f"/api/approvals/{approval_id}/consume", json=body, headers=_auth("test-token")
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "consumed"
    assert first.json()["consumed_at"] is not None

    second = client.post(
        f"/api/approvals/{approval_id}/consume", json=body, headers=_auth("test-token")
    )
    assert second.status_code == 409, second.text

    rows = _evidence_rows(tmp_path / "state.db")
    assert [r["event_type"] for r in rows] == ["proposed", "granted", "consumed"]
    assert _run(_verify())["valid"] is True


async def _verify():
    from lionagi_studio.services import approvals as approvals_mod

    return await approvals_mod.verify_evidence_chain()


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"action_kind": "launch_playbook", "params": {"name": "other"}}, 422),
        ({"action_kind": "delete_playbook", "params": {"name": "demo"}}, 422),
    ],
    ids=["other-params", "other-action"],
)
def test_consume_route_refuses_a_different_action_and_leaves_it_granted(
    tmp_path, monkeypatch, body, status
):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = _granted_over_http(client, {"name": "demo"})

    resp = client.post(
        f"/api/approvals/{approval_id}/consume", json=body, headers=_auth("test-token")
    )
    assert resp.status_code == status, resp.text
    check = client.get(f"/api/approvals/{approval_id}", headers=_auth("test-token"))
    assert check.json()["status"] == "granted"


def test_consume_route_refuses_a_pending_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    ).json()["id"]

    resp = client.post(
        f"/api/approvals/{approval_id}/consume",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=_auth("test-token"),
    )
    assert resp.status_code == 409, resp.text
    check = client.get(f"/api/approvals/{approval_id}", headers=_auth("test-token"))
    assert check.json()["status"] == "pending"


def test_consume_route_404_for_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    resp = client.post(
        "/api/approvals/does-not-exist/consume",
        json={"action_kind": "launch_playbook", "params": {}},
        headers=_auth("test-token"),
    )
    assert resp.status_code == 404, resp.text


async def _grant_directly(approval_id: str):
    from lionagi_studio.services import approvals as approvals_mod

    return await approvals_mod.grant_approval(approval_id)


def test_consume_route_403_on_an_open_daemon_and_leaves_it_granted(tmp_path, monkeypatch):
    """With no token configured any local process reaches the daemon, so a granted
    approval is not spendable over HTTP there: consuming fails closed, as granting does."""
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    client = _make_client(tmp_path / "state.db", monkeypatch)
    body = {"action_kind": "launch_playbook", "params": {"name": "demo"}}
    approval_id = client.post("/api/approvals/", json=body).json()["id"]
    assert _run(_grant_directly(approval_id))["status"] == "granted"

    resp = client.post(f"/api/approvals/{approval_id}/consume", json=body)
    assert resp.status_code == 403, resp.text
    assert "LIONAGI_STUDIO_AUTH_TOKEN" in resp.json()["detail"]
    assert client.get(f"/api/approvals/{approval_id}").json()["status"] == "granted"


@pytest.mark.parametrize("headers", [{}, _auth("wrong-token")], ids=["no-bearer", "wrong-bearer"])
def test_consume_route_refuses_a_caller_without_the_bearer_and_leaves_it_granted(
    tmp_path, monkeypatch, headers
):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    approval_id = _granted_over_http(client, {"name": "demo"})

    resp = client.post(
        f"/api/approvals/{approval_id}/consume",
        json={"action_kind": "launch_playbook", "params": {"name": "demo"}},
        headers=headers,
    )
    assert resp.status_code in (401, 403), resp.text
    check = client.get(f"/api/approvals/{approval_id}", headers=_auth("test-token"))
    assert check.json()["status"] == "granted"


def test_the_consume_guard_refuses_without_the_bearer_whatever_the_middleware_did():
    """The route's own check, apart from the admission middleware in front of it."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from lionagi_studio.services import approvals as approvals_mod

    def request(authorization: str | None) -> Request:
        headers = [] if authorization is None else [(b"authorization", authorization.encode())]
        return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
        for presented in (None, "Bearer wrong-token", "test-token", "Bearer test-token "):
            with pytest.raises(HTTPException) as refused:
                approvals_mod._require_daemon_credential(request(presented))
            assert refused.value.status_code == 403
        approvals_mod._require_daemon_credential(request("Bearer test-token"))


def _lifetime(row: dict) -> float:
    return row["expires_at"] - row["proposed_at"]


def test_server_setting_changes_the_default_lifetime(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_APPROVAL_TTL_SECONDS", "900")
    monkeypatch.delenv("LIONAGI_STUDIO_APPROVAL_MAX_TTL_SECONDS", raising=False)
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi_studio.services import approvals as approvals_mod

    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params={}))
    assert _lifetime(row) == pytest.approx(900, abs=1)


def test_requested_lifetime_within_the_cap_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("LIONAGI_STUDIO_APPROVAL_TTL_SECONDS", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_APPROVAL_MAX_TTL_SECONDS", raising=False)
    client = _make_client(tmp_path / "state.db", monkeypatch)
    resp = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {}, "ttl_seconds": 1800},
    )
    assert resp.status_code == 200, resp.text
    assert _lifetime(resp.json()) == pytest.approx(1800, abs=1)


def test_requested_lifetime_over_the_cap_is_refused_not_shortened(tmp_path, monkeypatch):
    monkeypatch.delenv("LIONAGI_STUDIO_APPROVAL_TTL_SECONDS", raising=False)
    monkeypatch.setenv("LIONAGI_STUDIO_APPROVAL_MAX_TTL_SECONDS", "600")
    client = _make_client(tmp_path / "state.db", monkeypatch)
    resp = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {}, "ttl_seconds": 601},
    )
    assert resp.status_code == 422, resp.text
    assert "600" in resp.json()["detail"]
    assert _evidence_rows(tmp_path / "state.db") == []


@pytest.mark.parametrize("ttl", [0, -5, "60", 1.5, True])
def test_requested_lifetime_must_be_a_positive_whole_number(tmp_path, monkeypatch, ttl):
    client = _make_client(tmp_path / "state.db", monkeypatch)
    resp = client.post(
        "/api/approvals/",
        json={"action_kind": "launch_playbook", "params": {}, "ttl_seconds": ttl},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    ("default", "cap"),
    [("0", None), ("soon", None), ("900", "600"), (None, "-1")],
    ids=["zero-default", "word-default", "default-over-cap", "negative-cap"],
)
def test_misconfigured_lifetime_settings_propose_nothing(tmp_path, monkeypatch, default, cap):
    for name, value in (
        ("LIONAGI_STUDIO_APPROVAL_TTL_SECONDS", default),
        ("LIONAGI_STUDIO_APPROVAL_MAX_TTL_SECONDS", cap),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi_studio.services import approvals as approvals_mod

    with pytest.raises(HTTPException) as exc_info:
        _run(approvals_mod.create_approval(action_kind="launch_playbook", params={}))
    assert exc_info.value.status_code == 500
    assert _evidence_rows(db_path) == []


def test_default_longer_than_the_unset_cap_raises_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_APPROVAL_TTL_SECONDS", "7200")
    monkeypatch.delenv("LIONAGI_STUDIO_APPROVAL_MAX_TTL_SECONDS", raising=False)

    from lionagi_studio.services import approvals as approvals_mod

    assert approvals_mod.approval_ttl_bounds() == (7200, 7200)


# Who decided: the decision principal on grant and deny

_ORIGIN = "http://localhost:5173"
_PRINCIPAL_COLUMNS = (
    "decision_principal_id",
    "decision_epoch",
    "decision_method",
    "decision_origin",
    "decision_scope",
)
_OLD_PAYLOAD_KEYS = (
    "id",
    "sequence",
    "event_type",
    "approval_id",
    "action_kind",
    "status_from",
    "status_to",
    "params_hash",
    "justification_class",
    "justification_reason",
    "created_at",
)


def _decision_headers(capability: str | None, *, origin: str = _ORIGIN) -> dict[str, str]:
    headers = {"authorization": "Bearer test-token", "origin": origin}
    if capability is not None:
        headers["x-studio-decision-token"] = capability
    return headers


def _pending(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))
    from lionagi_studio.services import approvals as approvals_mod

    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params={"n": 1}))
    return row["id"]


@pytest.fixture
def capability():
    from lionhub.serve.daemon.launch_access import mint_decision_capability, reset_launch_access

    reset_launch_access()
    yield mint_decision_capability(_ORIGIN)
    reset_launch_access()


def test_grant_with_a_decision_capability_records_who_granted(tmp_path, monkeypatch, capability):
    from lionagi_studio.services import approvals as approvals_mod

    db_path = tmp_path / "state.db"
    approval_id = _pending(db_path, monkeypatch)

    granted = _run(
        approvals_mod.grant_approval_route(
            approval_id, _fake_request(_decision_headers(capability))
        )
    )

    assert granted["status"] == "granted"
    assert granted["decision_principal_id"]
    assert granted["decision_epoch"]
    assert granted["decision_method"] == "pairing"
    assert granted["decision_origin"] == _ORIGIN
    assert granted["decision_scope"] == "operator:decide"
    assert _run(approvals_mod.get_approval(approval_id)) == granted
    proposed, grant_row = _evidence_rows(db_path)
    assert all(proposed[name] is None for name in _PRINCIPAL_COLUMNS)
    assert {name: grant_row[name] for name in _PRINCIPAL_COLUMNS} == {
        name: granted[name] for name in _PRINCIPAL_COLUMNS
    }
    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is True, verdict


def test_deny_with_a_decision_capability_records_who_denied(tmp_path, monkeypatch, capability):
    from lionagi_studio.services import approvals as approvals_mod

    db_path = tmp_path / "state.db"
    approval_id = _pending(db_path, monkeypatch)

    denied = _run(
        approvals_mod.deny_approval_route(
            approval_id,
            _fake_request(_decision_headers(capability)),
            approvals_mod._DenyBody(reason_class="policy", reason="not this week"),
        )
    )

    assert denied["status"] == "denied"
    assert denied["decision_origin"] == _ORIGIN
    deny_row = _evidence_rows(db_path)[-1]
    assert deny_row["event_type"] == "denied"
    assert deny_row["justification_reason"] == "not this week"
    assert deny_row["decision_principal_id"] == denied["decision_principal_id"]
    assert _run(approvals_mod.verify_evidence_chain())["valid"] is True


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda cap: _decision_headers(None), id="no-capability"),
        pytest.param(
            lambda cap: _decision_headers(cap, origin="https://elsewhere.example"),
            id="capability-from-another-origin",
        ),
        pytest.param(lambda cap: _decision_headers("not-a-capability"), id="unknown-capability"),
    ],
)
def test_a_grant_without_a_verified_capability_is_accepted_and_unattributed(
    tmp_path, monkeypatch, capability, headers
):
    from lionagi_studio.services import approvals as approvals_mod

    db_path = tmp_path / "state.db"
    approval_id = _pending(db_path, monkeypatch)

    granted = _run(
        approvals_mod.grant_approval_route(approval_id, _fake_request(headers(capability)))
    )

    assert granted["status"] == "granted"
    assert all(granted[name] is None for name in _PRINCIPAL_COLUMNS)
    assert all(_evidence_rows(db_path)[-1][name] is None for name in _PRINCIPAL_COLUMNS)


def test_a_grant_presenting_more_than_one_origin_is_unattributed(tmp_path, monkeypatch, capability):
    """A capability is bound to one origin, so a request naming two, even the paired origin
    twice, is recorded unattributed rather than attributed to either."""
    from starlette.requests import Request as StarletteRequest

    from lionagi_studio.services import approvals as approvals_mod

    db_path = tmp_path / "state.db"
    approval_id = _pending(db_path, monkeypatch)
    headers = [(k.encode(), v.encode()) for k, v in _decision_headers(capability).items()]
    headers.append((b"origin", _ORIGIN.encode()))

    granted = _run(
        approvals_mod.grant_approval_route(
            approval_id, StarletteRequest({"type": "http", "headers": headers})
        )
    )

    assert granted["status"] == "granted"
    assert all(granted[name] is None for name in _PRINCIPAL_COLUMNS)
    assert all(_evidence_rows(db_path)[-1][name] is None for name in _PRINCIPAL_COLUMNS)


def test_an_unattributed_row_hashes_exactly_as_rows_did_before_principals(tmp_path, monkeypatch):
    """Chains written before the principal columns still verify: an unattributed row's
    content hash covers the same payload keys as it always did."""
    from lionagi_studio.services import approvals as approvals_mod

    db_path = tmp_path / "state.db"
    approval_id = _pending(db_path, monkeypatch)
    _run(approvals_mod.grant_approval(approval_id))

    for row in _evidence_rows(db_path):
        old_payload = {key: row[key] for key in _OLD_PAYLOAD_KEYS}
        assert row["content_hash"] == approvals_mod._compute_content_hash(old_payload)


@pytest.mark.parametrize(
    ("attributed", "tamper"),
    [
        pytest.param(True, "decision_principal_id = NULL", id="principal-cleared"),
        pytest.param(True, "decision_origin = 'https://evil.example'", id="origin-changed"),
        pytest.param(False, "decision_principal_id = 'someone'", id="principal-added"),
        pytest.param(False, "decision_scope = 'operator:decide'", id="scope-added"),
    ],
)
def test_changing_the_recorded_principal_breaks_the_chain(
    tmp_path, monkeypatch, capability, attributed, tamper
):
    from lionagi_studio.services import approvals as approvals_mod
    from lionhub.orchestration.state_store import open_db

    db_path = tmp_path / "state.db"
    approval_id = _pending(db_path, monkeypatch)
    _run(
        approvals_mod.grant_approval_route(
            approval_id, _fake_request(_decision_headers(capability if attributed else None))
        )
    )
    assert _run(approvals_mod.verify_evidence_chain())["valid"] is True

    async def _tamper():
        async with open_db(str(db_path)) as db:
            await db.execute(f"UPDATE approval_evidence SET {tamper} WHERE sequence = 2")
            await db.commit()

    _run(_tamper())
    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict["valid"] is False
    assert "sequence 2: content hash mismatch" in verdict["errors"]


_PRE_PRINCIPAL_TABLES = """
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, action_kind TEXT NOT NULL, params_hash TEXT NOT NULL,
  session_id TEXT, status TEXT NOT NULL DEFAULT 'pending', proposed_at REAL NOT NULL,
  granted_at REAL, consumed_at REAL, expires_at REAL NOT NULL
);
CREATE TABLE approval_evidence (
  id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
  approval_id TEXT NOT NULL, action_kind TEXT NOT NULL, status_from TEXT,
  status_to TEXT NOT NULL, params_hash TEXT NOT NULL, justification_class TEXT,
  justification_reason TEXT, created_at REAL NOT NULL, content_hash TEXT NOT NULL,
  previous_hash TEXT NOT NULL, chain_hash TEXT NOT NULL, hmac_sig TEXT
);
"""


def _columns(db_path: Path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _write_pre_principal_chain(db_path: Path) -> None:
    """A proposed-then-granted chain in the shape and hash rows had before principals."""
    import sqlite3

    from lionagi_studio.services import approvals as approvals_mod

    now = time.time()
    params_hash = approvals_mod.compute_params_hash({"old": True})
    previous = approvals_mod.GENESIS_HASH
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_PRE_PRINCIPAL_TABLES)
        conn.execute(
            "INSERT INTO approvals (id, action_kind, params_hash, status, proposed_at, "
            "granted_at, expires_at) VALUES ('old', 'launch_playbook', ?, 'granted', ?, ?, ?)",
            (params_hash, now, now, now + 300),
        )
        for sequence, (event, status_from, status_to) in enumerate(
            [("proposed", None, "pending"), ("granted", "pending", "granted")], start=1
        ):
            payload = {
                "id": f"old-{sequence}",
                "sequence": sequence,
                "event_type": event,
                "approval_id": "old",
                "action_kind": "launch_playbook",
                "status_from": status_from,
                "status_to": status_to,
                "params_hash": params_hash,
                "justification_class": None,
                "justification_reason": None,
                "created_at": now,
            }
            content = approvals_mod._compute_content_hash(payload)
            chain = approvals_mod._compute_chain_hash(content, previous)
            conn.execute(
                "INSERT INTO approval_evidence VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (*payload.values(), content, previous, chain),
            )
            previous = chain


def test_the_fallback_gives_a_pre_principal_table_the_columns(tmp_path, monkeypatch, capability):
    """A ledger created before the principal columns, opened only through this module,
    gains them as NULL; its existing chain still verifies, and the next decision's
    principal is recorded and chained onto it."""
    from lionagi_studio.services import approvals as approvals_mod

    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-token")
    db_path = tmp_path / "bare.db"
    _patch_db(monkeypatch, db_path)
    _write_pre_principal_chain(db_path)
    assert "decision_principal_id" not in _columns(db_path, "approvals")

    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict == {**verdict, "valid": True, "total_entries": 2}
    assert all(_run(approvals_mod.get_approval("old"))[name] is None for name in _PRINCIPAL_COLUMNS)

    row = _run(approvals_mod.create_approval(action_kind="launch_playbook", params={}))
    granted = _run(
        approvals_mod.grant_approval_route(row["id"], _fake_request(_decision_headers(capability)))
    )

    for table in ("approvals", "approval_evidence"):
        assert set(_PRINCIPAL_COLUMNS) <= _columns(db_path, table)
    assert granted["decision_origin"] == _ORIGIN
    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict == {**verdict, "valid": True, "total_entries": 4}


def test_a_state_db_open_keeps_a_pre_principal_chain_verifiable(tmp_path, monkeypatch):
    """The same pre-change chain, upgraded by a StateDB open rather than the fallback."""
    from lionagi_studio.services import approvals as approvals_mod

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _write_pre_principal_chain(db_path)

    _run(_init_db(db_path))

    for table in ("approvals", "approval_evidence"):
        assert set(_PRINCIPAL_COLUMNS) <= _columns(db_path, table)
    verdict = _run(approvals_mod.verify_evidence_chain())
    assert verdict == {**verdict, "valid": True, "total_entries": 2}


def test_a_state_db_open_adds_the_columns_to_an_existing_ledger(tmp_path, monkeypatch):
    """An existing store at the current schema version gains the columns on its next
    writable open, with its approval rows intact and NULL in the new columns."""
    import sqlite3

    from lionagi_studio.services import approvals as approvals_mod

    db_path = tmp_path / "state.db"
    approval_id = _pending(db_path, monkeypatch)
    with sqlite3.connect(db_path) as conn:
        for table in ("approvals", "approval_evidence"):
            for name in _PRINCIPAL_COLUMNS:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    assert "decision_scope" not in _columns(db_path, "approval_evidence")

    _run(_init_db(db_path))

    for table in ("approvals", "approval_evidence"):
        assert set(_PRINCIPAL_COLUMNS) <= _columns(db_path, table)
    row = _run(approvals_mod.get_approval(approval_id))
    assert row["status"] == "pending"
    assert all(row[name] is None for name in _PRINCIPAL_COLUMNS)
    assert _run(approvals_mod.verify_evidence_chain())["valid"] is True
