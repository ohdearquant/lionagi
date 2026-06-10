# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the lionagi.casts public import surface."""

from __future__ import annotations

import importlib

_EXPECTED = (
    "Pattern",
    "PatternKind",
    "Role",
    "Mode",
    "list_roles",
    "list_modes",
    "Profile",
    "Pack",
    "RolePolicy",
    "RoleConfig",
    "build_emission_operable",
    "field_name_for",
    "SPAWN_ALLOWED_OPERATIONS",
    "Finding",
    "Conflict",
    "Gap",
    "Diagnosis",
    "Synthesis",
    "Verdict",
    "ComplianceVerdict",
    "RiskAssessment",
    "Objection",
    "Recommendation",
    "AnalysisResult",
    "ComplexityScore",
    "ExecutionPlan",
    "TaskAssignment",
    "DesignSpec",
    "ArtifactProduced",
    "VerificationResult",
    "Document",
    "OperationOutcome",
    "Proposal",
    "Postmortem",
    "EscalationRequest",
    "SpawnRequest",
)


def test_lionagi_casts_public_surface_imports():
    """``import lionagi.casts`` exposes its documented public surface."""
    mod = importlib.import_module("lionagi.casts")
    for name in _EXPECTED:
        assert hasattr(mod, name), f"{name!r} must be accessible from lionagi.casts"


def test_lionagi_casts_all_matches_expected():
    """``lionagi.casts.__all__`` contains exactly the declared public names."""
    mod = importlib.import_module("lionagi.casts")
    declared = set(mod.__all__)
    expected = set(_EXPECTED)
    missing = expected - declared
    extra = declared - expected
    assert not missing, f"Names missing from __all__: {sorted(missing)}"
    assert not extra, f"Undocumented names in __all__: {sorted(extra)}"


def test_field_name_for_standard_cases():
    """field_name_for produces correct snake_case for standard PascalCase names."""
    from pydantic import BaseModel

    from lionagi.casts import field_name_for

    def fn(name: str) -> str:
        return field_name_for(type(name, (BaseModel,), {"__name__": name}))

    assert fn("Finding") == "finding"
    assert fn("RiskAssessment") == "risk_assessment"
    assert fn("ComplianceVerdict") == "compliance_verdict"
    assert fn("SpawnRequest") == "spawn_request"
    assert fn("TaskAssignment") == "task_assignment"
    assert fn("OperationOutcome") == "operation_outcome"


def test_field_name_for_acronym_cases():
    """field_name_for handles acronym-led names without mangling."""
    from pydantic import BaseModel

    from lionagi.casts import field_name_for

    def fn(name: str) -> str:
        return field_name_for(type(name, (BaseModel,), {"__name__": name}))

    assert fn("CIResult") == "ci_result", f"got {fn('CIResult')!r}"
    assert fn("HTMLParser") == "html_parser", f"got {fn('HTMLParser')!r}"
    assert fn("URLConfig") == "url_config", f"got {fn('URLConfig')!r}"
    assert fn("HTTPSRequest") == "https_request", f"got {fn('HTTPSRequest')!r}"
