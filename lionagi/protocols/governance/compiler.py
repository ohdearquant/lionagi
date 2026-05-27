# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Six-phase Charter DSL compiler (P14).

Phases:
  1  Parse    — charter is already a validated CharterDocument (done by P13).
  2  Validate — cross-block consistency warnings.
  3  Resolve  — bind symbolic gate targets; collect unresolved names.
  4  Emit     — produce runtime target objects from each DSL block.
  5  Hash     — sha256 of canonical charter JSON.
  6  Activate — verify ratification hash (fail-closed); emit PolicyPin.
"""

from __future__ import annotations

import datetime
import hashlib

from pydantic import BaseModel, Field

from lionagi.protocols.governance.dsl import (
    AttachLevel,
    CharterDocument,
    CharterStatus,
)
from lionagi.protocols.governance.targets import (
    CharterPermissionPolicy,
    EvidenceRequirement,
    GateRegistration,
    PolicyPin,
    SoDRule,
    TraceExpectation,
)
from lionagi.protocols.governance.targets import RegistryEntry as RuntimeRegistryEntry

__all__ = [
    "CharterActivationError",
    "CharterCompiler",
    "CompilationResult",
]


class CharterActivationError(Exception):
    """Raised when charter activation fails (hash mismatch or unresolved target)."""


class CompilationResult(BaseModel):
    """All runtime artifacts produced by one successful compilation run."""

    gates: list[GateRegistration] = Field(default_factory=list)
    registry: list[RuntimeRegistryEntry] = Field(default_factory=list)
    sod_rules: list[SoDRule] = Field(default_factory=list)
    evidence_reqs: list[EvidenceRequirement] = Field(default_factory=list)
    trace_expectations: list[TraceExpectation] = Field(default_factory=list)
    permissions: list[CharterPermissionPolicy] = Field(default_factory=list)
    policy_pin: PolicyPin | None = None
    warnings: list[str] = Field(default_factory=list)


class CharterCompiler:
    """Compile a validated CharterDocument into runtime targets."""

    def compile(self, charter: CharterDocument) -> CompilationResult:
        result = CompilationResult()

        # Phase 2: Validate
        result.warnings = self._validate(charter)

        # Phase 3: Resolve — collect unresolved gate targets before emitting
        unresolved = self._resolve(charter)

        # Phase 5: Hash (computed before emit so charter_ref can reference it)
        charter_hash = hashlib.sha256(charter.model_dump_json().encode()).hexdigest()

        # Phase 4: Emit
        self._emit_gates(charter, result, charter_hash)
        self._emit_registry(charter, result)
        self._emit_sod(charter, result)
        self._emit_evidence(charter, result)
        self._emit_trace(charter, result)
        self._emit_permissions(charter, result)

        # Phase 6: Activate — fail closed on any error
        if unresolved:
            raise CharterActivationError(f"Unresolved gate target: {unresolved[0]}")

        ratification_hash = charter.metadata.ratification.hash
        if ratification_hash is not None:
            # Stored format is "sha256:<64hex>"; computed hash is plain hex.
            stored_hex = ratification_hash.removeprefix("sha256:")
            if stored_hex != charter_hash:
                raise CharterActivationError("Hash mismatch: charter may have been tampered")

        version = charter.metadata.version if charter.metadata else "1.0"
        result.policy_pin = PolicyPin(
            charter_hash=charter_hash,
            version=version,
            activated_at=datetime.datetime.utcnow().isoformat(),
        )

        return result

    # ── Phase 2 ──────────────────────────────────────────────────────────

    def _validate(self, charter: CharterDocument) -> list[str]:
        warnings: list[str] = []

        if charter.metadata.status == CharterStatus.DRAFT:
            warnings.append("Charter status is DRAFT; not ratified.")
        if charter.metadata.status == CharterStatus.PROPOSED:
            warnings.append("Charter status is PROPOSED; not yet accepted.")
        if charter.metadata.status == CharterStatus.SUPERSEDED:
            warnings.append("Charter status is SUPERSEDED; may be stale.")

        if not charter.agents:
            warnings.append("No agents defined in charter.")
        if not charter.registry.entries:
            warnings.append("Registry is empty.")
        if not charter.constraints:
            warnings.append("No constraints defined.")
        if charter.break_glass is None:
            warnings.append("Optional break_glass block is absent.")
        if charter.sod.active and not charter.sod.rules:
            warnings.append("SoD is active but no rules are defined.")

        return warnings

    # ── Phase 3 ──────────────────────────────────────────────────────────

    def _resolve(self, charter: CharterDocument) -> list[str]:
        """Return names of gate targets that cannot be resolved in this charter."""
        registered_values = {entry.value for entry in charter.registry.entries}
        declared_roles = {agent.role for agent in charter.agents}

        unresolved: list[str] = []

        for constraint in charter.constraints:
            if constraint.gate_id is None:
                continue
            attach = constraint.attach
            if attach.level == AttachLevel.ACTION and attach.tools:
                for tool in attach.tools:
                    if tool not in registered_values:
                        unresolved.append(tool)
            elif attach.level == AttachLevel.CLASS and attach.tool_class:
                # Class-level gates bind an entire tool class; no per-tool
                # registry check is possible at this phase.
                pass

        for rule in charter.sod.rules:
            for role in rule.roles:
                if role not in declared_roles:
                    unresolved.append(role)

        return unresolved

    # ── Phase 4 helpers ──────────────────────────────────────────────────

    def _emit_gates(
        self,
        charter: CharterDocument,
        result: CompilationResult,
        charter_hash: str,
    ) -> None:
        for constraint in charter.constraints:
            if constraint.gate_id is None:
                continue
            attach = constraint.attach
            tools: list[str] = []
            if attach.level == AttachLevel.ACTION:
                tools = list(attach.tools or [attach.action or ""])
            elif attach.level == AttachLevel.CLASS:
                tools = [attach.tool_class or ""]

            for tool in tools:
                if not tool:
                    continue
                result.gates.append(
                    GateRegistration(
                        target_tool=tool,
                        enforcement=constraint.enforcement,
                        gate_function=constraint.gate_id,
                        charter_ref=charter_hash,
                    )
                )

    def _emit_registry(
        self,
        charter: CharterDocument,
        result: CompilationResult,
    ) -> None:
        charter_id = charter.metadata.charter_id
        for entry in charter.registry.entries:
            # Only TOOL category entries are meaningful as runtime tool registry.
            # Non-tool entries (MODEL, MCP_ENDPOINT, URL, PATH_PREFIX) are still
            # emitted so the full registry snapshot is preserved.
            result.registry.append(
                RuntimeRegistryEntry(
                    tool_name=entry.value,
                    scope=entry.scope,
                    allowed=True,
                    source_charter=charter_id,
                )
            )

    def _emit_sod(
        self,
        charter: CharterDocument,
        result: CompilationResult,
    ) -> None:
        for rule in charter.sod.rules:
            result.sod_rules.append(
                SoDRule(
                    conflict_type=rule.conflict_type.value,
                    role_a=rule.roles[0],
                    role_b=rule.roles[1],
                    scope=rule.scope.value,
                )
            )

    def _emit_evidence(
        self,
        charter: CharterDocument,
        result: CompilationResult,
    ) -> None:
        seen: set[str] = set()

        def _add(event_type: str) -> None:
            if event_type in seen:
                return
            seen.add(event_type)
            result.evidence_reqs.append(EvidenceRequirement(event_type=event_type))

        for constraint in charter.constraints:
            for ev in constraint.evidence.required:
                _add(ev)

        if charter.break_glass is not None:
            for ev in charter.break_glass.evidence.required:
                _add(ev)

        for ev in charter.trace.require_evidence:
            _add(ev)

    def _emit_trace(
        self,
        charter: CharterDocument,
        result: CompilationResult,
    ) -> None:
        stamp_attrs = {field: "" for field in charter.trace.stamp}
        for span_name in charter.trace.require_spans:
            result.trace_expectations.append(
                TraceExpectation(
                    span_name=span_name,
                    required_attributes=dict(stamp_attrs),
                    sampling_rate=1.0,
                )
            )

    def _emit_permissions(
        self,
        charter: CharterDocument,
        result: CompilationResult,
    ) -> None:
        for rule in charter.permissions.allow:
            targets = rule.tools or [rule.action]
            for target in targets:
                result.permissions.append(
                    CharterPermissionPolicy(
                        tool_pattern=target,
                        mode="allow",
                        scope=rule.scope,
                    )
                )

        for rule in charter.permissions.deny:
            targets = rule.tools or [rule.action]
            for target in targets:
                result.permissions.append(
                    CharterPermissionPolicy(
                        tool_pattern=target,
                        mode="deny",
                        scope=rule.scope,
                    )
                )
