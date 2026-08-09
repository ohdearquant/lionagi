"""Tests for admin doctor and prune endpoints."""

from __future__ import annotations

import asyncio
import os
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
    db_path: Path,
    session_id: str,
    artifacts_path: str | None = None,
    updated_at: float | None = None,
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
            await db.execute(
                "UPDATE sessions SET artifacts_path = ? WHERE id = ?",
                (artifacts_path, session_id),
            )
        if updated_at is not None:
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (updated_at, session_id),
            )


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_admin_doctor_reports_missing_artifacts_phantom(tmp_path, monkeypatch):
    """Missing artifacts only counts once the session has also gone stale."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    missing_dir = str(tmp_path / "nonexistent_artifacts")
    stale_time = time.time() - 7200  # past doctor's default 1h staleness gate
    _run(_seed_running_session(db_path, sid, artifacts_path=missing_dir, updated_at=stale_time))
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


def test_db_health_reports_only_numbers_it_can_actually_measure(tmp_path, monkeypatch):
    """The payload carries no field that merely duplicates another.

    ``wal_pending`` used to be returned as a second copy of ``wal_bytes``. A
    field named for pending WAL frames invites exactly one inference, that
    frames are waiting, and the WAL file's size cannot support it: SQLite
    leaves a checkpointed WAL at its allocated size, so a fully checkpointed
    store reported a large pending count forever, in the alarming direction.

    It is dropped rather than populated because the only source for the number
    is ``PRAGMA wal_checkpoint``, which does not read the pending count, it
    performs a checkpoint. Populating the field would turn a health read into a
    writer against the store it reports on.

    Pinning the whole key set, not just the absence of that one name, is
    deliberate: the defect was a duplicate, and the next duplicate will have a
    different name.

    ``size_alert`` and ``size_threshold_bytes`` were added later and are named
    here on purpose. The threshold is configuration, not a measurement, and it
    is not derivable from anything else in the payload, so without it a reader
    cannot say whether the store is over the limit at all. ``size_alert`` is
    arithmetically derivable once the threshold is present, which is what makes
    it worth stating anyway: the comparison is a policy the producer owns, and a
    reader that re-implements it can drift from the server's own predicate
    silently, in whichever direction the mistake runs. Both come from the same
    helper ``/api/stats`` uses, so the two surfaces agree by construction rather
    than by two consumers happening to compute the same thing.

    The rule this test enforces is unchanged: no field that merely restates
    another. A derived field earns its place only by carrying a decision, and
    the reason has to be written down here.
    """
    from lionagi.studio.services.admin import db_health

    health = db_health()

    assert set(health) == {
        "size_bytes",
        "wal_bytes",
        "size_alert",
        "size_threshold_bytes",
    }, f"db_health grew a field; if it reports a real measurement, say so here: {health}"


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


# ─── _classify_phantom liveness/staleness gate (khive#1793) ──────────────────


def test_fresh_running_session_missing_artifacts_not_reaped(tmp_path):
    """A fresh running session whose artifacts dir doesn't exist yet is not a phantom."""
    import lionagi.studio.services.admin as admin_svc

    now = time.time()
    missing = str(tmp_path / "not_yet_written")
    row = {"id": str(uuid.uuid4()), "updated_at": now, "artifacts_path": missing}

    reason = admin_svc._classify_phantom(row, now=now, stale_seconds=3600, ps_snapshot="")
    assert reason is None


def test_stale_dead_session_missing_artifacts_still_reaped(tmp_path):
    """Cleanup is preserved: a stale, not-live session with missing artifacts still reaps."""
    import lionagi.studio.services.admin as admin_svc

    now = time.time()
    missing = str(tmp_path / "ghost")
    row = {"id": str(uuid.uuid4()), "updated_at": now - 7200, "artifacts_path": missing}

    reason = admin_svc._classify_phantom(row, now=now, stale_seconds=3600, ps_snapshot="")
    assert reason == "missing_artifacts"


def test_alive_session_never_reaped(tmp_path):
    """Liveness wins over both staleness and missing artifacts."""
    import lionagi.studio.services.admin as admin_svc

    now = time.time()
    missing = str(tmp_path / "ghost2")
    sid = str(uuid.uuid4())
    row = {"id": sid, "updated_at": now - 7200, "artifacts_path": missing}

    # session_id present in the ps snapshot signals a live process match.
    reason = admin_svc._classify_phantom(row, now=now, stale_seconds=3600, ps_snapshot=sid)
    assert reason is None


def test_stale_session_live_recorded_pid_not_reaped(tmp_path):
    """A recorded node_metadata pid that is live wins even with an empty ps snapshot."""
    import lionagi.studio.services.admin as admin_svc

    now = time.time()
    missing = str(tmp_path / "ghost3")
    row = {
        "id": str(uuid.uuid4()),
        "updated_at": now - 7200,
        "artifacts_path": missing,
        "node_metadata": {"pid": os.getpid()},
    }

    reason = admin_svc._classify_phantom(row, now=now, stale_seconds=3600, ps_snapshot="")
    assert reason is None


def test_stale_lock_gated_on_staleness(tmp_path):
    """A stale lock file only counts as zombie evidence once the session itself is stale.

    The lock is named ``job.lock`` because that is a name lionagi actually
    writes. This test used to use ``session.lock``, which nothing creates, so it
    passed on the strength of the suffix alone and would have kept passing for
    any file at all ending in ``.lock``.
    """
    import lionagi.studio.services.admin as admin_svc

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    lock = artifacts_dir / "job.lock"
    lock.write_text("x")
    old_mtime = time.time() - 7200
    os.utime(lock, (old_mtime, old_mtime))

    now = time.time()
    fresh_row = {
        "id": str(uuid.uuid4()),
        "updated_at": now,
        "artifacts_path": str(artifacts_dir),
    }
    assert (
        admin_svc._classify_phantom(fresh_row, now=now, stale_seconds=3600, ps_snapshot="") is None
    )

    stale_row = {
        "id": str(uuid.uuid4()),
        "updated_at": now - 7200,
        "artifacts_path": str(artifacts_dir),
    }
    assert (
        admin_svc._classify_phantom(stale_row, now=now, stale_seconds=3600, ps_snapshot="")
        == "stale_lock"
    )


def _aged(path, seconds_ago: float) -> None:
    path.write_text("x")
    when = time.time() - seconds_ago
    os.utime(path, (when, when))


def test_dependency_lockfile_is_not_evidence_of_a_dead_process(tmp_path):
    """A stale ``uv.lock`` alone must classify as nothing.

    This is the arm that separates the two rules, and it is the only one that
    does. Under a ``**/*.lock`` suffix match this row is ``stale_lock``; under a
    match on the names lionagi writes it is ``None``. Every other lock test here
    scores the same under both rules, so none of them can catch a regression to
    the suffix match.

    It is not hypothetical. A run's ``artifacts_path`` is routinely a repository
    root and the search is recursive, so one checked-in ``uv.lock`` classified
    every completed session in that repository as a zombie.
    """
    import lionagi.studio.services.admin as admin_svc

    artifacts_dir = tmp_path / "repo"
    artifacts_dir.mkdir()
    _aged(artifacts_dir / "uv.lock", 7200)

    now = time.time()
    stale_row = {
        "id": str(uuid.uuid4()),
        "updated_at": now - 7200,
        "artifacts_path": str(artifacts_dir),
    }
    assert (
        admin_svc._classify_phantom(stale_row, now=now, stale_seconds=3600, ps_snapshot="")
        != "stale_lock"
    )


def test_dependency_lockfile_does_not_mask_a_real_runtime_lock(tmp_path):
    """A ``uv.lock`` sitting beside a genuine stale ``job.lock`` still classifies.

    Without this, a fix could pass the arm above by giving up whenever a
    dependency lockfile is present, which would suppress the true positives the
    evidence exists to find.
    """
    import lionagi.studio.services.admin as admin_svc

    artifacts_dir = tmp_path / "repo"
    artifacts_dir.mkdir()
    _aged(artifacts_dir / "uv.lock", 7200)
    _aged(artifacts_dir / "job.lock", 7200)

    now = time.time()
    stale_row = {
        "id": str(uuid.uuid4()),
        "updated_at": now - 7200,
        "artifacts_path": str(artifacts_dir),
    }
    assert (
        admin_svc._classify_phantom(stale_row, now=now, stale_seconds=3600, ps_snapshot="")
        == "stale_lock"
    )


def test_finalize_lock_is_also_runtime_evidence(tmp_path):
    """``finalize.lock`` is the other name lionagi writes under a run tree.

    Named explicitly so a fix that hardcodes only ``job.lock`` fails here rather
    than silently narrowing the evidence to one of the two real locks.
    """
    import lionagi.studio.services.admin as admin_svc

    artifacts_dir = tmp_path / "repo"
    artifacts_dir.mkdir()
    nested = artifacts_dir / "run-1"
    nested.mkdir()
    _aged(nested / "finalize.lock", 7200)

    now = time.time()
    stale_row = {
        "id": str(uuid.uuid4()),
        "updated_at": now - 7200,
        "artifacts_path": str(artifacts_dir),
    }
    assert (
        admin_svc._classify_phantom(stale_row, now=now, stale_seconds=3600, ps_snapshot="")
        == "stale_lock"
    )


def test_stale_session_empty_but_existing_artifact_root_not_missing_artifacts(tmp_path):
    """Once allocate_run creates the artifact directory up front, a stale/not-live
    session whose artifacts dir exists (even empty, e.g. a bare chat run with no
    artifact_contract) must never classify as missing_artifacts -- only a
    genuinely absent directory counts as that evidence. It still reaps
    (process_dead), preserving cleanup for true positives."""
    import lionagi.studio.services.admin as admin_svc

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()  # exists, but empty -- no lock file, no output written

    now = time.time()
    row = {
        "id": str(uuid.uuid4()),
        "updated_at": now - 7200,
        "artifacts_path": str(artifacts_dir),
    }

    reason = admin_svc._classify_phantom(row, now=now, stale_seconds=3600, ps_snapshot="")
    assert reason != "missing_artifacts"
    assert reason == "process_dead"


# ─── /api/admin/health + /api/admin/transition ───────────────────────────────


def test_admin_health_reports_status_and_health_buckets(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    # A fresh artifacts dir keeps this session out of the ORPHANED bucket
    # (no artifacts + no messages) so it classifies HEALTHY and by_status
    # still reads "running" for it.
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _run(
        _seed_running_session(
            db_path, str(uuid.uuid4()), artifacts_path=str(artifacts_dir), updated_at=time.time()
        )
    )
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
    # All health buckets sum to total.
    assert sum(sess["by_health"].values()) == sess["total"]
    # by_status must stay liveness-aware: total count is preserved either way.
    assert sum(sess["by_status"].values()) == sess["total"]


def test_admin_health_running_bucket_excludes_confirmed_dead_running_session(tmp_path, monkeypatch):
    """A "running" DB row whose process is confirmed dead must not inflate
    by_status.running — that bucket should reflect liveness, not the raw
    (possibly stale) status column."""
    db_path = tmp_path / "state.db"
    _run(_seed_running_session(db_path, str(uuid.uuid4())))

    import lionagi.studio.services.admin as admin_mod

    monkeypatch.setattr(admin_mod, "process_liveness", lambda *a, **k: False)

    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.get("/api/admin/health")
    assert r.status_code == 200
    sess = r.json()["sessions"]

    assert sess["by_status"].get("running", 0) == 0
    # No artifacts + no messages recorded for this seed -> ORPHANED, not STALE.
    assert sess["by_status"].get("orphaned", 0) == 1
    assert sess["by_health"].get("orphaned", 0) == 1
    assert sum(sess["by_status"].values()) == sess["total"]


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
    """Admin operators cannot mark sessions completed or timed_out."""
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
    """Already-terminal sessions are reported as skipped, not silently no-op'd."""
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
    """Omitting both reason_code and reason returns 400, not 422."""
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
    """Health guard: fresh running session with recent activity must return 422."""
    import lionagi.studio.services.admin as admin_mod

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    # Simulate a live process so the classifier returns HEALTHY
    # (idle_seconds ≈ 0, process alive → HEALTHY).
    monkeypatch.setattr(admin_mod, "process_liveness", lambda *a, **k: True)

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


# ─── health guard re-evaluated per session, not pre-computed ─────────────────


def test_admin_transition_guard_re_evaluates_health_per_call(tmp_path, monkeypatch):
    """Health guard reads current DB state on each call, not a pre-computed snapshot."""
    import lionagi.studio.services.admin as admin_mod

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    # Simulate a live process so health is driven by activity threshold.
    monkeypatch.setattr(admin_mod, "process_liveness", lambda *a, **k: True)

    # Set last_message_at to ~2h ago and kind=agent (threshold=6h).
    # idle_seconds=2h > IDLE_THRESHOLD(1h) but < 6h → IDLE → refused.
    async def _set_idle():
        async with StateDB(db_path) as db:
            await db.execute(
                "UPDATE sessions SET last_message_at = ?, invocation_kind = ? WHERE id = ?",
                (time.time() - 7200, "agent", sid),
            )

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
            await db.execute(
                "UPDATE sessions SET last_message_at = ? WHERE id = ?",
                (time.time() - 7 * 3600, sid),
            )

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


# ─── reason_code in TransitionBody ───────────────────────────────────────────


def test_admin_transition_with_reason_code_succeeds(tmp_path, monkeypatch):
    """New-style clients can pass reason_code; classifier pins are deterministic."""
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
    # admin transition is refused on IDLE/HEALTHY.
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
                    await db.execute("UPDATE sessions SET status='running' WHERE id=?", (sid,))

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
            rows = await db.fetch_all(
                "SELECT reason_code, previous_status, status, evidence_refs "
                "FROM status_transitions "
                "WHERE entity_id = ? AND previous_status = 'running' AND status = 'failed'",
                (sid,),
            )
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
    """Each PhantomReason maps to its reason code and the classifier override wins."""
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

            row_t = await db.fetch_one(
                "SELECT evidence_refs FROM status_transitions "
                "WHERE entity_id = ? AND previous_status = 'running' AND status = 'failed'",
                (sid,),
            )
            refs = _json.loads(row_t["evidence_refs"] or "[]")
            assert any(r.get("kind") == expected_evidence_kind for r in refs), (
                f"evidence missing {expected_evidence_kind} kind: {refs}"
            )

    _run(_check())


def test_admin_transition_invalid_reason_code_returns_400(tmp_path, monkeypatch):
    """An unrecognised reason_code returns 400 before touching the DB."""
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


def test_admin_transition_legacy_reason_backwards_compat(tmp_path, monkeypatch):
    """Old clients that send only 'reason' (no reason_code) still succeed."""
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


def test_admin_health_names_a_session_the_way_every_other_surface_does(tmp_path, monkeypatch):
    """The health report must resolve a session's display name, not print the
    stored column.

    The `name` column can hold a raw prompt body. Every other surface reads it
    through resolve_display_name, which ranks the play, playbook and agent-role
    labels above it, so the API and the UI showed a clean label while this
    endpoint published the prompt for the very same session.
    """
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    prompt_as_name = (
        "|- You are a scheduled DRAFT-ONLY worker for the re-enrichment "
        "campaign. Do not write to the graph."
    )

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": pid,
                    "name": prompt_as_name,
                    "status": "running",
                    "started_at": time.time(),
                }
            )
            await db.execute(
                "UPDATE sessions SET agent_name = ? WHERE id = ?", ("claude-code", sid)
            )

    _run(_seed())

    import lionagi.studio.services.admin as admin_mod

    # No artifacts and no messages classify this ORPHANED, which is what puts
    # it in the `unhealthy` list this test reads.
    monkeypatch.setattr(admin_mod, "process_liveness", lambda *a, **k: False)

    client = _make_client(tmp_path, monkeypatch, db_path)
    body = client.get("/api/admin/health").json()
    rows = body["sessions"]["unhealthy"]
    assert rows, "the seeded session never reached the unhealthy list"
    row = next(r for r in rows if r["session_id"] == sid)

    assert not row["name"].startswith("|-"), "the raw prompt body is being published"
    assert "DRAFT-ONLY" not in row["name"]
    assert row["name"].startswith("claude-code"), row["name"]


def test_admin_health_prefers_the_play_name_like_the_session_list_does(tmp_path, monkeypatch):
    """The report must select every column the resolver ranks above the one it
    already selects.

    show_play_name sits ABOVE playbook_name in the display-name chain, so
    omitting it from the report's own SELECT does not degrade gracefully: the
    resolver reads None and answers with the tier below. The session list
    selects it, so a play session was named by its play there and by its
    playbook here -- the same two-names-for-one-session defect this endpoint
    was fixed for, one layer down. Both names are clean labels, which is why
    only a cross-surface comparison catches it.
    """
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": pid,
                    "name": "some raw prompt body",
                    "status": "running",
                    "started_at": time.time(),
                }
            )
            # Both tiers populated, and they differ. A row carrying only one
            # of them cannot tell the two orderings apart.
            await db.execute(
                "UPDATE sessions SET show_play_name = ?, playbook_name = ? WHERE id = ?",
                ("nightly-enrichment", "oss-feature", sid),
            )

    _run(_seed())

    import lionagi.studio.services.admin as admin_mod

    monkeypatch.setattr(admin_mod, "process_liveness", lambda *a, **k: False)

    client = _make_client(tmp_path, monkeypatch, db_path)
    rows = client.get("/api/admin/health").json()["sessions"]["unhealthy"]
    assert rows, "the seeded session never reached the unhealthy list"
    health_name = next(r for r in rows if r["session_id"] == sid)["name"]

    listed = client.get("/api/sessions").json()
    entries = listed["sessions"] if isinstance(listed, dict) else listed
    list_name = next(s for s in entries if s["id"] == sid)["name"]

    assert health_name == list_name, (
        f"health report says {health_name!r}, session list says {list_name!r}"
    )
    assert health_name == "nightly-enrichment"


def test_a_lock_inside_a_dependency_tree_is_not_searched(tmp_path):
    """A runtime lock buried in ``node_modules`` must not be found, and the same
    lock outside it must be.

    Both halves are the test. Only the pair separates "the walk skips dependency
    trees" from "the walk found nothing here anyway", and it is the second half
    that fails if the skip list is ever widened until it excludes real ground.

    The skip list is what makes matching by name affordable. Matching by suffix
    was fast for the wrong reason: a repository root nearly always has a
    dependency lockfile near the top, so the search hit one at once and stopped.
    Searching for the names we write means the usual answer is "not here", and
    reaching it costs a full traversal -- measured at 100 seconds for one
    projects directory, against 3 milliseconds for the suffix match. Nothing
    lionagi writes puts a run directory inside ``node_modules``, so the subtree
    is cost with no evidence in it.
    """
    import lionagi.studio.services.admin as admin_svc

    buried = tmp_path / "repo" / "node_modules" / "pkg"
    buried.mkdir(parents=True)
    _aged(buried / "job.lock", 7200)

    now = time.time()
    assert (
        admin_svc._find_stale_lock(tmp_path / "repo", cutoff=now - 3600) is None
    ), "a lock inside node_modules was searched; the subtree should be skipped"

    reachable = tmp_path / "repo" / "run-1"
    reachable.mkdir()
    _aged(reachable / "job.lock", 7200)
    found = admin_svc._find_stale_lock(tmp_path / "repo", cutoff=now - 3600)
    assert found is not None and found.name == "job.lock", (
        "the skip list swallowed a lock outside any skipped directory"
    )


def test_one_scan_answers_once_per_artifact_root(tmp_path):
    """Within a single scan, a root already answered is not walked again.

    Proven by changing the filesystem between the two calls: a cache that is
    genuinely consulted keeps returning the first answer, while a fresh cache
    sees the new lock. Asserting only that two calls agree would pass whether or
    not the cache is read, since without it both walks reach the same tree.

    This is the difference that matters at scale rather than a micro-optimisation.
    Sessions repeat their artifact roots heavily -- one root accounted for 152 of
    500 recent sessions on one machine -- and uncached, that root is re-walked
    152 times in one pass to produce one answer.
    """
    import lionagi.studio.services.admin as admin_svc

    root = tmp_path / "repo"
    (root / "run-1").mkdir(parents=True)
    now = time.time()
    cutoff = now - 3600

    cache: dict[tuple[str, float], Path | None] = {}
    assert admin_svc._find_stale_lock(root, cutoff=cutoff, cache=cache) is None
    assert len(cache) == 1, "the answer was not recorded, so the next call re-walks"

    _aged(root / "run-1" / "job.lock", 7200)

    assert admin_svc._find_stale_lock(root, cutoff=cutoff, cache=cache) is None, (
        "the cache was not consulted: the tree was walked a second time"
    )
    assert (
        admin_svc._find_stale_lock(root, cutoff=cutoff, cache={}) is not None
    ), "a fresh cache must see the lock, or the first assertion proves nothing"
    assert (
        admin_svc._find_stale_lock(root, cutoff=cutoff) is not None
    ), "omitting the cache must always walk"
