# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for flow budget propagation: _format_budget_preamble, equal-split arithmetic, and OrchestrationEnv.total_budget wiring."""

from __future__ import annotations

import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _equal_split(n: int, total_budget: int) -> int:
    """Replicate the per-assignment share from _run_flow_inner."""
    return int(total_budget / n)


def test_equal_split_3_assignments():
    assert _equal_split(3, 600) == 200


def test_equal_split_rounds_down():
    assert _equal_split(3, 700) == 233


def test_no_budget_preamble_when_total_budget_none():
    """The total_budget None guard produces no preamble entries."""
    total_budget = None
    n = 2
    preambles: dict[int, str] = {}
    if total_budget and n:
        share = int(total_budget / n)
        preambles[0] = _format_budget_preamble(1, n, share, time.time() + total_budget)
    assert preambles == {}


# ── OrchestrationEnv.total_budget ─────────────────────────────────────────


def test_orchestration_env_has_total_budget_field():
    """OrchestrationEnv must expose a total_budget attribute (None by default)."""
    import dataclasses

    from lionagi.cli.orchestrate._orchestration import OrchestrationEnv

    field_names = {f.name for f in dataclasses.fields(OrchestrationEnv)}
    assert "total_budget" in field_names


@pytest.mark.asyncio
async def test_setup_orchestration_passes_total_budget():
    """setup_orchestration must forward total_budget to OrchestrationEnv."""
    from lionagi.cli.orchestrate._orchestration import setup_orchestration

    # Patch the heavy internal calls so we don't need a live model. The
    # no-profile orchestrator now builds its branch via create_agent (the
    # canonical construction path), so that is what we stub.
    with (
        patch("lionagi.cli.orchestrate._orchestration.build_imodel_from_spec") as mock_imodel,
        patch("lionagi.cli.orchestrate._orchestration.allocate_run") as mock_run,
        patch(
            "lionagi.cli.orchestrate._orchestration.load_agent_profile",
            side_effect=FileNotFoundError,
        ),
        patch("lionagi.cli.orchestrate._orchestration.resolve_persisted_effort", return_value=None),
        patch(
            "lionagi.cli.orchestrate._orchestration.create_agent",
            new=AsyncMock(return_value=MagicMock(system=None)),
        ),
        patch("lionagi.cli.orchestrate._orchestration.Session"),
        patch("lionagi.cli.orchestrate._orchestration.OperationGraphBuilder"),
    ):
        # Wire up a minimal mock imodel
        mock_ep = MagicMock()
        mock_ep.config.provider = "openai"
        mock_ep.config.kwargs = {}
        mock_imodel.return_value.endpoint = mock_ep
        mock_run.return_value.ensure_artifact_root.return_value = None

        env = await setup_orchestration(
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


@pytest.mark.asyncio
async def test_setup_orchestration_total_budget_defaults_none():
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
        patch(
            "lionagi.cli.orchestrate._orchestration.create_agent",
            new=AsyncMock(return_value=MagicMock(system=None)),
        ),
        patch("lionagi.cli.orchestrate._orchestration.Session"),
        patch("lionagi.cli.orchestrate._orchestration.OperationGraphBuilder"),
    ):
        mock_ep = MagicMock()
        mock_ep.config.provider = "openai"
        mock_ep.config.kwargs = {}
        mock_imodel.return_value.endpoint = mock_ep
        mock_run.return_value.ensure_artifact_root.return_value = None

        env = await setup_orchestration(
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
