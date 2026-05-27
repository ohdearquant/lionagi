# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Runtime target types produced by the Charter DSL compiler (P14).

These are distinct from the DSL source models in dsl.py.  Each class here
represents one compiled artifact family emitted by CharterCompiler.compile().
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from lionagi.protocols.governance.dsl import Enforcement

__all__ = [
    "CharterPermissionPolicy",
    "EvidenceRequirement",
    "GateRegistration",
    "PolicyPin",
    "RegistryEntry",
    "SoDRule",
    "TraceExpectation",
]


class GateRegistration(BaseModel):
    """Compiled gate binding: one entry per tool gated by a constraint."""

    target_tool: str = ""
    enforcement: Enforcement = Enforcement.HARD
    gate_function: str = ""
    charter_ref: str = ""


class RegistryEntry(BaseModel):
    """Compiled registry allowlist entry.

    Note: this is the *runtime* registry entry, narrower than the DSL source
    model ``lionagi.protocols.governance.dsl.RegistryEntry``.
    """

    tool_name: str = ""
    scope: str = ""
    allowed: bool = True
    source_charter: str = ""


class SoDRule(BaseModel):
    """Compiled separation-of-duties rule."""

    conflict_type: str = ""
    role_a: str = ""
    role_b: str = ""
    scope: str = ""


class EvidenceRequirement(BaseModel):
    """Compiled evidence requirement for a single event type."""

    event_type: str = ""
    retention_tier: str = "PROTECTED"
    hash_algorithm: str = "sha256"
    sensitive_fields: list[str] = Field(default_factory=list)


class TraceExpectation(BaseModel):
    """Compiled trace expectation for a required span."""

    span_name: str = ""
    required_attributes: dict[str, str] = Field(default_factory=dict)
    sampling_rate: float = 1.0


class CharterPermissionPolicy(BaseModel):
    """Compiled permission policy rule.

    Named ``CharterPermissionPolicy`` to avoid collision with
    ``lionagi.agent.permissions.PermissionPolicy``.
    """

    tool_pattern: str = ""
    mode: str = ""
    scope: str = ""


class PolicyPin(BaseModel):
    """Activation record produced after successful hash verification."""

    charter_hash: str = ""
    version: str = ""
    activated_at: str = ""
