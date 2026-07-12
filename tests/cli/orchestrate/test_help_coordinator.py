# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Coordinator rung (plain Python routing, no LLM call) for
LionMessenger's 'help' event — team-mode transport for the unified help
signal, and the flow.py wiring that registers it. Fanout topology is out of
scope for this coordinator by design; it is not wired there."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    make_help_coordinator,
)
from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
from lionagi.engines import PlanningEngine
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger

from .test_flow_phases import _asyncio_coro
from .test_flow_phases import _make_env as _make_flow_env


class _FakeSession:
    def __init__(self):
        self.branches: list = []

    def include_branches(self, branch):
        self.branches.append(branch)


def _make_env(tmp_path):
    env = OrchestrationEnv(
        run=SimpleNamespace(agent_artifact_dir=lambda a: tmp_path / a),
        session=_FakeSession(),
        orc_branch=SimpleNamespace(),
        builder=SimpleNamespace(),
        orc_profile=None,
        default_model_spec="openai/gpt-4o-mini",
        bare=True,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=str(tmp_path),
    )
    return env


class TestMakeHelpCoordinator:
    def test_fyi_urgency_logs_but_does_not_touch_escalated_evidence(self, tmp_path, caplog):
        import logging

        env = _make_env(tmp_path)
        coordinator = make_help_coordinator(env)

        with caplog.at_level(logging.INFO, logger="lionagi.cli"):
            coordinator(
                name="alice", sender_id=uuid4(), reason="not sure who to ask", urgency="fyi"
            )

        assert getattr(env, "_escalated_evidence", None) is None
        assert any("help signal from alice" in r.message for r in caplog.records)

    def test_blocked_urgency_folds_into_escalated_evidence(self, tmp_path):
        env = _make_env(tmp_path)
        coordinator = make_help_coordinator(env)

        coordinator(name="bob", sender_id=uuid4(), reason="cannot proceed", urgency="blocked")

        assert env._escalated_evidence == [
            {"kind": "help_signal", "id": "bob", "label": "cannot proceed"}
        ]

    def test_blocked_urgency_appends_without_clobbering_prior_evidence(self, tmp_path):
        """The coordinator must APPEND, never overwrite — other mechanisms
        (e.g. flow's give_up escalated_operations) may already have written
        to env._escalated_evidence."""
        env = _make_env(tmp_path)
        env._escalated_evidence = [{"kind": "escalated_operation", "id": "op-1", "label": "op-1"}]
        coordinator = make_help_coordinator(env)

        coordinator(name="carol", sender_id=uuid4(), reason="stuck", urgency="blocked")

        assert env._escalated_evidence == [
            {"kind": "escalated_operation", "id": "op-1", "label": "op-1"},
            {"kind": "help_signal", "id": "carol", "label": "stuck"},
        ]

    def test_default_urgency_is_fyi_when_omitted(self, tmp_path):
        """Coordinator callback signature defaults urgency='fyi' defensively,
        matching MessengerRequest's own default."""
        env = _make_env(tmp_path)
        coordinator = make_help_coordinator(env)

        coordinator(name="dave", sender_id=uuid4(), reason="whatever")

        assert getattr(env, "_escalated_evidence", None) is None


class TestCoordinatorWiredAtMessengerConstruction:
    """The dead-.on() gap this closes: env.messenger.on('help', ...) must
    actually be called wherever LionMessenger(env.exchange) is constructed
    in flow.py — fanout.py's team topology is explicitly out of scope."""

    def test_flow_wires_help_coordinator(self):
        import inspect

        import lionagi.cli.orchestrate.flow as flow_mod

        src = inspect.getsource(flow_mod)
        assert 'env.messenger.on("help",' in src

    def test_messenger_help_action_actually_reaches_a_registered_coordinator(self, tmp_path):
        """End-to-end (below the CLI layer): a worker calling the messenger's
        'help' action reaches the coordinator built by make_help_coordinator,
        exactly as flow.py wires it."""
        env = _make_env(tmp_path)
        exchange = Exchange()
        messenger = LionMessenger(exchange)
        messenger.on("help", make_help_coordinator(env))

        branch_id = uuid4()
        exchange.register(branch_id)
        branch = SimpleNamespace(id=branch_id, msgs=SimpleNamespace(messages=[]))
        tool = messenger.bind(branch, roster={}, sender_name="worker-1")

        result = tool.func_callable(action="help", content="need authority", urgency="blocked")

        assert "sent a help signal" in result
        assert env._escalated_evidence == [
            {"kind": "help_signal", "id": "worker-1", "label": "need authority"}
        ]


class TestExecuteDagMergesHelpEvidenceWithEscalation:
    """Regression for the merge at flow.py's post-execution evidence-fold
    (~line 1042): a blocked help signal recorded via make_help_coordinator
    onto env._escalated_evidence BEFORE _execute_dag runs must survive
    alongside a normal DAG-executor escalation reported in dag_result — the
    fold must APPEND both lists, never overwrite the pre-existing one.
    Reverting that line back to a plain overwrite
    (``env._escalated_evidence = escalated_evidence``) drops the help-signal
    record recorded before the run; this test fails against that form."""

    @pytest.mark.asyncio
    async def test_preseeded_help_evidence_survives_alongside_dag_escalation(self, tmp_path):
        env = _make_flow_env(tmp_path)
        env._escalated_evidence = [{"kind": "help_signal", "id": "bob", "label": "cannot proceed"}]

        assignments = [TaskAssignment(task="x", assignee="researcher")]
        plan_result = _PlanResult(
            assignments=assignments,
            agent_ids=["researcher"],
            dep_indices=[[]],
            pool=[],
            budget_preambles={},
        )
        dag_state = _DagState(
            node_ids=["node-0"],
            known_nodes={"node-0"},
            deps_by_node={"node-0": []},
            reactive=False,
            spawn_roles=set(),
            role_base={},
            worker_models=["codex/gpt-5.5"],
        )

        fake_engine_run = MagicMock()
        fake_engine_run.run_dag = MagicMock(
            return_value=_asyncio_coro(
                {
                    "operation_results": {},
                    "spawned_operations": 0,
                    "escalated_operations": ["node-0"],
                }
            )
        )

        with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
            await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

        assert env._escalated_evidence == [
            {"kind": "help_signal", "id": "bob", "label": "cannot proceed"},
            {"kind": "escalated_operation", "id": "researcher", "label": "researcher"},
        ]
