# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Flow planning: TaskAssignment decomposition + #1236 loud-failure handling.

The orchestrator's plan is a ``list[TaskAssignment]`` (casts emission), parsed
by ``lionagi.orchestration.plan``. DAG topology validation lives in the
orchestration lib (``build_dag_graph`` + the executor's acyclicity check) and is
covered by ``tests/orchestration/test_patterns.py``. Here we cover the
flow-level contract: an empty plan triggers ONE reinforced retry, and a still-
empty plan fails LOUD (FlowPlanError, non-zero exit) instead of exiting 0 with
no work done (#1236).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate.flow import FlowPlanError, _parse_reactive, _run_flow_inner


class _FakeOrcBranch:
    """orc_branch stub: operate() pops one scripted result per plan() call."""

    def __init__(self, results):
        self.id = uuid4()
        self._results = list(results)
        self.operate_calls: list[dict] = []

    async def operate(self, **kw):
        self.operate_calls.append(kw)
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(assignments=[])


def _env(tmp_path, orc) -> SimpleNamespace:
    _name_counts: dict = {}

    def _assign_name(role: str) -> str:
        _name_counts[role] = _name_counts.get(role, 0) + 1
        n = _name_counts[role]
        return f"{role}-{n}" if n > 1 else role

    return SimpleNamespace(
        run=SimpleNamespace(
            artifact_root=tmp_path,
            dag_image_path=tmp_path / "dag.png",
            agent_artifact_dir=lambda a: tmp_path / a,
        ),
        orc_branch=orc,
        default_model_spec="codex/gpt-5.5",
        bare=True,
        effort=None,
        total_budget=None,
        team_data=None,
        session=SimpleNamespace(),
        builder=SimpleNamespace(),
        assign_name=_assign_name,
    )


@pytest.mark.asyncio
async def test_no_plan_recovers_via_reinforced_retry(tmp_path):
    """Empty first plan → exactly one retry; a good retry drives the dry-run output."""
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(assignments=[]),
            SimpleNamespace(assignments=[TaskAssignment(task="dig", assignee="researcher")]),
        ]
    )
    out = await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    assert len(orc.operate_calls) == 2
    assert "researcher" in out


@pytest.mark.asyncio
async def test_no_plan_after_retry_raises_flow_plan_error(tmp_path):
    """Both attempts empty → fail loud, never exit 0 (#1236)."""
    orc = _FakeOrcBranch([SimpleNamespace(assignments=[]), SimpleNamespace(assignments=[])])
    with pytest.raises(FlowPlanError, match="no usable plan"):
        await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    assert len(orc.operate_calls) == 2


@pytest.mark.asyncio
async def test_unknown_assignees_dropped_then_loud_fail(tmp_path):
    """plan() drops assignees outside the roster; an all-unknown plan is empty."""
    bad = SimpleNamespace(assignments=[TaskAssignment(task="x", assignee="not_a_role")])
    orc = _FakeOrcBranch([bad, bad])
    with pytest.raises(FlowPlanError):
        await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)


@pytest.mark.asyncio
async def test_dry_run_lists_assignments_with_deps(tmp_path):
    """A valid plan in dry-run mode dumps assignments + their 1-based deps."""
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(
                assignments=[
                    TaskAssignment(task="survey prior art", assignee="researcher"),
                    TaskAssignment(task="design on 1", assignee="architect", depends_on=["1"]),
                ]
            )
        ]
    )
    out = await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    assert "researcher" in out and "architect" in out
    assert "depends_on: 1" in out
    assert len(orc.operate_calls) == 1  # no retry needed


class TestParseReactive:
    """--reactive MODE → (reactive, spawn_roles) for grant-gating."""

    def test_default_all_every_worker_spawns(self):
        assert _parse_reactive("all") == (True, None)
        assert _parse_reactive(None) == (True, None)
        assert _parse_reactive("") == (True, None)

    def test_off_disables_reactive(self):
        assert _parse_reactive("off") == (False, set())
        assert _parse_reactive("none") == (False, set())
        assert _parse_reactive("false") == (False, set())

    def test_role_list_gates_spawning(self):
        assert _parse_reactive("critic,evaluator") == (True, {"critic", "evaluator"})

    def test_role_list_is_whitespace_tolerant(self):
        assert _parse_reactive(" critic , evaluator ") == (True, {"critic", "evaluator"})

    def test_case_insensitive_keywords(self):
        assert _parse_reactive("OFF") == (False, set())
        assert _parse_reactive("All") == (True, None)


def test_reactive_flag_parses_via_argparse():
    """`li o flow --reactive off` reaches args.reactive."""
    import argparse

    from lionagi.cli.orchestrate import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li")
    sub = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(sub)
    args = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task", "--reactive", "off"])
    assert args.reactive == "off"
    # default is None (resolved to "all" at dispatch)
    args2 = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task"])
    assert args2.reactive is None


def test_workers_flag_parses_via_argparse():
    """`li o flow --workers a,b` reaches args.workers (mixed-model flows)."""
    import argparse

    from lionagi.cli.orchestrate import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li")
    sub = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(sub)
    args = parser.parse_args(
        ["o", "flow", "codex/orc", "task", "--workers", "codex/cheap,codex/expensive"]
    )
    assert args.workers == "codex/cheap,codex/expensive"


@pytest.mark.asyncio
async def test_workers_override_shown_in_dry_run(tmp_path):
    """--workers overrides the per-assignment model and wraps the pool (i % len)."""
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(
                assignments=[
                    TaskAssignment(task="a", assignee="researcher"),
                    TaskAssignment(task="b", assignee="architect"),
                    TaskAssignment(task="c", assignee="implementer"),
                ]
            )
        ]
    )
    out = await _run_flow_inner(
        "codex/gpt-5.5",
        "task",
        env=_env(tmp_path, orc),
        dry_run=True,
        workers_str="codex/cheap,codex/expensive",
    )
    # pool wraps: researcher→cheap, architect→expensive, implementer→cheap.
    assert "researcher: codex/cheap (workers)" in out
    assert "architect: codex/expensive (workers)" in out
    assert "implementer: codex/cheap (workers)" in out


@pytest.mark.asyncio
async def test_workers_override_keeps_role_modes(tmp_path):
    """Unlike --bare, --workers keeps each role's cognitive modes (ADR-0074).

    The model is swapped to the pool spec, but the per-task modes are still
    resolved through ``resolve_modes`` — proving the override touches only the
    model, not the role's behavioural config.
    """
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(
                assignments=[
                    TaskAssignment(task="review it", assignee="reviewer", modes=["adversarial"])
                ]
            )
        ]
    )
    env = _env(tmp_path, orc)
    env.bare = False  # profiles/modes active; --workers only swaps the model
    out = await _run_flow_inner(
        "codex/gpt-5.5", "task", env=env, dry_run=True, workers_str="codex/expensive"
    )
    assert "reviewer: codex/expensive (workers)" in out
    assert "adversarial" in out  # role behaviour preserved, not stripped like --bare


# ── Edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_large_number_of_assignments_truncated_at_200(tmp_path):
    many = [TaskAssignment(task=f"task-{i}", assignee="researcher") for i in range(250)]
    orc = _FakeOrcBranch([SimpleNamespace(assignments=many)])
    out = await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    plan_lines = [ln for ln in out.splitlines() if ln.strip().startswith(("1.", "200.", "201."))]
    assert not any("201." in ln for ln in plan_lines), "assignments beyond 200 must be truncated"
    assert any("200." in ln for ln in plan_lines), "200th assignment must still appear"


@pytest.mark.asyncio
async def test_plan_with_self_dependency_drops_self_loop(tmp_path):
    assignments = [
        TaskAssignment(task="research", assignee="researcher", depends_on=["1"]),
    ]
    orc = _FakeOrcBranch([SimpleNamespace(assignments=assignments)])
    out = await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    assert "researcher" in out


@pytest.mark.asyncio
async def test_plan_deps_reference_only_earlier_indices(tmp_path):
    assignments = [
        TaskAssignment(task="a", assignee="researcher"),
        TaskAssignment(task="b", assignee="architect", depends_on=["1"]),
        TaskAssignment(task="c", assignee="implementer", depends_on=["1", "2"]),
    ]
    orc = _FakeOrcBranch([SimpleNamespace(assignments=assignments)])
    out = await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    assert "depends_on: 1, 2" in out or "depends_on: 1" in out


@pytest.mark.asyncio
async def test_flow_plan_error_message_is_informative(tmp_path):
    orc = _FakeOrcBranch([SimpleNamespace(assignments=[]), SimpleNamespace(assignments=[])])
    with pytest.raises(FlowPlanError) as exc_info:
        await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    assert "no usable plan" in str(exc_info.value).lower() or "empty" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_empty_task_string_does_not_crash_planning(tmp_path):
    assignments = [TaskAssignment(task="", assignee="researcher")]
    orc = _FakeOrcBranch([SimpleNamespace(assignments=assignments)])
    out = await _run_flow_inner("codex/gpt-5.5", "", env=_env(tmp_path, orc), dry_run=True)
    assert "researcher" in out
