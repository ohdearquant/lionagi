# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""casts — composable agent configuration: patterns, profiles, packs, and emission contracts."""

from .emission import (
    SPAWN_ALLOWED_OPERATIONS,
    AnalysisResult,
    ArtifactProduced,
    ComplexityScore,
    ComplianceVerdict,
    Conflict,
    DesignSpec,
    Diagnosis,
    Document,
    EscalationRequest,
    ExecutionPlan,
    Finding,
    Gap,
    Objection,
    OperationOutcome,
    Postmortem,
    Proposal,
    Recommendation,
    RiskAssessment,
    SpawnRequest,
    Synthesis,
    TaskAssignment,
    Verdict,
    VerificationResult,
    build_emission_operable,
    field_name_for,
)
from .pack import Pack, RoleConfig, RolePolicy
from .pattern import Mode, Pattern, PatternKind, Role, list_modes, list_roles
from .profile import Profile

__all__ = (
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
