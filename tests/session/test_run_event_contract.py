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

# #1539 — schema_version on every signal


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


# #1538 — RunEnd carries usage fields


def test_run_end_default_usage_fields():
    sig = RunEnd()
    assert sig.input_tokens == 0
    assert sig.output_tokens == 0
    # Unknown, not free -- no provider reported a cost.
    assert sig.total_cost_usd is None
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
    # Unknown, not free -- no messages, so no provider reported a cost.
    assert usage["total_cost_usd"] is None
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


def test_collect_branch_usage_preserves_explicit_zero_cost():
    """A provider that explicitly reports total_cost_usd=0.0 (a real, known-
    free call) must not be coerced into the "unknown" None sentinel -- `x or
    y` truthiness would silently drop the falsy 0.0 and fall through to the
    "cost" fallback (absent here), losing the explicit zero."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "total_cost_usd": 0.0,
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["total_cost_usd"] == 0.0
    assert usage["total_cost_usd"] is not None


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


# #2889 — cache-token dimensions (Anthropic cache_read/cache_creation,
# OpenAI prompt_tokens_details.cached_tokens)


def test_run_end_cache_token_fields_default():
    sig = RunEnd()
    assert sig.cached_tokens == 0
    assert sig.cache_write_tokens == 0


def test_run_end_cache_token_fields_explicit():
    sig = RunEnd(cached_tokens=500, cache_write_tokens=25)
    assert sig.cached_tokens == 500
    assert sig.cache_write_tokens == 25


def test_collect_branch_usage_anthropic_convention_pins_cache_dimensions():
    """Anthropic-style payload: input_tokens already excludes cache; cache
    reads/writes arrive as separate keys the old projection dropped entirely."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {
                "input_tokens": 80,
                "output_tokens": 30,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 25,
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 30
    assert usage["cached_tokens"] == 500
    assert usage["cache_write_tokens"] == 25


def test_collect_branch_usage_openai_convention_splits_cached_from_prompt_tokens():
    """OpenAI-style payload: prompt_tokens INCLUDES cached reads; the old
    projection folded them into input_tokens with no way to separate them
    back out. cached_tokens must be split OUT of input_tokens, not added
    alongside it."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 20
    assert usage["cached_tokens"] == 800
    assert usage["cache_write_tokens"] == 0


def test_collect_multi_branch_usage_sums_cache_dimensions():
    branch_a = MagicMock()
    branch_a.msgs.messages = [
        MagicMock(
            metadata={
                "model_response": {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 3,
                    },
                }
            }
        )
    ]
    branch_b = MagicMock()
    branch_b.msgs.messages = [
        MagicMock(
            metadata={
                "model_response": {
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 7,
                    },
                }
            }
        )
    ]
    usage = _collect_multi_branch_usage([branch_a, branch_b])
    assert usage["cached_tokens"] == 150
    assert usage["cache_write_tokens"] == 10


# #2379 — claude_code whole-agent-tree usage: a per-model modelUsage
# breakdown (subagent spend included) must be preferred over the flat,
# top-level-loop-only usage dict when both are present on the same message.


def test_collect_branch_usage_prefers_model_usage_whole_tree_over_flat_usage():
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            # Flat usage: top-level loop only, undercounts a Task-tool subagent spawn.
            "usage": {"input_tokens": 100, "output_tokens": 40},
            # modelUsage: whole tree, includes the subagent's spend too.
            "model_usage": {
                "claude-sonnet-5": {
                    "inputTokens": 100,
                    "outputTokens": 40,
                    "cacheReadInputTokens": 10,
                    "cacheCreationInputTokens": 2,
                },
                "claude-haiku-4-5": {
                    "inputTokens": 300,
                    "outputTokens": 150,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                },
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 400
    assert usage["output_tokens"] == 190
    assert usage["cached_tokens"] == 10
    assert usage["cache_write_tokens"] == 2
    # Not the flat (self-only) figures -- those would undercount the subagent.
    assert usage["input_tokens"] != 100


def test_collect_branch_usage_falls_back_to_flat_usage_when_model_usage_absent():
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 100, "output_tokens": 40},
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40


def test_collect_branch_usage_model_usage_zero_valued_map_stays_zero():
    """A genuinely zero-usage run reports a complete, well-shaped model_usage
    map whose values are all zero -- must NOT be misrouted to flat usage just
    because its sum is zero. Distinguishes 'valid entries summing to zero'
    from 'no valid entries' (the malformed-map case below)."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 999, "output_tokens": 999},
            "model_usage": {
                "claude-sonnet-5": {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                },
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cached_tokens"] == 0
    assert usage["cache_write_tokens"] == 0


def test_collect_branch_usage_partial_model_usage_falls_back_to_flat():
    """A truthy but partial/malformed model_usage map (entries missing the
    expected keys) must not erase real flat usage with zeros."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 80, "output_tokens": 30},
            "model_usage": {"claude-sonnet-5": {"inputTokens": 0, "outputTokens": 0}},
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 30


def test_collect_branch_usage_truncated_model_usage_entry_falls_back_to_flat():
    """A model_usage entry that is present but not a dict (e.g. truncated /
    corrupted mid-stream) must be treated as no valid entry."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 55, "output_tokens": 12},
            "model_usage": {"claude-sonnet-5": "truncated"},
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 55
    assert usage["output_tokens"] == 12


def test_collect_branch_usage_one_valid_entry_beside_one_incomplete_entry_falls_back():
    """A map with one well-shaped entry and one incomplete entry must not be
    trusted just because one entry passed -- that silently undercounts
    whatever the incomplete entry actually spent. The whole map falls back
    to flat usage rather than reporting the partial sum."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 80, "output_tokens": 30},
            "model_usage": {
                "claude-sonnet-5": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                },
                "claude-haiku-4-5": {"inputTokens": 999, "outputTokens": 999},
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 30


def test_collect_branch_usage_model_usage_non_integer_float_falls_back_to_flat():
    """A float token count (e.g. 10.9) must not silently truncate into the
    aggregate -- it is not a real token count, so the whole map is
    untrustworthy and flat usage is used instead."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 80, "output_tokens": 30},
            "model_usage": {
                "claude-sonnet-5": {
                    "inputTokens": 10.9,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                },
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 30


def test_collect_branch_usage_model_usage_none_value_falls_back_to_flat():
    """A None token count must not silently become zero -- it invalidates
    the entry rather than being coerced."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 80, "output_tokens": 30},
            "model_usage": {
                "claude-sonnet-5": {
                    "inputTokens": None,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                },
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 30


def test_collect_branch_usage_model_usage_string_value_falls_back_without_raising():
    """A non-numeric string token count must not raise out of the usage
    collector -- it invalidates the entry and the collector falls back to
    flat usage."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 80, "output_tokens": 30},
            "model_usage": {
                "claude-sonnet-5": {
                    "inputTokens": "oops",
                    "outputTokens": 5,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                },
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 30


def test_collect_branch_usage_no_subagent_result_uses_flat_usage():
    """No model_usage key at all (no subagent spawn occurred) -- ordinary
    flat-usage path, unaffected by the validation added for model_usage."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {"input_tokens": 21, "output_tokens": 9},
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 21
    assert usage["output_tokens"] == 9


def test_collect_branch_usage_openai_cached_tokens_exceeding_prompt_clamps_to_zero():
    """OpenAI cache metadata can report cached_tokens > prompt_tokens (a
    provider invariant violation); prompt_tokens - cached_tokens must clamp
    at 0 rather than reach an aggregate as a negative billable dimension.
    The clamped numbers alone look identical to a real full-cache hit, so
    usage_valid must mark the report as untrustworthy."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 100},
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 0
    assert usage["cached_tokens"] == 80
    assert usage["usage_valid"] is False


def test_collect_branch_usage_openai_valid_full_cache_hit_stays_valid():
    """A genuine full-cache hit (cached_tokens == prompt_tokens) produces the
    same clamped numbers as the invariant-violation case above, but must be
    distinguishable via usage_valid=True."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 0
    assert usage["cached_tokens"] == 80
    assert usage["usage_valid"] is True


def test_collect_branch_usage_openai_negative_prompt_tokens_clamps_and_marks_invalid():
    """A malformed negative prompt total must not escape into the aggregate
    as a negative input_tokens figure, and must be flagged invalid."""
    msg = MagicMock()
    msg.metadata = {
        "model_response": {
            "usage": {
                "prompt_tokens": -5,
                "completion_tokens": 0,
            },
        }
    }
    branch = MagicMock()
    branch.msgs.messages = [msg]
    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 0
    assert usage["usage_valid"] is False


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


# orchestration usage aggregation — sum usage across all DAG leg branches


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
    # Unknown, not free -- no branches, so no provider reported a cost.
    assert usage["total_cost_usd"] is None
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


# #1537 — NodeSpawned exists and carries expected fields


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


# #1540 — HookSignal suppressed for MESSAGE_ADD; other points still recorded


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


# NodeSpawned export from session package


def test_node_spawned_exported_from_session_package():
    from lionagi.session import NodeSpawned as NS

    assert NS is NodeSpawned
