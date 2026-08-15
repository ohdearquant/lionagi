# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression test for insert_artifact's natural-key upsert race.

insert_artifact() documents an upsert: two calls with the same natural key
(kind, name, invocation_id, session_id) must return the same stable id, with
the later call's content winning. The lookup used to run as a separate
autocommit SELECT (StateDB._read()) before the write transaction
(StateDB._tx()) ever opened. Two processes racing the same natural key could
both observe no existing row and both attempt an INSERT; the loser hit one
of the four partial unique indexes in schema.sql
(idx_artifacts_natural_key_*) as an IntegrityError instead of the documented
upsert.

Each worker's StateDB._tx() — the sole write choke point, unchanged by the
fix — is patched to rendezvous on a multiprocessing.Barrier immediately
before entering the real transaction. Pre-fix, the natural-key lookup has
already run (and found nothing) by the time either worker reaches that
rendezvous, so releasing the barrier reliably reproduces the collision
regardless of machine speed. Post-fix, the same rendezvous is harmless: the
lookup lives inside the same atomic statement as the write, so whichever
worker's transaction commits first simply wins the ON CONFLICT and the other
observes and updates the now-existing row. Two real OS processes against a
file-backed database are used deliberately — a single process (or an
in-memory connection) would not model two independent writer connections
racing on durable state.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import sqlite3
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB

_BARRIER_TIMEOUT_SECONDS = 15


def _init_db(db_path: Path) -> None:
    async def _open_close() -> None:
        db = StateDB(db_path)
        await db.open()
        await db.close()

    asyncio.run(_open_close())


def _seed_parents(db_path: Path, *, invocation_id: str | None, session_id: str | None) -> None:
    """Insert whatever invocation/session/progression rows the artifact's FK
    columns require. A plain sync connection is enough for a one-off seed —
    no need for the async engine machinery."""
    if invocation_id is None and session_id is None:
        return
    with sqlite3.connect(db_path) as conn:
        now = 0.0
        if invocation_id is not None:
            conn.execute(
                "INSERT INTO invocations (id, skill, started_at, created_at, updated_at) "
                "VALUES (?, 'race-test', ?, ?, ?)",
                (invocation_id, now, now, now),
            )
        if session_id is not None:
            prog_id = str(uuid.uuid4())
            conn.execute("INSERT INTO progressions (id, created_at) VALUES (?, ?)", (prog_id, now))
            conn.execute(
                "INSERT INTO sessions (id, created_at, progression_id, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, now, prog_id, now),
            )
        conn.commit()


def _count_artifacts(db_path: Path) -> int:
    async def _count() -> int:
        db = StateDB(db_path)
        await db.open()
        try:
            async with db._read() as conn:
                row = (
                    (await conn.execute(text("SELECT COUNT(*) AS n FROM artifacts")))
                    .mappings()
                    .first()
                )
            return row["n"]
        finally:
            await db.close()

    return asyncio.run(_count())


def _race_worker(
    db_path: str,
    label: str,
    kind: str,
    name: str,
    invocation_id: str | None,
    session_id: str | None,
    content: dict,
    barrier,
    result_queue,
) -> None:
    """Rendezvous at StateDB._tx() — the sole write choke point both before
    and after the fix — then upsert one artifact."""
    import lionagi.state.db as state_db_module

    original_tx = state_db_module.StateDB._tx

    @asynccontextmanager
    async def synchronized_tx(self):
        barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        async with original_tx(self) as conn:
            yield conn

    state_db_module.StateDB._tx = synchronized_tx

    async def _run() -> str:
        db = StateDB(db_path)
        await db.open()
        try:
            return await db.insert_artifact(
                kind=kind,
                name=name,
                content=content,
                invocation_id=invocation_id,
                session_id=session_id,
            )
        finally:
            await db.close()

    try:
        art_id = asyncio.run(_run())
    except Exception:  # noqa: BLE001
        result_queue.put((label, None, traceback.format_exc()))
    else:
        result_queue.put((label, art_id, None))


def _run_race(db_path: Path, jobs: list[tuple[str, str, str, str | None, str | None, dict]]):
    """jobs: list of (label, kind, name, invocation_id, session_id, content)."""
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(jobs))
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_race_worker,
            args=(str(db_path), label, kind, name, inv_id, ses_id, content, barrier, result_queue),
        )
        for label, kind, name, inv_id, ses_id, content in jobs
    ]

    try:
        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=30)

        results: dict[str, tuple[str | None, str | None]] = {}
        for _ in processes:
            try:
                label, art_id, tb = result_queue.get(timeout=2)
            except queue.Empty:
                pytest.fail("a worker exited without reporting a result")
            results[label] = (art_id, tb)
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()

    exit_codes = [p.exitcode for p in processes]
    assert all(code == 0 for code in exit_codes), exit_codes
    return results


# Same natural key, all four ADR-0077 shapes


@pytest.mark.parametrize(
    "use_invocation, use_session",
    [
        pytest.param(True, False, id="invocation_only"),
        pytest.param(False, True, id="session_only"),
        pytest.param(True, True, id="both"),
        pytest.param(False, False, id="unattached"),
    ],
)
def test_concurrent_insert_artifact_same_key_does_not_raise(
    tmp_path: Path, use_invocation: bool, use_session: bool
) -> None:
    db_path = tmp_path / "race.db"
    _init_db(db_path)

    invocation_id = str(uuid.uuid4()) if use_invocation else None
    session_id = str(uuid.uuid4()) if use_session else None
    _seed_parents(db_path, invocation_id=invocation_id, session_id=session_id)

    kind, name = "review_verdict", "same-key"
    jobs = [
        (label, kind, name, invocation_id, session_id, {"writer": label}) for label in ("A", "B")
    ]
    results = _run_race(db_path, jobs)

    for label, (art_id, tb) in results.items():
        assert tb is None, f"worker {label} raised:\n{tb}"

    id_a, _ = results["A"]
    id_b, _ = results["B"]
    assert id_a is not None and id_b is not None
    assert id_a == id_b, "both writers of the same natural key must return the same stable id"
    assert _count_artifacts(db_path) == 1, "the same natural key must upsert, not duplicate"


# Different-keys control: the harness must not itself serialize writers


def test_concurrent_insert_artifact_different_keys_both_succeed(tmp_path: Path) -> None:
    db_path = tmp_path / "race-control.db"
    _init_db(db_path)

    kind = "review_verdict"
    jobs = [(label, kind, f"key-{label}", None, None, {"writer": label}) for label in ("A", "B")]
    results = _run_race(db_path, jobs)

    for label, (art_id, tb) in results.items():
        assert tb is None, f"worker {label} raised:\n{tb}"

    id_a, _ = results["A"]
    id_b, _ = results["B"]
    assert id_a != id_b, "different natural keys must produce distinct artifacts"
    assert _count_artifacts(db_path) == 2
