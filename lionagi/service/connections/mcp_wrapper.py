# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias
from urllib.parse import urlparse

from lionagi.ln._hash import compute_hash
from lionagi.ln.concurrency import Lock

# Suppress MCP server logging by default
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("fastmcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Environment variable keys that should never be passed to MCP servers
_SENSITIVE_ENV_PATTERNS = frozenset(
    {
        "API_KEY",
        "API_SECRET",
        "API_TOKEN",
        "ACCESS_TOKEN",
        "AUTH_TOKEN",
        "AWS_SECRET",
        "AWS_SESSION_TOKEN",
        "CREDENTIAL",
        "DATABASE_URL",
        "DB_PASSWORD",
        "PASSWORD",
        "PRIVATE_KEY",
        "REFRESH_TOKEN",
        "SECRET_KEY",
        "SERVICE_TOKEN",
    }
)


__all__ = (
    "MCPSecurityConfig",
    "MCPConnectionPool",
    "create_mcp_tool",
    "is_synthetic_mcp_wrapper_schema",
    "validate_mcp_tool_admission",
)


@dataclass(frozen=True)
class MCPSecurityConfig:
    """Fail-closed security config for MCP connection pool."""

    allow_commands: bool = False
    command_allowlist: frozenset[str] | None = None
    allow_urls: bool = False
    url_allowlist: frozenset[str] | None = None
    env_denylist_patterns: frozenset[str] = field(default_factory=lambda: _SENSITIVE_ENV_PATTERNS)
    filter_sensitive_env: bool = True
    max_connections_per_server: int = 5

    @classmethod
    def trusted(cls) -> MCPSecurityConfig:
        """The named, observable transport-trust decision (ADR-0011 delta row 3).

        Allows command and URL transports. A caller must reach for this
        deliberately -- omitting a policy at MCP load time no longer implies
        trust; it now preserves the fail-closed default above instead.
        """
        return cls(allow_commands=True, allow_urls=True)


# --- Generic-executor admission rule -----------------------------------
# Registration-time admission control, independent of MCPSecurityConfig
# (transport auth) and PermissionPolicy (invocation-time) — see docs/internals/runtime.md.

AdmissionReason: TypeAlias = Literal[
    "unbounded-command-input",
    "unbounded-process-input",
    "unbounded-script-payload",
    "executor-description-with-broad-input",
    "executor-identity-with-insufficient-schema",
]

_STRONG_EXECUTOR_NAMES = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "shell",
        "cmd",
        "powershell",
        "pwsh",
        "terminal",
        "exec",
        "exec_command",
        "execute_command",
        "run_command",
        "run_shell",
        "shell_exec",
        "command_exec",
        "spawn_process",
        "run_process",
    }
)

_EXECUTOR_DESCRIPTION_PHRASES = (
    "arbitrary command",
    "arbitrary commands",
    "arbitrary shell",
    "execute command",
    "execute commands",
    "executes command",
    "executes commands",
    "execute a command",
    "execute os command",
    "execute os commands",
    "executes os commands",
    "execute an os command",
    "execute system command",
    "execute system commands",
    "executes system commands",
    "execute a system command",
    "execute shell command",
    "execute shell commands",
    "executes shell commands",
    "execute a shell command",
    "execute terminal command",
    "execute terminal commands",
    "executes terminal commands",
    "execute a terminal command",
    "run command",
    "run commands",
    "runs command",
    "runs commands",
    "run a command",
    "run os command",
    "run os commands",
    "runs os commands",
    "run an os command",
    "run system command",
    "run system commands",
    "runs system commands",
    "run a system command",
    "run shell command",
    "run shell commands",
    "runs shell commands",
    "run a shell command",
    "run terminal command",
    "run terminal commands",
    "runs terminal commands",
    "run a terminal command",
    "run shell",
    "run a shell",
    "execute script",
    "execute scripts",
    "executes scripts",
    "execute a script",
    "spawn process",
    "spawn processes",
    "spawns processes",
    "spawn a process",
    "shell command executor",
    "shell command runner",
    "command line executor",
    "command line runner",
)

_COMMAND_KEYS = frozenset({"command", "cmd", "command_line", "shell_command"})
_PROGRAM_KEYS = frozenset({"program", "executable", "binary"})
_ARGUMENT_KEYS = frozenset({"args", "argv"})
_PAYLOAD_KEYS = frozenset({"script", "code", "input", "text"})
_SELECTOR_KEYS = frozenset({"shell", "interpreter"})
_AUXILIARY_KEYS = frozenset(
    {
        "cwd",
        "working_directory",
        "working_dir",
        "env",
        "environment",
        "stdin",
        "timeout",
        "timeout_seconds",
        "shell",
        "interpreter",
        "user",
    }
)
_CATEGORIZED_KEYS = _COMMAND_KEYS | _PROGRAM_KEYS | _ARGUMENT_KEYS | _PAYLOAD_KEYS | _SELECTOR_KEYS

_CAMEL_BOUNDARY_LOWER_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_BOUNDARY_UPPER_RUN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RUN = re.compile(r"[^a-zA-Z0-9]+")
_REPEATED_UNDERSCORE = re.compile(r"_+")
_NON_ALPHANUM_RUN = re.compile(r"[^a-z0-9]+")


def _normalize_mcp_identifier(name: object) -> str:
    """Case-fold a tool/property name to `_`-joined tokens, splitting camelCase first."""
    if not isinstance(name, str):
        return ""
    split = _CAMEL_BOUNDARY_UPPER_RUN.sub("_", _CAMEL_BOUNDARY_LOWER_UPPER.sub("_", name))
    folded = split.casefold()
    replaced = _NON_ALNUM_RUN.sub("_", folded)
    return _REPEATED_UNDERSCORE.sub("_", replaced).strip("_")


def _normalize_mcp_description(description: object) -> str:
    """Case-fold a description to single-space-joined tokens for phrase matching."""
    if not isinstance(description, str):
        return ""
    folded = description.casefold()
    return _NON_ALPHANUM_RUN.sub(" ", folded).strip()


def _has_strong_executor_name(tool_name: object) -> bool:
    return _normalize_mcp_identifier(tool_name) in _STRONG_EXECUTOR_NAMES


def _has_executor_description_signal(description: object) -> bool:
    normalized = _normalize_mcp_description(description)
    if not normalized:
        return False
    padded = f" {normalized} "
    return any(f" {phrase} " in padded for phrase in _EXECUTOR_DESCRIPTION_PHRASES)


_IDENTIFIER_LIKE_KEY_PATTERN = re.compile(
    r"^(?:[a-z0-9]+_)*(?:id|ids|path|paths|uri|url|uuid|slug)$"
)

# Tokens that taint an identifier-shaped key back to executor-shaped (e.g.
# `executable_path`) — see docs/internals/runtime.md.
_EXEC_TAINTED_KEY_TOKENS = frozenset(
    {"command", "cmd", "shell", "script", "program", "binary", "executable", "argv", "args"}
)


def _is_identifier_like_key(norm_key: str) -> bool:
    """True for dynamic-but-benign resource identifiers (`service_id`,
    `resource_path`, ...) — excluded from the strong-name bounding fallback.
    See docs/internals/runtime.md."""
    return bool(_IDENTIFIER_LIKE_KEY_PATTERN.match(norm_key))


def _is_exec_tainted_key(norm_key: str) -> bool:
    """True when a key's own tokens name an executor channel (`executable_path`,
    `script_path`, ...), overriding the identifier-suffix exemption.
    See docs/internals/runtime.md."""
    return any(token in _EXEC_TAINTED_KEY_TOKENS for token in norm_key.split("_"))


# Non-object types whose instances aren't intrinsically finite; a type union
# including one bypasses all object-shaped constraints. See docs/internals/runtime.md.
_UNBOUNDED_NON_OBJECT_TYPES = frozenset({"string", "number", "integer", "array"})


def _type_union_has_free_form_alternative(top_type: list, schema: Mapping) -> bool:
    if "enum" in schema or "const" in schema:
        return False
    return any(t in _UNBOUNDED_NON_OBJECT_TYPES for t in top_type)


# Keywords that are annotation-only when siblings of $ref — add no sibling
# obligation. See docs/internals/runtime.md.
_ANNOTATION_ONLY_REF_SIBLING_KEYWORDS = frozenset(
    {"description", "title", "$comment", "examples", "default", "$defs", "definitions"}
)


def _has_structural_ref_siblings(siblings: Mapping) -> bool:
    """True when a $ref node's siblings include anything beyond pure
    annotation — i.e., per Draft 2020-12, something that constrains the same
    instance and must be evaluated. See docs/internals/runtime.md."""
    return any(key not in _ANNOTATION_ONLY_REF_SIBLING_KEYWORDS for key in siblings)


# --- Keyword registry for the sufficiency proof --------------------------
# Classifies every Draft 2020-12 keyword into one of four classes, then
# walks the whole document unconditionally. See docs/internals/runtime.md.

# Annotation-only: carry no assertion that admits/denies an instance value.
_INERT_ANNOTATION_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "$comment",
        "$schema",
        "$id",
        "$anchor",
        "$vocabulary",
        "format",
        "contentEncoding",
        "contentMediaType",
        # `contentSchema`'s inertness holds ONLY while the content-assertion
        # vocabulary is disabled (the default dialect). See docs/internals/runtime.md.
        "contentSchema",
    }
)

# Narrows the admitted set; carries no recursable subschema of its own.
_BOUNDING_KEYWORDS = frozenset(
    {
        "type",
        "const",
        "enum",
        "required",
        "dependentRequired",
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "pattern",
        "maxItems",
        "minItems",
        "uniqueItems",
        "maxContains",
        "minContains",
        "maxProperties",
        "minProperties",
    }
)

# Applicators the proof RECURSES into and credits.
_MODELED_APPLICATOR_KEYWORDS = frozenset(
    {
        "properties",
        "additionalProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "$ref",
        "$defs",
        "definitions",
    }
)

# Applicators recognized by name but not modeled — presence anywhere denies
# the node outright; promoting one requires its own soundness argument.
# See docs/internals/runtime.md.
_DENIED_APPLICATOR_KEYWORDS = frozenset(
    {
        "patternProperties",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "not",
        "contains",
        "items",
        "prefixItems",
        "$dynamicRef",
        "$dynamicAnchor",
        "$recursiveRef",
        "$recursiveAnchor",
    }
)

_KeywordClass: TypeAlias = Literal["inert", "bounding", "modeled", "denied", "unknown"]


def _classify_keyword(name: str) -> _KeywordClass:
    """Classify a JSON Schema keyword into exactly one of the four registry
    classes; unrecognized names fail closed as UNKNOWN. See docs/internals/runtime.md."""
    if name in _INERT_ANNOTATION_KEYWORDS:
        return "inert"
    if name in _BOUNDING_KEYWORDS:
        return "bounding"
    if name in _MODELED_APPLICATOR_KEYWORDS:
        return "modeled"
    if name in _DENIED_APPLICATOR_KEYWORDS:
        return "denied"
    return "unknown"


def _property_value_may_be_object_shaped(value: object) -> bool:
    """True when a declared property's VALUE could itself resolve to an
    OBJECT instance, requiring the boundedness proof to recurse into it.
    Deliberately narrow — see docs/internals/runtime.md."""
    if not isinstance(value, Mapping):
        return False
    value_type = value.get("type")
    if value_type is not None and _schema_type_includes(value_type, "object"):
        return True
    return any(_classify_keyword(key) == "modeled" for key in value)


def _schema_is_insufficient(input_schema: object) -> bool:
    """Top-level sufficiency gate: insufficient if EITHER the
    object-boundedness proof OR the structural-coverage proof fails,
    combined by OR. See docs/internals/runtime.md."""
    if input_schema is None or not isinstance(input_schema, Mapping):
        return True
    if _object_boundedness_insufficient(input_schema, input_schema, frozenset(), 0, [0]):
        return True
    return _structural_coverage_insufficient(input_schema, input_schema, frozenset(), 0, [0])


def _object_boundedness_insufficient(
    schema: Mapping,
    root_schema: Mapping,
    seen_refs: frozenset[str],
    depth: int,
    budget: list[int],
) -> bool:
    """Recursive, union-aware TYPE-GATE + CLOSEDNESS check, orthogonal to
    `_structural_coverage_insufficient`. See docs/internals/runtime.md for
    the full 6-step order-of-checks argument."""
    budget[0] += 1
    if budget[0] > _MAX_SCHEMA_WALK_NODES or depth > _MAX_SCHEMA_WALK_DEPTH:
        return True

    top_type = schema.get("type")
    # A Draft 2020-12 type array (e.g. ["object","null"]) is an object schema
    # if "object" is among its types; only excluding "object" entirely is
    # insufficient.
    if top_type is not None and not _schema_type_includes(top_type, "object"):
        return True
    # A type union including "object" plus a free-form alternative is only as
    # bounded as its least-bounded branch — see docs/internals/runtime.md.
    if isinstance(top_type, list) and _type_union_has_free_form_alternative(top_type, schema):
        return True

    ref = schema.get(_REF_KEYWORD)
    if ref is not None:
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen_refs:
            return True
        resolved = _resolve_local_ref(ref, root_schema)
        if resolved is None:
            return True
        target_insufficient = _object_boundedness_insufficient(
            resolved, root_schema, seen_refs | {ref}, depth + 1, budget
        )
        if not target_insufficient:
            return False
        # Draft 2020-12 evaluates $ref SIBLINGS — a closed target doesn't
        # make the node sufficient if a structural sibling reopens it.
        # See docs/internals/runtime.md.
        siblings = {k: v for k, v in schema.items() if k != _REF_KEYWORD}
        if _has_structural_ref_siblings(siblings):
            return _object_boundedness_insufficient(
                siblings, root_schema, seen_refs | {ref}, depth + 1, budget
            )
        return True

    for comp_key in ("oneOf", "anyOf"):
        branches = schema.get(comp_key)
        if branches is not None:
            if not isinstance(branches, list) or not branches:
                return True
            for branch in branches:
                if not isinstance(branch, Mapping):
                    return True
                if _object_boundedness_insufficient(
                    branch, root_schema, seen_refs, depth + 1, budget
                ):
                    return True
            # Every alternative independently proved sufficient.
            return False

    all_of = schema.get("allOf")
    if isinstance(all_of, list) and all_of and "properties" not in schema:
        for branch in all_of:
            if isinstance(branch, Mapping) and not _object_boundedness_insufficient(
                branch, root_schema, seen_refs, depth + 1, budget
            ):
                return False
        return True

    # A top-level const/enum pins the whole instance to literal value(s),
    # satisfying the type-gate regardless of type.
    if "const" in schema or "enum" in schema:
        return False

    # LEAF-OBJECT branch: type evaluated node-locally here; see the
    # docstring above / docs/internals/runtime.md.
    if top_type is None:
        return True

    if "properties" in schema and not isinstance(schema["properties"], Mapping):
        return True
    properties = schema.get("properties")
    props = properties if isinstance(properties, Mapping) else {}
    additional = schema.get("additionalProperties")
    if not props:
        return additional is not False
    # additionalProperties defaults to permissive; a fixed "operation" enum
    # doesn't stop a sibling "command" property riding alongside it.
    # See docs/internals/runtime.md.
    object_closed = additional is False or (
        isinstance(additional, Mapping) and ("enum" in additional or "const" in additional)
    )
    if not object_closed:
        return True
    # OUTER-object closedness says nothing about a DECLARED property's own
    # object-shaped VALUE — re-checked recursively; see docstring above.
    for prop_value in props.values():
        if _property_value_may_be_object_shaped(prop_value) and _object_boundedness_insufficient(
            prop_value, root_schema, seen_refs, depth + 1, budget
        ):
            return True
    return False


def _structural_coverage_insufficient(
    schema: object,
    root_schema: Mapping,
    seen_refs: frozenset[str],
    depth: int,
    budget: list[int],
) -> bool:
    """Total, registry-driven traversal: does any position carry a keyword
    the sufficiency proof doesn't model? See docs/internals/runtime.md."""
    budget[0] += 1
    if budget[0] > _MAX_SCHEMA_WALK_NODES or depth > _MAX_SCHEMA_WALK_DEPTH:
        return True
    if not isinstance(schema, Mapping):
        return True

    for key, value in schema.items():
        keyword_class = _classify_keyword(key)
        if keyword_class == "inert" or keyword_class == "bounding":
            continue
        if keyword_class == "denied":
            return True
        if keyword_class == "unknown":
            if _is_vendor_annotation_keyword(key, value, budget):
                continue
            if _could_carry_subschema(value, budget):
                return True
            continue

        # keyword_class == "modeled": recurse into every subschema slot.
        if key == "properties":
            if not isinstance(value, Mapping):
                return True
            for prop_value in value.values():
                if _structural_coverage_insufficient(
                    prop_value, root_schema, seen_refs, depth + 1, budget
                ):
                    return True
        elif key == "additionalProperties":
            # A boolean value is closedness, not a recursable subschema --
            # that question belongs to `_object_boundedness_insufficient`.
            if isinstance(value, Mapping):
                if _structural_coverage_insufficient(
                    value, root_schema, seen_refs, depth + 1, budget
                ):
                    return True
        elif key in ("allOf", "anyOf", "oneOf"):
            if not isinstance(value, list) or not value:
                return True
            for branch in value:
                if not isinstance(branch, Mapping):
                    return True
                if _structural_coverage_insufficient(
                    branch, root_schema, seen_refs, depth + 1, budget
                ):
                    return True
        elif key == _REF_KEYWORD:
            if not isinstance(value, str) or not value.startswith("#/") or value in seen_refs:
                return True
            resolved = _resolve_local_ref(value, root_schema)
            if resolved is None:
                return True
            if _structural_coverage_insufficient(
                resolved, root_schema, seen_refs | {value}, depth + 1, budget
            ):
                return True
        elif key in ("$defs", "definitions"):
            if not isinstance(value, Mapping):
                return True
            # $defs entries visited UNCONDITIONALLY even if unreferenced —
            # see docs/internals/runtime.md.
            for sub_schema in value.values():
                if not isinstance(sub_schema, Mapping):
                    return True
                if _structural_coverage_insufficient(
                    sub_schema, root_schema, seen_refs, depth + 1, budget
                ):
                    return True
    return False


def _property_is_bounded(prop_schema: object) -> bool:
    if not isinstance(prop_schema, Mapping):
        return False
    if "enum" in prop_schema or "const" in prop_schema:
        return True
    # Deliberately no array carve-out: array boundedness needs BOTH
    # prefixItems and items checked together. See docs/internals/runtime.md.
    return False


def _schema_type_includes(type_value: object, target: str) -> bool:
    """True when a JSON Schema `type` (string or Draft 2020-12 type array) allows `target`."""
    if isinstance(type_value, list):
        return target in type_value
    return type_value == target


def _item_schema_reaches_free_form_string(
    item_schema: object,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    result: _SchemaWalkResult,
) -> bool:
    """True when an array's items/prefixItems member schema may itself
    admit an arbitrary string — i.e. a free-form, argv-shaped channel.
    See docs/internals/runtime.md."""
    if item_schema is True:
        return True
    if item_schema is False:
        return False
    if not isinstance(item_schema, Mapping):
        # Malformed item schema (not a boolean, not a mapping): cannot be
        # proven bounded -- fail closed.
        return True
    if depth > _MAX_SCHEMA_WALK_DEPTH:
        result.unresolvable = True
        return True
    if not _consume_node_budget(result):
        return True
    if _property_is_bounded(item_schema):
        return False
    item_type = item_schema.get("type")
    if item_type is not None:
        if _schema_type_includes(item_type, "string"):
            return True
        if _schema_type_includes(item_type, "array"):
            return _array_reaches_free_form(item_schema, root_schema, depth + 1, seen_refs, result)
        return False

    ref = item_schema.get(_REF_KEYWORD)
    branches: list[object] = []
    if isinstance(ref, str):
        if ref.startswith("#/") and ref not in seen_refs:
            resolved = _resolve_local_ref(ref, root_schema)
            if resolved is None:
                return True
            seen_refs = seen_refs | {ref}
            branches.append(resolved)
        else:
            # External or cyclic $ref: cannot be proven bounded.
            return True

    for comp_key in ("allOf", "anyOf", "oneOf"):
        comp = item_schema.get(comp_key)
        if isinstance(comp, list):
            branches.extend(comp)
    for single_key in _SINGLE_SUBSCHEMA_KEYWORDS:
        branch = item_schema.get(single_key)
        if branch is not None:
            branches.append(branch)

    if branches:
        return any(
            _item_schema_reaches_free_form_string(branch, root_schema, depth + 1, seen_refs, result)
            for branch in branches
        )

    # No type, no $ref, no composition: a genuinely empty/opaque schema
    # (`{}`) constrains nothing -- conservatively free-form.
    return True


def _array_reaches_free_form(
    array_schema: Mapping,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    result: _SchemaWalkResult,
) -> bool:
    """True when an array-shaped schema node admits a free-form element
    (argv-shaped channel). See docs/internals/runtime.md for the
    prefixItems/items semantics."""
    prefix_items = array_schema.get("prefixItems")
    if isinstance(prefix_items, list):
        for item in prefix_items:
            if _item_schema_reaches_free_form_string(
                item, root_schema, depth + 1, seen_refs, result
            ):
                return True

    if "items" not in array_schema:
        # No `items` keyword: per Draft 2020-12 this defaults to `true`,
        # leaving every position beyond `prefixItems` totally unconstrained.
        return True

    items_val = array_schema.get("items")
    if items_val is False:
        # items: false closes the tuple to exactly its (already-checked) prefix.
        return False

    return _item_schema_reaches_free_form_string(
        items_val, root_schema, depth + 1, seen_refs, result
    )


def _property_is_free_form(
    prop_schema: object,
    is_categorized_key: bool,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    result: _SchemaWalkResult,
) -> bool:
    # JSON Schema boolean `true` matches any value, so it is at least as
    # permissive as an untyped free-form string; `false` matches nothing.
    if prop_schema is True:
        return True
    if prop_schema is False or not isinstance(prop_schema, Mapping):
        return False
    if _property_is_bounded(prop_schema):
        return False
    prop_type = prop_schema.get("type")
    if _schema_type_includes(prop_type, "string"):
        return True
    if _schema_type_includes(prop_type, "array"):
        return _array_reaches_free_form(prop_schema, root_schema, depth, seen_refs, result)
    if prop_type is None and is_categorized_key:
        return True
    return False


_MAX_SCHEMA_WALK_DEPTH = 12
_MAX_SCHEMA_WALK_NODES = 5000

# --- Walker keyword whitelist -------------------------------------------
# WHITELIST not blacklist — enumerating "keywords we understand" avoids the
# denylist arms race. See docs/internals/runtime.md.

# Keywords whose value never carries a subschema; contentSchema included by
# the same argued exception as above. See docs/internals/runtime.md.
_SCALAR_ONLY_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "description",
        "title",
        "format",
        "pattern",
        "required",
        "default",
        "examples",
        "$defs",
        "definitions",
        "contentSchema",
    }
)

# Applicator/structural keywords the walker knows how to traverse.
_SINGLE_SUBSCHEMA_KEYWORDS = frozenset({"if", "then", "else", "not"})
_LIST_OF_SUBSCHEMAS_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# `items` is Draft 2020-12 single-schema ("the rest of the array") or
# Draft-07-style list-of-schemas (positional/tuple validation); both walked.
_ITEMS_KEYWORD = "items"
_PROPERTIES_KEYWORD = "properties"
_PATTERN_PROPERTIES_KEYWORD = "patternProperties"
_ADDITIONAL_PROPERTIES_KEYWORD = "additionalProperties"
_REF_KEYWORD = "$ref"

# $dynamicRef/$recursiveRef are schema-bearing but string-valued, so the
# Mapping-shape check misses them — recognized by keyword identity instead.
# See docs/internals/runtime.md.
_UNRESOLVABLE_REFERENCE_KEYWORDS = frozenset({"$dynamicRef", "$recursiveRef"})

_KNOWN_SCHEMA_KEYWORDS = (
    _SCALAR_ONLY_SCHEMA_KEYWORDS
    | _SINGLE_SUBSCHEMA_KEYWORDS
    | _LIST_OF_SUBSCHEMAS_KEYWORDS
    | {
        _ITEMS_KEYWORD,
        _PROPERTIES_KEYWORD,
        _PATTERN_PROPERTIES_KEYWORD,
        _ADDITIONAL_PROPERTIES_KEYWORD,
        _REF_KEYWORD,
    }
)


# Explicit enumeration, NOT a min*/max* spelling heuristic — a prefix test
# would reopen the whitelist bypass. See docs/internals/runtime.md.
_NUMERIC_BOUND_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minContains",
        "maxContains",
        "multipleOf",
    }
)


def _is_known_scalar_only_keyword(key: str) -> bool:
    return key in _SCALAR_ONLY_SCHEMA_KEYWORDS or key in _NUMERIC_BOUND_KEYWORDS


def _could_carry_subschema(value: object, budget: list[int], depth: int = 0) -> bool:
    """True when an unrecognized keyword's value is shaped like it could
    hold a schema (mapping, or nested list containing one) — the signal
    that makes it unresolvable. See docs/internals/runtime.md."""
    budget[0] += 1
    if budget[0] > _MAX_SCHEMA_WALK_NODES or depth > _MAX_SCHEMA_WALK_DEPTH:
        return True
    if isinstance(value, Mapping):
        return True
    if isinstance(value, list):
        return any(_could_carry_subschema(item, budget, depth + 1) for item in value)
    return False


def _is_inert_annotation_value(value: object, budget: list[int], depth: int = 0) -> bool:
    """True when value cannot itself carry a subschema (recursively scalar,
    or a list/mapping of such with no schema-vocabulary keys).
    See docs/internals/runtime.md."""
    budget[0] += 1
    if budget[0] > _MAX_SCHEMA_WALK_NODES or depth > _MAX_SCHEMA_WALK_DEPTH:
        return False
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_inert_annotation_value(item, budget, depth + 1) for item in value)
    if isinstance(value, Mapping):
        if any(key in _KNOWN_SCHEMA_KEYWORDS for key in value):
            return False
        return all(_is_inert_annotation_value(v, budget, depth + 1) for v in value.values())
    return False


def _is_vendor_annotation_keyword(key: str, value: object, budget: list[int]) -> bool:
    """True for a keyword that is annotation-only AND whose value carries no
    schema-bearing content (x- prefix or $comment, and demonstrably inert
    value). See docs/internals/runtime.md."""
    if key != "$comment" and not key.startswith("x-"):
        return False
    return _is_inert_annotation_value(value, budget)


def _mark_unknown_schema_keywords(schema: Mapping, result: _SchemaWalkResult) -> None:
    """Whitelist enforcement: any keyword this walker doesn't understand,
    with a subschema-shaped value, is unresolvable — deny-by-default for
    unmodeled keywords. See docs/internals/runtime.md."""
    if any(key in schema for key in _UNRESOLVABLE_REFERENCE_KEYWORDS):
        result.unresolvable = True
        return
    # Value inspection shares the walker's node budget — exhaustion fails
    # closed via the helpers. See docs/internals/runtime.md.
    budget = [result.nodes_visited]
    for key, value in schema.items():
        if key in _KNOWN_SCHEMA_KEYWORDS or _is_known_scalar_only_keyword(key):
            continue
        if _is_vendor_annotation_keyword(key, value, budget):
            continue
        if _could_carry_subschema(value, budget):
            result.unresolvable = True
            break
    result.nodes_visited = max(result.nodes_visited, budget[0])


# Object-applicator keywords — presence means the property is a
# restated/composed schema, not a leaf value; recurse instead of treating
# as scalar.
_OBJECT_CONTAINER_KEYWORDS = (
    _PROPERTIES_KEYWORD,
    _PATTERN_PROPERTIES_KEYWORD,
    _ADDITIONAL_PROPERTIES_KEYWORD,
    _REF_KEYWORD,
    "allOf",
    "anyOf",
    "oneOf",
    "if",
    "then",
    "else",
    "not",
)
# Array-shape keywords — an array can be BOTH a free-form leaf channel AND
# hide a command channel inside an object-shaped item; both checked, no
# short-circuit.
_ARRAY_ITEM_KEYWORDS = (_ITEMS_KEYWORD, "prefixItems")


class _SchemaWalkResult:
    """Accumulates classifier evidence discovered while traversing a
    (possibly nested/composed) JSON Schema input descriptor."""

    __slots__ = (
        "free_form_command_keys",
        "free_form_program_keys",
        "free_form_argument_keys",
        "free_form_payload_keys",
        "non_auxiliary_free_form_keys",
        "non_identifier_free_form_keys",
        "selector_key_present",
        "unresolvable",
        "nodes_visited",
    )

    def __init__(self) -> None:
        self.free_form_command_keys: set[str] = set()
        self.free_form_program_keys: set[str] = set()
        self.free_form_argument_keys: set[str] = set()
        self.free_form_payload_keys: set[str] = set()
        self.non_auxiliary_free_form_keys: set[str] = set()
        self.non_identifier_free_form_keys: set[str] = set()
        self.selector_key_present = False
        # unresolvable = a channel could not be proven bounded (unresolvable
        # $ref, cycle, budget trip, malformed shape, unrecognized keyword).
        # See docs/internals/runtime.md.
        self.unresolvable = False
        # nodes_visited: total-work budget companion to the depth cap,
        # bounds runtime against extreme fan-out (e.g. huge anyOf lists).
        self.nodes_visited = 0


def _consume_node_budget(result: _SchemaWalkResult) -> bool:
    """Count one unit of walker work; returns False once the node budget is
    exceeded, so callers can stop early. See docs/internals/runtime.md."""
    result.nodes_visited += 1
    if result.nodes_visited > _MAX_SCHEMA_WALK_NODES:
        result.unresolvable = True
        return False
    return True


def _resolve_local_ref(ref: str, root_schema: Mapping) -> Mapping | None:
    """Resolve a same-document `$ref` (e.g. `#/$defs/Foo`) against
    `root_schema`. Returns None if the pointer cannot be resolved locally."""
    node: Any = root_schema
    for raw_part in ref[2:].split("/"):
        if raw_part == "":
            continue
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, Mapping) else None


def _compile_pattern_or_mark_unresolvable(
    pattern: object, result: _SchemaWalkResult
) -> re.Pattern | None:
    """Compile a patternProperties regex key; a non-string key or invalid
    regex fails closed rather than being silently skipped."""
    if not isinstance(pattern, str):
        result.unresolvable = True
        return None
    try:
        return re.compile(pattern)
    except re.error:
        result.unresolvable = True
        return None


def _record_free_form_key(norm_key: str, result: _SchemaWalkResult) -> None:
    if norm_key in _COMMAND_KEYS:
        result.free_form_command_keys.add(norm_key)
    elif norm_key in _PROGRAM_KEYS:
        result.free_form_program_keys.add(norm_key)
    elif norm_key in _ARGUMENT_KEYS:
        result.free_form_argument_keys.add(norm_key)
    elif norm_key in _PAYLOAD_KEYS:
        result.free_form_payload_keys.add(norm_key)
    if norm_key not in _AUXILIARY_KEYS:
        result.non_auxiliary_free_form_keys.add(norm_key)
        if _is_exec_tainted_key(norm_key) or not _is_identifier_like_key(norm_key):
            result.non_identifier_free_form_keys.add(norm_key)


def _composition_branch_reaches_free_form(
    prop_schema: object,
    root_schema: Mapping,
    is_categorized_key: bool,
    depth: int,
    seen_refs: frozenset[str],
    result: _SchemaWalkResult,
) -> bool:
    """True when any composition/conditional/$ref branch of a keyed
    property resolves to a free-form leaf — catches indirection like
    anyOf/if-then wrapping. See docs/internals/runtime.md."""
    if not isinstance(prop_schema, Mapping):
        return False
    if depth > _MAX_SCHEMA_WALK_DEPTH:
        result.unresolvable = True
        return False
    branches: list[object] = []
    ref = prop_schema.get(_REF_KEYWORD)
    if isinstance(ref, str) and ref.startswith("#/") and ref not in seen_refs:
        resolved = _resolve_local_ref(ref, root_schema)
        if resolved is not None:
            seen_refs = seen_refs | {ref}
            branches.append(resolved)
    for comp_key in ("allOf", "anyOf", "oneOf"):
        comp = prop_schema.get(comp_key)
        if isinstance(comp, list):
            branches.extend(comp)
    for single_key in _SINGLE_SUBSCHEMA_KEYWORDS:
        branch = prop_schema.get(single_key)
        if branch is not None:
            branches.append(branch)
    for branch in branches:
        if not _consume_node_budget(result):
            return False
        if _property_is_free_form(
            branch, is_categorized_key, root_schema, depth, seen_refs, result
        ):
            return True
        if _composition_branch_reaches_free_form(
            branch, root_schema, is_categorized_key, depth + 1, seen_refs, result
        ):
            return True
    return False


def _consider_property(
    raw_key: object,
    prop_schema: object,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    is_strong_name: bool,
    is_executor_description: bool,
    result: _SchemaWalkResult,
) -> None:
    norm_key = _normalize_mcp_identifier(raw_key)
    if norm_key in _SELECTOR_KEYS:
        result.selector_key_present = True

    if isinstance(prop_schema, Mapping):
        # Leaf-shaped property schemas never reach _walk_schema, so the
        # whitelist is enforced here too (redundant-but-harmless for
        # container properties).
        _mark_unknown_schema_keywords(prop_schema, result)
        if any(k in prop_schema for k in _OBJECT_CONTAINER_KEYWORDS):
            # A container isn't itself a command value; walk its properties,
            # but first attribute composed free-form leaves. See docs/internals/runtime.md.
            if _composition_branch_reaches_free_form(
                prop_schema,
                root_schema,
                norm_key in _CATEGORIZED_KEYS,
                depth,
                seen_refs,
                result,
            ):
                _record_free_form_key(norm_key, result)
            _walk_schema(
                prop_schema,
                root_schema,
                depth + 1,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )
            return
        if any(k in prop_schema for k in _ARRAY_ITEM_KEYWORDS):
            # Walk items/prefixItems for a hidden channel, but fall through
            # to the leaf check — the array property itself may also be
            # free-form.
            _walk_schema(
                prop_schema,
                root_schema,
                depth + 1,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )

    if not _property_is_free_form(
        prop_schema, norm_key in _CATEGORIZED_KEYS, root_schema, depth, seen_refs, result
    ):
        return

    _record_free_form_key(norm_key, result)


def _walk_subschema_list(
    branches: object,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    is_strong_name: bool,
    is_executor_description: bool,
    result: _SchemaWalkResult,
) -> None:
    if not isinstance(branches, list):
        result.unresolvable = True
        return
    for branch in branches:
        if not _consume_node_budget(result):
            return
        _walk_schema(
            branch,
            root_schema,
            depth + 1,
            seen_refs,
            is_strong_name,
            is_executor_description,
            result,
        )


def _walk_schema(
    schema: object,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    is_strong_name: bool,
    is_executor_description: bool,
    result: _SchemaWalkResult,
) -> None:
    """Bounded, cycle-safe, budgeted traversal collecting classifier
    evidence over the whitelist of recognized keywords; any other
    subschema-shaped keyword is unresolvable. See docs/internals/runtime.md."""
    if depth > _MAX_SCHEMA_WALK_DEPTH:
        result.unresolvable = True
        return
    if not _consume_node_budget(result):
        return
    if not isinstance(schema, Mapping):
        return

    ref = schema.get(_REF_KEYWORD)
    if ref is not None:
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen_refs:
            # External/non-local or cyclic reference: cannot be proven
            # bounded from this document alone.
            result.unresolvable = True
            return
        resolved = _resolve_local_ref(ref, root_schema)
        if resolved is None:
            result.unresolvable = True
            return
        _walk_schema(
            resolved,
            root_schema,
            depth + 1,
            seen_refs | {ref},
            is_strong_name,
            is_executor_description,
            result,
        )
        # $ref SIBLINGS are not discarded — fall through so every other
        # keyword on this node is still walked. See docs/internals/runtime.md.

    for comp_key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        branches = schema.get(comp_key)
        if branches is not None:
            _walk_subschema_list(
                branches,
                root_schema,
                depth,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )

    for single_key in _SINGLE_SUBSCHEMA_KEYWORDS:
        branch_schema = schema.get(single_key)
        if branch_schema is not None:
            if not _consume_node_budget(result):
                return
            _walk_schema(
                branch_schema,
                root_schema,
                depth + 1,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )

    items_schema = schema.get(_ITEMS_KEYWORD)
    if items_schema is not None:
        if isinstance(items_schema, list):
            _walk_subschema_list(
                items_schema,
                root_schema,
                depth,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )
        elif isinstance(items_schema, Mapping):
            if _consume_node_budget(result):
                _walk_schema(
                    items_schema,
                    root_schema,
                    depth + 1,
                    seen_refs,
                    is_strong_name,
                    is_executor_description,
                    result,
                )
        elif items_schema is True or items_schema is False:
            # Boolean item schemas ("any item"/"no items") carry no nested
            # subschema to walk.
            pass
        else:
            result.unresolvable = True

    properties = schema.get(_PROPERTIES_KEYWORD)
    if properties is not None:
        if not isinstance(properties, Mapping):
            result.unresolvable = True
        else:
            for raw_key, prop_schema in properties.items():
                if not _consume_node_budget(result):
                    break
                _consider_property(
                    raw_key,
                    prop_schema,
                    root_schema,
                    depth,
                    seen_refs,
                    is_strong_name,
                    is_executor_description,
                    result,
                )

    pattern_properties = schema.get(_PATTERN_PROPERTIES_KEYWORD)
    if pattern_properties is not None:
        if not isinstance(pattern_properties, Mapping):
            result.unresolvable = True
        else:
            for pattern, pattern_schema in pattern_properties.items():
                if not _consume_node_budget(result):
                    break
                compiled = _compile_pattern_or_mark_unresolvable(pattern, result)
                if compiled is None:
                    continue
                matched_key = next((key for key in _CATEGORIZED_KEYS if compiled.search(key)), None)
                if matched_key is not None:
                    _consider_property(
                        matched_key,
                        pattern_schema,
                        root_schema,
                        depth,
                        seen_refs,
                        is_strong_name,
                        is_executor_description,
                        result,
                    )

    additional_properties = schema.get(_ADDITIONAL_PROPERTIES_KEYWORD)
    if additional_properties is not None and additional_properties is not False:
        if isinstance(additional_properties, Mapping) and _consume_node_budget(result):
            # additionalProperties object-valued schema is a reachable
            # subschema — walk it so a command channel behind a dynamic map
            # key isn't missed.
            _walk_schema(
                additional_properties,
                root_schema,
                depth + 1,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )
        if _property_is_free_form(
            additional_properties, True, root_schema, depth, seen_refs, result
        ):
            # No fixed key name for a free-form map channel — only counts
            # as evidence when corroborated by the tool's name/description.
            if is_strong_name or is_executor_description:
                _record_free_form_key("<additionalProperties>", result)

    _mark_unknown_schema_keywords(schema, result)


def _classify_generic_executor(
    tool_name: str,
    input_schema: object | None,
    description: str | None,
) -> AdmissionReason | None:
    is_strong_name = _has_strong_executor_name(tool_name)
    is_executor_description = _has_executor_description_signal(description)
    schema_insufficient = _schema_is_insufficient(input_schema)

    result = _SchemaWalkResult()
    if isinstance(input_schema, Mapping):
        top_type = input_schema.get("type")
        if top_type is None or _schema_type_includes(top_type, "object"):
            _walk_schema(
                input_schema,
                input_schema,
                0,
                frozenset(),
                is_strong_name,
                is_executor_description,
                result,
            )

    has_free_form_command = bool(result.free_form_command_keys)
    s_process = bool(result.free_form_program_keys) and bool(result.free_form_argument_keys)
    s_payload = bool(result.free_form_payload_keys) and (
        result.selector_key_present or is_strong_name or is_executor_description
    )
    s_broad = bool(result.non_auxiliary_free_form_keys)

    # An unbounded command-shaped field is dangerous alone — no name/
    # description corroboration required to deny it. See docs/internals/runtime.md.
    if has_free_form_command:
        return "unbounded-command-input"
    if s_process:
        return "unbounded-process-input"
    if s_payload:
        return "unbounded-script-payload"
    if is_executor_description and (s_broad or result.unresolvable):
        return "executor-description-with-broad-input"
    # A strong executor identity must be affirmatively demonstrated safe —
    # unresolvable or executor-shaped remainder leaves it uncorroborated.
    # See docs/internals/runtime.md.
    if is_strong_name and (
        schema_insufficient or result.unresolvable or result.non_identifier_free_form_keys
    ):
        return "executor-identity-with-insufficient-schema"
    return None


# create_mcp_tool() wraps every tool in async def mcp_callable(**kwargs);
# this is that wrapper's own deterministic reflected schema, not remote
# descriptor metadata. See docs/internals/runtime.md.
_SYNTHETIC_MCP_WRAPPER_PARAMETERS = {
    "type": "object",
    "properties": {"kwargs": {"type": "string", "description": None}},
    "required": ["kwargs"],
}


def is_synthetic_mcp_wrapper_schema(
    mcp_tool_name: str,
    advertised_name: object,
    input_schema: object,
    description: object,
) -> bool:
    """True when a prebuilt Tool's schema is the auto-generated **kwargs
    wrapper (see the module comment above `_SYNTHETIC_MCP_WRAPPER_PARAMETERS`)."""
    return (
        advertised_name == mcp_tool_name
        and description == f"MCP tool: {mcp_tool_name}"
        and input_schema == _SYNTHETIC_MCP_WRAPPER_PARAMETERS
    )


def validate_mcp_tool_admission(
    tool_name: str,
    input_schema: object | None,
    description: str | None,
) -> None:
    """Raise PermissionError when an MCP descriptor exposes a generic
    executor. Pure/synchronous, registration-time only — see docs/internals/runtime.md."""
    reason = _classify_generic_executor(tool_name, input_schema, description)
    if reason is None:
        return
    raise PermissionError(
        f"MCP tool {tool_name!r} was not registered: generic executor surface "
        f"detected [{reason}]. Expose a structured, bounded operation instead; "
        "this admission rule has no configuration opt-out."
    )


def _filter_env(env: dict[str, str], config: MCPSecurityConfig) -> dict[str, str]:
    """Remove env vars matching deny-listed substrings (case-insensitive)."""
    if not config.filter_sensitive_env:
        return env

    filtered = {}
    deny = config.env_denylist_patterns
    for key, value in env.items():
        key_upper = key.upper()
        if any(pattern in key_upper for pattern in deny):
            logger.debug(f"Filtered sensitive env var: {key}")
            continue
        filtered[key] = value
    return filtered


def _validate_command(command: str, config: MCPSecurityConfig) -> None:
    """Fail-closed: deny unless allow_commands=True and passes allowlist."""
    if not config.allow_commands:
        raise PermissionError(
            f"MCP command transport is disabled (allow_commands=False). "
            f"Set MCPSecurityConfig(allow_commands=True) to permit command-based MCP servers. "
            f"Blocked command: '{command}'"
        )

    if config.command_allowlist is None:
        # allow_commands=True and no allowlist: any bare or path command is permitted.
        return

    if "/" in command or "\\" in command:
        bare = os.path.basename(command)
        if bare in config.command_allowlist:
            raise ValueError(
                f"Command contains path separator: '{command}'. "
                f"Use bare command name '{bare}' instead."
            )
        raise ValueError(
            f"Command '{command}' not in allowlist. Allowed: {sorted(config.command_allowlist)}"
        )

    if command not in config.command_allowlist:
        raise ValueError(
            f"Command '{command}' not in allowlist. Allowed: {sorted(config.command_allowlist)}"
        )


def _validate_url(url: str, config: MCPSecurityConfig) -> None:
    """Fail-closed: deny unless allow_urls=True and scheme is https/wss."""
    if not config.allow_urls:
        raise PermissionError(
            f"MCP URL transport is disabled (allow_urls=False). "
            f"Set MCPSecurityConfig(allow_urls=True) to permit URL-based MCP servers. "
            f"Blocked URL: '{url}'"
        )

    parsed = urlparse(url)
    if parsed.scheme not in ("https", "wss"):
        raise ValueError(
            f"MCP URL transport requires https or wss scheme. Got '{parsed.scheme}' in URL: '{url}'"
        )

    if config.url_allowlist is not None:
        host = parsed.hostname or ""
        if host not in config.url_allowlist:
            raise ValueError(
                f"MCP URL host '{host}' not in allowlist. Allowed: {sorted(config.url_allowlist)}"
            )


class MCPConnectionPool:
    """Connection pool for MCP clients with fail-closed security."""

    _clients: dict[str, Any] = {}
    _configs: dict[str, dict] = {}
    _lock: Lock | None = None
    _lock_guard: threading.Lock = threading.Lock()
    _security: MCPSecurityConfig | None = None
    # Per-server policy keyed by content signature so reconnects
    # re-apply the same authorization instead of falling back to fail-closed.
    _server_security: dict[str, MCPSecurityConfig] = {}

    @staticmethod
    def _policy_key(server_config: dict[str, Any]) -> str:
        """Content-based key for per-server policy registry."""
        if "server" in server_config:
            return f"server:{server_config['server']}"
        material = {k: v for k, v in server_config.items() if not k.startswith("_")}
        blob = json.dumps(material, sort_keys=True, default=str)
        return f"inline:{compute_hash(blob)}"

    @classmethod
    def remember_security(
        cls, server_config: dict[str, Any], security: MCPSecurityConfig | None
    ) -> None:
        """Record the policy a server was authorized under. No-op if None."""
        if security is not None:
            cls._server_security[cls._policy_key(server_config)] = security

    @classmethod
    def _get_lock(cls) -> Lock:
        # Lazy creation avoids binding to an event loop at import time (3.10-3.11).
        if cls._lock is None:
            with cls._lock_guard:
                if cls._lock is None:
                    cls._lock = Lock()
        return cls._lock

    @classmethod
    def set_security_config(cls, config: MCPSecurityConfig) -> None:
        """Set security config for new connections. Existing ones unaffected."""
        cls._security = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.cleanup()

    @classmethod
    def load_config(cls, path: str = ".mcp.json") -> list[str]:
        """Load MCP server configurations from a .mcp.json file; returns
        only the server names declared in THIS file (see docs/internals/runtime.md
        for the _configs accumulation gotcha)."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"MCP config file not found: {path}")

        try:
            with open(config_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid JSON in MCP config file: {e.msg}", e.doc, e.pos
            ) from e

        if not isinstance(data, dict):
            raise ValueError("MCP config must be a JSON object")

        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be a dictionary")

        cls._configs.update(servers)
        return list(servers.keys())

    @classmethod
    async def get_client(
        cls,
        server_config: dict[str, Any],
        security: MCPSecurityConfig | None = None,
    ) -> Any:
        """Get or create a pooled MCP client."""
        # Explicit policy authorizes this server for future reconnects;
        # absent one, recover the policy the server was loaded under.
        if security is not None:
            cls.remember_security(server_config, security)
        else:
            security = cls._server_security.get(cls._policy_key(server_config))

        if "server" in server_config:
            server_name = server_config["server"]
            if server_name not in cls._configs:
                cls.load_config()
            if server_name not in cls._configs:
                raise ValueError(f"Unknown MCP server: {server_name}")

            config = cls._configs[server_name]
            cache_key = f"server:{server_name}"
        else:
            config = server_config
            cache_key = f"inline:{config.get('command')}:{id(config)}"

        async with cls._get_lock():
            if cache_key in cls._clients:
                client = cls._clients[cache_key]
                if hasattr(client, "is_connected") and client.is_connected():
                    return client
                else:
                    del cls._clients[cache_key]

            client = await cls._create_client(config, security=security)
            cls._clients[cache_key] = client
            return client

    @classmethod
    async def _create_client(
        cls,
        config: dict[str, Any],
        security: MCPSecurityConfig | None = None,
    ) -> Any:
        """Create a new MCP client from config (fail-closed)."""
        if not isinstance(config, dict):
            raise ValueError("Config must be a dictionary")

        if not any(k in config for k in ["url", "command"]):
            raise ValueError("Config must have either 'url' or 'command' key")

        # Precedence: explicit > process-global > fail-closed default.
        if security is not None:
            effective_security = security
        elif cls._security is not None:
            effective_security = cls._security
        else:
            effective_security = MCPSecurityConfig()

        # Validate BEFORE any import or transport construction.
        if "url" in config:
            _validate_url(config["url"], effective_security)
        elif "command" in config:
            _validate_command(config["command"], effective_security)

        try:
            from fastmcp import Client as FastMCPClient
        except ImportError:
            raise ImportError("FastMCP not installed. Run: pip install fastmcp") from None

        if "url" in config:
            client = FastMCPClient(config["url"])
        elif "command" in config:
            command = config["command"]
            args = config.get("args", [])
            if not isinstance(args, list):
                raise ValueError("Config 'args' must be a list")

            env = os.environ.copy()
            env.update(config.get("env", {}))

            env = _filter_env(env, effective_security)

            if not (
                config.get("debug", False) or os.environ.get("MCP_DEBUG", "").lower() == "true"
            ):
                env.setdefault("LOG_LEVEL", "ERROR")
                env.setdefault("PYTHONWARNINGS", "ignore")
                env.setdefault("FASTMCP_QUIET", "true")
                env.setdefault("MCP_QUIET", "true")

            from fastmcp.client.transports import StdioTransport

            transport = StdioTransport(
                command=command,
                args=args,
                env=env,
            )
            client = FastMCPClient(transport)
        else:
            raise ValueError("Config must have 'url' or 'command'")

        await client.__aenter__()
        return client

    @classmethod
    async def cleanup(cls):
        async with cls._get_lock():
            for cache_key, client in cls._clients.items():
                try:
                    await client.__aexit__(None, None, None)
                except Exception as e:
                    logging.debug(f"Error cleaning up MCP client {cache_key}: {e}")
            cls._clients.clear()


def create_mcp_tool(mcp_config: dict[str, Any], tool_name: str) -> Any:
    """Create an async callable wrapping MCP tool execution."""

    async def mcp_callable(**kwargs):
        actual_tool_name = mcp_config.get("_original_tool_name", tool_name)

        config_for_client = {k: v for k, v in mcp_config.items() if not k.startswith("_")}

        client = await MCPConnectionPool.get_client(config_for_client)

        result = await client.call_tool(actual_tool_name, kwargs)

        if hasattr(result, "content"):
            content = result.content
            if isinstance(content, list) and len(content) == 1:
                item = content[0]
                if hasattr(item, "text"):
                    return item.text
                elif isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")
            return content
        elif isinstance(result, list) and len(result) == 1:
            item = result[0]
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")

        return result

    mcp_callable.__name__ = tool_name
    mcp_callable.__doc__ = f"MCP tool: {tool_name}"

    return mcp_callable
