# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest


async def test_p95_latency_excludes_reaper_observed_rows(tmp_path, monkeypatch):
    """A reaped row's ended_at is when the reaper noticed a stale run, not how
    long anything ran, so it must not enter the latency sample.

    The reaper writes a ``{"kind": "invocation"}`` entry into
    ``status_evidence_refs`` and a run's own deadline write does not, so the
    metric can tell the two apart by that entry even though both carry status
    ``timed_out`` and reason code ``run.timed_out.deadline``. Here a 2 h row the
    reaper ends sits beside a 30 s row the run recorded itself: the percentile
    must be the 30 s row's.
    """
    import time as _time
    from types import SimpleNamespace

    from lionagi.state.db import StateDB
    from lionhub.orchestration.scheduler import lifecycle as lifecycle_mod

    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    monkeypatch.setattr(lifecycle_mod, "time", SimpleNamespace(time=lambda: 1_000_000.0))
    async with StateDB(path) as db:
        await db.create_invocation(
            {
                "id": "reaped",
                "skill": "agent",
                "status": "running",
                "started_at": 992_000.0,
                "session_count": 1,
            }
        )
        await db.create_invocation(
            {
                "id": "finished",
                "skill": "agent",
                "status": "completed",
                "started_at": 999_970.0,
                "ended_at": 1_000_000.0,
            }
        )

    assert await lifecycle_mod.reap_stale_invocations(deadline_seconds=7200) == 1

    async with StateDB(path) as db:
        reaped = await db.get_invocation("reaped")
        assert reaped["status"] == "timed_out"
        assert reaped["ended_at"] == 1_000_000.0
        # The reaper's 8000s observation is excluded; the 30s of real work stands.
        assert await db.metric_value("p95_latency_ms", window_start=0.0) == 30_000.0
