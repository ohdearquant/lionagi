"""Unit tests for this harness's arm switch against a real DependencyAwareExecutor (ADR-0088)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from suites.steering.arms import make_steer_entry, suppress_operator_render  # noqa: E402
from suites.steering.fixture import (  # noqa: E402
    IMPLEMENT_INSTRUCTION,
    PLAN_INSTRUCTION,
    STEER_TEXT,
)

from lionagi.operations.flow import DependencyAwareExecutor  # noqa: E402
from lionagi.operations.node import create_operation  # noqa: E402
from lionagi.protocols.graph.edge import Edge  # noqa: E402
from lionagi.protocols.graph.graph import Graph  # noqa: E402
from lionagi.session.branch import Branch  # noqa: E402
from lionagi.session.session import Session  # noqa: E402


def _build_executor():
    op1 = create_operation("operate", parameters={"instruction": PLAN_INSTRUCTION})
    op2 = create_operation("operate", parameters={"instruction": IMPLEMENT_INSTRUCTION})
    graph = Graph()
    graph.add_node(op1)
    graph.add_node(op2)
    graph.add_edge(Edge(head=op1.id, tail=op2.id, label=["depends_on"]))
    executor = DependencyAwareExecutor(session=Session(), graph=graph)
    # Simulate op1 having already completed, without running a real branch.
    executor.results[op1.id] = "draft plan: write counter.py using def count_rows(path): ..."
    executor.operation_branches[op2.id] = Branch()
    return executor, op1, op2


def test_arm0_no_steer_injects_nothing():
    executor, _op1, op2 = _build_executor()
    executor._prepare_operation(op2)
    ctx = op2.parameters.get("context") or {}
    assert "operator_messages" not in ctx
    assert op2.parameters["instruction"] == IMPLEMENT_INSTRUCTION
    assert "rendered_into_op" not in op2.metadata


def test_arm1_steer_buried_survives_unrendered():
    executor, _op1, op2 = _build_executor()
    executor.context.content["operator_messages"] = [make_steer_entry()]
    with suppress_operator_render():
        executor._prepare_operation(op2)
    ctx = op2.parameters.get("context") or {}
    assert "operator_messages" in ctx
    assert ctx["operator_messages"][0]["text"] == STEER_TEXT
    assert "[OPERATOR STEER]" not in op2.parameters["instruction"]
    assert op2.parameters["instruction"] == IMPLEMENT_INSTRUCTION
    assert "rendered_into_op" not in op2.metadata


def test_arm2_steer_rendered_lifts_into_instruction():
    executor, _op1, op2 = _build_executor()
    executor.context.content["operator_messages"] = [make_steer_entry()]
    executor._prepare_operation(op2)
    ctx = op2.parameters.get("context") or {}
    assert "operator_messages" not in ctx
    assert "[OPERATOR STEER]" in op2.parameters["instruction"]
    assert STEER_TEXT in op2.parameters["instruction"]
    assert op2.metadata["rendered_into_op"] == str(op2.id)


def test_suppress_operator_render_restores_original_after_context_exit():
    import importlib

    flow_module = importlib.import_module("lionagi.operations.flow")

    original = flow_module._render_operator_messages
    with suppress_operator_render():
        assert flow_module._render_operator_messages is not original
    assert flow_module._render_operator_messages is original
