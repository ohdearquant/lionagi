# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Team-lifecycle wiring: `TeamLifecycleCoordinator` unit tests plus
`_execute_dag` integration (fake executor for decision-logic tests, the
real `ReactiveExecutor` for the on_op_complete wakeup-round regressions).
No real agent or LLM call anywhere — branches are stubbed."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli import team
from lionagi.cli.orchestrate import flow as _flow
from lionagi.cli.orchestrate._orchestration import make_team_lifecycle_coordinator
from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
from lionagi.engines import PlanningEngine
from lionagi.operations.node import Operation
from lionagi.protocols.graph.graph import Graph
from lionagi.session.exchange import Exchange
from lionagi.session.session import Session

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


class TestTeamLifecycleCoordinatorExchangeUnion:
    async def test_exchange_only_mail_revives_a_worker_the_file_inbox_cannot_see(self):
        """Alice done -> bob messenger-sends alice (Exchange only, never the
        team file) -> bob done -> alice must still be revived."""
        _make_team("t8", ["orchestrator", "alice", "bob"])
        alice_branch = _FakeBranch("alice")
        bob_branch = _FakeBranch("bob")
        exchange = Exchange()
        exchange.register(alice_branch.id)
        exchange.register(bob_branch.id)
        coord = make_team_lifecycle_coordinator(
            "t8",
            ["alice", "bob"],
            {"alice": alice_branch, "bob": bob_branch},
            messenger_bound={"alice": True, "bob": True},
            exchange=exchange,
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        exchange.send(sender=bob_branch.id, recipient=alice_branch.id, content="see this")
        await exchange.collect(bob_branch.id)
        coord.on_done(name="bob", sender_id=uuid4(), reason="")

        state = coord.check_round()
        assert state.should_continue
        assert "alice" in state.pending_targets

        ops = coord.build_round_operations(state, prompt="task")
        assert len(ops) == 1
        prior = ops[0].request["context"][1]["prior_team_messages"]
        assert prior["messages"] == [{"from": "bob", "content": "see this"}]

        # Consumed — a second read must not re-trigger on the same mail.
        state2 = coord.check_round()
        assert "alice" not in state2.pending_targets

    async def test_exchange_and_file_mail_are_both_folded_into_one_round(self):
        _make_team("t9", ["orchestrator", "alice", "bob"])
        alice_branch = _FakeBranch("alice")
        bob_branch = _FakeBranch("bob")
        exchange = Exchange()
        exchange.register(alice_branch.id)
        exchange.register(bob_branch.id)
        coord = make_team_lifecycle_coordinator(
            "t9",
            ["alice", "bob"],
            {"alice": alice_branch, "bob": bob_branch},
            messenger_bound={"alice": True, "bob": True},
            exchange=exchange,
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        with team._locked_team("t9") as data:
            data["messages"].append(
                {
                    "id": "m1",
                    "from": "orchestrator",
                    "to": ["alice"],
                    "content": "file note",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )
        exchange.send(sender=bob_branch.id, recipient=alice_branch.id, content="exchange note")
        await exchange.collect(bob_branch.id)
        coord.on_done(name="bob", sender_id=uuid4(), reason="")

        state = coord.check_round()
        ops = coord.build_round_operations(state, prompt="task")
        prior = ops[0].request["context"][1]["prior_team_messages"]
        assert prior["total_count"] == 2
        contents = {m["content"] for m in prior["messages"]}
        assert contents == {"file note", "exchange note"}

    async def test_no_exchange_configured_falls_back_to_file_inbox_only(self):
        """exchange=None (CLI-only team) must behave exactly as before —
        no AttributeError, file-inbox quiescence unaffected."""
        _make_team("t10", ["orchestrator", "alice"])
        coord = make_team_lifecycle_coordinator(
            "t10", ["alice"], {"alice": _FakeBranch("alice")}, messenger_bound={"alice": True}
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        state = coord.check_round()
        assert state.quiescent
        assert not state.should_continue


# ── Source-level wiring guard (mirrors TestCoordinatorWiredAtMessengerConstruction) ──


def test_flow_wires_team_lifecycle_done_and_finished_callbacks():
    import inspect

    src = inspect.getsource(_flow)
    assert "_team_coordinator.on_done" in src
    assert "_team_coordinator.on_finished" in src


# ── _execute_dag integration against a fake executor (decision-logic only) ──


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


# ── _execute_dag integration against the REAL ReactiveExecutor ─────────────
# No fake executor: PlanningEngine.new_run is unpatched, so eng_run.run_dag
# drives the real ReactiveExecutor. Only branch.operate is stubbed.


def _build_stub_branch(operate_fn, name: str = "alice") -> MagicMock:
    """MagicMock branch whose "operate" resolves via operate_fn; .clone()
    rebuilds fresh, sharing the same operate_fn closure (the executor
    always clones an injected op's branch). ``name`` must be a real string —
    the executor's NodeStarted/NodeCompleted signals pydantic-validate it."""

    def _build() -> MagicMock:
        b = MagicMock()
        b.id = uuid4()
        b.name = name
        b._message_manager = MagicMock()
        b._message_manager.pile = MagicMock()
        b._message_manager.pile.clear = MagicMock()
        b.metadata = {}
        b.operate = AsyncMock(side_effect=operate_fn)
        b.get_operation = MagicMock(side_effect=lambda name: {"operate": b.operate}.get(name))
        b.clone = MagicMock(side_effect=lambda sender=None: _build())
        return b

    return _build()


def _plan_and_dag_real(
    node: Operation, agent_id: str, *, worker_branches=None, messenger_bound=None
):
    """Like _plan_and_dag, but node_ids key off *node*'s real UUID — the
    real executor's operation_results is keyed by Operation.id."""
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=[agent_id],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[node.id],
        known_nodes={node.id},
        deps_by_node={node.id: []},
        reactive=True,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
        worker_branches=worker_branches or {},
        messenger_bound=messenger_bound or {},
    )
    return plan_result, dag_state


async def test_execute_dag_real_executor_wakes_alice_before_task_group_closes(tmp_path):
    """Finding-1 regression: alice leaves herself mail and signals done in
    one turn, no sleep — the wakeup round must still fire against the real
    executor, with no poll loop left to race."""
    team_id = "e2e-real-team"
    _make_team(team_id, ["orchestrator", "alice"])

    env = _make_env(tmp_path)
    env.team_data = {"id": team_id, "name": "e2e", "members": ["orchestrator", "alice"]}
    env.messenger = _FakeMessenger()

    calls: list[int] = []

    async def alice_operate(**kw):
        turn = len(calls)
        calls.append(turn)
        if turn == 0:
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
            team.post_done_signal(team_id, worker="alice", summary="first pass done")
            return "first pass done"
        team.post_done_signal(team_id, worker="alice", summary="all clear now")
        return "all clear now"

    alice_branch = _build_stub_branch(alice_operate)

    session = Session()
    session.default_branch = alice_branch
    env.session = session

    graph = Graph()
    node = Operation(operation="operate", parameters={"instruction": "do the work"})
    node.branch_id = alice_branch.id
    graph.add_node(node)
    env.builder.get_graph = lambda: graph

    plan_result, dag_state = _plan_and_dag_real(
        node, "alice", worker_branches={"alice": alice_branch}, messenger_bound={"alice": True}
    )

    exec_result = await _execute_dag(
        env, plan_result, dag_state, max_concurrent=1, max_ops=0, team_max_rounds=2
    )

    # The round-injected op actually ran — proves inject() was not rejected.
    assert len(calls) == 2
    assert exec_result.agent_results[0]["response"] == "first pass done"


async def test_execute_dag_real_executor_respects_team_max_rounds_bound(tmp_path):
    """Alice leaves new mail every turn; team_max_rounds=1 caps it at one
    injected round against the real executor."""
    team_id = "e2e-real-bounded"
    _make_team(team_id, ["orchestrator", "alice"])

    env = _make_env(tmp_path)
    env.team_data = {"id": team_id, "name": "e2e", "members": ["orchestrator", "alice"]}
    env.messenger = _FakeMessenger()

    calls: list[int] = []

    async def alice_operate(**kw):
        turn = len(calls)
        calls.append(turn)
        with team._locked_team(team_id) as data:
            data["messages"].append(
                {
                    "id": f"m{turn}",
                    "from": "orchestrator",
                    "to": ["alice"],
                    "content": f"round {turn} note",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )
        team.post_done_signal(team_id, worker="alice", summary=f"pass {turn}")
        return f"pass {turn}"

    alice_branch = _build_stub_branch(alice_operate)

    session = Session()
    session.default_branch = alice_branch
    env.session = session

    graph = Graph()
    node = Operation(operation="operate", parameters={"instruction": "do the work"})
    node.branch_id = alice_branch.id
    graph.add_node(node)
    env.builder.get_graph = lambda: graph

    plan_result, dag_state = _plan_and_dag_real(
        node, "alice", worker_branches={"alice": alice_branch}, messenger_bound={"alice": True}
    )

    await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0, team_max_rounds=1)

    assert len(calls) == 2  # initial turn + exactly one bounded round


async def test_execute_dag_real_executor_rejected_injection_does_not_crash_the_run(tmp_path):
    """max_ops=1 zeroes the executor's spawn budget, so the real
    ReactiveExecutor rejects the injected round op — the run must still
    finish cleanly with no leaked task."""
    team_id = "e2e-real-rejected"
    _make_team(team_id, ["orchestrator", "alice"])

    env = _make_env(tmp_path)
    env.team_data = {"id": team_id, "name": "e2e", "members": ["orchestrator", "alice"]}
    env.messenger = _FakeMessenger()

    calls: list[int] = []

    async def alice_operate(**kw):
        turn = len(calls)
        calls.append(turn)
        with team._locked_team(team_id) as data:
            data["messages"].append(
                {
                    "id": "m1",
                    "from": "orchestrator",
                    "to": ["alice"],
                    "content": "one more thing",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )
        team.post_done_signal(team_id, worker="alice", summary=f"pass {turn}")
        return f"pass {turn}"

    alice_branch = _build_stub_branch(alice_operate)

    session = Session()
    session.default_branch = alice_branch
    env.session = session

    graph = Graph()
    node = Operation(operation="operate", parameters={"instruction": "do the work"})
    node.branch_id = alice_branch.id
    graph.add_node(node)
    env.builder.get_graph = lambda: graph

    plan_result, dag_state = _plan_and_dag_real(
        node, "alice", worker_branches={"alice": alice_branch}, messenger_bound={"alice": True}
    )

    tasks_before = asyncio.all_tasks()
    exec_result = await _execute_dag(
        env, plan_result, dag_state, max_concurrent=1, max_ops=1, team_max_rounds=2
    )

    # Rejected: max_ops=1 leaves a zero spawn budget, so the coordinator's
    # attempted round never actually ran alice a second time.
    assert len(calls) == 1
    assert exec_result.agent_results[0]["response"] == "pass 0"
    leaked = asyncio.all_tasks() - tasks_before
    assert leaked == set()


async def test_check_round_sees_undelivered_outbox_mail_without_a_pre_collect(tmp_path):
    """Bob's last turn sends to alice via the Exchange and never awaits
    collect(); neither does this test. A real ReactiveExecutor is
    constructed and stepped by hand (bypassing flow()'s Session/Pile branch
    resolution and _execute_dag's background sync task, both of which
    would mask the race) so check_round's own force-delivery is the only
    thing that can surface bob's mail before alice's terminal check."""
    from lionagi.operations.flow import ReactiveExecutor

    team_id = "e2e-real-outbox-race"
    _make_team(team_id, ["orchestrator", "alice", "bob"])

    exchange = Exchange()
    calls: dict[str, list] = {"alice": [], "bob": []}

    async def alice_operate(**kw):
        turn = len(calls["alice"])
        calls["alice"].append(turn)
        if turn == 0:
            team.post_done_signal(team_id, worker="alice", summary="alice done")
            return "alice done"
        return "alice woke"

    async def bob_operate(**kw):
        calls["bob"].append(len(calls["bob"]))
        exchange.send(sender=bob_branch.id, recipient=alice_branch.id, content="check this")
        team.post_done_signal(team_id, worker="bob", summary="bob done")
        return "bob done"

    alice_branch = _build_stub_branch(alice_operate, name="alice")
    bob_branch = _build_stub_branch(bob_operate, name="bob")
    exchange.register(alice_branch.id)
    exchange.register(bob_branch.id)

    session = Session()
    session.default_branch = alice_branch

    graph = Graph()
    alice_node = Operation(operation="operate", parameters={"instruction": "go"})
    alice_node.branch_id = alice_branch.id
    bob_node = Operation(operation="operate", parameters={"instruction": "go"})
    bob_node.branch_id = bob_branch.id
    graph.add_node(alice_node)
    graph.add_node(bob_node)

    coord = make_team_lifecycle_coordinator(
        team_id,
        ["alice", "bob"],
        {"alice": alice_branch, "bob": bob_branch},
        messenger_bound={"alice": True, "bob": True},
        exchange=exchange,
    )
    executor_ref: dict[str, Any] = {}

    def on_op_complete(node):
        state = coord.check_round()
        if not state.should_continue:
            return
        for op in coord.build_round_operations(state, prompt="task"):
            executor_ref["executor"].inject(op, independent=True)

    executor = ReactiveExecutor(
        session=session,
        graph=graph,
        max_concurrent=2,
        default_branch=alice_branch,
        executor_ref=executor_ref,
        on_op_complete=on_op_complete,
    )
    # A MagicMock never round-trips through the real Session.branches
    # Pile, so route each node to its own stub branch directly.
    executor.operation_branches[alice_node.id] = alice_branch
    executor.operation_branches[bob_node.id] = bob_branch

    await executor.execute()

    # Alice must be woken by a round carrying bob's message.
    assert len(calls["bob"]) == 1
    assert len(calls["alice"]) == 2


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
