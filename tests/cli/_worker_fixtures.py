# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Persistence setup doubles with real worker ownership and recorded writes."""

from __future__ import annotations

import time
import uuid
from typing import Any

from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
from lionagi.state.cli_workers import _consume_worker_spawn_continuation, _WorkerSpawnOwner


class RecordingWorkerStore:
    def __init__(self, session: dict[str, Any], records: list[dict[str, Any]]) -> None:
        self.session = session
        self.records = records
        self.closed = False

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        if self.closed:
            raise RuntimeError("the store handle is closed")
        return dict(self.session) if session_id == self.session["id"] else None

    async def register_cli_worker_spawn(self, payload: dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("the store handle is closed")
        for recorded in self.records:
            if recorded["spawn_id"] == payload["spawn_id"]:
                if recorded != payload:
                    raise ValueError("the spawn identity changed")
                return
        if (
            payload["session_id"] != self.session["id"]
            or payload["generation_id"] != self.session["cli_worker_generation"]
            or self.session["status"] != "running"
        ):
            raise ValueError("the worker does not belong to the running generation")
        self.records.append(dict(payload))

    async def close(self) -> None:
        self.closed = True


class WorkerSetup:
    def __init__(self, session_id: str = "sess-0") -> None:
        self.session_id = session_id
        self.records: list[dict[str, Any]] = []
        self.contexts: list[dict[str, Any]] = []
        self._session: dict[str, Any] | None = None

    async def setup(self, branch=None, **kwargs) -> dict[str, Any]:
        continuation = kwargs.get("_worker_spawn_continuation")
        associations = {
            "run_id": kwargs.get("run_id"),
            "branch_id": str(branch.id) if branch is not None else None,
        }
        if continuation is None:
            if self._session is not None and self._session["status"] == "running":
                raise _WorkerSpawnAdmissionError("a running session requires a continuation")
            self._session = {
                "id": self.session_id,
                "status": "running",
                "cli_worker_generation": str(uuid.uuid4()),
            }
            store = RecordingWorkerStore(self._session, self.records)
            owner = _WorkerSpawnOwner(
                store,
                session_id=self.session_id,
                generation_id=self._session["cli_worker_generation"],
                **associations,
            )
        else:
            if self._session is None:
                raise _WorkerSpawnAdmissionError("there is no execution to continue")
            store = RecordingWorkerStore(self._session, self.records)
            owner = await _consume_worker_spawn_continuation(continuation, store, **associations)
        context = {
            "session_id": self.session_id,
            "worker_spawn_owner": owner,
            "worker_spawn_db_closed": None,
            "worker_spawn_teardown_status": None,
            "worker_spawn_store": store,
        }
        self.contexts.append(context)
        return context

    async def teardown(self, context, *, status="completed", defer_terminal=False, **kwargs):
        if context is None:
            return status
        await context["worker_spawn_owner"].aclose()
        store = context["worker_spawn_store"]
        if not defer_terminal:
            store.session["status"] = status
        await store.close()
        context["worker_spawn_db_closed"] = store
        context["worker_spawn_teardown_status"] = status
        return status


async def create_worker_session(db, *, run_id: str | None = None) -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    progression_id = uuid.uuid4().hex
    generation = str(uuid.uuid4())
    await db.create_progression(progression_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": progression_id,
            "status": "running",
            "cli_worker_generation": generation,
            "invocation_kind": "flow",
            "started_at": time.time(),
            "node_metadata": {"flow_control_poller": "active"},
        }
    )
    session = await db.get_session(session_id)
    assert session["status"] == "running"
    assert session["cli_worker_generation"] == generation
    return {
        "db": db,
        "session_id": session_id,
        "session_prog_id": progression_id,
        "worker_spawn_owner": _WorkerSpawnOwner(
            db, session_id=session_id, generation_id=generation, run_id=run_id
        ),
        "worker_spawn_db_closed": None,
        "worker_spawn_teardown_status": None,
    }
