# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li o fanout`: planner overshooting max_tasks must fail clean, not raw-traceback.

`plan()` raises a bare ValueError when the orchestrator returns more assignments
than the worker cap even after the cap was stated in guidance. `li o flow`
translates that into `FlowPlanError` and maps it to a clean exit code via
`extra_handlers` in `_run_orch_command`. `li o fanout` calls the same `plan()`
with `max_tasks=num_workers` but had neither translation, so the ValueError
escaped `run_orchestrate` as a raw traceback instead of a logged error + exit
code 1. This module covers both halves of the fix: the fanout.py translation
and the __init__.py exit-code wiring.
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.cli._util import EXIT_CODE_BY_STATUS
from lionagi.cli.orchestrate import add_orchestrate_subparser, run_orchestrate
from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.cli.orchestrate.fanout import FanoutPlanError
from lionagi.engines import PlanningEngine

from .test_fanout_artifacts import _fanout_env


def _parse_fanout_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="li")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(subparsers)
    return parser.parse_args(["o", "fanout", *argv])


async def test_run_fanout_translates_over_max_tasks_value_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """plan() raising ValueError for an over-cap plan must surface as
    FanoutPlanError from `_run_fanout`, not a bare ValueError."""
    env, run, session = _fanout_env(tmp_path)

    engine_run = type("EngineRunStub", (), {})()
    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    monkeypatch.setattr(
        fanout_module,
        "stop_live_persist",
        AsyncMock(side_effect=lambda env, status: status),
    )
    monkeypatch.setattr(
        fanout_module,
        "plan",
        AsyncMock(
            side_effect=ValueError("orchestrator returned 3 assignments, exceeding max_tasks=2")
        ),
    )
    monkeypatch.setattr(fanout_module, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(fanout_module, "role_roster", lambda model: "worker")
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, **kwargs: engine_run)

    with pytest.raises(FanoutPlanError, match="exceeding max_tasks"):
        await fanout_module._run_fanout(
            "codex/model",
            "work",
            num_workers=2,
        )


def test_run_orchestrate_fanout_plan_error_clean_exit(caplog):
    """`run_orchestrate` must map a FanoutPlanError from `_run_fanout` to a
    logged error + non-zero exit code — never let it (or any BaseException)
    escape as a raw traceback."""
    args = _parse_fanout_args(["claude", "do the thing"])

    with patch(
        "lionagi.cli.orchestrate._run_fanout",
        AsyncMock(
            side_effect=FanoutPlanError(
                "orchestrator returned 5 assignments, exceeding max_tasks=3"
            )
        ),
    ):
        with caplog.at_level("ERROR"):
            code = run_orchestrate(args)

    assert code == EXIT_CODE_BY_STATUS["failed"]
    assert any("exceeding max_tasks" in rec.message for rec in caplog.records)


def test_run_orchestrate_fanout_unhandled_error_still_propagates():
    """Sanity check that the new extra_handlers entry is narrow: an unrelated
    BaseException from `_run_fanout` (not a FanoutPlanError/timeout) must still
    propagate rather than being swallowed into a clean exit."""
    args = _parse_fanout_args(["claude", "do the thing"])

    with patch(
        "lionagi.cli.orchestrate._run_fanout",
        AsyncMock(side_effect=RuntimeError("unrelated failure")),
    ):
        with pytest.raises(RuntimeError, match="unrelated failure"):
            run_orchestrate(args)
