# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Team-lifecycle wiring: `TeamLifecycleCoordinator` unit tests plus
`_execute_dag` integration (fake executor for decision-logic tests, the
real `ReactiveExecutor` for the on_op_complete wakeup-round regressions).
No real agent or LLM call anywhere — branches are stubbed."""

from __future__ import annotations

import asyncio
import importlib
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

    def test_attached_team_ignores_prior_run_done_and_wakes_only_this_runs_own_idle(self):
        """Integration reproduction of the issue: `--team-attach` reuses a
        team file whose prior run left `researcher` and `critic` both
        `done`, plus one still-unread message from that prior run addressed
        to `critic`. Wired the way `_execute_dag` wires it in production
        (`message_boundary=len(messages already on disk at attach time)`),
        the coordinator must NOT treat this fresh run's workers as already
        idle/pending before either has posted a signal of its own."""
        team_id = "attach-t"
        _make_team(team_id, ["orchestrator", "researcher", "critic"])
        team.post_done_signal(team_id, worker="researcher", summary="prior run pass 1")
        team.post_done_signal(team_id, worker="critic", summary="prior run pass 1")
        with team._locked_team(team_id) as data:
            data["messages"].append(
                {
                    "id": "hist1",
                    "from": "researcher",
                    "to": ["critic"],
                    "content": "leftover from last run",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )

        # This is what `_execute_dag` does: snapshot the message count at
        # attach time and hand it in as the new run's history boundary.
        attach_snapshot = team._load_team(team_id)
        boundary = len(attach_snapshot["messages"])

        coord = make_team_lifecycle_coordinator(
            team_id,
            ["researcher", "critic"],
            {"researcher": _FakeBranch("researcher"), "critic": _FakeBranch("critic")},
            message_boundary=boundary,
        )

        # Neither worker has done anything in THIS run yet — must read as
        # still active, not quiescent, and no round pending for critic.
        state0 = coord.check_round()
        assert state0.active_workers == frozenset({"researcher", "critic"})
        assert not state0.should_continue
        assert not state0.quiescent

        # researcher finishes its own first turn in this run.
        coord.on_done(name="researcher", sender_id=uuid4(), reason="this run pass 1")
        state1 = coord.check_round()
        # critic hasn't posted its own signal yet in this run -> still
        # active -> no premature round injected on its behalf.
        assert "critic" in state1.active_workers
        assert not state1.should_continue

        # critic now finishes its own first turn in this run too.
        coord.on_done(name="critic", sender_id=uuid4(), reason="this run pass 1")
        state2 = coord.check_round()
        # Both idle for real now -> the leftover historical mail to critic
        # (never scoped out for content messages) still surfaces as a round.
        assert state2.idle_workers == frozenset({"researcher", "critic"})
        assert state2.pending_targets == frozenset({"critic"})
        assert state2.should_continue


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

    def test_build_round_operations_stamps_assignee_and_spawn_id_for_attribution(self):
        """role_node_builder (patterns.py) stamps assignee+spawn_id on every
        reactively-injected node; flow.py's finalize-time result scan and
        checkpoint capture both key off those two fields. A round op that
        skips this stamping surfaces in agent_results as an anonymous
        "spawned"/"spawn-N" entry instead of being attributed to its worker
        and round (regression watch)."""
        _make_team("t4b", ["orchestrator", "alice"])
        alice_branch = _FakeBranch("alice")
        coord = make_team_lifecycle_coordinator(
            "t4b", ["alice"], {"alice": alice_branch}, messenger_bound={"alice": True}
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        with team._locked_team("t4b") as data:
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
        ops = coord.build_round_operations(state, prompt="original task")

        assert len(ops) == 1
        op = ops[0]
        assert op.metadata["assignee"] == "alice"
        assert op.metadata["spawn_id"] == "alice-round1"
        assert op.metadata["reference_id"] == "alice-round1"
        # assignee/spawn_id must agree with reference_id, matching
        # role_node_builder's unconditional stamp-together invariant.
        assert op.metadata["spawn_id"] == op.metadata["reference_id"]

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

    async def test_message_mid_handoff_prevents_quiescence(self, monkeypatch):
        """Mail removed from an outbox but not yet delivered must keep its
        recipient pending until the handoff completes."""
        _make_team("t11", ["orchestrator", "alice", "bob"])
        alice_branch = _FakeBranch("alice")
        bob_branch = _FakeBranch("bob")
        exchange = Exchange()
        exchange.register(alice_branch.id)
        exchange.register(bob_branch.id)
        coord = make_team_lifecycle_coordinator(
            "t11",
            ["alice", "bob"],
            {"alice": alice_branch, "bob": bob_branch},
            messenger_bound={"alice": True, "bob": True},
            exchange=exchange,
        )
        coord.on_done(name="alice", sender_id=uuid4(), reason="")
        coord.on_done(name="bob", sender_id=uuid4(), reason="")
        exchange.send(sender=bob_branch.id, recipient=alice_branch.id, content="check this")

        handoff_started = asyncio.Event()
        release_handoff = asyncio.Event()
        exchange_module = importlib.import_module("lionagi.session.exchange")
        real_gather = exchange_module.gather

        async def paused_gather(*aws, **kwargs):
            handoff_started.set()
            await release_handoff.wait()
            return await real_gather(*aws, **kwargs)

        monkeypatch.setattr(exchange_module, "gather", paused_gather)
        collect_task = asyncio.create_task(exchange.collect(bob_branch.id))
        await handoff_started.wait()

        state = coord.check_round()
        assert not state.quiescent
        assert state.should_continue
        assert state.pending_targets == frozenset({"alice"})

        release_handoff.set()
        await collect_task


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
    """Alice leaves herself mail and signals done in one turn, no sleep —
    the wakeup must fire before the real executor's task group closes."""
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

    # The round op's result must be attributed to alice/round1, not surface
    # as a generic anonymous "spawned"/"spawn-N" entry (regression watch:
    # build_round_operations must stamp assignee+spawn_id like every other
    # reactively-injected node).
    assert len(exec_result.agent_results) == 2
    round_result = exec_result.agent_results[1]
    assert round_result["spawned"] is True
    assert round_result["assignee"] == "alice"
    assert round_result["name"] == "alice"
    assert round_result["id"] == "alice-round1"
    assert round_result["response"] == "all clear now"


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


async def test_execute_dag_followup_contains_message_still_in_handoff(tmp_path, monkeypatch):
    """A worker woken by in-transit mail receives that mail in its context."""
    team_id = "e2e-real-in-flight-context"
    _make_team(team_id, ["orchestrator", "alice", "bob"])

    env = _make_env(tmp_path)
    env.team_data = {
        "id": team_id,
        "name": "e2e",
        "members": ["orchestrator", "alice", "bob"],
    }
    env.messenger = _FakeMessenger()
    exchange = Exchange()
    env.exchange = exchange

    alice_done = asyncio.Event()
    handoff_started = asyncio.Event()
    release_handoff = asyncio.Event()
    followup_contexts: list[list[dict]] = []
    collect_tasks: list[asyncio.Task] = []

    exchange_module = importlib.import_module("lionagi.session.exchange")
    real_gather = exchange_module.gather

    async def paused_gather(*aws, **kwargs):
        handoff_started.set()
        await release_handoff.wait()
        return await real_gather(*aws, **kwargs)

    monkeypatch.setattr(exchange_module, "gather", paused_gather)

    async def alice_operate(**kw):
        if not alice_done.is_set():
            team.post_done_signal(team_id, worker="alice", summary="waiting")
            alice_done.set()
            return "waiting"
        followup_contexts.append(kw["context"])
        release_handoff.set()
        team.post_done_signal(team_id, worker="alice", summary="reviewed")
        return "reviewed"

    async def bob_operate(**kw):
        await alice_done.wait()
        exchange.send(
            sender=bob_branch.id,
            recipient=alice_branch.id,
            content="please check the handoff",
        )
        collect_tasks.append(asyncio.create_task(exchange.collect(bob_branch.id)))
        await handoff_started.wait()
        team.post_done_signal(team_id, worker="bob", summary="message sent")
        return "message sent"

    async def worker_operate(**kw):
        if kw["instruction"] == "bob":
            return await bob_operate(**kw)
        return await alice_operate(**kw)

    alice_branch = _build_stub_branch(worker_operate, name="alice")
    bob_branch = _build_stub_branch(worker_operate, name="bob")
    exchange.register(alice_branch.id)
    exchange.register(bob_branch.id)

    session = Session()
    session.branches.include(alice_branch)
    session.branches.include(bob_branch)
    session.default_branch = alice_branch
    env.session = session

    graph = Graph()
    alice_node = Operation(operation="operate", parameters={"instruction": "alice"})
    alice_node.branch_id = alice_branch.id
    bob_node = Operation(operation="operate", parameters={"instruction": "bob"})
    bob_node.branch_id = bob_branch.id
    graph.add_node(alice_node)
    graph.add_node(bob_node)
    env.builder.get_graph = lambda: graph

    plan_result = _PlanResult(
        assignments=[
            TaskAssignment(task="wait for messages", assignee="researcher"),
            TaskAssignment(task="send a message", assignee="researcher"),
        ],
        agent_ids=["alice", "bob"],
        dep_indices=[[], []],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[alice_node.id, bob_node.id],
        known_nodes={alice_node.id, bob_node.id},
        deps_by_node={alice_node.id: [], bob_node.id: []},
        reactive=True,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5", "codex/gpt-5.5"],
        worker_branches={"alice": alice_branch, "bob": bob_branch},
        messenger_bound={"alice": True, "bob": True},
    )

    try:
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=2,
            max_ops=0,
            team_max_rounds=1,
        )
    finally:
        release_handoff.set()
        if collect_tasks:
            await asyncio.gather(*collect_tasks)

    assert len(followup_contexts) == 1
    prior = followup_contexts[0][1]["prior_team_messages"]
    assert prior["total_count"] == 1
    assert prior["messages"] == [{"from": "bob", "content": "please check the handoff"}]


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


async def test_execute_dag_rejects_full_wakeup_batch_without_consuming_mail(tmp_path):
    """When one spawn slot cannot wake two idle workers, neither worker's
    mail is read and neither worker is marked active."""
    team_id = "e2e-real-partial-capacity"
    _make_team(team_id, ["orchestrator", "alice", "bob"])

    env = _make_env(tmp_path)
    env.team_data = {
        "id": team_id,
        "name": "e2e",
        "members": ["orchestrator", "alice", "bob"],
    }
    env.messenger = _FakeMessenger()

    calls: dict[str, list[int]] = {"alice": [], "bob": []}
    ready = 0
    all_ready = asyncio.Event()

    async def worker_operate(worker: str, **kw):
        nonlocal ready
        calls[worker].append(len(calls[worker]))
        with team._locked_team(team_id) as data:
            data["messages"].append(
                {
                    "id": f"mail-{worker}-{len(calls[worker])}",
                    "from": "orchestrator",
                    "to": [worker],
                    "content": f"more work for {worker}",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                }
            )
        team.post_done_signal(team_id, worker=worker, summary="waiting")
        ready += 1
        if ready == 2:
            all_ready.set()
        await all_ready.wait()
        return "waiting"

    async def alice_operate(**kw):
        return await worker_operate(kw["instruction"], **kw)

    async def bob_operate(**kw):
        return await worker_operate(kw["instruction"], **kw)

    alice_branch = _build_stub_branch(alice_operate, name="alice")
    bob_branch = _build_stub_branch(bob_operate, name="bob")
    session = Session()
    session.branches.include(alice_branch)
    session.branches.include(bob_branch)
    session.default_branch = alice_branch
    env.session = session

    graph = Graph()
    alice_node = Operation(operation="operate", parameters={"instruction": "alice"})
    alice_node.branch_id = alice_branch.id
    bob_node = Operation(operation="operate", parameters={"instruction": "bob"})
    bob_node.branch_id = bob_branch.id
    graph.add_node(alice_node)
    graph.add_node(bob_node)
    env.builder.get_graph = lambda: graph

    plan_result = _PlanResult(
        assignments=[
            TaskAssignment(task="x", assignee="researcher"),
            TaskAssignment(task="y", assignee="researcher"),
        ],
        agent_ids=["alice", "bob"],
        dep_indices=[[], []],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[alice_node.id, bob_node.id],
        known_nodes={alice_node.id, bob_node.id},
        deps_by_node={alice_node.id: [], bob_node.id: []},
        reactive=True,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5", "codex/gpt-5.5"],
        worker_branches={"alice": alice_branch, "bob": bob_branch},
        messenger_bound={"alice": True, "bob": True},
    )

    await _execute_dag(env, plan_result, dag_state, max_concurrent=2, max_ops=3, team_max_rounds=2)

    assert calls == {"alice": [0], "bob": [0]}
    data = team._load_team(team_id)
    mail = [msg for msg in data["messages"] if msg["kind"] == "message"]
    assert len(mail) == 2
    assert all(msg["read_by"] == {} for msg in mail)
    assert all(msg["kind"] != "wakeup" for msg in data["messages"])
    state = team.compute_quiescence(
        data["messages"],
        worker_names=["alice", "bob"],
        rounds_run=0,
        max_rounds=2,
    )
    assert state.pending_targets == frozenset({"alice", "bob"})
    assert not state.active_workers


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
