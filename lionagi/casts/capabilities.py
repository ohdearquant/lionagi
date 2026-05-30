# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from lionagi.ln.types import Operable, Spec

__all__ = (
    "Finding",
    "Verdict",
    "ComplianceVerdict",
    "RiskAssessment",
    "AnalysisResult",
    "Conflict",
    "Gap",
    "ExecutionPlan",
    "ComplexityScore",
    "ArtifactProduced",
    "VerificationResult",
    "EscalationRequest",
    "ROLE_CAPABILITIES",
    "capability_models",
    "capability_operable",
)


# ---------------------------------------------------------------------------
# Capability payload models
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    description: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    severity: str | None = None
    evidence: str | None = None
    source: str | None = None


class Verdict(BaseModel):
    verdict: str
    rationale: str
    evidence: str | None = None
    reversible_by: str | None = None


class ComplianceVerdict(BaseModel):
    verdict: str
    control: str
    evidence_refs: list[str] = Field(default_factory=list)


class RiskAssessment(BaseModel):
    failure_mode: str
    likelihood: float = Field(ge=0.0, le=1.0)
    impact: float = Field(ge=0.0, le=1.0)
    mitigation: str | None = None


class AnalysisResult(BaseModel):
    metric: str
    value: float
    ci_95: tuple[float, float] | None = None
    p_value: float | None = None


class Conflict(BaseModel):
    sources: list[str]
    nature: str


class Gap(BaseModel):
    area: str
    what_is_unknown: str


class ExecutionPlan(BaseModel):
    steps: list[str]
    dependencies: list[str] = Field(default_factory=list)
    exit_criteria: str | None = None


class ComplexityScore(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    rationale: str


class ArtifactProduced(BaseModel):
    path: str
    kind: str
    description: str | None = None
    verified: bool = False


class VerificationResult(BaseModel):
    suite: str
    passed: bool
    coverage: float | None = None
    gaps: list[str] = Field(default_factory=list)


class EscalationRequest(BaseModel):
    reason: str
    context: dict = Field(default_factory=dict)
    blocking: bool = True
    from_role: str | None = None


# ---------------------------------------------------------------------------
# Role → capability model mapping
# ---------------------------------------------------------------------------

ROLE_CAPABILITIES: dict[str, tuple[type[BaseModel], ...]] = {
    # Evaluation / quality
    "critic": (Verdict, Finding),
    "reviewer": (Verdict, Finding),
    "auditor": (ComplianceVerdict, Finding),
    "arbitrator": (Verdict,),
    "evaluator": (Verdict, Finding),
    "tester": (VerificationResult, Finding),
    # Research / discovery
    "researcher": (Finding, Conflict, Gap),
    "analyst": (AnalysisResult, Finding),
    "explorer": (Finding, Gap),
    "investigator": (Finding, Gap),
    "troubleshooter": (Finding,),
    "assessor": (RiskAssessment, Finding),
    "contrarian": (Finding,),
    "commentator": (Finding,),
    "synthesizer": (Finding, Conflict),
    # Planning / coordination
    "orchestrator": (ExecutionPlan,),
    "strategist": (ComplexityScore, ExecutionPlan),
    "planner": (ExecutionPlan,),
    "coordinator": (ExecutionPlan,),
    "architect": (ExecutionPlan, ArtifactProduced),
    "modeler": (ArtifactProduced,),
    "innovator": (Finding,),
    "suggester": (Finding,),
    # Implementation / production
    "implementer": (ArtifactProduced, VerificationResult),
    "prototyper": (ArtifactProduced, VerificationResult),
    "refactorer": (ArtifactProduced, VerificationResult),
    "migrator": (ArtifactProduced, VerificationResult),
    "deployer": (ArtifactProduced,),
    "operator": (ArtifactProduced,),
    # Content
    "writer": (ArtifactProduced,),
    "translator": (ArtifactProduced,),
    "scribe": (ArtifactProduced,),
    "curator": (ArtifactProduced,),
    # Communication / facilitation
    "facilitator": (Finding,),
    "negotiator": (Finding,),
    "mentor": (Finding,),
    "persona": (Finding,),
    "responder": (Finding,),
    "postmortem_lead": (Finding,),
    "entrepreneur": (Finding,),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _field_name(model: type[BaseModel]) -> str:
    """Convert a CamelCase model class name to snake_case for use as a Spec name."""
    return _CAMEL_RE.sub("_", model.__name__).lower()


# ---------------------------------------------------------------------------
# Public builder API
# ---------------------------------------------------------------------------


def capability_models(role: str) -> tuple[type[BaseModel], ...]:
    """Return the full capability model tuple for *role*, always including EscalationRequest.

    For roles not present in ROLE_CAPABILITIES the base is empty, so the
    return value is ``(EscalationRequest,)``.
    """
    base = ROLE_CAPABILITIES.get(role, ())
    if EscalationRequest not in base:
        return (*base, EscalationRequest)
    return base


def capability_operable(role: str) -> Operable | None:
    """Build an :class:`~lionagi.ln.types.Operable` for *role*.

    Returns ``None`` when the role has no capabilities mapped (i.e. is absent
    from :data:`ROLE_CAPABILITIES`).  For known roles the returned Operable
    always includes an ``escalation_request`` spec in addition to the
    role-specific models.
    """
    if role not in ROLE_CAPABILITIES:
        return None
    models = capability_models(role)
    specs = tuple(Spec(m, name=_field_name(m)) for m in models)
    return Operable(specs, name=f"{role}_capabilities")
