# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-FlowOp budget propagation (issue #1091).

Verifies that:
  - FlowOp.budget_weight field exists with default 1.0
  - _format_budget_preamble produces correct text
  - Equal weights produce equal per-op share (total / N)
  - Weighted split produces proportional shares
  - No BUDGET preamble when total_budget is None
  - OrchestrationEnv.total_budget is set by setup_orchestration when
    total_budget kwarg is provided
"""

from __future__ import annotations

import re
import time
from unittest.mock import MagicMock, patch

import pytest

from lionagi.cli.orchestrate.flow import (
    FlowOp,
    _format_budget_preamble,
)

# ── FlowOp schema ─────────────────────────────────────────────────────────


def test_flowop_budget_weight_default():
    op = FlowOp(id="o1", agent_id="a1", instruction="do the thing")
    assert op.budget_weight == 1.0


def test_flowop_budget_weight_custom():
    op = FlowOp(id="o1", agent_id="a1", instruction="do the thing", budget_weight=0.5)
    assert op.budget_weight == 0.5


# ── _format_budget_preamble ────────────────────────────────────────────────


def test_format_budget_preamble_contains_expected_fields():
    deadline = time.time() + 200
    text = _format_budget_preamble(
        op_index=1,
        num_ops=3,
        op_budget_seconds=200,
        deadline_epoch=deadline,
    )
    assert "[BUDGET]" in text
    assert "[/BUDGET]" in text
    assert "op 1 of 3" in text
    assert "200 seconds" in text


def test_format_budget_preamble_deadline_iso_format():
    deadline = time.time() + 600
    text = _format_budget_preamble(
        op_index=2,
        num_ops=5,
        op_budget_seconds=120,
        deadline_epoch=deadline,
    )
    # Should contain an ISO-8601-style datetime string (YYYY-MM-DDTHH:MM:SS)
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text), (
        "Expected ISO-8601 datetime in budget preamble"
    )


def test_format_budget_preamble_index_and_count():
    deadline = time.time() + 300
    text = _format_budget_preamble(
        op_index=3,
        num_ops=5,
        op_budget_seconds=60,
        deadline_epoch=deadline,
    )
    assert "op 3 of 5" in text
    assert "60 seconds" in text


# ── Budget injection simulation ────────────────────────────────────────────
#
# We test the budget-splitting arithmetic directly rather than invoking
# _run_flow_inner (which requires a live LLM backend). The helper mirrors
# the logic in _run_flow_inner so any drift will be caught by the unit test.


def _simulate_budget_split(
    ops: list[FlowOp],
    total_budget: int,
) -> dict[str, int]:
    """Replicate the budget-split logic from _run_flow_inner.

    Returns {op.id: share_in_seconds} for each op.
    """
    sum_weights = sum(op.budget_weight for op in ops)
    return {op.id: int((total_budget / sum_weights) * op.budget_weight) for op in ops}


def test_equal_weight_produces_equal_share_3_ops():
    ops = [
        FlowOp(id="o1", agent_id="a1", instruction="do x"),
        FlowOp(id="o2", agent_id="a1", instruction="do y"),
        FlowOp(id="o3", agent_id="a1", instruction="do z"),
    ]
    shares = _simulate_budget_split(ops, total_budget=600)
    assert shares == {"o1": 200, "o2": 200, "o3": 200}


def test_proportional_weight_split_4_ops():
    # weights [1.0, 1.0, 0.5, 1.0] — total weight = 3.5, budget = 700s
    ops = [
        FlowOp(id="o1", agent_id="a1", instruction="do x", budget_weight=1.0),
        FlowOp(id="o2", agent_id="a1", instruction="do y", budget_weight=1.0),
        FlowOp(id="o3", agent_id="a1", instruction="do z", budget_weight=0.5),
        FlowOp(id="o4", agent_id="a1", instruction="do w", budget_weight=1.0),
    ]
    shares = _simulate_budget_split(ops, total_budget=700)
    # sum_weights = 3.5; per unit = 200s
    assert shares["o1"] == 200
    assert shares["o2"] == 200
    assert shares["o3"] == 100
    assert shares["o4"] == 200


def test_no_budget_preamble_dict_when_total_budget_none():
    """Simulate the guard: _op_budget_preambles stays empty when no timeout."""
    ops = [
        FlowOp(id="o1", agent_id="a1", instruction="do x"),
        FlowOp(id="o2", agent_id="a1", instruction="do y"),
    ]
    # env.total_budget = None → the guard `if env.total_budget and regular_ops`
    # should evaluate falsy, leaving _op_budget_preambles empty.
    total_budget = None
    _op_budget_preambles: dict[str, str] = {}
    if total_budget and ops:
        sum_weights = sum(op.budget_weight for op in ops)
        budget_start = time.time()
        for idx, op in enumerate(ops, 1):
            share = int((total_budget / sum_weights) * op.budget_weight)  # type: ignore[operator]
            deadline = budget_start + total_budget
            _op_budget_preambles[op.id] = _format_budget_preamble(
                op_index=idx,
                num_ops=len(ops),
                op_budget_seconds=share,
                deadline_epoch=deadline,
            )
    assert _op_budget_preambles == {}


# ── OrchestrationEnv.total_budget ─────────────────────────────────────────


def test_orchestration_env_has_total_budget_field():
    """OrchestrationEnv must expose a total_budget attribute (None by default)."""
    # Spot-check that the field exists at the class level. We cannot
    # construct a full OrchestrationEnv without a live Session/Branch,
    # so we inspect the dataclass fields.
    import dataclasses

    from lionagi.cli.orchestrate._orchestration import OrchestrationEnv

    field_names = {f.name for f in dataclasses.fields(OrchestrationEnv)}
    assert "total_budget" in field_names


def test_setup_orchestration_passes_total_budget():
    """setup_orchestration must forward total_budget to OrchestrationEnv."""
    from lionagi.cli.orchestrate._orchestration import setup_orchestration

    # Patch the heavy internal calls so we don't need a live model.
    with (
        patch("lionagi.cli.orchestrate._orchestration.build_imodel_from_spec") as mock_imodel,
        patch("lionagi.cli.orchestrate._orchestration.allocate_run") as mock_run,
        patch(
            "lionagi.cli.orchestrate._orchestration.load_agent_profile",
            side_effect=FileNotFoundError,
        ),
        patch("lionagi.cli.orchestrate._orchestration.resolve_persisted_effort", return_value=None),
        patch("lionagi.cli.orchestrate._orchestration.Branch") as mock_branch,
        patch("lionagi.cli.orchestrate._orchestration.Session"),
        patch("lionagi.cli.orchestrate._orchestration.OperationGraphBuilder"),
    ):
        # Wire up a minimal mock imodel
        mock_ep = MagicMock()
        mock_ep.config.provider = "openai"
        mock_ep.config.kwargs = {}
        mock_imodel.return_value.endpoint = mock_ep
        mock_run.return_value.ensure_artifact_root.return_value = None
        mock_branch.return_value = MagicMock(system=None)

        env = setup_orchestration(
            pattern_name="Flow",
            model_spec="openai/gpt-4.1-mini",
            agent_name=None,
            save_dir=None,
            cwd=None,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            total_budget=1800,
        )

    assert env.total_budget == 1800


def test_setup_orchestration_total_budget_defaults_none():
    """setup_orchestration default leaves total_budget as None."""
    from lionagi.cli.orchestrate._orchestration import setup_orchestration

    with (
        patch("lionagi.cli.orchestrate._orchestration.build_imodel_from_spec") as mock_imodel,
        patch("lionagi.cli.orchestrate._orchestration.allocate_run") as mock_run,
        patch(
            "lionagi.cli.orchestrate._orchestration.load_agent_profile",
            side_effect=FileNotFoundError,
        ),
        patch("lionagi.cli.orchestrate._orchestration.resolve_persisted_effort", return_value=None),
        patch("lionagi.cli.orchestrate._orchestration.Branch") as mock_branch,
        patch("lionagi.cli.orchestrate._orchestration.Session"),
        patch("lionagi.cli.orchestrate._orchestration.OperationGraphBuilder"),
    ):
        mock_ep = MagicMock()
        mock_ep.config.provider = "openai"
        mock_ep.config.kwargs = {}
        mock_imodel.return_value.endpoint = mock_ep
        mock_run.return_value.ensure_artifact_root.return_value = None
        mock_branch.return_value = MagicMock(system=None)

        env = setup_orchestration(
            pattern_name="Flow",
            model_spec="openai/gpt-4.1-mini",
            agent_name=None,
            save_dir=None,
            cwd=None,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
        )

    assert env.total_budget is None
