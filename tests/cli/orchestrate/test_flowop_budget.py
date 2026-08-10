# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for flow budget propagation: _format_budget_preamble, the critical-path share arithmetic, and OrchestrationEnv.total_budget wiring."""

from __future__ import annotations

import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.cli.orchestrate.flow import (
    _build_budget_preambles,
    _format_budget_preamble,
    critical_path_depth,
    op_budget_share,
)

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


# ── Critical-path depth ────────────────────────────────────────────────────
#
# These call the shipped functions rather than a copy of their arithmetic. The
# previous version of this section defined its own `_equal_split` helper and
# asserted against that, so it agreed with itself no matter what the flow did.


def test_depth_of_a_straight_chain_is_the_op_count():
    # 1 → 2 → 3: nothing overlaps, so every op is on the critical path.
    assert critical_path_depth([[], [0], [1]]) == 3


def test_depth_of_independent_ops_is_one():
    # No edges at all: the ops run together, so the chain is one op long.
    assert critical_path_depth([[], [], [], []]) == 1


def test_depth_of_a_fan_in_counts_the_longest_chain_only():
    # Three producers in parallel feeding one consumer — the shape the run
    # canvas draws for a writer fan-in. Four ops, but only two run in sequence.
    assert critical_path_depth([[], [], [], [0, 1, 2]]) == 2


def test_depth_takes_the_longest_of_two_unequal_branches():
    # 0 → 1 → 2 alongside a lone 3, both feeding 4.
    assert critical_path_depth([[], [0], [1], [], [2, 3]]) == 4


def test_depth_of_an_empty_plan_is_zero():
    assert critical_path_depth([]) == 0


def test_depth_ignores_a_dependency_pointing_outside_the_plan():
    # A malformed index must not raise: this only sizes a hint in a prompt.
    assert critical_path_depth([[], [7]]) == 1


def test_share_divides_by_chain_length_not_op_count():
    # The case that motivated the change: 4 ops, 900s, three of them parallel.
    # By op count each op is told 225s; two of those four ops actually run in
    # sequence, so the honest figure is 450s.
    assert op_budget_share(900, [[], [], [], [0, 1, 2]], 4) == 450


def test_share_is_unchanged_for_a_straight_chain():
    # The no-op guarantee: where nothing overlaps, this must not hand out more
    # time than dividing by the op count did.
    assert op_budget_share(600, [[], [0], [1]], 3) == 200


def test_share_rounds_down():
    assert op_budget_share(700, [[], [0], [1]], 3) == 233


def test_share_falls_back_to_the_op_count_without_dependency_data():
    assert op_budget_share(600, [], 3) == 200


# ── The concurrency cap serializes ops the dependency graph says are parallel ──
#
# Depth alone is not a lower bound on makespan. `--max-concurrent 1` runs four
# independent ops one after another, and a share computed from depth 1 hands
# each of them the whole wall clock, so the first two spend the deadline and
# the rest are cancelled part-written.


def test_share_counts_scheduling_rounds_when_a_cap_serializes_independent_ops():
    # Four independent ops, cap of 1: the graph permits full overlap, the cap
    # permits none. Four rounds, so 900s / 4.
    assert op_budget_share(900, [[], [], [], []], 4, 1) == 225


def test_share_counts_partial_rounds_under_a_cap_that_allows_some_overlap():
    # Cap of 2 over four independent ops is two rounds, not four.
    assert op_budget_share(900, [[], [], [], []], 4, 2) == 450


def test_share_rounds_a_partial_final_batch_up():
    # Three ops at a cap of 2 still needs two rounds: one pair, then a single.
    assert op_budget_share(900, [[], [], []], 3, 2) == 450


def test_share_takes_the_dependency_chain_when_it_is_longer_than_the_rounds():
    # A 3-chain under a cap of 3: rounds is 1, so depth decides. The cap being
    # present must not shorten a share the dependencies already justify.
    assert op_budget_share(900, [[], [0], [1]], 3, 3) == 300


def test_share_takes_the_rounds_when_they_exceed_the_dependency_chain():
    # Fan-in: depth 2, but a cap of 1 forces all four into sequence.
    assert op_budget_share(900, [[], [], [], [0, 1, 2]], 4, 1) == 225


def test_share_treats_a_non_positive_cap_as_unbounded():
    # 0 is how the executor spells "no limit" (flow.py's `conc` fallback), so
    # depth decides and the pre-cap answer is preserved. This is the arm that
    # keeps the default path honest: every caller that passes no cap lands here.
    assert op_budget_share(900, [[], [], [], [0, 1, 2]], 4, 0) == 450
    assert op_budget_share(900, [[], [], [], [0, 1, 2]], 4) == 450


# ── The share an op is actually told ────────────────────────────────────────
#
# `op_budget_share` being right says nothing about whether the flow uses it.
# The equal-split divisor this PR removes survived a green suite for exactly
# that reason: every test called the arithmetic directly, and the call site
# went unread. These go through the function that builds the preamble text.


def _seconds_in(preamble: str) -> int:
    m = re.search(r"(\d+) seconds", preamble)
    assert m, f"no budget figure in preamble: {preamble!r}"
    return int(m.group(1))


def test_preamble_carries_the_critical_path_share_not_the_equal_split():
    # Three producers into one consumer, 900s. Equal split would say 225.
    preambles = _build_budget_preambles(900, [[], [], [], [0, 1, 2]], 4, 0, time.time() + 900)
    assert set(preambles) == {0, 1, 2, 3}
    assert {_seconds_in(t) for t in preambles.values()} == {450}


def test_preamble_reflects_the_concurrency_cap():
    preambles = _build_budget_preambles(900, [[], [], [], []], 4, 1, time.time() + 900)
    assert {_seconds_in(t) for t in preambles.values()} == {225}


def test_preamble_numbers_the_ops_from_one():
    preambles = _build_budget_preambles(600, [[], [0], [1]], 3, 0, time.time() + 600)
    assert "op 1 of 3" in preambles[0]
    assert "op 3 of 3" in preambles[2]


def test_no_preambles_without_a_total_budget():
    assert _build_budget_preambles(None, [[], []], 2, 0, time.time()) == {}
    assert _build_budget_preambles(0, [[], []], 2, 0, time.time()) == {}


def test_no_preambles_for_an_empty_plan():
    assert _build_budget_preambles(900, [], 0, 0, time.time() + 900) == {}


def test_the_flow_builds_its_preambles_through_the_shared_helper():
    """The flow's own call site must not do this arithmetic itself.

    The three tests above pin `_build_budget_preambles`, which pins nothing
    about whether `_run_flow_inner` calls it — restoring an inline equal split
    there leaves all of them green. That is the gap that let the original
    divisor ship. Read the function's AST and require the call.
    """
    import ast
    import inspect
    import textwrap

    from lionagi.cli.orchestrate import flow as flow_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(flow_mod._run_flow_inner)))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_build_budget_preambles" in called, (
        "_run_flow_inner must build budget preambles through the shared helper, "
        "so the divisor it uses is the one the tests above pin"
    )
    assert "_format_budget_preamble" not in called, (
        "_run_flow_inner formats preambles inline again, which puts the divisor "
        "back out of reach of every test in this file"
    )


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
