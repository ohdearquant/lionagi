# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for flow planning: TaskAssignment decomposition and loud failure on empty plan."""

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
        pack=None,
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
    """Both attempts empty → fail loud, never exit 0."""
    orc = _FakeOrcBranch([SimpleNamespace(assignments=[]), SimpleNamespace(assignments=[])])
    with pytest.raises(FlowPlanError, match="no usable plan"):
        await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)
    assert len(orc.operate_calls) == 2


@pytest.mark.asyncio
async def test_over_max_tasks_plan_raises_flow_plan_error(tmp_path):
    """plan() raises ValueError when the orchestrator overshoots max_tasks even
    after the cap was stated in guidance — flow.py must translate that into the
    same FlowPlanError channel as every other plan-time failure, not let a bare
    ValueError escape uncaught."""
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(
                assignments=[
                    TaskAssignment(task="a", assignee="researcher"),
                    TaskAssignment(task="b", assignee="architect"),
                ]
            )
        ]
    )
    with pytest.raises(FlowPlanError, match="exceeding max_tasks"):
        await _run_flow_inner(
            "codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True, max_ops=1
        )


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


@pytest.mark.asyncio
async def test_dry_run_drops_forward_dependency(tmp_path):
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(
                assignments=[
                    TaskAssignment(task="first", assignee="researcher", depends_on=["2"]),
                    TaskAssignment(task="second", assignee="architect"),
                ]
            )
        ]
    )

    out = await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)

    assert "depends_on:" not in out


@pytest.mark.asyncio
async def test_dry_run_drops_self_non_integer_and_out_of_range_dependencies(tmp_path):
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(
                assignments=[
                    TaskAssignment(task="first", assignee="researcher", depends_on=["1"]),
                    TaskAssignment(task="second", assignee="architect", depends_on=["x", "9"]),
                ]
            )
        ]
    )

    out = await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)

    assert "depends_on:" not in out


@pytest.mark.asyncio
async def test_dry_run_rejects_dependency_cycle(tmp_path):
    orc = _FakeOrcBranch(
        [
            SimpleNamespace(
                assignments=[
                    TaskAssignment(task="first", assignee="researcher", depends_on=["2"]),
                    TaskAssignment(task="second", assignee="architect", depends_on=["1"]),
                ]
            )
        ]
    )

    with pytest.raises(FlowPlanError, match="cycle"):
        await _run_flow_inner("codex/gpt-5.5", "task", env=_env(tmp_path, orc), dry_run=True)


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
    """--workers swaps the model spec but preserves each role's cognitive modes (unlike --bare)."""
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


# ── pack routing ─────────────────────────────────────────────────────────


def _pack_env(tmp_path, orc, pack_yaml: str) -> SimpleNamespace:
    """env with bare=False and a pack loaded from a YAML string."""
    from lionagi.casts.pack import Pack

    pack_file = tmp_path / "routing.yaml"
    pack_file.write_text(pack_yaml, encoding="utf-8")
    env = _env(tmp_path, orc)
    env.bare = False
    env.pack = Pack.from_file(pack_file)
    return env


@pytest.mark.asyncio
async def test_pack_routing_shown_in_dry_run(tmp_path):
    """A pack with writer.model appears as (pack) in dry-run model resolution."""
    orc = _FakeOrcBranch(
        [SimpleNamespace(assignments=[TaskAssignment(task="draft docs", assignee="writer")])]
    )
    env = _pack_env(
        tmp_path,
        orc,
        "name: test-routing\nroles:\n  writer:\n    model: codex/codex-cheap\n",
    )
    out = await _run_flow_inner("codex/gpt-5.5", "task", env=env, dry_run=True)
    assert "writer: codex/codex-cheap (pack)" in out


@pytest.mark.asyncio
async def test_workers_override_beats_pack_routing(tmp_path):
    """--workers explicit spec overrides pack-sourced model (precedence rule)."""
    orc = _FakeOrcBranch(
        [SimpleNamespace(assignments=[TaskAssignment(task="draft docs", assignee="writer")])]
    )
    env = _pack_env(
        tmp_path,
        orc,
        "name: test-routing\nroles:\n  writer:\n    model: codex/pack-model\n",
    )
    out = await _run_flow_inner(
        "codex/gpt-5.5", "task", env=env, dry_run=True, workers_str="codex/explicit"
    )
    assert "writer: codex/explicit (workers)" in out
    assert "(pack)" not in out


def test_pack_flag_parses_via_argparse():
    """`li o flow --pack PATH` and `li o fanout --pack PATH` reach args.pack."""
    import argparse

    from lionagi.cli.orchestrate import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li")
    sub = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(sub)

    args = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task", "--pack", "/tmp/r.yaml"])
    assert args.pack == "/tmp/r.yaml"

    args2 = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task"])
    assert args2.pack is None

    args3 = parser.parse_args(["o", "fanout", "codex/gpt-5.5", "task", "--pack", "/tmp/r.yaml"])
    assert args3.pack == "/tmp/r.yaml"
