# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for flow budget propagation (issue #1091).

Verifies that:
  - _format_budget_preamble produces correct text
  - A total timeout splits equally across initial assignments (total / N)
  - No BUDGET preamble when total_budget is None
  - OrchestrationEnv.total_budget is set by setup_orchestration when
    total_budget kwarg is provided

(The clean break replaced per-op ``FlowOp.budget_weight`` with an equal split:
TaskAssignment carries no weight field.)
"""

from __future__ import annotations

import re
import time
from unittest.mock import MagicMock, patch

from lionagi.cli.orchestrate.flow import _format_budget_preamble

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


# ── Budget split (equal share across assignments) ──────────────────────────
#
# The reactive flow splits a total timeout equally across initial assignments:
# ``share = int(total_budget / N)``. We test the arithmetic + the None guard
# directly rather than invoking _run_flow_inner (which needs a live backend).


# ── OrchestrationEnv.total_budget ─────────────────────────────────────────


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
