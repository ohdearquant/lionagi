# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""NudgeEngine — just-in-time guidance and status nudges for coding-agent tool calls.

Rides the CodingToolkit "*" post-hook plane, evaluating NudgeRules against
live branch state and merging triggered messages under a token cap.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lionagi.service.token_budget import TokenBudget, get_token_budget
from lionagi.service.token_calculator import TokenCalculator

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

logger = logging.getLogger(__name__)

__all__ = (
    "NudgeContext",
    "NudgeEngine",
    "NudgeRule",
    "ToolCallRecord",
    "default_nudge_rules",
)


@dataclass(frozen=True)
class ToolCallRecord:
    """One entry in the engine's tool-call ring buffer."""

    tool_name: str
    action: str
    ok: bool
    ts: float


@dataclass
class NudgeContext:
    """Snapshot handed to rule conditions/messages on each evaluate() call."""

    budget: TokenBudget
    n_active: int
    n_total: int
    n_evicted: int
    n_action_results: int
    n_files: int
    recent_calls: tuple[ToolCallRecord, ...]
    fired: dict[str, int]
    call_count: int

    def recent(self, tool_name: str, limit: int | None = None) -> list[ToolCallRecord]:
        """Calls matching `tool_name`, oldest first; `limit` keeps only the most recent."""
        calls = [c for c in self.recent_calls if c.tool_name == tool_name]
        return calls[-limit:] if limit else calls


@dataclass
class NudgeRule:
    """A condition -> message rule, gated by a firing policy and ordered by priority."""

    id: str
    condition: Callable[[NudgeContext], bool]
    message: str | Callable[[NudgeContext], str]
    policy: str | tuple[str, int] = "always"
    priority: int = 0

    def render(self, ctx: NudgeContext) -> str:
        return self.message(ctx) if callable(self.message) else self.message


def _status_message(ctx: NudgeContext) -> str:
    parts = [
        f"context {ctx.budget.used // 1000}k/{ctx.budget.limit // 1000}k tokens "
        f"({ctx.budget.usage_pct:.0%})"
    ]
    parts.append(f"{ctx.n_active} messages")
    if ctx.n_action_results > 0:
        parts.append(f"{ctx.n_action_results} action results")
    if ctx.n_files > 0:
        parts.append(f"{ctx.n_files} files tracked")
    if ctx.n_evicted > 0:
        parts.append(f"{ctx.n_evicted} evicted")
    return f"[System: {', '.join(parts)}]"


def _jit_guidance_message(ctx: NudgeContext) -> str:
    from lionagi.tools.context.context import ContextTool

    return ContextTool.GUIDANCE


STATUS_RULE = NudgeRule(
    id="status",
    condition=lambda ctx: True,
    message=_status_message,
    policy="always",
    priority=100,
)

CRITICAL_RULE = NudgeRule(
    id="budget_critical",
    condition=lambda ctx: ctx.budget.is_critical,
    message="⚠️ Context nearly full — evict old action results now.",
    policy="always",
    priority=90,
)

JIT_GUIDANCE_RULE = NudgeRule(
    id="jit_guidance",
    condition=lambda ctx: ctx.budget.usage_pct >= 0.6,
    message=_jit_guidance_message,
    policy="once",
    priority=80,
)

BASH_FAILURE_RULE = NudgeRule(
    id="bash_failure_streak",
    condition=lambda ctx: (
        len(recent := ctx.recent("bash", 3)) == 3 and all(not c.ok for c in recent)
    ),
    message=(
        "3 consecutive bash failures — diagnose the failure before retrying "
        "(read the exact error, don't just re-run the same command)."
    ),
    policy=("cooldown", 10),
    priority=60,
)

WARNING_RULE = NudgeRule(
    id="budget_warning",
    condition=lambda ctx: ctx.budget.is_warning and not ctx.budget.is_critical,
    message="Consider evicting earlier action results to free space.",
    policy=("cooldown", 5),
    priority=50,
)


def default_nudge_rules() -> list[NudgeRule]:
    """Fresh copy of the default rule set (status + budget nudges + JIT guidance)."""
    return [STATUS_RULE, CRITICAL_RULE, JIT_GUIDANCE_RULE, BASH_FAILURE_RULE, WARNING_RULE]


class NudgeEngine:
    """Evaluates NudgeRules against a Branch's live state and merges triggered messages."""

    def __init__(
        self,
        branch: Branch,
        rules: list[NudgeRule] | None = None,
        ring_size: int = 20,
        max_tokens: int = 250,
    ):
        self.branch = branch
        self.rules = list(rules) if rules is not None else default_nudge_rules()
        self.max_tokens = max_tokens
        self._ring: deque[ToolCallRecord] = deque(maxlen=ring_size)
        self._fired: dict[str, int] = {}
        self._last_fired_at: dict[str, int] = {}
        self._call_count = 0

    def record_call(self, tool_name: str, action: str, ok: bool) -> None:
        """Append a tool call to the ring buffer; feeds streak-style rule conditions."""
        self._ring.append(ToolCallRecord(tool_name=tool_name, action=action, ok=ok, ts=time.time()))

    def evaluate(self, *, files_tracked: int = 0) -> str | None:
        """Build a NudgeContext, fire eligible rules, and return the merged suffix.

        Once/cooldown bookkeeping commits only for rules whose message survives
        the token-cap merge — a dropped message must not count as delivered.
        See docs/internals/runtime.md.
        """
        self._call_count += 1
        ctx = self._build_context(files_tracked)
        candidates: list[tuple[NudgeRule, str]] = []
        for rule in self.rules:
            try:
                eligible = self._should_fire(rule, ctx)
            except Exception:
                logger.warning("nudge rule %r condition raised; skipping", rule.id, exc_info=True)
                continue
            if not eligible:
                continue
            try:
                msg = rule.render(ctx)
            except Exception:
                logger.warning("nudge rule %r render raised; skipping", rule.id, exc_info=True)
                continue
            candidates.append((rule, msg))
        candidates.sort(key=lambda pair: -pair[0].priority)
        merged, survivors = self._merge(candidates)
        for rule in survivors:
            self._mark_fired(rule)
        return merged

    def _build_context(self, files_tracked: int) -> NudgeContext:
        from lionagi.protocols.messages import ActionResponse

        budget = get_token_budget(self.branch)
        msgs = self.branch.msgs
        active = self.branch.progression
        total = msgs.progression
        pile = msgs.messages
        n_action_results = sum(
            1 for uid in active if uid in pile and isinstance(pile[uid], ActionResponse)
        )
        return NudgeContext(
            budget=budget,
            n_active=len(active),
            n_total=len(total),
            n_evicted=len(total) - len(active),
            n_action_results=n_action_results,
            n_files=files_tracked,
            recent_calls=tuple(self._ring),
            fired=dict(self._fired),
            call_count=self._call_count,
        )

    def _should_fire(self, rule: NudgeRule, ctx: NudgeContext) -> bool:
        if not rule.condition(ctx):
            return False
        policy = rule.policy
        if policy == "once":
            return self._fired.get(rule.id, 0) == 0
        if policy == "always":
            return True
        if isinstance(policy, tuple) and policy[0] == "cooldown":
            n = policy[1]
            last = self._last_fired_at.get(rule.id)
            return last is None or (self._call_count - last) >= n
        return True

    def _mark_fired(self, rule: NudgeRule) -> None:
        self._fired[rule.id] = self._fired.get(rule.id, 0) + 1
        self._last_fired_at[rule.id] = self._call_count

    def _merge(self, candidates: list[tuple[NudgeRule, str]]) -> tuple[str | None, list[NudgeRule]]:
        """Merge candidate messages under the token cap; return (text, surviving rules).

        Only the returned survivor list had its message delivered — gate
        once/cooldown bookkeeping on that, not on `candidates`.
        """
        if not candidates:
            return None, []
        kept_msgs: list[str] = []
        kept_rules: list[NudgeRule] = []
        used_tokens = 0
        for rule, msg in candidates:
            cost = TokenCalculator.tokenize(msg) if msg else 0
            if kept_msgs and used_tokens + cost > self.max_tokens:
                continue
            kept_msgs.append(msg)
            kept_rules.append(rule)
            used_tokens += cost
        merged = " ".join(kept_msgs) if kept_msgs else None
        return merged, kept_rules
