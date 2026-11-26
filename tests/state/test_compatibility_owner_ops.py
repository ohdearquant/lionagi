# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility-mode status of an operator turn, read through the Studio application.

The rest of the compatibility-mode ownership tests, and the managed store they share, live with the
hub's state tests. This one drives the Studio operator coordinator, which the hub's test tree does
not import, so it stays here.
"""

from __future__ import annotations

import pytest

from lionhub.state.compat import CompatibilityProfile


@pytest.mark.parametrize(
    ("compatibility", "recorded"),
    [(None, "completed"), ("profile", "failed")],
    ids=["native", "compatibility"],
)
async def test_an_operator_turn_records_the_status_its_teardown_decided_in_compatibility_mode(
    tmp_path, monkeypatch, compatibility, recorded
):
    """A turn the engine finished whose teardown the store refused reads `failed` in its run.json
    in compatibility mode (ADR-0128 D6); natively run.json keeps the turn's own status."""
    import json
    import time
    from types import SimpleNamespace

    import anyio

    import lionhub.orchestration.runs as runs_mod
    from lionagi_studio.operator.coordinator import OperatorCoordinator
    from lionagi_studio.operator.store import OperatorStore
    from lionagi_studio.operator.types import OperatorEngineEvent
    from lionhub.state.compat import Closure

    monkeypatch.setattr("lionhub.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    profile = (
        CompatibilityProfile(
            name="operator-records",
            version=1,
            closure=Closure(tables=()),
            writes={},
            operations={"run"},
        )
        if compatibility == "profile"
        else None
    )

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=profile)}

    async def teardown(ctx, *, status="completed", **kwargs):
        return "failed"

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)

    class _Engine:
        async def _stream(self, _turn):
            yield OperatorEngineEvent(
                "text", {"content": "reply", "format": "plain", "role": "assistant"}
            )

        def stream(self, turn):
            return self._stream(turn)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_Engine
    )
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            manifests = list((tmp_path / "runs").glob("operator-*/run.json"))
            if manifests:
                status = json.loads(manifests[0].read_text()).get("status")
                if status != "running":
                    break
            await anyio.sleep(0.02)
    finally:
        await coordinator.shutdown()

    assert status == recorded
