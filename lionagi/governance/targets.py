# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Runtime target types produced by the Charter DSL compiler."""

from __future__ import annotations

from pydantic import Field

from lionagi.models.schema_model import SchemaModel

from .dsl import Enforcement

__all__ = [
    "CharterPermissionPolicy",
    "EvidenceRequirement",
    "GateRegistration",
    "PolicyPin",
    "RegistryEntry",
    "SoDRule",
    "TraceExpectation",
]


class GateRegistration(SchemaModel):
    target_tool: str = ""
    enforcement: Enforcement = Enforcement.HARD
    gate_function: str = ""
    charter_ref: str = ""


class RegistryEntry(SchemaModel):
    """Runtime registry allowlist entry (narrower than ``dsl.RegistryEntry``)."""

    tool_name: str = ""
    scope: str = ""
    allowed: bool = True
    source_charter: str = ""


class SoDRule(SchemaModel):
    conflict_type: str = ""
    role_a: str = ""
    role_b: str = ""
    scope: str = ""


class EvidenceRequirement(SchemaModel):
    event_type: str = ""
    retention_tier: str = "PROTECTED"
    hash_algorithm: str = "sha256"
    sensitive_fields: list[str] = Field(default_factory=list)


class TraceExpectation(SchemaModel):
    span_name: str = ""
    required_attributes: dict[str, str] = Field(default_factory=dict)
    sampling_rate: float = 1.0


class CharterPermissionPolicy(SchemaModel):
    tool_pattern: str = ""
    mode: str = ""
    scope: str = ""


class PolicyPin(SchemaModel):
    charter_hash: str = ""
    version: str = ""
    activated_at: str = ""
