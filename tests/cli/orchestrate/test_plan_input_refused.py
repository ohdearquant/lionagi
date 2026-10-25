# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A construction refusal reaches `li o fanout` and `li o flow` as a clean failure.

A request the Codex CLI cannot take is refused at construction with a
``CLIInputRefusedError``, which is deliberately not a ValueError. The three
``plan()`` call sites in `fanout.py` and `flow.py` caught ValueError to turn a
plan-time failure into a ``FanoutPlanError``/``FlowPlanError`` and a clean exit
code; the refusal passed those clauses and escaped as a raw traceback. Each
site now catches it beside ValueError, with the same translation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from lionagi._errors import CLIInputRefusedError
from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.cli.orchestrate import flow as flow_module
from lionagi.cli.orchestrate.fanout import FanoutPlanError
from lionagi.cli.orchestrate.flow import FlowPlanError
from lionhub.orchestration.engines import PlanningEngine

from .test_fanout_artifacts import _fanout_env
from .test_flow_planning import _env as _flow_env

REFUSAL = "the Codex turn input is 2,000,000 characters, past the CLI's limit"


@pytest.mark.asyncio
async def test_fanout_translates_a_construction_refusal(tmp_path, monkeypatch):
    env, _run, _session = _fanout_env(tmp_path)

    engine_run = type("EngineRunStub", (), {})()
    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    monkeypatch.setattr(
        fanout_module,
        "stop_live_persist",
        AsyncMock(side_effect=lambda env, status: status),
    )
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(side_effect=CLIInputRefusedError(REFUSAL)))
    monkeypatch.setattr(fanout_module, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(fanout_module, "role_roster", lambda model: "worker")
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, **kwargs: engine_run)

    with pytest.raises(FanoutPlanError, match="past the CLI's limit"):
        await fanout_module._run_fanout("codex/model", "work", num_workers=2)


@pytest.mark.asyncio
async def test_flow_translates_a_construction_refusal(tmp_path):
    class _RefusingOrc:
        id = None

        async def operate(self, **kw):
            raise CLIInputRefusedError(REFUSAL)

    with pytest.raises(FlowPlanError, match="past the CLI's limit"):
        await flow_module._run_flow_inner(
            "codex/gpt-5.5", "task", env=_flow_env(tmp_path, _RefusingOrc()), dry_run=True
        )


@pytest.mark.asyncio
async def test_flow_translates_a_construction_refusal_on_the_retry(tmp_path):
    """The reinforced retry is a second call site, and it carries the same translation."""
    from types import SimpleNamespace

    class _Orc:
        id = None

        def __init__(self):
            self.calls = 0

        async def operate(self, **kw):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(assignments=[])
            raise CLIInputRefusedError(REFUSAL)

    orc = _Orc()
    with pytest.raises(FlowPlanError, match="past the CLI's limit"):
        await flow_module._run_flow_inner(
            "codex/gpt-5.5", "task", env=_flow_env(tmp_path, orc), dry_run=True
        )
    assert orc.calls == 2
