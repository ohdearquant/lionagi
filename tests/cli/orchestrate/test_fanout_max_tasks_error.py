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

from lionagi._errors import EmptyOutgoingContentError
from lionagi.casts.emission import TaskAssignment
from lionagi.cli._util import EXIT_CODE_BY_STATUS
from lionagi.cli.orchestrate import add_orchestrate_subparser, run_orchestrate
from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.cli.orchestrate._orchestration import WorkerBuildError, resolve_modes
from lionagi.cli.orchestrate.fanout import FanoutPlanError
from lionagi.engines import PlanningEngine

from .test_fanout_artifacts import _fanout_env


def _parse_fanout_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="li")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(subparsers)
    return parser.parse_args(["o", "fanout", *argv])


class _WorkerBoundaryReached(Exception):
    """Stop the integration test after fanout hands the plan to a worker."""


async def test_fanout_planner_gets_mode_roster_and_preserves_valid_modes(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    env, _, _ = _fanout_env(tmp_path)
    planned = TaskAssignment(task="challenge the proposal", assignee="critic", modes=["premortem"])
    seen_guidance: list[str] = []
    effective_modes: list[set[str]] = []

    async def plan_with_mode_contract(*args, guidance: str, **kwargs):
        seen_guidance.append(guidance)
        return [planned]

    async def build_worker(env, *, role, modes, **kwargs):
        effective_modes.append(set(resolve_modes(role, modes, env.pack)))
        raise _WorkerBoundaryReached

    monkeypatch.setattr(fanout_module, "plan", plan_with_mode_contract)
    monkeypatch.setattr(fanout_module, "available_roles", lambda: ["critic"])
    monkeypatch.setattr(fanout_module, "role_roster", lambda model: "ROLE ROSTER")
    monkeypatch.setattr(
        fanout_module,
        "mode_roster",
        lambda pack: "MODE ROSTER: critic accepts only premortem",
        raising=False,
    )
    monkeypatch.setattr(fanout_module, "build_worker_branch", build_worker)

    with caplog.at_level("WARNING", logger="lionagi.cli"):
        with pytest.raises(WorkerBuildError) as exc_info:
            await fanout_module._run_fanout_inner("codex/model", "work", env=env)

    assert isinstance(exc_info.value.__cause__, _WorkerBoundaryReached)
    assert seen_guidance == ["ROLE ROSTER\n\nMODE ROSTER: critic accepts only premortem"]
    assert effective_modes == [set(planned.modes)]
    assert not [record for record in caplog.records if "dropping" in record.message]


@pytest.mark.parametrize(
    ("role", "mode", "message"),
    [
        ("critic", "fast", "not permitted"),
        ("writer", "not-a-real-mode", "unknown mode"),
    ],
)
async def test_fanout_rejects_invalid_planned_mode_before_building_any_worker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    mode: str,
    message: str,
):
    env, _, _ = _fanout_env(tmp_path)
    assignments = [TaskAssignment(task="work", assignee=role, modes=[mode])]
    build_worker = AsyncMock()

    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=assignments))
    monkeypatch.setattr(fanout_module, "available_roles", lambda: [role])
    monkeypatch.setattr(fanout_module, "role_roster", lambda model: "ROLE ROSTER")
    monkeypatch.setattr(fanout_module, "mode_roster", lambda pack: "MODE ROSTER", raising=False)
    monkeypatch.setattr(fanout_module, "build_worker_branch", build_worker)

    with pytest.raises(FanoutPlanError, match=message):
        await fanout_module._run_fanout_inner("codex/model", "work", env=env)

    build_worker.assert_not_awaited()


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


async def test_run_fanout_reraises_empty_outgoing_content_error_as_itself(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """EmptyOutgoingContentError subclasses ValueError, so a genuinely dropped
    instruction must NOT be misreported as a `FanoutPlanError` (an orchestrator
    max_tasks overshoot) — it must propagate as itself, mirroring the
    exemption `li o flow` already carries for the same plan() call."""
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
        AsyncMock(side_effect=EmptyOutgoingContentError("instruction_len=42")),
    )
    monkeypatch.setattr(fanout_module, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(fanout_module, "role_roster", lambda model: "worker")
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, **kwargs: engine_run)

    with pytest.raises(EmptyOutgoingContentError, match="instruction_len=42"):
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
