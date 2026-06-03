# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Emission contracts — the typed payloads a role PRODUCES (behavior, not authority).

Composed by union, no security semantics. Field descriptions flow into the
output JSON schema, so write them as agent-facing guidance.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from lionagi.ln.types import Operable, Spec

__all__ = (
    # discovery
    "Finding",
    "Conflict",
    "Gap",
    "Diagnosis",
    "Synthesis",
    # judgement
    "Verdict",
    "ComplianceVerdict",
    "RiskAssessment",
    "Objection",
    "Recommendation",
    # analysis
    "AnalysisResult",
    "ComplexityScore",
    # planning / coordination
    "ExecutionPlan",
    "TaskAssignment",
    "DesignSpec",
    # production
    "ArtifactProduced",
    "VerificationResult",
    "Document",
    "OperationOutcome",
    # generative / retrospective
    "Proposal",
    "Postmortem",
    # universal
    "EscalationRequest",
    "SpawnRequest",
    "build_emission_operable",
)


# ---------------------------------------------------------------------------
# Discovery — what is true / what was found
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """A single discovered fact, issue, or observation — one per distinct point."""

    description: str = Field(
        description="The finding itself, stated as one concrete, self-contained claim."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        default=0.5,
        description="How sure you are this finding is correct (0=guess, 1=certain).",
    )
    severity: str | None = Field(
        default=None, description="Impact level if applicable: critical | major | minor."
    )
    evidence: str | None = Field(
        default=None,
        description="The concrete proof — quote, path:line, output, or measurement.",
    )
    source: str | None = Field(
        default=None, description="Where this came from: a file, URL, command, or prior step."
    )


class Conflict(BaseModel):
    """A contradiction between two or more sources that must be surfaced, not silently resolved."""

    sources: list[str] = Field(
        description="The conflicting sources (≥2): files, claims, references, or steps."
    )
    nature: str = Field(description="What exactly they disagree about.")


class Gap(BaseModel):
    """An identified unknown — something the work needs but does not yet have."""

    area: str = Field(description="The domain or topic where knowledge is missing.")
    what_is_unknown: str = Field(description="The specific question that remains unanswered.")


class Diagnosis(BaseModel):
    """A causal explanation: observed symptom → root cause → remedy."""

    symptom: str = Field(description="The observable problem or failure as it presents.")
    root_cause: str = Field(
        description="The underlying cause, traced past symptoms to the actual origin."
    )
    remedy: str | None = Field(
        default=None, description="The fix that addresses the root cause (not the symptom)."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        default=0.5,
        description="Confidence that this root cause is correct, not merely plausible.",
    )
    evidence: str | None = Field(
        default=None, description="What proves the causal link — repro, logs, bisect, trace."
    )


class Synthesis(BaseModel):
    """An integrated view across multiple inputs — names through-lines and tensions."""

    summary: str = Field(description="The integrated takeaway across all inputs.")
    themes: list[str] = Field(
        default_factory=list, description="Cross-cutting themes that recur across sources."
    )
    sources: list[str] = Field(
        default_factory=list, description="What was synthesized (the inputs)."
    )
    tensions: list[str] = Field(
        default_factory=list,
        description="Unresolved disagreements or trade-offs the synthesis exposes.",
    )


# ---------------------------------------------------------------------------
# Judgement — what is the call
# ---------------------------------------------------------------------------


class Verdict(BaseModel):
    """A terminal judgement on an artifact or claim — issue exactly one."""

    verdict: str = Field(
        description="The decision, e.g. APPROVE | APPROVE-WITH-FIXES | REQUEST-CHANGES | REJECT."
    )
    rationale: str = Field(
        description="Why this verdict — the reasoning that justifies it, grounded in findings."
    )
    evidence: str | None = Field(
        default=None, description="The strongest concrete support for the verdict."
    )
    reversible_by: str | None = Field(
        default=None,
        description="For a negative verdict: what evidence or change would reverse it.",
    )


class ComplianceVerdict(BaseModel):
    """A pass/fail judgement against one named control or policy."""

    verdict: str = Field(description="compliant | non-compliant | not-applicable.")
    control: str = Field(description="The control/policy/requirement identifier being evaluated.")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="References (paths, IDs, links) substantiating the verdict.",
    )


class RiskAssessment(BaseModel):
    """One identified failure mode with its likelihood, impact, and mitigation."""

    failure_mode: str = Field(description="What could go wrong, stated concretely.")
    likelihood: float = Field(
        ge=0.0, le=1.0, description="Probability of occurrence (0=never, 1=certain)."
    )
    impact: float = Field(
        ge=0.0, le=1.0, description="Severity if it occurs (0=negligible, 1=catastrophic)."
    )
    mitigation: str | None = Field(
        default=None, description="The action that reduces likelihood or impact."
    )


class Objection(BaseModel):
    """An adversarial challenge to a target's strongest form, not a weak version."""

    target: str = Field(description="The claim, proposal, or decision being challenged.")
    objection: str = Field(description="The specific flaw, gap, or failure mode.")
    strongest_target_form: str | None = Field(
        default=None,
        description="The strongest version of the target you are attacking (steelman first).",
    )
    what_would_resolve: str | None = Field(
        default=None, description="What evidence or change would neutralize this objection."
    )


class Recommendation(BaseModel):
    """Advice with alternatives — non-terminal (unlike Verdict, the recipient decides)."""

    recommendation: str = Field(description="The recommended course of action.")
    rationale: str = Field(description="Why this is recommended.")
    alternatives: list[str] = Field(
        default_factory=list, description="Other viable options considered."
    )
    tradeoffs: str | None = Field(
        default=None, description="What the recommendation gives up relative to alternatives."
    )


# ---------------------------------------------------------------------------
# Analysis — measurement
# ---------------------------------------------------------------------------


class AnalysisResult(BaseModel):
    """One measured metric from an experiment or analysis — quantitative, not prose."""

    metric: str = Field(description="What was measured (name the metric precisely).")
    value: float = Field(description="The measured value.")
    ci_95: tuple[float, float] | None = Field(
        default=None, description="95% confidence interval as (low, high), if computed."
    )
    p_value: float | None = Field(
        default=None, description="Significance level, if a hypothesis was tested."
    )


class ComplexityScore(BaseModel):
    """A normalized estimate of task complexity, with justification."""

    score: float = Field(
        ge=0.0, le=1.0, description="Complexity from 0 (trivial) to 1 (multi-phase, deep)."
    )
    rationale: str = Field(description="Why this score — the drivers of the complexity.")


# ---------------------------------------------------------------------------
# Planning / coordination
# ---------------------------------------------------------------------------


class ExecutionPlan(BaseModel):
    """An ordered plan of work — what an orchestrator or planner hands down for execution."""

    steps: list[str] = Field(description="Ordered steps, each an actionable unit of work.")
    dependencies: list[str] = Field(
        default_factory=list,
        description="Cross-step or external dependencies that constrain ordering.",
    )
    exit_criteria: str | None = Field(
        default=None, description="The condition that means the plan is complete."
    )


class TaskAssignment(BaseModel):
    """A unit of work delegated to an executor — the coordination primitive."""

    task: str = Field(description="The unit of work, stated as a concrete objective.")
    assignee: str = Field(description="Who/what executes it (a role, agent, or actor).")
    inputs: list[str] = Field(
        default_factory=list, description="What the assignee needs to start (artifacts, context)."
    )
    exit_criteria: str | None = Field(
        default=None, description="How to know this assignment is done."
    )
    depends_on: list[str] = Field(
        default_factory=list, description="Other tasks that must complete first."
    )
    modes: list[str] = Field(
        default_factory=list,
        description="Cognitive modes to overlay on the assignee for THIS task "
        "(e.g. adversarial, premortem, evidential) — override the role's "
        "defaults only when the subtask calls for a specific reasoning style. "
        "Leave empty to use the role's defaults.",
    )


class DesignSpec(BaseModel):
    """A design or architecture decision — structural output before any code."""

    summary: str = Field(description="The design in one paragraph — what is being built and how.")
    components: list[str] = Field(
        default_factory=list, description="The parts and their responsibilities."
    )
    decisions: list[str] = Field(
        default_factory=list, description="Key design decisions made, each with its 'why'."
    )
    alternatives: list[str] = Field(
        default_factory=list, description="Designs considered and rejected, and why."
    )


# ---------------------------------------------------------------------------
# Production — what was made / done
# ---------------------------------------------------------------------------


class ArtifactProduced(BaseModel):
    """A concrete build output — code, config, or compiled artifact (prose → Document)."""

    path: str = Field(description="Where the artifact lives (path, URL, or identifier).")
    kind: str = Field(description="What it is: code | config | build | data | schema | ...")
    description: str | None = Field(default=None, description="What the artifact contains or does.")
    verified: bool = Field(
        default=False,
        description="Whether the artifact was actually checked to work (tested/run), not just produced.",
    )


class VerificationResult(BaseModel):
    """The outcome of running a test or verification suite."""

    suite: str = Field(description="What was run (test suite, check, or command).")
    passed: bool = Field(description="Whether it passed. Report honestly, even on failure.")
    coverage: float | None = Field(
        default=None, description="Fraction of the target exercised (0–1), if known."
    )
    gaps: list[str] = Field(
        default_factory=list, description="What remains untested or unverified."
    )


class Document(BaseModel):
    """Authored prose — docs, summaries, translations (distinct from ArtifactProduced)."""

    title: str = Field(description="The document title or subject.")
    content: str = Field(description="The full authored content.")
    format: str | None = Field(
        default=None, description="markdown | plain | html | ... (default markdown)."
    )
    audience: str | None = Field(
        default=None, description="Who this is written for — shapes tone and depth."
    )


class OperationOutcome(BaseModel):
    """The result of acting on a live system — what changed and how to undo it."""

    action: str = Field(description="The operation performed (deploy, migrate, restart, ...).")
    target: str = Field(description="The system, service, or environment acted on.")
    status: str = Field(description="succeeded | failed | partial.")
    changes: list[str] = Field(
        default_factory=list, description="What actually changed in the system."
    )
    rollback: str | None = Field(
        default=None, description="How to reverse this operation if needed."
    )


# ---------------------------------------------------------------------------
# Generative / retrospective
# ---------------------------------------------------------------------------


class Proposal(BaseModel):
    """A novel idea framed for evaluation (not yet a plan)."""

    idea: str = Field(description="The proposal, stated concretely.")
    value_proposition: str = Field(description="Why it matters — the benefit if it works.")
    feasibility: str | None = Field(
        default=None, description="How realistic it is, and what it would take."
    )
    risks: list[str] = Field(
        default_factory=list, description="What could make it fail or backfire."
    )


class Postmortem(BaseModel):
    """A blameless retrospective — what happened and what to change."""

    summary: str = Field(description="What happened, in one paragraph.")
    timeline: list[str] = Field(default_factory=list, description="Ordered sequence of events.")
    root_cause: str = Field(description="The underlying cause, traced past proximate triggers.")
    contributing_factors: list[str] = Field(
        default_factory=list, description="Conditions that made the outcome more likely."
    )
    action_items: list[str] = Field(
        default_factory=list, description="Concrete changes to prevent recurrence."
    )


# ---------------------------------------------------------------------------
# Universal
# ---------------------------------------------------------------------------


class EscalationRequest(BaseModel):
    """Hand off to a human or higher authority — the universal escape hatch."""

    reason: str = Field(
        description="Why escalation is needed — the blocker or decision beyond your authority."
    )
    context: dict = Field(
        default_factory=dict,
        description="Structured context the recipient needs to decide (relevant state, options).",
    )
    blocking: bool = Field(
        default=True, description="Whether work cannot continue until this is resolved."
    )
    from_role: str | None = Field(default=None, description="The role raising the escalation.")


class SpawnRequest(BaseModel):
    """Add a new operation to the RUNNING workflow — emit when work beyond the
    current plan is discovered. Grows the live DAG without halting it (reactive
    self-expansion). The emitting node becomes the new op's upstream by default."""

    instruction: str = Field(
        description="The new unit of work, stated as a concrete, self-contained objective."
    )
    assignee: str | None = Field(
        default=None,
        description="Role to execute it (researcher, implementer, critic, ...). "
        "Omit to reuse the emitter's own role/branch.",
    )
    operation: str = Field(
        default="operate",
        description="lionagi operation to run: operate | chat | communicate | ReAct. Default operate.",
    )
    independent: bool = Field(
        default=False,
        description="If true the new op starts immediately with no dependency on you. "
        "If false (default) it runs after you and inherits your output as context.",
    )
    reason: str | None = Field(
        default=None,
        description="Why this work is needed now and why it fell outside the original plan.",
    )


# ---------------------------------------------------------------------------
# Operable builder
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _field_name(model: type[BaseModel]) -> str:
    """CamelCase model class name -> snake_case Spec name."""
    return _CAMEL_RE.sub("_", model.__name__).lower()


def build_emission_operable(
    emits: tuple[type[BaseModel], ...], /, *, name: str = "emissions"
) -> Operable | None:
    """Build an :class:`Operable` from an emission tuple.

    Returns ``None`` when *emits* is empty (the role declares no structured
    emission contract). For a non-empty contract, ``EscalationRequest`` is
    always appended — any role that emits anything may also escalate.
    """
    models = tuple(emits)
    if not models:
        return None
    if EscalationRequest not in models:
        models = (*models, EscalationRequest)
    specs = tuple(Spec(m, name=_field_name(m)) for m in models)
    return Operable(specs, name=name)
