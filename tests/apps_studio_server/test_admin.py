"""Tests for #1014 admin doctor and prune endpoints."""

from __future__ import annotations

import asyncio
import time
import uuid
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


async def _seed_running_session(
    db_path: Path, session_id: str, artifacts_path: str | None = None
) -> None:
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": pid,
                "name": "test-session",
                "status": "running",
                "started_at": time.time(),
            }
        )
        if artifacts_path is not None:
            await db.db.execute(
                "UPDATE sessions SET artifacts_path = ? WHERE id = ?",
                (artifacts_path, session_id),
            )
            await db.db.commit()


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from lionagi.studio.app import app

    return TestClient(app)


def test_admin_doctor_reports_missing_artifacts_phantom(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    missing_dir = str(tmp_path / "nonexistent_artifacts")
    _run(_seed_running_session(db_path, sid, artifacts_path=missing_dir))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/admin/doctor")
    assert r.status_code == 200
    data = r.json()
    assert "phantom_sessions" in data
    assert "db_health" in data
    assert "diagnostic_run_at" in data
    assert data["db_health"]["size_bytes"] > 0

    phantoms = data["phantom_sessions"]
    assert len(phantoms) >= 1
    reasons = {p["reason"] for p in phantoms}
    assert "missing_artifacts" in reasons


def test_admin_doctor_no_db_returns_empty_health(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.db"
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/admin/doctor")
    assert r.status_code == 200
    data = r.json()
    assert data["phantom_sessions"] == []
    assert data["db_health"]["size_bytes"] == 0
    assert data["db_health"]["wal_bytes"] == 0
    assert data["db_health"]["wal_pending"] == 0


def test_admin_prune_selected_sessions(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    s1 = str(uuid.uuid4())
    s2 = str(uuid.uuid4())
    _run(_seed_running_session(db_path, s1))
    _run(_seed_running_session(db_path, s2))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post("/api/admin/prune", json={"session_ids": [s1]})
    assert r.status_code == 200
    assert r.json()["pruned"] == 1

    # Verify s1 gone, s2 remains via doctor
    r2 = client.get("/api/admin/doctor")
    remaining_ids = {p["session_id"] for p in r2.json()["phantom_sessions"]}
    assert s1 not in remaining_ids


def test_admin_prune_rejects_empty_body(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post("/api/admin/prune", json={})
    assert r.status_code == 422


# ─── ADR-0024: /api/admin/health + /api/admin/transition ─────────────────────


def test_admin_health_reports_status_and_health_buckets(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_seed_running_session(db_path, str(uuid.uuid4())))
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.get("/api/admin/health")
    assert r.status_code == 200
    body = r.json()
    assert "sessions" in body
    sess = body["sessions"]
    assert "by_status" in sess
    assert "by_health" in sess
    # Seeded one running session.
    assert sess["by_status"].get("running") == 1
    # All ADR-0024 health buckets sum to total.
    assert sum(sess["by_health"].values()) == sess["total"]


def test_admin_transition_marks_running_session_failed(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "failed",
            "reason": "manual cleanup after restart",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transitioned"] == [sid]
    assert body["skipped"] == []
    assert body["event_id"]  # admin_events row written

    # Verify DB state changed.
    async def _check():
        async with StateDB(db_path) as db:
            row = await db.get_session(sid)
            assert row["status"] == "failed"
            assert row["ended_at"] is not None
            events = await db.list_admin_events(action="transition")
            assert len(events) == 1
            assert events[0]["actor"] == "admin"

    _run(_check())


def test_admin_transition_rejects_invalid_target(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": ["any"],
            "target_status": "completed",  # not in admin-allowed set
            "reason": "test",
        },
    )
    assert r.status_code == 422


def test_admin_transition_skips_non_running(tmp_path, monkeypatch):
    """Already-terminal sessions are reported as skipped, not silently no-op."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    async def _terminal():
        async with StateDB(db_path) as db:
            await db.update_session(sid, status="completed")

    _run(_terminal())

    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "failed",
            "reason": "test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transitioned"] == []
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["session_id"] == sid


def test_admin_transition_requires_reason(tmp_path, monkeypatch):
    """Omitting both reason_code and reason returns 400 (not 422)."""
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": ["x"],
            "target_status": "failed",
        },
    )
    assert r.status_code == 400


def test_admin_transition_rejects_healthy_session(tmp_path, monkeypatch):
    """ADR-0024 health guard: fresh running session with recent activity → 422."""
    import lionagi.studio.services.admin as admin_mod

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    # Simulate a live process so the classifier returns HEALTHY
    # (idle_seconds ≈ 0, process alive → HEALTHY).
    monkeypatch.setattr(admin_mod, "_live_process_matches", lambda *_: True)

    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "failed",
            "reason": "cleanup attempt",
        },
    )
    assert r.status_code == 422
    assert "healthy" in r.json()["detail"].lower()


# ─── ADR-0024/FIX-2: health guard re-evaluated per session, not pre-computed ──


def test_admin_transition_guard_re_evaluates_health_per_call(tmp_path, monkeypatch):
    """Health guard reads current session state on each transition_sessions call.

    Bumping last_message_at between two calls changes the health classification;
    the guard must use the freshest DB state each time (not a pre-computed snapshot).
    This verifies the merged per-session classify+UPDATE loop correctly re-reads
    state, minimizing the TOCTOU window between health check and destructive write.
    """
    import lionagi.studio.services.admin as admin_mod

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    # Simulate a live process so health is driven by activity threshold.
    monkeypatch.setattr(admin_mod, "_live_process_matches", lambda *_: True)

    # Set last_message_at to ~2h ago and kind=agent (threshold=6h).
    # idle_seconds=2h > IDLE_THRESHOLD(1h) but < 6h → IDLE → refused.
    async def _set_idle():
        async with StateDB(db_path) as db:
            await db.db.execute(
                "UPDATE sessions SET last_message_at = ?, invocation_kind = ? WHERE id = ?",
                (time.time() - 7200, "agent", sid),
            )
            await db.db.commit()

    _run(_set_idle())

    client = _make_client(tmp_path, monkeypatch, db_path)

    # First call: IDLE → refused.
    r1 = client.post(
        "/api/admin/transition",
        json={"session_ids": [sid], "target_status": "failed", "reason": "cleanup"},
    )
    assert r1.status_code == 422
    assert "idle" in r1.json()["detail"].lower()

    # Bump last_message_at past the 6h agent threshold → UNRESPONSIVE → allowed.
    async def _bump_past_threshold():
        async with StateDB(db_path) as db:
            await db.db.execute(
                "UPDATE sessions SET last_message_at = ? WHERE id = ?",
                (time.time() - 7 * 3600, sid),
            )
            await db.db.commit()

    _run(_bump_past_threshold())

    # Second call: guard re-evaluates from current DB state → succeeds.
    r2 = client.post(
        "/api/admin/transition",
        json={"session_ids": [sid], "target_status": "failed", "reason": "cleanup"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["transitioned"] == [sid]
    assert body["skipped"] == []


# ─── ADR-0028: reason_code in TransitionBody ────────────────────────────────


def test_admin_transition_with_reason_code_succeeds(tmp_path, monkeypatch):
    """New-style clients can pass reason_code; classifier-HEALTHY path
    preserves the operator's code.

    Pin the classifier deterministically — without this, the seeded
    session looks orphaned (no live process, no artifacts) and the
    real test target (operator-code preservation) is masked. The
    classifier-override paths get their own tests below.
    """
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))
    # Pin classifier: no phantom cause, IDLE health → operator's code wins.
    import lionagi.state.health as health_mod
    import lionagi.studio.services.admin as admin_svc
    from lionagi.state.health import SessionHealth

    monkeypatch.setattr(admin_svc, "_classify_phantom", lambda *a, **kw: None)
    # Patch the source module — admin.transition_sessions() lazy-imports
    # classify_session_health, so patching admin_svc directly does not work.
    monkeypatch.setattr(health_mod, "classify_session_health", lambda *a, **kw: SessionHealth.IDLE)
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "failed",
            "reason_code": "run.failed.exception",
            "reason_summary": "Operator forced failure after alert.",
        },
    )
    # IDLE is one of the "healthy enough to refuse" classifications —
    # admin transition is refused on IDLE/HEALTHY per ADR-0024 §C.
    # Pin to STALE instead so we get a real classifier-override case.
    monkeypatch.setattr(health_mod, "classify_session_health", lambda *a, **kw: SessionHealth.STALE)
    # Re-issue against the same (running) session — the previous call
    # was rejected with 4xx because health was IDLE.
    if r.status_code != 200:
        # Re-seed if the prior call somehow transitioned.
        async def _ensure_running():
            async with StateDB(db_path) as db:
                row = await db.get_session(sid)
                if row and row["status"] != "running":
                    await db.db.execute("UPDATE sessions SET status='running' WHERE id=?", (sid,))
                    await db.db.commit()

        _run(_ensure_running())
        r = client.post(
            "/api/admin/transition",
            json={
                "session_ids": [sid],
                "target_status": "failed",
                "reason_code": "run.failed.exception",
                "reason_summary": "Operator forced failure after alert.",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transitioned"] == [sid]
    assert body["skipped"] == []

    # STALE without phantom_reason → classifier writes HEALTH_STALE_NO_HEARTBEAT.
    async def _check():
        async with StateDB(db_path) as db:
            row = await db.get_session(sid)
            assert row["status"] == "failed"
            assert row["status_reason_code"] == "session.stale.no_heartbeat"
            assert row["status_reason_summary"] == "Operator forced failure after alert."
            cur = await db.db.execute(
                "SELECT reason_code, previous_status, status, evidence_refs "
                "FROM status_transitions WHERE entity_id = ?",
                (sid,),
            )
            rows = await cur.fetchall()
            assert len(rows) == 1
            assert rows[0]["reason_code"] == "session.stale.no_heartbeat"
            assert rows[0]["previous_status"] == "running"
            assert rows[0]["status"] == "failed"
            # Evidence ref must include the classifier source.
            import json as _json

            refs = _json.loads(rows[0]["evidence_refs"] or "[]")
            assert any(r.get("kind") == "session_health" for r in refs)

    _run(_check())


@pytest.mark.parametrize(
    "phantom_reason, expected_code, expected_evidence_kind",
    [
        ("process_dead", "session.phantom.process_dead", "phantom_classification"),
        (
            "missing_artifacts",
            "session.phantom.missing_artifacts",
            "phantom_classification",
        ),
        ("stale_lock", "session.zombie.stale_locks", "phantom_classification"),
    ],
)
def test_admin_transition_phantom_classifier_override(
    tmp_path, monkeypatch, phantom_reason, expected_code, expected_evidence_kind
):
    """Each PhantomReason value maps to its specific reason code, and the
    classifier override wins over the operator's chosen code.

    Pin the classifier so the assertion is deterministic — without
    monkeypatching the test would race against the actual fs/ps state
    of the seeded session.
    """
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))
    import lionagi.state.health as health_mod
    import lionagi.studio.services.admin as admin_svc
    from lionagi.state.health import SessionHealth

    monkeypatch.setattr(admin_svc, "_classify_phantom", lambda *a, **kw: phantom_reason)
    # Force a non-HEALTHY/IDLE so the admin transition gate passes.
    monkeypatch.setattr(health_mod, "classify_session_health", lambda *a, **kw: SessionHealth.STALE)
    client = _make_client(tmp_path, monkeypatch, db_path)

    # Operator passes a generic code; classifier should override.
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "failed",
            "reason_code": "run.failed.exception",
            "reason_summary": "operator picked something generic",
        },
    )
    assert r.status_code == 200, r.text

    async def _check():
        async with StateDB(db_path) as db:
            row = await db.get_session(sid)
            assert row["status_reason_code"] == expected_code, (
                f"classifier override didn't win: got {row['status_reason_code']!r}, "
                f"expected {expected_code!r}"
            )
            import json as _json

            cur = await db.db.execute(
                "SELECT evidence_refs FROM status_transitions WHERE entity_id = ?",
                (sid,),
            )
            row_t = await cur.fetchone()
            refs = _json.loads(row_t["evidence_refs"] or "[]")
            assert any(r.get("kind") == expected_evidence_kind for r in refs), (
                f"evidence missing {expected_evidence_kind} kind: {refs}"
            )

    _run(_check())


def test_admin_transition_invalid_reason_code_returns_400(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": ["any"],
            "target_status": "failed",
            "reason_code": "not.a.real.code",
        },
    )
    assert r.status_code == 400
    assert "reason_code" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_admin_transition_skips_aborted_terminal_session(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    async def _make_aborted():
        async with StateDB(db_path) as db:
            await db.update_session(sid, status="aborted")

    _run(_make_aborted())
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post(
        "/api/admin/transition",
        json={"session_ids": [sid], "target_status": "failed", "reason": "cleanup"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transitioned"] == []
    assert len(body["skipped"]) == 1
    assert "aborted" in body["skipped"][0]["reason"]


def test_admin_transition_skips_timed_out_terminal_session(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    async def _make_timed_out():
        async with StateDB(db_path) as db:
            await db.update_session(sid, status="timed_out")

    _run(_make_timed_out())
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post(
        "/api/admin/transition",
        json={"session_ids": [sid], "target_status": "failed", "reason": "cleanup"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transitioned"] == []
    assert len(body["skipped"]) == 1
    assert "timed_out" in body["skipped"][0]["reason"]


def test_find_pid_file_non_numeric_content_returns_none(tmp_path):
    from lionagi.studio.services.admin import _find_pid_file

    pid_file = tmp_path / "session.pid"
    pid_file.write_text("not-a-number")
    result = _find_pid_file(tmp_path)
    assert result is None


def test_find_pid_file_empty_content_returns_none(tmp_path):
    from lionagi.studio.services.admin import _find_pid_file

    pid_file = tmp_path / "session.pid"
    pid_file.write_text("")
    result = _find_pid_file(tmp_path)
    assert result is None


def test_find_pid_file_glob_pattern_non_numeric_returns_none(tmp_path):
    from lionagi.studio.services.admin import _find_pid_file

    pid_file = tmp_path / "custom.pid"
    pid_file.write_text("  garbage  ")
    result = _find_pid_file(tmp_path)
    assert result is None


def test_admin_prune_large_session_list(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    session_ids = [str(uuid.uuid4()) for _ in range(50)]
    for sid in session_ids:
        _run(_seed_running_session(db_path, sid))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post("/api/admin/prune", json={"session_ids": session_ids})
    assert r.status_code == 200
    assert r.json()["pruned"] == 50


def test_db_health_no_wal_file(tmp_path, monkeypatch):
    import lionagi.studio.services.admin as admin_mod

    fake_db = tmp_path / "test.db"
    fake_db.write_bytes(b"x" * 4096)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", fake_db)

    result = admin_mod.db_health()
    assert result["size_bytes"] == 4096
    assert result["wal_bytes"] == 0
    assert result["wal_pending"] == 0


def test_admin_transition_legacy_reason_backwards_compat(tmp_path, monkeypatch):
    """Old clients that send only 'reason' (no reason_code) still succeed.

    The router synthesises a reason_code from target_status and uses the
    free-text 'reason' as reason_summary; classifier override applies as
    usual (here pinned to STALE for determinism).
    """
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))
    import lionagi.state.health as health_mod
    import lionagi.studio.services.admin as admin_svc
    from lionagi.state.health import SessionHealth

    monkeypatch.setattr(admin_svc, "_classify_phantom", lambda *a, **kw: None)
    monkeypatch.setattr(health_mod, "classify_session_health", lambda *a, **kw: SessionHealth.STALE)
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "aborted",
            "reason": "Legacy client cleanup",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transitioned"] == [sid]

    # Verify the legacy compat path: 'reason' (no reason_code) maps via
    # _LEGACY_ADMIN_REASON_CODES['aborted'] → run.aborted.user, and the
    # free-text 'reason' becomes reason_summary. The classifier is
    # pinned to STALE here so we get the override on top.
    async def _check():
        async with StateDB(db_path) as db:
            row = await db.get_session(sid)
            assert row["status"] == "aborted"
            # STALE → session.stale.no_heartbeat (classifier override).
            assert row["status_reason_code"] == "session.stale.no_heartbeat"
            assert row["status_reason_summary"] == "Legacy client cleanup"

    _run(_check())
