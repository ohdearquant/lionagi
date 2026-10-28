# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A persisted agent run with no worker owner proceeds only when its setup result says why.

ADR-0104 Amendment 4, Limits: "its setup result says so in `worker_spawns_unrecorded`, and a
persisted setup result that carries no owner and does not say so is refused".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
from lionagi.state.compat import Closure, CompatibilityProfile
from tests.cli.test_agent_notify_without_persistence import _wire_agent_stubs


@pytest.mark.asyncio
async def test_an_ownerless_setup_result_that_does_not_say_why_is_refused(monkeypatch, tmp_path):
    _wire_agent_stubs(
        monkeypatch,
        tmp_path,
        persist={"db": SimpleNamespace(compatibility=None), "session_id": "s1"},
    )
    from lionagi.cli.agent import _run_agent

    with pytest.raises(_WorkerSpawnAdmissionError, match="no admitted worker owner"):
        await _run_agent("codex/model", "do the thing")


@pytest.mark.asyncio
async def test_a_setup_result_that_declares_unrecorded_spawns_runs_with_no_recorder(
    monkeypatch, tmp_path
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi._cli_worker_context import _get_cli_worker_recorder

    _wire_agent_stubs(
        monkeypatch,
        tmp_path,
        # Only a store in compatibility mode yields this result, and the runner skips the
        # steer sweep there, so the stand-in store carries a compatibility profile.
        persist={
            "db": SimpleNamespace(
                compatibility=CompatibilityProfile(
                    name="agent-records",
                    version=1,
                    closure=Closure(tables=()),
                    writes={},
                    operations={"run"},
                )
            ),
            "session_id": "s1",
            "worker_spawn_owner": None,
            "worker_spawns_unrecorded": True,
        },
    )
    recorders = []

    async def operate(self, instruction=None, **kw):
        recorders.append(_get_cli_worker_recorder())
        return "done"

    async def no_drain(*a, **kw):
        return None

    monkeypatch.setattr(Branch, "operate", operate)
    monkeypatch.setattr(agent_mod, "_drain_pending_steers", no_drain)

    result = await agent_mod._run_agent("codex/model", "do the thing")

    assert result[3] == "completed"
    assert recorders == [None], "an unrecorded run's workers launch with no recorder"
