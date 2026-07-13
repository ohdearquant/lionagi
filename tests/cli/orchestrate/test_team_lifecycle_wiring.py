# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Team-lifecycle wiring: `TeamLifecycleCoordinator` (mirrors
`make_help_coordinator`'s test shape in `test_help_coordinator.py`) and the
`_execute_dag` integration that polls it for wakeup rounds (mirrors
`test_control_poller_lifecycle.py`'s fake-run_dag pattern for the control
poller). Covers requirement (a) done-signal-as-code, (b) bounded wakeup
rounds, and (c) quiescence termination end to end with fake workers — no
real agent or LLM call anywhere in this file."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli import team
from lionagi.cli.orchestrate import flow as _flow
from lionagi.cli.orchestrate._orchestration import make_team_lifecycle_coordinator
from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
from lionagi.engines import PlanningEngine

from .test_flow_phases import _FakeBranch, _make_env


@pytest.fixture(autouse=True)
def _teams_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(team, "TEAMS_DIR", tmp_path / "teams")
    return tmp_path / "teams"


def _make_team(team_id: str, members: list[str]) -> None:
    path = team._teams_dir() / f"{team_id}.json"
    path.write_text(json.dumps({"id": team_id, "name": "t", "members": members, "messages": []}))


class _FakeMessenger:
    """Records `.on(event, callback)` registrations without any Exchange."""

    def __init__(self):
        self.callbacks: dict[str, object] = {}

    def on(self, event, callback):
        self.callbacks[event] = callback


class _FakeExecutor:
    """Minimal ReactiveExecutor stand-in: records every injected Operation."""

    def __init__(self):
        self.injected: list = []

    def inject(self, operation, *, after=None, independent=False):
        self.injected.append(operation)
        return True


# ── TeamLifecycleCoordinator: unit-level (no _execute_dag involved) ────────


class TestTeamLifecycleCoordinatorOnDoneOnFinished:
    def test_on_done_writes_a_structured_done_signal(self):
        _make_team("t1", ["orchestrator", "alice"])
        coord = make_team_lifecycle_coordinator("t1", ["alice"], {"alice": _FakeBranch("alice")})

        coord.on_done(name="alice", sender_id=uuid4(), reason="first pass complete")

        data = team._load_team("t1")
        assert len(data["messages"]) == 1
        msg = data["messages"][0]
        assert msg["kind"] == "done"
        assert msg["from"] == "alice"
        assert msg["content"] == "first pass complete"

    def test_on_finished_writes_a_finished_signal(self):
        _make_team("t2", ["orchestrator", "alice"])
        coord = make_team_lifecycle_coordinator("t2", ["alice"], {"alice": _FakeBranch("alice")})

        coord.on_finished(name="alice", sender_id=uuid4(), reason="permanently done")

        data = team._load_team("t2")
        assert data["messages"][0]["kind"] == "finished"

    def test_on_done_swallows_missing_team_rather_than_crashing_the_run(self):
        """A worker's tool call must never propagate a team-file error back
        up through the messenger callback into the run itself."""
        coord = make_team_lifecycle_coordinator(
            "nonexistent-team", ["alice"], {"alice": _FakeBranch("alice")}
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="x")  # must not raise


class TestTeamLifecycleCoordinatorCheckRound:
    def test_check_round_reflects_current_team_file_state(self):
        _make_team("t3", ["orchestrator", "alice", "bob"])
        coord = make_team_lifecycle_coordinator(
            "t3", ["alice", "bob"], {"alice": _FakeBranch("alice"), "bob": _FakeBranch("bob")}
        )

        state0 = coord.check_round()
        assert not state0.quiescent  # nobody done yet

        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        coord.on_done(name="bob", sender_id=uuid4(), reason="")
        state1 = coord.check_round()
        assert state1.quiescent  # both done, no pending mail


class TestTeamLifecycleCoordinatorBuildRoundOperations:
    def test_build_round_operations_targets_the_workers_own_branch(self):
        _make_team("t4", ["orchestrator", "alice"])
        alice_branch = _FakeBranch("alice")
        coord = make_team_lifecycle_coordinator(
            "t4", ["alice"], {"alice": alice_branch}, messenger_bound={"alice": True}
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        # Unread mail waiting for alice.
        with team._locked_team("t4") as data:
            data["messages"].append(
                {
                    "id": "m1",
                    "from": "bob",
                    "to": ["alice"],
                    "content": "come back",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )

        state = coord.check_round()
        assert state.should_continue
        ops = coord.build_round_operations(state, prompt="original task")

        assert len(ops) == 1
        op = ops[0]
        assert op.branch_id == alice_branch.id
        assert op.request["actions"] is True
        ctx = op.request["context"]
        assert ctx[0] == {"original_task": "original task"}
        prior = ctx[1]["prior_team_messages"]
        assert prior["total_count"] == 1
        assert prior["messages"] == [{"from": "bob", "content": "come back"}]

    def test_build_round_operations_omits_actions_for_non_messenger_worker(self):
        _make_team("t5", ["orchestrator", "cli-worker"])
        branch = _FakeBranch("cli-worker")
        coord = make_team_lifecycle_coordinator(
            "t5", ["cli-worker"], {"cli-worker": branch}, messenger_bound={"cli-worker": False}
        )
        coord.on_done(name="cli-worker", sender_id=uuid4(), reason="")
        with team._locked_team("t5") as data:
            data["messages"].append(
                {
                    "id": "m1",
                    "from": "orchestrator",
                    "to": ["cli-worker"],
                    "content": "more to do",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )

        state = coord.check_round()
        ops = coord.build_round_operations(state, prompt="task")
        assert "actions" not in ops[0].request

    def test_build_round_operations_increments_rounds_run_once_per_batch(self):
        _make_team("t6", ["orchestrator", "alice", "bob"])
        coord = make_team_lifecycle_coordinator(
            "t6", ["alice", "bob"], {"alice": _FakeBranch("alice"), "bob": _FakeBranch("bob")}
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        coord.on_done(name="bob", sender_id=uuid4(), reason="")
        with team._locked_team("t6") as data:
            data["messages"].append(
                {
                    "id": "m1",
                    "from": "orchestrator",
                    "to": ["*"],
                    "content": "go",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )

        state = coord.check_round()
        ops = coord.build_round_operations(state, prompt="task")
        assert len(ops) == 2  # both alice and bob had pending broadcast mail
        assert coord.rounds_run == 1

    def test_build_round_operations_posts_a_wakeup_signal_so_the_round_is_not_repeated(self):
        _make_team("t7", ["orchestrator", "alice"])
        coord = make_team_lifecycle_coordinator("t7", ["alice"], {"alice": _FakeBranch("alice")})
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        with team._locked_team("t7") as data:
            data["messages"].append(
                {
                    "id": "m1",
                    "from": "orchestrator",
                    "to": ["alice"],
                    "content": "go",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )

        state = coord.check_round()
        coord.build_round_operations(state, prompt="task")

        # A second read, before alice posts done again, must not re-trigger
        # (she's active again thanks to the coordinator's own wakeup post).
        state2 = coord.check_round()
        assert not state2.should_continue
        assert "alice" in state2.active_workers


# ── Source-level wiring guard (mirrors TestCoordinatorWiredAtMessengerConstruction) ──


def test_flow_wires_team_lifecycle_done_and_finished_callbacks():
    import inspect

    src = inspect.getsource(_flow)
    assert "_team_coordinator.on_done" in src
    assert "_team_coordinator.on_finished" in src


# ── _execute_dag integration: real polling loop against a fake executor ────


def _plan_and_dag(agent_id: str, *, worker_branches=None, messenger_bound=None):
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=[agent_id],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[agent_id],
        known_nodes={agent_id},
        deps_by_node={agent_id: []},
        reactive=True,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
        worker_branches=worker_branches or {},
        messenger_bound=messenger_bound or {},
    )
    return plan_result, dag_state


async def test_execute_dag_wakes_idle_worker_with_pending_mail_then_quiesces(tmp_path, monkeypatch):
    """End-to-end (fake workers, no LLM): alice signals done, a teammate
    leaves her a message, the coordinator wakes her with one injected
    Operation carrying prior_team_messages, and the run settles once she
    signals done again with nothing left pending."""
    monkeypatch.setattr(_flow, "_TEAM_POLL_INTERVAL", 0.02)

    team_id = "e2e-team"
    _make_team(team_id, ["orchestrator", "alice"])

    env = _make_env(tmp_path)
    env.team_data = {"id": team_id, "name": "e2e", "members": ["orchestrator", "alice"]}
    env.messenger = _FakeMessenger()

    alice_branch = _FakeBranch("alice")
    plan_result, dag_state = _plan_and_dag(
        "alice",
        worker_branches={"alice": alice_branch},
        messenger_bound={"alice": True},
    )

    fake_executor = _FakeExecutor()

    async def _fake_run_dag(graph, *, executor_ref, **_kw):
        executor_ref["executor"] = fake_executor
        # Turn 1: alice finishes and signals done.
        team.post_done_signal(team_id, worker="alice", summary="first pass done")
        # A teammate leaves her something before she's revived.
        with team._locked_team(team_id) as data:
            data["messages"].append(
                {
                    "id": "m1",
                    "from": "orchestrator",
                    "to": ["alice"],
                    "content": "please double-check section 2",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )
        await asyncio.sleep(0.12)  # several poll ticks — the round must fire
        assert len(fake_executor.injected) == 1
        # Turn 2 (simulated): alice signals done again with nothing pending.
        team.post_done_signal(team_id, worker="alice", summary="all clear now")
        await asyncio.sleep(0.08)  # let the loop observe quiescence and stop
        return {"operation_results": {"alice": "ok"}, "spawned_operations": 0}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _fake_run_dag

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(
            env, plan_result, dag_state, max_concurrent=1, max_ops=0, team_max_rounds=2
        )

    assert exec_result.agent_results[0]["response"] == "ok"
    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    assert injected.branch_id == alice_branch.id
    assert injected.request["actions"] is True
    prior = injected.request["context"][1]["prior_team_messages"]
    assert prior["messages"] == [
        {"from": "orchestrator", "content": "please double-check section 2"}
    ]


async def test_execute_dag_respects_team_max_rounds_bound(tmp_path, monkeypatch):
    """Teammate keeps leaving new mail every turn — without a bound this
    would loop forever; team_max_rounds=1 caps it at exactly one round."""
    monkeypatch.setattr(_flow, "_TEAM_POLL_INTERVAL", 0.02)

    team_id = "e2e-bounded"
    _make_team(team_id, ["orchestrator", "alice"])

    env = _make_env(tmp_path)
    env.team_data = {"id": team_id, "name": "e2e", "members": ["orchestrator", "alice"]}
    env.messenger = _FakeMessenger()

    alice_branch = _FakeBranch("alice")
    plan_result, dag_state = _plan_and_dag(
        "alice", worker_branches={"alice": alice_branch}, messenger_bound={"alice": True}
    )
    fake_executor = _FakeExecutor()

    def _leave_mail(n: int) -> None:
        with team._locked_team(team_id) as data:
            data["messages"].append(
                {
                    "id": f"m{n}",
                    "from": "orchestrator",
                    "to": ["alice"],
                    "content": f"round {n} note",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )

    async def _fake_run_dag(graph, *, executor_ref, **_kw):
        executor_ref["executor"] = fake_executor
        team.post_done_signal(team_id, worker="alice", summary="pass 1")
        _leave_mail(1)
        await asyncio.sleep(0.1)  # round 1 fires
        team.post_done_signal(team_id, worker="alice", summary="pass 2")
        _leave_mail(2)
        await asyncio.sleep(0.1)  # round budget exhausted — must NOT fire again
        return {"operation_results": {"alice": "ok"}, "spawned_operations": 0}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _fake_run_dag

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(
            env, plan_result, dag_state, max_concurrent=1, max_ops=0, team_max_rounds=1
        )

    assert len(fake_executor.injected) == 1


async def test_execute_dag_team_task_cancelled_cleanly_at_run_end(tmp_path, monkeypatch):
    """No leaked polling task once _execute_dag returns, mirroring
    test_ctl_task_and_hb_task_are_cancelled_cleanly_at_run_end."""
    monkeypatch.setattr(_flow, "_TEAM_POLL_INTERVAL", 0.02)

    team_id = "e2e-cleanup"
    _make_team(team_id, ["orchestrator", "alice"])

    env = _make_env(tmp_path)
    env.team_data = {"id": team_id, "name": "e2e", "members": ["orchestrator", "alice"]}
    env.messenger = _FakeMessenger()

    alice_branch = _FakeBranch("alice")
    plan_result, dag_state = _plan_and_dag(
        "alice", worker_branches={"alice": alice_branch}, messenger_bound={"alice": True}
    )
    fake_executor = _FakeExecutor()

    async def _fake_run_dag(graph, *, executor_ref, **_kw):
        executor_ref["executor"] = fake_executor
        await asyncio.sleep(0.05)
        return {"operation_results": {"alice": "ok"}, "spawned_operations": 0}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _fake_run_dag

    tasks_before = asyncio.all_tasks()
    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    leaked = asyncio.all_tasks() - tasks_before
    assert leaked == set()


async def test_execute_dag_without_team_data_never_constructs_a_coordinator(tmp_path):
    """Team mode inactive: no coordinator wired, no polling task started —
    a plain flow run must be completely unaffected by this feature."""
    env = _make_env(tmp_path)  # team_data defaults to None
    plan_result, dag_state = _plan_and_dag("solo")
    dag_state.reactive = False

    fake_executor = _FakeExecutor()

    async def _fake_run_dag(graph, *, executor_ref, **_kw):
        executor_ref["executor"] = fake_executor
        return {"operation_results": {"solo": "ok"}, "spawned_operations": 0}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _fake_run_dag

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert exec_result.agent_results[0]["response"] == "ok"
    assert fake_executor.injected == []
