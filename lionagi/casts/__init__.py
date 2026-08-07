# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""casts — composable agent configuration: patterns, profiles, packs, and emission contracts."""

from ..ln._lazy_init import lazy_import

_LAZY_MAP: dict[str, tuple[str, str | None]] = {
    "build_catalog": ("catalog", None),
    "SPAWN_ALLOWED_OPERATIONS": ("emission", None),
    "AnalysisResult": ("emission", None),
    "ArtifactProduced": ("emission", None),
    "ComplexityScore": ("emission", None),
    "ComplianceVerdict": ("emission", None),
    "Conflict": ("emission", None),
    "DesignSpec": ("emission", None),
    "Diagnosis": ("emission", None),
    "Document": ("emission", None),
    "EscalationRequest": ("emission", None),
    "ExecutionPlan": ("emission", None),
    "Finding": ("emission", None),
    "Gap": ("emission", None),
    "Objection": ("emission", None),
    "OperationOutcome": ("emission", None),
    "Postmortem": ("emission", None),
    "Proposal": ("emission", None),
    "Recommendation": ("emission", None),
    "RiskAssessment": ("emission", None),
    "SpawnRequest": ("emission", None),
    "Synthesis": ("emission", None),
    "TaskAssignment": ("emission", None),
    "Verdict": ("emission", None),
    "VerificationResult": ("emission", None),
    "build_emission_operable": ("emission", None),
    "field_name_for": ("emission", None),
    "Pack": ("pack", None),
    "RoleConfig": ("pack", None),
    "RolePolicy": ("pack", None),
    "Mode": ("pattern", None),
    "Pattern": ("pattern", None),
    "PatternKind": ("pattern", None),
    "Role": ("pattern", None),
    "list_modes": ("pattern", None),
    "list_roles": ("pattern", None),
    "Profile": ("profile", None),
}


def __getattr__(name: str):
    return lazy_import(name, _LAZY_MAP, __name__, globals())


__all__ = (
    # catalog (read-only metadata seam)
    "build_catalog",
    # pattern layer
    "Pattern",
    "PatternKind",
    "Role",
    "Mode",
    "list_roles",
    "list_modes",
    # profile
    "Profile",
    # pack layer
    "Pack",
    "RolePolicy",
    "RoleConfig",
    # emission builder
    "build_emission_operable",
    "field_name_for",
    "SPAWN_ALLOWED_OPERATIONS",
    # emission contracts — discovery
    "Finding",
    "Conflict",
    "Gap",
    "Diagnosis",
    "Synthesis",
    # emission contracts — judgement
    "Verdict",
    "ComplianceVerdict",
    "RiskAssessment",
    "Objection",
    "Recommendation",
    # emission contracts — analysis
    "AnalysisResult",
    "ComplexityScore",
    # emission contracts — planning / coordination
    "ExecutionPlan",
    "TaskAssignment",
    "DesignSpec",
    # emission contracts — production
    "ArtifactProduced",
    "VerificationResult",
    "Document",
    "OperationOutcome",
    # emission contracts — generative / retrospective
    "Proposal",
    "Postmortem",
    # emission contracts — universal
    "EscalationRequest",
    "SpawnRequest",
)
