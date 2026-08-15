# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Signal types and per-node lifecycle projection for the reactive bus (ADR-0033); see per-class docstrings for payload contract. schema_version bumps only on breaking field removal/rename."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel

from ..protocols.generic.element import Element

__all__ = (
    "Signal",
    "StructuredOutput",
    "RunStart",
    "RunEnd",
    "RunFailed",
    "NodeSpawned",
    "NodeStarted",
    "NodeCompleted",
    "NodeFailed",
    "NodeSkipped",
    "NodeQueued",
    "NodeAwaitingApproval",
    "NodeEscalated",
    "NodePaused",
    "GateDenied",
    "MessageAdded",
    "DispatchSignal",
    "NodeLifecycleState",
    "lane_for",
    "build_run_end",
    "SIGNAL_SCHEMA_VERSION",
)

SIGNAL_SCHEMA_VERSION: int = 1


class Signal(Element):
    """Observable envelope carrying a payload into the reactive bus."""

    data: Any = None
    emitter_role: str | None = None
    schema_version: int = SIGNAL_SCHEMA_VERSION


class StructuredOutput(Signal):
    """Signal whose payload is a structured (typed) model."""

    data: BaseModel


class RunStart(Signal):
    """Run lifecycle: beginning."""


class RunEnd(Signal):
    """Run lifecycle: completed. total_cost_usd is None (unknown) unless a provider actually reports a dollar cost — never coerced to 0.0 (free). input_tokens is uncached prompt tokens in both provider conventions; cached_tokens/cache_write_tokens carry the billing dimensions that convention otherwise drops or folds in. usage_valid is False when a provider usage report violated a token-count invariant (e.g. cached_tokens > prompt_tokens) — the numeric fields are still a safe clamped shape, but a billing consumer needing to distinguish a real full-cache hit from a provider bug must check this flag."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    total_cost_usd: float | None = None
    num_turns: int = 0
    duration_ms: float = 0.0
    usage_valid: bool = True


class RunFailed(Signal):
    """Run lifecycle: raised. data is the exception."""


class NodeSpawned(Signal):
    """A DAG node was accepted into the running graph (reactive spawn)."""

    op_id: str = ""
    parent_id: str | None = None
    independent: bool = False
    assignee: str | None = None
    instruction: str | None = None


class NodeStarted(Signal):
    """DAG node lifecycle: began executing."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class NodeCompleted(Signal):
    """DAG node lifecycle: finished successfully."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class NodeFailed(Signal):
    """DAG node lifecycle: raised during execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class NodeSkipped(Signal):
    """DAG node lifecycle: never ran, because an edge condition said not to.

    A terminal outcome like NodeCompleted and NodeFailed, but not an error:
    the node was deliberately passed over, so a reader must be able to tell it
    apart from one that ran and raised. Why it was skipped is not carried here
    -- the gate's reason code, id and name are already on the operation's
    metadata and in its entry in the flow results.
    """

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class GateDenied(Signal):
    """Governance gate denied a proposed action."""


class MessageAdded(Signal):
    """A message was added to a branch. data is the RoledMessage."""


class DispatchSignal(Signal):
    """Outbound dispatch payload contract (ADR-0059); one stable envelope shared by every dispatch kind so the transport template never churns per-kind."""

    dispatch_id: str = ""
    kind: str = ""  # e.g. "revival_ping" | "terminal_notify"
    deliver_to: str = ""
    attempt: int = 0
    ack_token: str | None = None
    body: dict = {}


# -- Extended node lifecycle (ADR-0033): queued → running → awaiting_approval →
# succeeded|failed|escalated. NodeStarted/Completed/Failed (above) cover running/succeeded/failed; these three cover the rest.


class NodeQueued(Signal):
    """A DAG operation node entered the runnable graph, queued for execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class NodeAwaitingApproval(Signal):
    """A DAG operation node is paused waiting for an external approval decision."""

    op_id: str = ""
    name: str = ""
    reason: str | None = None


class NodeEscalated(Signal):
    """A DAG node escalated or sent a help signal. route: "higher_tier" (retry), "give_up" (terminal), or "notify" (soft, non-terminal)."""

    op_id: str = ""
    name: str = ""
    reason: str = ""
    route: str = ""  # "higher_tier" | "give_up" | "notify"
    escalation_request: Any = None


class NodePaused(Signal):
    """A DAG operation node is blocked at an operation boundary, awaiting resume()."""

    op_id: str = ""
    name: str = ""


# -- Lifecycle projection (ADR-0033) ------------------------------------------

#: The eight canonical per-node lifecycle states.
NodeLifecycleState = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "paused",
    "succeeded",
    "failed",
    "skipped",
    "escalated",
]

#: Terminal lanes are sticky. "skipped" belongs here because a node passed over
#: by an edge condition is finished, not waiting -- but it is deliberately kept
#: distinct from "failed", which means the node ran and raised.
_TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "skipped", "escalated"})


def _signal_to_state(sig: Any) -> NodeLifecycleState | None:
    """Return the lifecycle state implied by *sig*, or ``None`` if not state-bearing."""
    if isinstance(sig, NodeQueued):
        return "queued"
    if isinstance(sig, NodeStarted | RunStart):
        return "running"
    if isinstance(sig, NodeAwaitingApproval):
        return "awaiting_approval"
    if isinstance(sig, NodePaused):
        return "paused"
    if isinstance(sig, NodeCompleted | RunEnd):
        return "succeeded"
    if isinstance(sig, NodeFailed | RunFailed):
        return "failed"
    if isinstance(sig, NodeSkipped):
        return "skipped"
    if isinstance(sig, NodeEscalated):
        req = sig.escalation_request
        # Soft ("fyi") urgency is informational only, not terminal; only
        # "blocked" urgency (default) or no request pins to "escalated".
        if getattr(req, "urgency", "blocked") == "fyi":
            return None
        return "escalated"
    # StructuredOutput carrying an EscalationRequest also projects to escalated,
    # unless it is a soft ("fyi") help signal.
    if isinstance(sig, StructuredOutput):
        from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

        if isinstance(sig.data, EscalationRequest) and sig.data.urgency != "fyi":
            return "escalated"
    return None


def lane_for(signals: Iterable[Signal | Any]) -> NodeLifecycleState:
    """Project a pre-filtered single-node signal stream to its current lifecycle lane; terminal states are sticky."""
    state: NodeLifecycleState = "queued"
    in_terminal: bool = False
    for sig in signals:
        new = _signal_to_state(sig)
        if new is None:
            continue
        # Terminal is sticky unless a new attempt explicitly resets to queued/running.
        if in_terminal and new not in ("queued", "running"):
            continue
        state = new
        in_terminal = state in _TERMINAL
    return state


def _extract_usage_dims(usage: dict[str, Any]) -> tuple[int, int, int, int, bool]:
    """Split a raw provider usage dict into (input, output, cached,
    cache_write, is_valid) tokens, normalizing Anthropic- and OpenAI-style
    shapes to "uncached prompt tokens" for input — see docs/internals/core.md
    (session/signal.py) for the per-provider field layout and the meaning of
    ``is_valid``.
    """
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        cached = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        return input_tokens, output_tokens, cached, cache_write, True

    prompt_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    details = usage.get("prompt_tokens_details")
    cached = int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
    is_valid = prompt_tokens >= 0 and 0 <= cached <= prompt_tokens
    prompt_tokens = max(0, prompt_tokens)
    cached = max(0, min(cached, prompt_tokens))
    return prompt_tokens - cached, output_tokens, cached, 0, is_valid


_MODEL_USAGE_ENTRY_KEYS = frozenset(
    {"inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"}
)


def _as_nonneg_int(value: Any) -> int | None:
    """A token count must be a real non-negative integer -- not a truncating
    float, not None-as-zero, not a numeric-looking string. Returns the int,
    or None if *value* cannot represent a token count at all."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    return None


def _sum_model_usage(model_usage: dict[str, Any]) -> tuple[int, int, int, int, bool]:
    """Sum per-model whole-tree token counts (including descendant subagent
    spend) from a claude_code CLI ``modelUsage`` map. Returns ``(input,
    output, cached, cache_write, has_valid_entry)`` — see docs/internals/core.md
    (session/signal.py) for the entry-validity and all-or-nothing rules.
    """
    input_tokens = output_tokens = cached = cache_write = 0
    all_valid = bool(model_usage)
    for entry in model_usage.values():
        if not isinstance(entry, dict) or not _MODEL_USAGE_ENTRY_KEYS.issubset(entry):
            all_valid = False
            continue
        counts = {key: _as_nonneg_int(entry.get(key)) for key in _MODEL_USAGE_ENTRY_KEYS}
        if any(count is None for count in counts.values()):
            all_valid = False
            continue
        input_tokens += counts["inputTokens"]
        output_tokens += counts["outputTokens"]
        cached += counts["cacheReadInputTokens"]
        cache_write += counts["cacheCreationInputTokens"]
    return input_tokens, output_tokens, cached, cache_write, all_valid


def _collect_branch_usage(branch: Any) -> dict[str, Any]:
    """Sum provider-reported usage across all AssistantResponse messages on branch. total_cost_usd stays None (unknown) until a message actually reports a cost — never coerced to 0.0 (free)."""
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    cache_write_tokens = 0
    total_cost_usd: float | None = None
    num_turns = 0
    usage_valid = True

    try:
        messages = list(branch.msgs.messages)
    except Exception:  # noqa: BLE001
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "total_cost_usd": None,
            "num_turns": 0,
            "usage_valid": True,
        }

    for msg in messages:
        mr = (
            getattr(msg, "metadata", {}).get("model_response") if hasattr(msg, "metadata") else None
        )
        if not isinstance(mr, dict):
            continue
        model_usage = mr.get("model_usage")
        has_valid_model_usage = False
        if isinstance(model_usage, dict) and model_usage:
            # Whole-tree per-model breakdown (claude_code CLI subagent
            # spawns) supersedes the flat usage dict below, which only
            # reflects the top-level loop -- but only once validated: a
            # truthy map with no well-shaped entry (missing keys, non-dict)
            # must not silently erase real flat usage with zeros.
            m_in, m_out, m_cached, m_cache_write, has_valid_model_usage = _sum_model_usage(
                model_usage
            )
        if not has_valid_model_usage:
            usage = mr.get("usage") if isinstance(mr.get("usage"), dict) else mr
            m_in, m_out, m_cached, m_cache_write, m_valid = _extract_usage_dims(usage)
            usage_valid = usage_valid and m_valid
        input_tokens += m_in
        output_tokens += m_out
        cached_tokens += m_cached
        cache_write_tokens += m_cache_write
        # Presence, not truthiness: an explicit total_cost_usd=0.0 (real free
        # call) must not fall through `x or y` to the cost/None fallback.
        if "total_cost_usd" in mr and mr["total_cost_usd"] is not None:
            cost = mr["total_cost_usd"]
        elif "cost" in mr and mr["cost"] is not None:
            cost = mr["cost"]
        else:
            cost = None
        if isinstance(cost, (int, float)):
            total_cost_usd = (total_cost_usd or 0.0) + float(cost)
        num_turns += int(mr.get("num_turns", 0) or 0)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
        "usage_valid": usage_valid,
    }


def _collect_multi_branch_usage(branches: Iterable[Any]) -> dict[str, Any]:
    """Sum _collect_branch_usage across multiple branches (multi-leg DAG runs). duration_ms is excluded — wall-clock across parallel legs isn't simply summable."""
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    cache_write_tokens = 0
    total_cost_usd: float | None = None
    num_turns = 0
    usage_valid = True

    for branch in branches:
        usage = _collect_branch_usage(branch)
        input_tokens += usage["input_tokens"]
        output_tokens += usage["output_tokens"]
        cached_tokens += usage["cached_tokens"]
        cache_write_tokens += usage["cache_write_tokens"]
        if usage["total_cost_usd"] is not None:
            total_cost_usd = (total_cost_usd or 0.0) + usage["total_cost_usd"]
        num_turns += usage["num_turns"]
        usage_valid = usage_valid and usage["usage_valid"]

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
        "usage_valid": usage_valid,
    }


def build_run_end(branch: Any, *, duration_ms: float = 0.0, result: Any = None) -> RunEnd:
    """Build a RunEnd signal with usage populated from branch message history."""
    usage = _collect_branch_usage(branch)
    return RunEnd(data=result, duration_ms=duration_ms, **usage)
