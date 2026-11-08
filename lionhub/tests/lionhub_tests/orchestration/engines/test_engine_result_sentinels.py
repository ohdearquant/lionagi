# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Engine result values must remain distinct from its private no-result marker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lionagi.ln.types import Undefined, Unset
from lionhub.orchestration.engines.engine import Engine, EngineBudgetError, EngineResult


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_stop", (False, True), ids=("normal", "partial-export"))
@pytest.mark.parametrize(
    "value_kind",
    (
        "none",
        "false",
        "zero",
        "empty-list",
        "empty-dict",
        "unset",
        "undefined",
        "empty-text",
        "text",
    ),
)
async def test_engine_preserves_explicit_results_across_normal_and_partial_export(
    value_kind, budget_stop
):
    value = {
        "none": None,
        "false": False,
        "zero": 0,
        "empty-list": [],
        "empty-dict": {},
        "unset": Unset,
        "undefined": Undefined,
        "empty-text": "",
        "text": "complete",
    }[value_kind]

    def forbidden_spawn(*_args):
        pytest.fail("this model-free characterization must not spawn a CLI")

    class ValueEngine(Engine):
        partial_calls = 0

        def new_run(self, **_kwargs):
            # Only the per-run context is a test double. Exercise the real
            # run/degrade/wrap methods without constructing a Session or agent.
            return SimpleNamespace(
                run_id="value-characterization",
                _deadline=None,
                _active=set(),
                _budget_notified=False,
                emission_failures=[],
                _agent_errors=[],
                agents_made=0,
                _live_cli_processes=lambda: (),
                _track_cli_process=forbidden_spawn,
                by_type=lambda _event_type: [],
            )

        async def _run(self, run, *args, **kwargs):
            if budget_stop:
                raise EngineBudgetError("test-local budget stop")
            return value

        async def _partial_export(self, run, *args, **kwargs):
            self.partial_calls += 1
            return value

    engine = ValueEngine(max_agents=0)
    result = await engine.run()

    assert engine.partial_calls == int(budget_stop)
    if value_kind in ("empty-text", "text"):
        assert isinstance(result, EngineResult)
        assert result.text == value
        assert result.degraded is budget_stop
        assert result.degrade_reason == ("budget" if budget_stop else "")
        assert result.events_by_type(object) == []
    else:
        assert result is value
        if value_kind == "empty-list":
            assert result == []
        elif value_kind == "empty-dict":
            assert result == {}
