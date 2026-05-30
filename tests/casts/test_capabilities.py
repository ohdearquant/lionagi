# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from lionagi.casts.capabilities import (
    ROLE_CAPABILITIES,
    AnalysisResult,
    ArtifactProduced,
    ComplexityScore,
    ComplianceVerdict,
    Conflict,
    EscalationRequest,
    ExecutionPlan,
    Finding,
    Gap,
    RiskAssessment,
    Verdict,
    VerificationResult,
    _field_name,
    capability_models,
    capability_operable,
)
from lionagi.ln.types import Operable
from lionagi.session.session import Session
from lionagi.session.signal import StructuredOutput


def _roles_on_disk() -> set[str]:
    """Role names from lionagi/casts/roles/*.md, excluding TEMPLATE and modes/."""
    roles_dir = Path(__file__).parent.parent.parent / "lionagi" / "casts" / "roles"
    return {p.stem for p in roles_dir.glob("*.md") if p.stem != "TEMPLATE"}


# ---------------------------------------------------------------------------
# Every capability model instantiates with required fields
# ---------------------------------------------------------------------------


class TestCapabilityModels:
    def test_finding_instantiates(self):
        f = Finding(description="A finding", confidence=0.8)
        assert f.description == "A finding"
        assert f.confidence == 0.8

    def test_finding_confidence_clamps_high(self):
        with pytest.raises(Exception):
            Finding(description="bad", confidence=1.5)

    def test_finding_confidence_clamps_low(self):
        with pytest.raises(Exception):
            Finding(description="bad", confidence=-0.1)

    def test_finding_optional_fields_default_none(self):
        f = Finding(description="x")
        assert f.severity is None
        assert f.evidence is None
        assert f.source is None

    def test_verdict_instantiates(self):
        v = Verdict(verdict="approve", rationale="looks good")
        assert v.verdict == "approve"
        assert v.evidence is None

    def test_compliance_verdict_instantiates(self):
        cv = ComplianceVerdict(verdict="pass", control="ISO-27001-A.8.2")
        assert cv.evidence_refs == []

    def test_risk_assessment_instantiates(self):
        ra = RiskAssessment(failure_mode="OOM", likelihood=0.3, impact=0.9)
        assert ra.likelihood == 0.3

    def test_risk_assessment_likelihood_clamps(self):
        with pytest.raises(Exception):
            RiskAssessment(failure_mode="x", likelihood=1.5, impact=0.5)

    def test_analysis_result_instantiates(self):
        ar = AnalysisResult(metric="latency_p99", value=42.0)
        assert ar.ci_95 is None
        assert ar.p_value is None

    def test_conflict_instantiates(self):
        c = Conflict(sources=["a", "b"], nature="contradiction")
        assert len(c.sources) == 2

    def test_gap_instantiates(self):
        g = Gap(area="auth", what_is_unknown="session expiry semantics")
        assert g.area == "auth"

    def test_execution_plan_instantiates(self):
        ep = ExecutionPlan(steps=["step1", "step2"])
        assert ep.dependencies == []
        assert ep.exit_criteria is None

    def test_complexity_score_bounds(self):
        with pytest.raises(Exception):
            ComplexityScore(score=1.1, rationale="too high")

    def test_complexity_score_valid(self):
        cs = ComplexityScore(score=0.7, rationale="complex DB schema")
        assert cs.score == 0.7

    def test_artifact_produced_defaults(self):
        ap = ArtifactProduced(path="/out/file.py", kind="module")
        assert not ap.verified
        assert ap.description is None

    def test_verification_result_instantiates(self):
        vr = VerificationResult(suite="unit", passed=True, coverage=0.92)
        assert vr.gaps == []

    def test_escalation_request_defaults(self):
        er = EscalationRequest(reason="blocked on infra")
        assert er.blocking is True
        assert er.context == {}
        assert er.from_role is None

    def test_all_models_are_pydantic_base_model(self):
        models = [
            Finding,
            Verdict,
            ComplianceVerdict,
            RiskAssessment,
            AnalysisResult,
            Conflict,
            Gap,
            ExecutionPlan,
            ComplexityScore,
            ArtifactProduced,
            VerificationResult,
            EscalationRequest,
        ]
        for m in models:
            assert issubclass(m, BaseModel), f"{m.__name__} is not a BaseModel subclass"


# ---------------------------------------------------------------------------
# ROLE_CAPABILITIES covers all roles on disk
# ---------------------------------------------------------------------------


class TestRoleCapabilitiesCoverage:
    def test_all_disk_roles_in_role_capabilities(self):
        disk_roles = _roles_on_disk()
        missing = disk_roles - set(ROLE_CAPABILITIES)
        assert not missing, f"Roles on disk missing from ROLE_CAPABILITIES: {missing}"

    def test_no_extra_roles_beyond_disk(self):
        disk_roles = _roles_on_disk()
        extra = set(ROLE_CAPABILITIES) - disk_roles
        assert not extra, f"ROLE_CAPABILITIES has keys not on disk: {extra}"

    def test_no_empty_tuples_in_role_capabilities(self):
        for role, models in ROLE_CAPABILITIES.items():
            assert len(models) >= 1, f"{role!r} maps to an empty tuple"

    def test_all_values_are_pydantic_base_model_subclasses(self):
        for role, models in ROLE_CAPABILITIES.items():
            for m in models:
                assert issubclass(m, BaseModel), f"{role}: {m} is not a BaseModel subclass"


# ---------------------------------------------------------------------------
# capability_models() always includes EscalationRequest
# ---------------------------------------------------------------------------


class TestCapabilityModelsFunction:
    def test_researcher_always_includes_escalation(self):
        models = capability_models("researcher")
        assert EscalationRequest in models

    def test_unknown_role_returns_just_escalation(self):
        models = capability_models("no_such_role_xyz")
        assert models == (EscalationRequest,)

    def test_known_role_preserves_role_models(self):
        # critic maps to (Verdict, Finding); escalation appended
        models = capability_models("critic")
        assert EscalationRequest in models
        assert Verdict in models
        assert Finding in models

    @pytest.mark.parametrize("role", list(ROLE_CAPABILITIES))
    def test_every_role_includes_escalation(self, role):
        assert EscalationRequest in capability_models(role)

    def test_escalation_not_duplicated_when_already_present(self):
        # EscalationRequest should not appear twice even if role mapping included it
        models = capability_models("researcher")
        assert models.count(EscalationRequest) == 1


# ---------------------------------------------------------------------------
# capability_operable('researcher') builds and validates
# ---------------------------------------------------------------------------


class TestCapabilityOperable:
    def test_researcher_operable_not_none(self):
        op = capability_operable("researcher")
        assert op is not None
        assert isinstance(op, Operable)

    def test_researcher_operable_has_finding_spec(self):
        op = capability_operable("researcher")
        assert "finding" in op.allowed()

    def test_researcher_operable_has_escalation_spec(self):
        op = capability_operable("researcher")
        assert "escalation_request" in op.allowed()

    def test_researcher_operable_name(self):
        op = capability_operable("researcher")
        assert op.name == "researcher_capabilities"

    def test_unknown_role_returns_none(self):
        assert capability_operable("unknown_role_xyz") is None

    def test_field_name_helper_cases(self):
        assert _field_name(Finding) == "finding"
        assert _field_name(EscalationRequest) == "escalation_request"
        assert _field_name(VerificationResult) == "verification_result"
        assert _field_name(ComplianceVerdict) == "compliance_verdict"
        assert _field_name(AnalysisResult) == "analysis_result"
        assert _field_name(ExecutionPlan) == "execution_plan"
        assert _field_name(ComplexityScore) == "complexity_score"
        assert _field_name(ArtifactProduced) == "artifact_produced"
        assert _field_name(RiskAssessment) == "risk_assessment"

    def test_critic_operable_contains_verdict(self):
        op = capability_operable("critic")
        assert op is not None
        assert "verdict" in op.allowed()


# ---------------------------------------------------------------------------
# Session.observe(Finding) fires when StructuredOutput(data=Finding) emitted
# ---------------------------------------------------------------------------


class TestSessionObserveFinding:
    async def test_observe_finding_fires(self):
        s = Session()
        seen: list[Finding] = []
        s.observe(Finding, lambda f, _: seen.append(f))

        await s.default_branch.emit(
            StructuredOutput(data=Finding(description="SQL injection in query", confidence=0.95))
        )
        assert len(seen) == 1
        assert seen[0].description == "SQL injection in query"
        assert seen[0].confidence == 0.95

    async def test_observe_finding_does_not_fire_for_other_payload(self):
        s = Session()
        finding_seen: list = []
        verdict_seen: list = []
        s.observe(Finding, lambda f, _: finding_seen.append(f))
        s.observe(Verdict, lambda v, _: verdict_seen.append(v))

        await s.default_branch.emit(
            StructuredOutput(data=Verdict(verdict="approved", rationale="all good"))
        )
        assert finding_seen == []
        assert len(verdict_seen) == 1

    async def test_observe_finding_and_escalation_route_separately(self):
        s = Session()
        findings: list = []
        escalations: list = []
        s.observe(Finding, lambda f, _: findings.append(f))
        s.observe(EscalationRequest, lambda e, _: escalations.append(e))

        await s.default_branch.emit(StructuredOutput(data=Finding(description="gap found")))
        await s.default_branch.emit(StructuredOutput(data=EscalationRequest(reason="need infra")))

        assert len(findings) == 1
        assert len(escalations) == 1

    async def test_multiple_findings_all_dispatched(self):
        s = Session()
        seen: list = []
        s.observe(Finding, lambda f, _: seen.append(f.description))

        for desc in ["bug A", "bug B", "bug C"]:
            await s.default_branch.emit(StructuredOutput(data=Finding(description=desc)))

        assert seen == ["bug A", "bug B", "bug C"]
