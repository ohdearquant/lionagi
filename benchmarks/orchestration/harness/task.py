"""Task + result types — benchmark-agnostic interface.

A ``Task`` is one unit of work with ground-truth labels. The mutation suite
produces tasks where a known defect was planted (or deliberately left clean);
a future SWE-bench adapter produces tasks from real GitHub issues. Either way
the runner consumes a ``Task`` and the suite's scorer consumes a ``RunResult``
plus the task's ``labels`` to produce metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Label:
    """One ground-truth fact about a task.

    ``kind``: ``defect`` (a real planted bug that SHOULD be found) or
    ``intended`` (behavior that LOOKS suspicious but is correct — flagging it
    at >= ``Medium`` severity is a false positive).
    """

    kind: str  # "defect" | "intended"
    location: str  # e.g. "lionagi/hooks/bus.py:194 blocking_emit"
    summary: str  # what the defect is, or what's intended
    true_severity: str = "none"  # none | low | medium | high | critical


@dataclass(frozen=True, slots=True)
class Task:
    """A benchmark task with ground-truth labels."""

    id: str
    prompt: str  # the review/work instruction handed to the orchestration
    labels: tuple[Label, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    """Raw output of running one config on one task once (one trial)."""

    task_id: str
    config_key: str
    trial: int
    outputs: list[str]  # per-node final text (single → 1 item)
    wall_seconds: float
    spawned: int = 0
    error: str | None = None
    # Compute accounting — the matched-compute foundation (cost.py prices these).
    # Tokens are summed across ALL agents/branches in the run, so a 4-agent flow
    # carries 4 agents' worth of tokens vs a single agent's. ``usage_source`` is
    # "reported" (provider usage), "estimated" (tokenized fallback, undercounts
    # CLI internal turns), or "mixed".
    input_tokens: int = 0  # uncached prompt tokens
    output_tokens: int = 0  # completion (incl. reasoning for Anthropic, NOT codex)
    cached_tokens: int = 0  # cached prompt tokens (billed at cached rate)
    num_turns: int = 0
    n_calls: int = 0
    usage_source: str = "none"  # reported | estimated | mixed | none
    reasoning_disclosed: bool = True  # False ⇒ codex reasoning unbilled (cost is a floor)
    model: str = ""  # config model, for pricing at report time
    # ADR-0089: which sandbox backend hosted this trial's workspace. "inprocess"
    # is the pre-seam default; never pool "inprocess" and a real backend's
    # numbers together when a report starts comparing across backends.
    backend: str = "inprocess"


@dataclass(slots=True)
class ScoredResult:
    """A RunResult scored against the task's labels."""

    task_id: str
    config_key: str
    trial: int
    found_defect: bool  # recall: did it flag the planted defect?
    false_positive: bool  # precision: did it flag intended behavior as Medium+?
    engaged: bool  # did it actually examine the labeled code path? (anti-laziness)
    reported_severity: str | None  # what severity it gave the headline finding
    severity_error: int | None  # |reported - true| on an ordinal scale, if applicable
    wall_seconds: float = 0.0
    notes: str = ""
    # Compute, carried through from the RunResult so the report can price lift.
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    est_cost_usd: float = 0.0
    usage_source: str = "none"
    reasoning_disclosed: bool = True
    model: str = ""
