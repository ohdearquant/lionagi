"""Scale contracts for run-list process-liveness fallback (issue #3108)."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import psutil
import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")


def _running_row(*, session_id: str, node_metadata: dict[str, Any] | None) -> dict[str, Any]:
    now = time.time()
    return {
        "id": session_id,
        "name": "process snapshot contract",
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "last_message_at": now,
        "node_metadata": node_metadata,
    }


def _isolate_snapshot_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep module-global cache state from coupling otherwise independent tests."""
    import lionagi.studio.services.admin as admin_svc

    monkeypatch.setattr(admin_svc, "_PS_SNAPSHOT_CACHE", None, raising=False)
    monkeypatch.setattr(admin_svc, "_PS_SNAPSHOT_INFLIGHT", None, raising=False)
    monkeypatch.setattr(admin_svc, "_PS_SNAPSHOT_METRICS", None, raising=False)


async def _stub_run_dependencies(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
) -> None:
    import lionagi.studio.services.run_tags as run_tags
    import lionagi.studio.services.runs as runs_svc

    async def list_sessions(**_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    async def tags_for_sessions(_ids: list[str]) -> dict[str, list[str]]:
        return {}

    monkeypatch.setattr(runs_svc._sessions_svc, "list_sessions", list_sessions)
    monkeypatch.setattr(run_tags, "tags_for_sessions", tags_for_sessions)


def test_identity_complete_run_page_does_not_capture_process_table(monkeypatch):
    """Targeted PID identity must be evaluated before the host-wide fallback."""
    import lionagi.studio.services.admin as admin_svc
    import lionagi.studio.services.runs as runs_svc

    _isolate_snapshot_cache(monkeypatch)
    create_time = psutil.Process(os.getpid()).create_time()
    rows = [
        _running_row(
            session_id=f"identity-{idx}",
            node_metadata={"pid": os.getpid(), "pid_create_time": create_time},
        )
        for idx in range(20)
    ]

    def forbidden_capture() -> str:
        raise AssertionError("identity-complete pages must not enumerate every OS process")

    monkeypatch.setattr(admin_svc, "_ps_snapshot", forbidden_capture)

    async def exercise() -> list[dict[str, Any]]:
        await _stub_run_dependencies(monkeypatch, rows)
        return await runs_svc.list_runs(limit=20)

    result = asyncio.run(exercise())
    assert len(result) == 20


def test_concurrent_legacy_run_pages_share_one_process_capture(monkeypatch):
    """Concurrent viewers inside the TTL share one off-loop fallback capture."""
    import lionagi.studio.services.admin as admin_svc
    import lionagi.studio.services.runs as runs_svc

    _isolate_snapshot_cache(monkeypatch)
    session_id = "legacy-session-without-pid"
    rows = [_running_row(session_id=session_id, node_metadata=None)]
    captures = 0

    def slow_capture() -> str:
        nonlocal captures
        captures += 1
        time.sleep(0.08)
        return f"1234 li agent --resume {session_id}"

    monkeypatch.setattr(admin_svc, "_ps_snapshot", slow_capture)

    async def exercise() -> list[list[dict[str, Any]]]:
        await _stub_run_dependencies(monkeypatch, rows)
        tasks = [asyncio.create_task(runs_svc.list_runs(limit=1)) for _ in range(12)]
        # The fallback runs off-loop: this timer must fire while the capture is
        # still sleeping, rather than only after the listing already finished.
        await asyncio.sleep(0.02)
        assert any(not task.done() for task in tasks)
        return await asyncio.gather(*tasks)

    pages = asyncio.run(exercise())
    assert captures == 1
    assert all(page[0]["effective_health"] == "healthy" for page in pages)


def test_admin_health_exposes_process_fallback_coverage(monkeypatch):
    """Operators can see identity coverage, fallback volume, cache age and scan cost."""
    import lionagi.studio.services.admin as admin_svc

    _isolate_snapshot_cache(monkeypatch)
    monkeypatch.setattr(admin_svc, "require_file_store", lambda: None)
    monkeypatch.setattr(admin_svc, "store_exists", lambda: False)
    monkeypatch.setattr(admin_svc, "db_health", lambda: {})

    async def code_identity() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(admin_svc, "_code_identity_report", code_identity)

    report = asyncio.run(admin_svc.health_report())
    diagnostics = report["process_snapshot"]
    assert diagnostics == {
        "captures": 0,
        "cache_hits": 0,
        "singleflight_hits": 0,
        "identity_resolved": 0,
        "fallback_checks": 0,
        "last_scan_duration_ms": None,
        "cache_age_ms": None,
    }


def test_invocation_health_avoids_host_scan_for_terminal_and_identity_rows(monkeypatch):
    """Invocation listings inherit the same targeted-first process contract."""
    import lionagi.studio.services.admin as admin_svc
    import lionagi.studio.services.invocations as invocations_svc

    _isolate_snapshot_cache(monkeypatch)
    now = time.time()
    create_time = psutil.Process(os.getpid()).create_time()
    sessions = [
        {
            **_running_row(
                session_id="identity-child",
                node_metadata={"pid": os.getpid(), "pid_create_time": create_time},
            ),
            "last_message_at": now,
        },
        {
            "id": "completed-child",
            "status": "completed",
            "updated_at": now - 10,
            "last_message_at": now - 10,
            "node_metadata": None,
        },
    ]

    def forbidden_capture() -> str:
        raise AssertionError("resolved and terminal invocation children need no process table")

    monkeypatch.setattr(admin_svc, "_ps_snapshot", forbidden_capture)
    health, last_activity = asyncio.run(invocations_svc._invocation_health(sessions, now=now))
    assert health == "healthy"
    assert last_activity == now


def test_a_second_event_loop_gets_its_own_capture_instead_of_a_cross_loop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-flight capture is a Task, and a Task belongs to one event loop.

    Sharing it through a process-global meant a caller running its own loop
    reached `asyncio.shield` on a Task owned by the serving loop and got a
    cross-loop RuntimeError instead of the snapshot. Two real loops are driven
    here, in separate threads, with the capture held open until both callers
    are inside it -- a sequential test cannot reach the state at all, because
    the in-flight slot is empty by the time the second call runs.
    """
    import threading

    import lionagi.studio.services.admin as admin_svc

    _isolate_snapshot_cache(monkeypatch)

    both_arrived = threading.Barrier(2, timeout=10)
    captures = 0
    captures_lock = threading.Lock()

    def slow_capture() -> str:
        nonlocal captures
        with captures_lock:
            captures += 1
        # Hold the capture open until the other loop has also entered
        # cached_ps_snapshot, so the second caller meets a PENDING in-flight
        # entry rather than a finished one.
        try:
            both_arrived.wait()
        except threading.BrokenBarrierError:  # pragma: no cover - timeout path
            pass
        return "PID COMMAND\n1 init\n"

    monkeypatch.setattr(admin_svc, "_ps_snapshot", slow_capture)

    results: dict[str, object] = {}

    def run_on_its_own_loop(name: str) -> None:
        try:
            results[name] = asyncio.run(admin_svc.cached_ps_snapshot())
        except BaseException as exc:  # noqa: BLE001 - the defect raised RuntimeError
            results[name] = exc

    threads = [
        threading.Thread(target=run_on_its_own_loop, args=(name,), daemon=True)
        for name in ("first", "second")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a caller never returned"

    for name in ("first", "second"):
        assert not isinstance(results[name], BaseException), (
            f"{name} loop raised instead of receiving a snapshot: {results[name]!r}"
        )
        assert results[name] == "PID COMMAND\n1 init\n"

    # Both loops really did run concurrently and each captured for itself:
    # the barrier only releases when two captures are in flight at once, so
    # this also proves the two calls overlapped rather than ran back to back.
    assert captures == 2


@pytest.mark.asyncio
async def test_concurrent_callers_on_one_loop_still_share_a_single_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive arm: per-loop scoping must not disable singleflight.

    Without this, the fix above is equally satisfied by removing the shared
    capture entirely and letting every caller run its own `ps`.
    """
    import lionagi.studio.services.admin as admin_svc

    _isolate_snapshot_cache(monkeypatch)

    captures = 0
    release = asyncio.Event()

    def counted_capture() -> str:
        nonlocal captures
        captures += 1
        return "PID COMMAND\n1 init\n"

    monkeypatch.setattr(admin_svc, "_ps_snapshot", counted_capture)

    async def gated() -> str:
        await release.wait()
        return await admin_svc.cached_ps_snapshot()

    waiters = [asyncio.create_task(gated()) for _ in range(4)]
    await asyncio.sleep(0)
    release.set()
    values = await asyncio.gather(*waiters)

    assert values == ["PID COMMAND\n1 init\n"] * 4
    assert captures == 1
    metrics = admin_svc._ps_snapshot_metrics_state()
    assert int(metrics["singleflight_hits"] or 0) >= 1
