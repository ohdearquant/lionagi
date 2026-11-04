"""Shared real-store fixtures for stale-process quarantine tests."""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.cli import kill as kill_cli
from lionagi.state.db import StateDB

GENERATION = "11111111-1111-4111-8111-111111111111"
NEXT_GENERATION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(kill_cli, "_pid_alive", lambda _pid: True)


async def seed(kind="session", *, metadata=None, extras=None):
    entity_id = str(uuid.uuid4())
    meta = {"pid": 54321, "pid_create_time": 100.0} if metadata is None else metadata
    async with StateDB() as db:
        if kind == "session":
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": entity_id,
                    "progression_id": progression_id,
                    "cli_worker_generation": GENERATION,
                    "status": "running",
                    "started_at": time.time() - 7200,
                    "node_metadata": meta,
                    **(extras or {}),
                }
            )
        else:
            await db.create_invocation(
                {
                    "id": entity_id,
                    "skill": "test",
                    "status": "running",
                    "started_at": time.time() - 7200,
                    "node_metadata": meta,
                }
            )
    return entity_id


async def fetch_row(kind, entity_id):
    async with StateDB() as db:
        return await (
            db.get_session(entity_id) if kind == "session" else db.get_invocation(entity_id)
        )


def probe(monkeypatch, verdicts):
    iterator = iter(verdicts)

    def identity(_pid, _name, **kwargs):
        result = next(iterator)
        if result == "unverifiable" and kwargs.get("_reason_out") is not None:
            kwargs["_reason_out"].append("permission_denied")
        return result

    monkeypatch.setattr(kill_cli, "check_pid_identity", identity)


async def sweep(monkeypatch, verdicts, *, dry_run=False):
    probe(monkeypatch, verdicts)
    assert await kill_cli._do_kill_all_stale(threshold_seconds=3600, dry_run=dry_run) == 0
