# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the run-event signal contract: schema_version, RunEnd usage fields,
NodeSpawned, parent/depends_on edges, and HookSignal suppression for message.add.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from lionagi.hooks.bus import HookBus, HookPoint, HookSignal
from lionagi.session.signal import (
    SIGNAL_SCHEMA_VERSION,
    NodeCompleted,
    NodeFailed,
    NodeQueued,
    NodeSpawned,
    NodeStarted,
    RunEnd,
    RunFailed,
    RunStart,
    Signal,
    _collect_branch_usage,
    _collect_multi_branch_usage,
    build_run_end,
)

# ---------------------------------------------------------------------------
# #1539 — schema_version on every signal
# ---------------------------------------------------------------------------


def test_schema_version_constant():
    assert SIGNAL_SCHEMA_VERSION == 1


def test_schema_version_on_run_start():
    assert RunStart().schema_version == SIGNAL_SCHEMA_VERSION


def test_schema_version_on_run_end():
    assert RunEnd().schema_version == SIGNAL_SCHEMA_VERSION


def test_schema_version_on_node_started():
    assert NodeStarted().schema_version == SIGNAL_SCHEMA_VERSION


def test_schema_version_on_node_spawned():
    assert NodeSpawned().schema_version == SIGNAL_SCHEMA_VERSION


def test_schema_version_on_run_failed():
    assert RunFailed().schema_version == SIGNAL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# #1538 — RunEnd carries usage fields
# ---------------------------------------------------------------------------


def test_run_end_default_usage_fields():
    sig = RunEnd()
    assert sig.input_tokens == 0
    assert sig.output_tokens == 0
    assert sig.total_cost_usd == 0.0
    assert sig.num_turns == 0
    assert sig.duration_ms == 0.0


def test_run_end_explicit_usage_fields():
    sig = RunEnd(
        input_tokens=100, output_tokens=50, total_cost_usd=0.01, num_turns=2, duration_ms=1234.5
    )
    assert sig.input_tokens == 100
    assert sig.output_tokens == 50
    assert sig.total_cost_usd == pytest.approx(0.01)
    assert sig.num_turns == 2
    assert sig.duration_ms == pytest.approx(1234.5)


def test_collect_branch_usage_empty():
    branch = MagicMock()
    branch.msgs.messages = []
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_cost_usd"] == 0.0
    assert usage["num_turns"] == 0


def test_collect_branch_usage_openai_convention():
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"prompt_tokens": 40, "completion_tokens": 20},
            "total_cost_usd": 0.005,
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 40
    assert usage["output_tokens"] == 20
    assert usage["total_cost_usd"] == pytest.approx(0.005)


def test_collect_branch_usage_anthropic_convention():
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 80, "output_tokens": 30},
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 30


def test_build_run_end_populates_from_branch():
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    sig = build_run_end(branch, duration_ms=500.0, result="ok")
    assert isinstance(sig, RunEnd)
    assert sig.input_tokens == 10
    assert sig.output_tokens == 5
    assert sig.duration_ms == pytest.approx(500.0)
    assert sig.data == "ok"


# ---------------------------------------------------------------------------
# orchestration usage aggregation — sum usage across all DAG leg branches
# ---------------------------------------------------------------------------


def _branch_with_usage(
    *, input_tokens=0, output_tokens=0, total_cost_usd=0.0, num_turns=0
) -> MagicMock:
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "total_cost_usd": total_cost_usd,
            "num_turns": num_turns,
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    return branch


def test_collect_multi_branch_usage_empty():
    usage = _collect_multi_branch_usage([])
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_cost_usd"] == 0.0
    assert usage["num_turns"] == 0


def test_collect_multi_branch_usage_single_branch_matches_collect_branch_usage():
    branch = _branch_with_usage(
        input_tokens=40, output_tokens=20, total_cost_usd=0.005, num_turns=1
    )
    assert _collect_multi_branch_usage([branch]) == _collect_branch_usage(branch)


def test_collect_multi_branch_usage_sums_across_branches():
    """The most important assertion in this module: aggregated usage must be the
    SUM across every branch in a multi-leg DAG run, not just one leg's value and
    not zero (the orchestrator/play/flow gap this aggregator fixes).
    """
    orchestrator = _branch_with_usage(
        input_tokens=10, output_tokens=5, total_cost_usd=0.001, num_turns=1
    )
    worker_a = _branch_with_usage(
        input_tokens=100, output_tokens=50, total_cost_usd=0.02, num_turns=3
    )
    worker_b = _branch_with_usage(
        input_tokens=200, output_tokens=75, total_cost_usd=0.03, num_turns=2
    )

    usage = _collect_multi_branch_usage([orchestrator, worker_a, worker_b])

    assert usage["input_tokens"] == 10 + 100 + 200
    assert usage["output_tokens"] == 5 + 50 + 75
    assert usage["total_cost_usd"] == pytest.approx(0.001 + 0.02 + 0.03)
    assert usage["num_turns"] == 1 + 3 + 2
    # Not just the max/last leg's value — a real sum, and not zero.
    assert usage["input_tokens"] != max(10, 100, 200)
    assert usage["input_tokens"] > 0


def test_collect_multi_branch_usage_skips_branches_that_raise():
    class _RaisingMsgs:
        @property
        def messages(self):
            raise RuntimeError("boom")

    good = _branch_with_usage(input_tokens=10, output_tokens=5)
    bad = MagicMock()
    bad.msgs = _RaisingMsgs()

    usage = _collect_multi_branch_usage([good, bad])
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5


# ---------------------------------------------------------------------------
# #1537 — NodeSpawned exists and carries expected fields
# ---------------------------------------------------------------------------


def test_node_spawned_defaults():
    sig = NodeSpawned()
    assert sig.op_id == ""
    assert sig.parent_id is None
    assert sig.independent is False
    assert sig.assignee is None
    assert sig.instruction is None


def test_node_spawned_with_parent():
    sig = NodeSpawned(
        op_id="child-1",
        parent_id="parent-0",
        independent=False,
        assignee="worker",
        instruction="do X",
    )
    assert sig.op_id == "child-1"
    assert sig.parent_id == "parent-0"
    assert sig.independent is False
    assert sig.assignee == "worker"
    assert sig.instruction == "do X"


def test_node_spawned_independent():
    sig = NodeSpawned(op_id="orphan", independent=True)
    assert sig.independent is True
    assert sig.parent_id is None


def test_node_lifecycle_signals_carry_parent_id():
    for cls in (NodeStarted, NodeCompleted, NodeFailed, NodeQueued):
        sig = cls(op_id="x", parent_id="p", depends_on=["a", "b"])
        assert sig.parent_id == "p"
        assert sig.depends_on == ["a", "b"]


def test_node_lifecycle_signals_parent_defaults_none():
    for cls in (NodeStarted, NodeCompleted, NodeFailed, NodeQueued):
        sig = cls()
        assert sig.parent_id is None
        assert sig.depends_on == []


# ---------------------------------------------------------------------------
# #1540 — HookSignal suppressed for MESSAGE_ADD; other points still recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_bus_suppresses_message_add_hook_signal():
    from lionagi.session.observer import SessionObserver

    obs = SessionObserver()
    recorded: list[Any] = []

    # observe(Signal, handler) catches all Signal subclasses; handler gets (matched, ctx)
    obs.observe(Signal, lambda sig, _ctx: recorded.append(sig))

    bus = HookBus(observer=obs)
    handler_called = []

    async def my_handler(**kwargs):
        handler_called.append(kwargs)

    bus.on(HookPoint.MESSAGE_ADD, my_handler)
    await bus.emit(HookPoint.MESSAGE_ADD, message={"id": "m1"}, session_id="s1")

    # Handler itself was called (side effect preserved)
    assert len(handler_called) == 1
    # But NO HookSignal was emitted on the observer transport
    hook_sigs = [s for s in recorded if isinstance(s, HookSignal)]
    assert hook_sigs == [], f"Expected no HookSignal for MESSAGE_ADD, got {hook_sigs}"


@pytest.mark.asyncio
async def test_hook_bus_records_other_points():
    from lionagi.session.observer import SessionObserver

    obs = SessionObserver()
    recorded: list[Any] = []

    obs.observe(Signal, lambda sig, _ctx: recorded.append(sig))

    bus = HookBus(observer=obs)
    await bus.emit(HookPoint.SESSION_START, session_id="s1")

    hook_sigs = [
        s for s in recorded if isinstance(s, HookSignal) and s.point == HookPoint.SESSION_START
    ]
    assert len(hook_sigs) == 1


# ---------------------------------------------------------------------------
# NodeSpawned export from session package
# ---------------------------------------------------------------------------


def test_node_spawned_exported_from_session_package():
    from lionagi.session import NodeSpawned as NS

    assert NS is NodeSpawned
