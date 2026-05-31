# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pydantic v2 models for Charter DSL v0.

These models represent the *source document* schema — the YAML that
charter authors write.  They are distinct from the ADR-0047 runtime
types (AgentCharter, SessionCharter, etc.) which are compiler outputs.

Spec: docs/governance/charter-dsl-v0.md
Style: docs/governance/standards/dsl-style.md
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ActorIdSource",
    "AgentDef",
    "AttachDef",
    "AttachLevel",
    "BreakGlassAttestation",
    "BreakGlassDef",
    "BreakGlassNotification",
    "CharterDocument",
    "CharterKind",
    "CharterMetadata",
    "CharterStatus",
    "ConflictType",
    "ConstraintDef",
    "Enforcement",
    "EvidenceDef",
    "ManagerSurface",
    "PermissionResolution",
    "PermissionRule",
    "PermissionsDef",
    "Ratification",
    "RegistryCategory",
    "RegistryDef",
    "RegistryEntry",
    "SodDef",
    "SodRule",
    "SodScope",
    "TraceDef",
]

_EXECUTABLE_TOKENS = re.compile(r"__import__|eval\(|exec\(|lambda\s|subprocess")

_WILDCARD_CHARS = re.compile(r"[*?]")

_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

_DURATION_PATTERN = re.compile(r"^(\d+)(m|h)$")


def _reject_executable(v: str, field_name: str) -> str:
    if _EXECUTABLE_TOKENS.search(v):
        raise ValueError(f"Executable token detected in {field_name}: {v!r}")
    return v


def _reject_wildcards(v: str, field_name: str) -> str:
    if _WILDCARD_CHARS.search(v):
        raise ValueError(f"Wildcards are invalid in v0 ({field_name}): {v!r}")
    return v


def _duration_minutes(v: str) -> int:
    m = _DURATION_PATTERN.match(v)
    if not m:
        raise ValueError(f"Invalid duration format: {v!r}")
    amount, unit = int(m.group(1)), m.group(2)
    return amount * 60 if unit == "h" else amount


# ──────────────────────────────── Enums ────────────────────────────────


class CharterKind(str, Enum):
    AGENT = "agent_charter"
    SESSION = "session_charter"


class CharterStatus(str, Enum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class ActorIdSource(str, Enum):
    BRANCH_ID = "branch_id"
    SESSION_ACTOR_ID = "session_actor_id"


class RegistryCategory(str, Enum):
    TOOL = "tool"
    MODEL = "model"
    MCP_ENDPOINT = "mcp_endpoint"
    URL = "url"
    PATH_PREFIX = "path_prefix"


class ManagerSurface(str, Enum):
    ACTION = "ActionManager"
    MESSAGE = "MessageManager"
    IMODEL = "iModelManager"
    DATALOGGER = "DataLogger"


class Enforcement(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"
    ADVISORY = "ADVISORY"


class AttachLevel(str, Enum):
    CLASS = "class"
    ACTION = "action"


class ConflictType(str, Enum):
    TRANSACTION_DUAL_CONTROL = "transaction_dual_control"
    RECORD_CUSTODY = "record_custody"
    AUDIT_INDEPENDENCE = "audit_independence"
    APPROVAL_CHAIN = "approval_chain"
    ACCESS_CONTROL = "access_control"


class SodScope(str, Enum):
    SESSION = "session"
    TASK = "task"
    GLOBAL = "global"


# ──────────────────────────────── Models ───────────────────────────────


class Ratification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hash: str | None = Field(
        default=None,
        description="sha256:<64-hex-chars>, required for accepted/superseded.",
    )
    signed_at: str | None = Field(
        default=None,
        description="ISO 8601 UTC timestamp.",
    )

    @field_validator("hash", mode="before")
    @classmethod
    def _validate_hash(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _HASH_PATTERN.match(v):
            raise ValueError(f"Invalid hash format (expected sha256:<64hex>): {v!r}")
        return v


class CharterMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charter_id: str = Field(description="Unique charter identifier.")
    version: str = Field(description="Semver version string.")
    status: CharterStatus = Field(description="Charter lifecycle status.")
    policy_release: str = Field(description="Policy release identifier.")
    authored_by: str = Field(description="Author identity.")
    implemented_by: str = Field(description="Implementer identity.")
    ratification: Ratification = Field(description="Ratification hash and timestamp.")

    @field_validator("version", mode="before")
    @classmethod
    def _validate_semver(cls, v: str) -> str:
        if not _SEMVER_PATTERN.match(v):
            raise ValueError(f"Invalid semver: {v!r}")
        return v

    @field_validator("charter_id", "policy_release", "authored_by", "implemented_by", mode="after")
    @classmethod
    def _reject_exec_in_strings(cls, v: str) -> str:
        return _reject_executable(v, "metadata string field")


class AgentDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(description="Unique agent identifier.")
    actor_id_source: ActorIdSource = Field(description="How the runtime resolves actor identity.")
    role: str = Field(description="Agent role name.")
    allowed_models: list[str] = Field(description="Allowed model identifiers.")
    allowed_tools: list[str] = Field(description="Allowed tool identifiers.")
    allowed_operations: list[str] | None = Field(
        default=None,
        description="Allowed operation types (optional).",
    )

    @field_validator("agent_id", "role", mode="after")
    @classmethod
    def _reject_exec(cls, v: str) -> str:
        return _reject_executable(v, "agent field")

    @field_validator("allowed_tools", mode="after")
    @classmethod
    def _reject_tool_wildcards(cls, v: list[str]) -> list[str]:
        for tool in v:
            _reject_wildcards(tool, "allowed_tools")
            _reject_executable(tool, "allowed_tools")
        return v


class RegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: RegistryCategory = Field(description="Registry category.")
    value: str = Field(description="Exact registry value (no wildcards).")
    scope: str = Field(description="Scope level.")
    scope_id: str = Field(description="Scope target identifier.")
    reason: str = Field(description="Why this entry is registered.")
    evidence_refs: list[str] = Field(description="Evidence reference IDs.")

    @field_validator("value", mode="after")
    @classmethod
    def _reject_value_wildcards(cls, v: str) -> str:
        _reject_wildcards(v, "registry value")
        return _reject_executable(v, "registry value")

    @field_validator("reason", mode="after")
    @classmethod
    def _reject_reason_exec(cls, v: str) -> str:
        return _reject_executable(v, "registry reason")

    @model_validator(mode="after")
    def _path_prefix_format(self) -> RegistryEntry:
        if self.category == RegistryCategory.PATH_PREFIX:
            if not self.value.startswith("/") or not self.value.endswith("/"):
                raise ValueError(f"path_prefix must start and end with '/': {self.value!r}")
        return self


class RegistryDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: str = Field(description="Snapshot timing.")
    entries: list[RegistryEntry] = Field(
        default_factory=list,
        description="Registry entries.",
    )


class AttachDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: AttachLevel = Field(description="Attachment level.")
    tool_class: str | None = Field(
        default=None,
        description="Required when level is 'class'.",
    )
    action: str | None = Field(
        default=None,
        description="Required when level is 'action'.",
    )
    tools: list[str] | None = Field(
        default=None,
        description="Optional tool scope for action-level.",
    )

    @model_validator(mode="after")
    def _check_level_fields(self) -> AttachDef:
        if self.level == AttachLevel.CLASS and not self.tool_class:
            raise ValueError("attach.level 'class' requires 'tool_class'")
        if self.level == AttachLevel.ACTION and not self.action:
            raise ValueError("attach.level 'action' requires 'action'")
        return self


class EvidenceDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[str] = Field(description="Required evidence types.")


class ConstraintDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(description="Unique constraint identifier.")
    description: str = Field(description="What this constraint enforces.")
    gate_id: str | None = Field(
        default=None,
        description="Gate binding (mutually exclusive with hook_name).",
    )
    hook_name: str | None = Field(
        default=None,
        description="Hook binding (mutually exclusive with gate_id).",
    )
    hook_phase: str | None = Field(
        default=None,
        description="Required when hook_name is set.",
    )
    manager_surface: ManagerSurface = Field(
        description="Which manager surface this constraint targets."
    )
    enforcement: Enforcement = Field(description="Enforcement level.")
    attach: AttachDef = Field(description="Attachment specification.")
    evidence: EvidenceDef = Field(description="Evidence requirements.")

    @field_validator("enforcement", mode="before")
    @classmethod
    def _normalize_enforcement(cls, v: object) -> object:
        if isinstance(v, str):
            return v.upper()
        return v

    @model_validator(mode="after")
    def _exactly_one_binding(self) -> ConstraintDef:
        has_gate = self.gate_id is not None
        has_hook = self.hook_name is not None
        if has_gate and has_hook:
            raise ValueError(
                f"Constraint {self.constraint_id!r}: both gate_id and "
                f"hook_name present — exactly one required."
            )
        if not has_gate and not has_hook:
            raise ValueError(
                f"Constraint {self.constraint_id!r}: neither gate_id nor "
                f"hook_name present — exactly one required."
            )
        if has_hook and not self.hook_phase:
            raise ValueError(f"Constraint {self.constraint_id!r}: hook_name requires hook_phase.")
        return self

    @field_validator("constraint_id", "description", mode="after")
    @classmethod
    def _reject_exec(cls, v: str) -> str:
        return _reject_executable(v, "constraint field")


class SodRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(description="Unique SoD rule identifier.")
    conflict_type: ConflictType = Field(description="Conflict category.")
    roles: list[str] = Field(description="Exactly two conflicting roles.")
    scope: SodScope = Field(description="Scope of the conflict rule.")
    because: str = Field(description="Rationale for this separation.")

    @field_validator("roles", mode="after")
    @classmethod
    def _exactly_two_roles(cls, v: list[str]) -> list[str]:
        if len(v) != 2:
            raise ValueError(f"SoD rule requires exactly 2 roles, got {len(v)}")
        if v[0] == v[1]:
            raise ValueError("SoD rule roles must be distinct")
        return v

    @field_validator("rule_id", "because", mode="after")
    @classmethod
    def _reject_exec(cls, v: str) -> str:
        return _reject_executable(v, "sod field")


class SodDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool = Field(description="Whether SoD enforcement is active.")
    rules: list[SodRule] = Field(
        default_factory=list,
        description="SoD conflict rules.",
    )


class PermissionResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specificity_order: list[str] = Field(description="Resolution precedence order.")
    tie: str = Field(description="Tie-break policy.")


class PermissionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(description="Unique permission rule identifier.")
    scope: str = Field(description="Permission scope level.")
    roles: list[str] = Field(description="Affected roles.")
    action: str = Field(description="Action type.")
    tools: list[str] | None = Field(
        default=None,
        description="Tool targets.",
    )
    resources: list[str] | None = Field(
        default=None,
        description="Resource targets.",
    )
    requires_evidence: list[str] | None = Field(
        default=None,
        description="Required evidence for allow rules.",
    )
    because: str = Field(description="Rationale for this permission.")

    @field_validator("rule_id", "because", mode="after")
    @classmethod
    def _reject_exec(cls, v: str) -> str:
        return _reject_executable(v, "permission field")

    @field_validator("tools", mode="after")
    @classmethod
    def _reject_tool_wildcards(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        for tool in v:
            _reject_wildcards(tool, "permission tools")
        return v


class PermissionsDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str = Field(description="Default policy (must be 'deny').")
    resolution: PermissionResolution = Field(description="Resolution strategy.")
    allow: list[PermissionRule] = Field(
        default_factory=list,
        description="Allow rules.",
    )
    deny: list[PermissionRule] = Field(
        default_factory=list,
        description="Deny rules.",
    )


class BreakGlassAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver_role: str = Field(description="Role that must approve.")
    requires_reason: bool = Field(
        default=True,
        description="Whether a reason is mandatory.",
    )


class BreakGlassNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(description="Notification target.")
    on_events: list[str] = Field(description="Events that trigger notify.")


class BreakGlassDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(description="Whether break-glass is enabled.")
    expires_after: str = Field(description="Max window duration (e.g. '15m', '30m').")
    attestation: BreakGlassAttestation = Field(description="Attestation requirements.")
    temporary_grants: list[str] = Field(
        default_factory=list,
        description="Temporary tool grants during break-glass.",
    )
    notifications: list[BreakGlassNotification] = Field(
        default_factory=list,
        description="Notification channels.",
    )
    evidence: EvidenceDef = Field(description="Evidence requirements for break-glass.")

    @field_validator("expires_after", mode="after")
    @classmethod
    def _max_30m(cls, v: str) -> str:
        minutes = _duration_minutes(v)
        if minutes > 30:
            raise ValueError(f"break_glass.expires_after exceeds 30m limit: {v!r}")
        return v

    @field_validator("temporary_grants", mode="after")
    @classmethod
    def _reject_grant_wildcards(cls, v: list[str]) -> list[str]:
        for g in v:
            _reject_wildcards(g, "temporary_grants")
        return v


class TraceDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stamp: list[str] = Field(description="Fields stamped on every span.")
    require_spans: list[str] = Field(description="Canonical span names required.")
    require_evidence: list[str] = Field(
        description="Evidence types required.",
    )


class CharterDocument(BaseModel):
    """Top-level Charter DSL v0 document model."""

    model_config = ConfigDict(extra="forbid")

    charter_dsl: str = Field(description="DSL version string.")
    kind: CharterKind = Field(description="Charter kind.")
    metadata: CharterMetadata = Field(description="Charter metadata block.")
    agents: list[AgentDef] = Field(description="Agent definitions.")
    registry: RegistryDef = Field(description="Registry block.")
    constraints: list[ConstraintDef] = Field(description="Constraint definitions.")
    sod: SodDef = Field(description="Separation of duties block.")
    permissions: PermissionsDef = Field(description="Permissions block.")
    break_glass: BreakGlassDef | None = Field(
        default=None,
        description="Optional break-glass block.",
    )
    trace: TraceDef = Field(description="Trace requirements block.")

    @field_validator("charter_dsl", mode="after")
    @classmethod
    def _check_dsl_version(cls, v: str) -> str:
        if v != "0.1":
            raise ValueError(f"Unsupported charter_dsl version: {v!r}")
        return v
