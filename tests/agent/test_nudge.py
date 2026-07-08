# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.agent.nudge: NudgeEngine rule evaluation, policies, and merging."""

from __future__ import annotations

import lionagi.agent.nudge as nudge_mod
from lionagi.agent.nudge import (
    NudgeContext,
    NudgeEngine,
    NudgeRule,
    ToolCallRecord,
    default_nudge_rules,
)
from lionagi.service.token_budget import TokenBudget
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(monkeypatch, used: int, limit: int, rules=None) -> NudgeEngine:
    branch = Branch()
    budget = TokenBudget(used=used, limit=limit)
    monkeypatch.setattr(nudge_mod, "get_token_budget", lambda b: budget)
    return NudgeEngine(branch, rules=rules)


# ---------------------------------------------------------------------------
# Default rule set: status (always)
# ---------------------------------------------------------------------------


def test_status_rule_fires_every_call(monkeypatch):
    engine = _make_engine(monkeypatch, used=1_000, limit=100_000)
    for _ in range(3):
        msg = engine.evaluate()
        assert msg is not None
        assert "context 1k/100k tokens" in msg


def test_status_rule_includes_evicted_and_action_result_counts(monkeypatch):
    engine = _make_engine(monkeypatch, used=100, limit=100_000)
    msg = engine.evaluate()
    assert "0 messages" in msg  # empty branch, no evicted/action results shown


# ---------------------------------------------------------------------------
# Default rule set: budget_critical (always)
# ---------------------------------------------------------------------------


def test_critical_rule_fires_every_call(monkeypatch):
    engine = _make_engine(monkeypatch, used=95_000, limit=100_000)  # 95% -> critical
    msgs = [engine.evaluate() for _ in range(3)]
    assert all("nearly full" in m for m in msgs)


# ---------------------------------------------------------------------------
# Default rule set: budget_warning (cooldown 5), mutually exclusive with critical
# ---------------------------------------------------------------------------


def test_warning_rule_does_not_fire_when_critical(monkeypatch):
    engine = _make_engine(monkeypatch, used=95_000, limit=100_000)
    msg = engine.evaluate()
    assert "Consider evicting" not in msg


def test_warning_rule_cooldown_suppresses_then_refires(monkeypatch):
    engine = _make_engine(monkeypatch, used=75_000, limit=100_000)  # 75% warning, not critical
    first = engine.evaluate()
    assert "Consider evicting" in first

    second = engine.evaluate()
    assert "Consider evicting" not in second

    for _ in range(3):
        engine.evaluate()

    sixth = engine.evaluate()  # call_count=6, last fired at 1 -> 6-1=5 >= cooldown(5)
    assert "Consider evicting" in sixth


# ---------------------------------------------------------------------------
# Default rule set: jit_guidance (once)
# ---------------------------------------------------------------------------


def test_jit_guidance_fires_exactly_once(monkeypatch):
    from lionagi.tools.context.context import ContextTool

    # Default rules + DEFAULT token cap — regression guard for the cap being
    # sized so the shipped configuration can actually deliver its own guidance.
    engine = _make_engine(
        monkeypatch, used=65_000, limit=100_000
    )  # 65% -> JIT threshold, no warning
    first = engine.evaluate()
    assert ContextTool.GUIDANCE in first

    second = engine.evaluate()
    assert ContextTool.GUIDANCE not in (second or "")

    third = engine.evaluate()
    assert ContextTool.GUIDANCE not in (third or "")


def test_default_cap_delivers_jit_guidance_exactly_once(monkeypatch):
    """Regression: under the DEFAULT rule set and DEFAULT token cap, the JIT
    guidance must be delivered — either on the first eligible call or a later
    one — and exactly once, never silently consumed by a cap-drop."""
    from lionagi.tools.context.context import ContextTool

    engine = _make_engine(monkeypatch, used=65_000, limit=100_000)
    delivered = []
    for _ in range(5):
        msg = engine.evaluate()
        if msg and ContextTool.GUIDANCE in msg:
            delivered.append(msg)
    assert len(delivered) == 1


def test_once_rule_dropped_by_cap_is_not_marked_fired(monkeypatch):
    """A once-policy rule whose message is dropped by the token cap must remain
    eligible — firing state is only committed for messages that actually survive
    the merge, never for candidates the cap trimmed away."""
    filler = NudgeRule(
        id="filler", condition=lambda ctx: True, message="filler", policy="always", priority=100
    )
    once_rule = NudgeRule(
        id="big_once", condition=lambda ctx: True, message="X" * 200, policy="once", priority=50
    )
    engine = _make_engine(monkeypatch, used=1_000, limit=100_000, rules=[filler, once_rule])
    engine.max_tokens = 5  # filler survives (considered first); big_once gets dropped

    first = engine.evaluate()
    assert "filler" in first
    assert "X" * 200 not in first
    assert engine._fired.get("big_once", 0) == 0  # dropped by cap, not consumed

    engine.max_tokens = 10_000  # now both fit
    second = engine.evaluate()
    assert "X" * 200 in second
    assert engine._fired.get("big_once", 0) == 1

    third = engine.evaluate()
    assert "X" * 200 not in third  # delivered for real -> policy=once suppresses further


def test_jit_guidance_does_not_fire_below_threshold(monkeypatch):
    from lionagi.tools.context.context import ContextTool

    engine = _make_engine(monkeypatch, used=10_000, limit=100_000)  # 10%
    msg = engine.evaluate()
    assert ContextTool.GUIDANCE not in (msg or "")


# ---------------------------------------------------------------------------
# Bash failure streak (cooldown 10)
# ---------------------------------------------------------------------------


def test_bash_failure_streak_requires_three_consecutive_failures(monkeypatch):
    engine = _make_engine(monkeypatch, used=100, limit=100_000)
    engine.record_call("bash", "", False)
    engine.record_call("bash", "", False)
    msg = engine.evaluate()
    assert "diagnose" not in (msg or "").lower()


def test_bash_failure_streak_fires_on_third_failure(monkeypatch):
    engine = _make_engine(monkeypatch, used=100, limit=100_000)
    engine.record_call("bash", "", False)
    engine.record_call("bash", "", False)
    engine.evaluate()  # call_count=1, only 2 failures so far
    engine.record_call("bash", "", False)
    msg = engine.evaluate()  # call_count=2, 3 failures now
    assert "diagnose" in msg.lower()


def test_bash_failure_streak_cooldown_suppresses_immediate_refire(monkeypatch):
    engine = _make_engine(monkeypatch, used=100, limit=100_000)
    for _ in range(3):
        engine.record_call("bash", "", False)
    engine.evaluate()  # fires; call_count=1
    engine.record_call("bash", "", False)
    msg = engine.evaluate()  # still a 3-fail streak, but cooldown(10) blocks immediate refire
    assert "diagnose" not in msg.lower()


def test_bash_failure_streak_ignores_successes():
    ctx = NudgeContext(
        budget=TokenBudget(used=0, limit=100),
        n_active=0,
        n_total=0,
        n_evicted=0,
        n_action_results=0,
        n_files=0,
        recent_calls=(
            ToolCallRecord("bash", "", True, 0.0),
            ToolCallRecord("bash", "", False, 1.0),
            ToolCallRecord("bash", "", False, 2.0),
        ),
        fired={},
        call_count=0,
    )
    from lionagi.agent.nudge import BASH_FAILURE_RULE

    assert BASH_FAILURE_RULE.condition(ctx) is False  # only 2 of last 3 failed


# ---------------------------------------------------------------------------
# NudgeContext.recent()
# ---------------------------------------------------------------------------


def test_context_recent_filters_by_tool_and_limits():
    calls = (
        ToolCallRecord("bash", "", True, 0.0),
        ToolCallRecord("reader", "", True, 1.0),
        ToolCallRecord("bash", "", False, 2.0),
        ToolCallRecord("bash", "", False, 3.0),
    )
    ctx = NudgeContext(
        budget=TokenBudget(used=0, limit=100),
        n_active=0,
        n_total=0,
        n_evicted=0,
        n_action_results=0,
        n_files=0,
        recent_calls=calls,
        fired={},
        call_count=0,
    )
    bash_calls = ctx.recent("bash")
    assert len(bash_calls) == 3
    last_two = ctx.recent("bash", 2)
    assert len(last_two) == 2
    assert [c.ok for c in last_two] == [False, False]


# ---------------------------------------------------------------------------
# Priority ordering + token-cap dropping
# ---------------------------------------------------------------------------


def test_priority_ordering_and_token_cap_drops_lowest_priority(monkeypatch):
    rules = [
        NudgeRule(
            id="low",
            condition=lambda ctx: True,
            message="low priority filler text that costs several tokens",
            policy="always",
            priority=1,
        ),
        NudgeRule(
            id="high",
            condition=lambda ctx: True,
            message="HIGH",
            policy="always",
            priority=100,
        ),
    ]
    engine = _make_engine(monkeypatch, used=100, limit=100_000, rules=rules)
    engine.max_tokens = 1  # tiny cap: only the first (highest-priority) message survives
    msg = engine.evaluate()
    assert "HIGH" in msg
    assert "low priority" not in msg


def test_merge_preserves_priority_order_when_all_fit(monkeypatch):
    rules = [
        NudgeRule(id="a", condition=lambda ctx: True, message="A", policy="always", priority=1),
        NudgeRule(id="b", condition=lambda ctx: True, message="B", policy="always", priority=100),
        NudgeRule(id="c", condition=lambda ctx: True, message="C", policy="always", priority=50),
    ]
    engine = _make_engine(monkeypatch, used=100, limit=100_000, rules=rules)
    msg = engine.evaluate()
    assert msg.index("B") < msg.index("C") < msg.index("A")


# ---------------------------------------------------------------------------
# Rules that never fire -> None
# ---------------------------------------------------------------------------


def test_no_rules_returns_none(monkeypatch):
    engine = _make_engine(monkeypatch, used=0, limit=100_000, rules=[])
    assert engine.evaluate() is None


def test_all_conditions_false_returns_none(monkeypatch):
    rules = [NudgeRule(id="never", condition=lambda ctx: False, message="x", policy="always")]
    engine = _make_engine(monkeypatch, used=0, limit=100_000, rules=rules)
    assert engine.evaluate() is None


# ---------------------------------------------------------------------------
# default_nudge_rules() returns independent instances
# ---------------------------------------------------------------------------


def test_default_nudge_rules_are_fresh_each_call():
    a = default_nudge_rules()
    b = default_nudge_rules()
    assert a is not b
    assert [r.id for r in a] == [r.id for r in b]


# ---------------------------------------------------------------------------
# Per-rule exception containment — a raising rule never breaks evaluate()
# ---------------------------------------------------------------------------


def test_raising_condition_is_contained_other_rules_still_fire(monkeypatch):
    def bad_condition(ctx):
        raise RuntimeError("boom")

    bad_rule = NudgeRule(
        id="bad", condition=bad_condition, message="should never appear", policy="always"
    )
    good_rule = NudgeRule(id="good", condition=lambda ctx: True, message="GOOD", policy="always")
    engine = _make_engine(monkeypatch, used=100, limit=100_000, rules=[bad_rule, good_rule])

    msg = engine.evaluate()
    assert msg is not None
    assert "GOOD" in msg
    assert "should never appear" not in msg


def test_raising_render_is_contained_other_rules_still_fire(monkeypatch):
    def bad_message(ctx):
        raise RuntimeError("boom")

    bad_rule = NudgeRule(id="bad", condition=lambda ctx: True, message=bad_message, policy="always")
    good_rule = NudgeRule(id="good", condition=lambda ctx: True, message="GOOD", policy="always")
    engine = _make_engine(monkeypatch, used=100, limit=100_000, rules=[bad_rule, good_rule])

    msg = engine.evaluate()
    assert msg is not None
    assert "GOOD" in msg


def test_raising_rule_is_never_marked_fired(monkeypatch):
    def bad_condition(ctx):
        raise RuntimeError("boom")

    bad_rule = NudgeRule(id="bad", condition=bad_condition, message="x", policy="once")
    engine = _make_engine(monkeypatch, used=100, limit=100_000, rules=[bad_rule])

    engine.evaluate()
    engine.evaluate()
    assert engine._fired.get("bad", 0) == 0
