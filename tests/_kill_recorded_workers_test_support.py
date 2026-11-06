# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

import os
from uuid import uuid4


class _CleanupDB:
    def __init__(self, status="failed", *, guards=(), outcome="applied"):
        from contextlib import asynccontextmanager

        self.session = {
            "id": "session-test",
            "status": status,
            "cli_worker_generation": "generation-a",
            "updated_at": 42.0,
            "node_metadata": {"pid": 76543},
        }
        self.records = [
            {
                "spawn_id": str(uuid4()),
                "pid": pid,
                "pgid": pid,
                "pid_create_time": 10.0,
                "pid_host": "host",
                "pid_boot_time": 1.0,
                "process_identity_mode": "local",
            }
            for pid in (76541, 76542)
        ]
        self.guards = iter(guards)
        self.outcome = outcome
        self.cancelled = []
        self.cancel_identities = []
        self.settled = []
        self.guard_calls = []
        self.in_signal_guard = False

        @asynccontextmanager
        async def guard(*args):
            self.guard_calls.append(args)
            verdict = next(self.guards, "unchanged")
            if isinstance(verdict, Exception):
                raise verdict
            self.in_signal_guard = True
            try:
                yield verdict
            finally:
                self.in_signal_guard = False

        self.guard_cli_worker_signal = guard

    async def snapshot_cli_worker_spawns(self, session_id):
        assert session_id == self.session["id"]
        return {"session": dict(self.session), "spawns": list(self.records)}

    async def cancel_session_if_worker_generation(self, session_id, generation, **kwargs):
        self.cancelled.append((session_id, generation))
        self.cancel_identities.append(kwargs["expected_runner_identity"])
        if self.outcome == "applied":
            self.session["status"] = "cancelled"
        return self.outcome

    async def get_session(self, session_id):
        assert session_id == self.session["id"]
        return dict(self.session)

    async def settle_cli_worker_spawn(self, spawn_id, **kwargs):
        self.settled.append(spawn_id)
        return True


def _stub_recorded_groups(monkeypatch, module):
    import lionhub.orchestration.kill as kill_service
    from lionagi.ln._proc import _RecordedGroupWitness

    sent = []
    monkeypatch.setattr(
        kill_service,
        "_admit_recorded_worker_group",
        lambda identity, **kwargs: _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        ),
    )
    monkeypatch.setattr(kill_service, "_recorded_worker_group_matches", lambda witness: "matches")
    monkeypatch.setattr(kill_service, "_recorded_worker_group_state", lambda identity: "empty")
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    return sent
