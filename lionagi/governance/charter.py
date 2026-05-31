# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Charter DSL v0 parser, validator, and JSON Schema export.

- ``CharterParser.parse`` — YAML string → CharterDocument with line-specific errors
- ``parse_charter`` — convenience wrapper (raises pydantic.ValidationError)
- ``validate_charter`` — semantic validation returning structured errors
- ``charter_json_schema`` — JSON Schema for IDE integration

Spec: docs/governance/charter-dsl-v0.md §4 (validation rules)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lionagi.governance.dsl import (
    _EXECUTABLE_TOKENS,
    _WILDCARD_CHARS,
    CharterDocument,
    CharterKind,
    CharterStatus,
)
from lionagi.governance.errors import CharterParseError

__all__ = [
    "CharterParseError",
    "CharterParser",
    "CharterValidationError",
    "charter_json_schema",
    "parse_charter",
    "validate_charter",
]

_VALID_TOP_KEYS = frozenset(
    {
        "charter_dsl",
        "kind",
        "metadata",
        "agents",
        "registry",
        "constraints",
        "sod",
        "permissions",
        "break_glass",
        "trace",
    }
)

_REQUIRED_SPECIFICITY_ORDER = ["resource", "role", "tenant", "global"]


class CharterValidationError:
    """A single semantic validation finding."""

    __slots__ = ("path", "message", "severity")

    def __init__(self, path: str, message: str, severity: str = "error") -> None:
        self.path = path
        self.message = message
        self.severity = severity

    def __repr__(self) -> str:
        return f"[{self.severity.upper()}] {self.path}: {self.message}"

    def __str__(self) -> str:
        return repr(self)


# ──────────────────────── YAML line-map helpers ────────────────────────


def _build_yaml_line_map(node: Any, prefix: str = "") -> dict[str, int]:
    """Return field-path → 1-indexed line-number from a PyYAML node tree."""
    result: dict[str, int] = {}
    _walk_yaml_node(node, prefix, result)
    return result


def _walk_yaml_node(node: Any, path: str, out: dict[str, int]) -> None:
    import yaml

    if node is None:
        return
    line = node.start_mark.line + 1  # 1-indexed
    out[path or "<root>"] = line
    if isinstance(node, yaml.MappingNode):
        for key_node, val_node in node.value:
            child = f"{path}.{key_node.value}" if path else key_node.value
            _walk_yaml_node(val_node, child, out)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            _walk_yaml_node(item, f"{path}[{i}]", out)


def _line_for(line_map: dict[str, int], *paths: str) -> int | None:
    for p in paths:
        if p in line_map:
            return line_map[p]
    return None


# ────────────────────────── CharterParser ──────────────────────────────


class CharterParser:
    """Parse and validate Charter DSL v0 YAML with line-specific diagnostics."""

    @staticmethod
    def parse(yaml_text: str) -> CharterDocument:
        """Parse ``yaml_text`` into a ``CharterDocument``.

        Raises ``CharterParseError`` for:
        - tab characters
        - YAML syntax errors
        - legacy ``apiVersion`` key
        - wildcards in tool-identifier fields

        Re-raises ``pydantic.ValidationError`` for schema violations.
        """
        import yaml

        # Tab check — report first offending line
        if "\t" in yaml_text:
            bad_line = next(i + 1 for i, ln in enumerate(yaml_text.splitlines()) if "\t" in ln)
            raise CharterParseError("Tabs are not allowed in charter YAML", line=bad_line)

        # Parse YAML AST (preserves line info)
        try:
            root_node = yaml.compose(yaml_text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = (mark.line + 1) if mark is not None else None
            raise CharterParseError(str(exc), line=line) from exc

        if root_node is None or not isinstance(root_node, yaml.MappingNode):
            raise CharterParseError("Charter YAML must be a mapping at the top level")

        # Build line map from AST
        line_map = _build_yaml_line_map(root_node)

        # Legacy apiVersion rejection
        for key_node, _ in root_node.value:
            if key_node.value == "apiVersion":
                line = key_node.start_mark.line + 1
                raise CharterParseError(
                    "Legacy format detected. Migrate: remove 'apiVersion', "
                    "use 'charter.version' instead.",
                    line=line,
                )

        # Convert to Python dict for Pydantic validation
        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            raise CharterParseError("Charter YAML must be a mapping at the top level")

        # Check wildcards in tool-identifier fields only (not prose strings)
        for _fp, _val in _extract_tool_identifier_values(data):
            if _WILDCARD_CHARS.search(_val):
                _line = _line_for(line_map, _fp)
                raise CharterParseError(
                    f"Wildcards are invalid in Charter DSL v0. Use exact tool "
                    f"identifiers and enumerate each allowed tool. "
                    f"(field: {_fp!r}, value: {_val!r})",
                    line=_line,
                )

        try:
            return CharterDocument.model_validate(data)
        except ValidationError as exc:
            # Surface the first error with a line hint
            first = exc.errors()[0]
            loc_path = ".".join(str(p) for p in first.get("loc", ()))
            line = _line_for(line_map, loc_path, loc_path.split(".")[0])
            msg = f"{loc_path}: {first['msg']}"
            raise CharterParseError(msg, line=line) from exc

    @staticmethod
    def schema() -> dict[str, Any]:
        """Return JSON Schema for CharterDocument (IDE integration)."""
        return CharterDocument.model_json_schema()


def _extract_tool_identifier_values(raw: dict) -> list[tuple[str, str]]:
    """Return (field_path, value) for all tool-identifier fields in raw dict.

    Only checks fields that are semantically tool identifiers — not prose
    strings like description, reason, or because.
    """
    results: list[tuple[str, str]] = []

    for i, agent in enumerate(raw.get("agents") or []):
        if isinstance(agent, dict):
            for j, tool in enumerate(agent.get("allowed_tools") or []):
                if isinstance(tool, str):
                    results.append((f"agents[{i}].allowed_tools[{j}]", tool))

    registry = raw.get("registry") or {}
    if isinstance(registry, dict):
        for i, entry in enumerate(registry.get("entries") or []):
            if isinstance(entry, dict):
                val = entry.get("value")
                if isinstance(val, str):
                    results.append((f"registry.entries[{i}].value", val))

    for i, constraint in enumerate(raw.get("constraints") or []):
        if isinstance(constraint, dict):
            attach = constraint.get("attach") or {}
            if isinstance(attach, dict):
                for j, tool in enumerate(attach.get("tools") or []):
                    if isinstance(tool, str):
                        results.append((f"constraints[{i}].attach.tools[{j}]", tool))

    permissions = raw.get("permissions") or {}
    if isinstance(permissions, dict):
        for i, rule in enumerate(permissions.get("allow") or []):
            if isinstance(rule, dict):
                for j, tool in enumerate(rule.get("tools") or []):
                    if isinstance(tool, str):
                        results.append((f"permissions.allow[{i}].tools[{j}]", tool))
        for i, rule in enumerate(permissions.get("deny") or []):
            if isinstance(rule, dict):
                for j, tool in enumerate(rule.get("tools") or []):
                    if isinstance(tool, str):
                        results.append((f"permissions.deny[{i}].tools[{j}]", tool))

    bg = raw.get("break_glass") or {}
    if isinstance(bg, dict):
        for i, grant in enumerate(bg.get("temporary_grants") or []):
            if isinstance(grant, str):
                results.append((f"break_glass.temporary_grants[{i}]", grant))

    return results


# ────────────────── Convenience wrappers (original API) ────────────────


def parse_charter(source: str | Path) -> CharterDocument:
    """Parse a YAML charter from a string or file path.

    Raises ``ValueError`` for tab characters or non-mapping YAML.
    Raises ``pydantic.ValidationError`` for schema violations.
    """
    import yaml

    if isinstance(source, Path) or (
        isinstance(source, str)
        and "\n" not in source
        and Path(source).suffix in (".yaml", ".yml")
        and Path(source).exists()
    ):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source

    if "\t" in text:
        raise ValueError("Tabs are not allowed in charter YAML source")

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Charter YAML must be a mapping at the top level")

    return CharterDocument.model_validate(data)


def validate_charter(
    doc: CharterDocument,
    *,
    raw_yaml: str | None = None,
) -> list[CharterValidationError]:
    """Run semantic validation beyond what Pydantic enforces.

    Returns a list of errors/warnings. Empty list = all checks pass.
    """
    errors: list[CharterValidationError] = []

    _check_agent_count(doc, errors)
    _check_metadata_semantics(doc, errors)
    _check_permissions_invariants(doc, errors)
    _check_sod_roles_declared(doc, errors)
    _check_duplicate_ids(doc, errors)
    _check_allowed_tools_in_registry(doc, errors)
    _check_break_glass_approver(doc, errors)
    _check_sod_active_for_accepted(doc, errors)
    _check_constraint_binding_exclusivity(doc, errors)
    _deep_scan_executable_tokens(doc.model_dump(mode="python"), "", errors)

    if raw_yaml is not None:
        _check_unknown_top_keys(raw_yaml, errors)

    return errors


def charter_json_schema() -> dict[str, Any]:
    """Export JSON Schema for CharterDocument (IDE integration)."""
    return CharterDocument.model_json_schema()


# ─────────────────────── Semantic check helpers ───────────────────────


def _check_agent_count(doc: CharterDocument, errors: list[CharterValidationError]) -> None:
    n = len(doc.agents)
    if doc.kind == CharterKind.AGENT and n != 1:
        errors.append(
            CharterValidationError(
                "agents",
                f"agent_charter requires exactly 1 agent, got {n}",
            )
        )
    elif doc.kind == CharterKind.SESSION and n < 2:
        errors.append(
            CharterValidationError(
                "agents",
                f"session_charter requires at least 2 agents, got {n}",
            )
        )


def _check_metadata_semantics(doc: CharterDocument, errors: list[CharterValidationError]) -> None:
    meta = doc.metadata
    if meta.status in (CharterStatus.ACCEPTED, CharterStatus.SUPERSEDED):
        if meta.ratification.hash is None:
            errors.append(
                CharterValidationError(
                    "metadata.ratification.hash",
                    f"Hash required for status '{meta.status.value}'",
                )
            )
    if meta.status != CharterStatus.DRAFT:
        if meta.authored_by == meta.implemented_by:
            errors.append(
                CharterValidationError(
                    "metadata",
                    "authored_by and implemented_by must differ for "
                    f"non-draft charters (both are {meta.authored_by!r})",
                )
            )


def _check_permissions_invariants(
    doc: CharterDocument, errors: list[CharterValidationError]
) -> None:
    if doc.permissions.default != "deny":
        errors.append(
            CharterValidationError(
                "permissions.default",
                f"Must be 'deny', got {doc.permissions.default!r}",
            )
        )
    res = doc.permissions.resolution
    if res.specificity_order != _REQUIRED_SPECIFICITY_ORDER:
        errors.append(
            CharterValidationError(
                "permissions.resolution.specificity_order",
                f"Must be {_REQUIRED_SPECIFICITY_ORDER}, got {res.specificity_order}",
            )
        )
    if res.tie != "deny":
        errors.append(
            CharterValidationError(
                "permissions.resolution.tie",
                f"Must be 'deny', got {res.tie!r}",
            )
        )

    for rule in doc.permissions.allow:
        if not rule.requires_evidence:
            errors.append(
                CharterValidationError(
                    f"permissions.allow[{rule.rule_id}]",
                    "Allow rules must include requires_evidence",
                    severity="warning",
                )
            )

    for rule in doc.permissions.deny:
        if not rule.because or rule.because.strip() in (
            "not allowed",
            "Not allowed",
        ):
            errors.append(
                CharterValidationError(
                    f"permissions.deny[{rule.rule_id}]",
                    "Deny rules must have a specific 'because' rationale",
                )
            )


def _check_sod_roles_declared(doc: CharterDocument, errors: list[CharterValidationError]) -> None:
    declared_roles = {a.role for a in doc.agents}
    for rule in doc.sod.rules:
        for role in rule.roles:
            if role not in declared_roles:
                errors.append(
                    CharterValidationError(
                        f"sod.rules[{rule.rule_id}]",
                        f"Role {role!r} not declared in agents",
                    )
                )


def _check_duplicate_ids(doc: CharterDocument, errors: list[CharterValidationError]) -> None:
    _find_dupes(
        [a.agent_id for a in doc.agents],
        "agents",
        "agent_id",
        errors,
    )
    _find_dupes(
        [c.constraint_id for c in doc.constraints],
        "constraints",
        "constraint_id",
        errors,
    )
    all_rule_ids: list[str] = []
    all_rule_ids.extend(r.rule_id for r in doc.sod.rules)
    all_rule_ids.extend(r.rule_id for r in doc.permissions.allow)
    all_rule_ids.extend(r.rule_id for r in doc.permissions.deny)
    _find_dupes(all_rule_ids, "rules", "rule_id", errors)


def _find_dupes(
    ids: list[str],
    section: str,
    field: str,
    errors: list[CharterValidationError],
) -> None:
    seen: set[str] = set()
    for id_ in ids:
        if id_ in seen:
            errors.append(
                CharterValidationError(
                    section,
                    f"Duplicate {field}: {id_!r}",
                )
            )
        seen.add(id_)


def _check_allowed_tools_in_registry(
    doc: CharterDocument, errors: list[CharterValidationError]
) -> None:
    registry_tool_values = {e.value for e in doc.registry.entries if e.category.value == "tool"}
    for agent in doc.agents:
        for tool in agent.allowed_tools:
            if tool not in registry_tool_values:
                errors.append(
                    CharterValidationError(
                        f"agents[{agent.agent_id}].allowed_tools",
                        f"Tool {tool!r} not in registry entries",
                    )
                )


def _check_break_glass_approver(doc: CharterDocument, errors: list[CharterValidationError]) -> None:
    if doc.break_glass is None:
        return
    approver = doc.break_glass.attestation.approver_role
    agent_roles = {a.role for a in doc.agents}
    if approver in agent_roles:
        errors.append(
            CharterValidationError(
                "break_glass.attestation.approver_role",
                f"Approver role {approver!r} must not be a requesting agent role",
                severity="warning",
            )
        )


def _check_sod_active_for_accepted(
    doc: CharterDocument, errors: list[CharterValidationError]
) -> None:
    if doc.metadata.status == CharterStatus.ACCEPTED and not doc.sod.active:
        errors.append(
            CharterValidationError(
                "sod.active",
                "Accepted charters require sod.active=true unless a "
                "policy release explicitly allows disabled SoD",
            )
        )


def _check_constraint_binding_exclusivity(
    doc: CharterDocument, errors: list[CharterValidationError]
) -> None:
    """No tool may be targeted by both a gate-bound and a hook-bound constraint."""
    gate_tools: set[str] = set()
    hook_tools: set[str] = set()

    for c in doc.constraints:
        if c.gate_id is not None and c.attach.tools:
            gate_tools.update(c.attach.tools)
        elif c.hook_name is not None and c.attach.tools:
            hook_tools.update(c.attach.tools)

    overlap = gate_tools & hook_tools
    for tool in sorted(overlap):
        errors.append(
            CharterValidationError(
                "constraints",
                f"Constraint binding exclusivity violation: tool {tool!r} "
                f"appears in both gate-bound and hook-bound constraints",
            )
        )


def _deep_scan_executable_tokens(
    data: Any, path: str, errors: list[CharterValidationError]
) -> None:
    if isinstance(data, str):
        if _EXECUTABLE_TOKENS.search(data):
            errors.append(
                CharterValidationError(
                    path or "<root>",
                    f"Executable token in string value: {data!r}",
                )
            )
    elif isinstance(data, dict):
        for k, v in data.items():
            _deep_scan_executable_tokens(v, f"{path}.{k}" if path else k, errors)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _deep_scan_executable_tokens(v, f"{path}[{i}]", errors)


def _check_unknown_top_keys(raw_yaml: str, errors: list[CharterValidationError]) -> None:
    import yaml

    data = yaml.safe_load(raw_yaml)
    if not isinstance(data, dict):
        return
    for key in data:
        if key not in _VALID_TOP_KEYS:
            errors.append(
                CharterValidationError(
                    f"<top-level>.{key}",
                    f"Unknown top-level key: {key!r}",
                )
            )
