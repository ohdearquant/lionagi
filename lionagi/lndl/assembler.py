# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL value assembler — turns parsed LNDL programs into typed Python values.

The assembler takes:
- a Program (lvars, lacts, out_block from the LNDL parser)
- a target type (response_format Pydantic model)
- optionally a mapping of alias → executed action result

and returns a dict ready for `target.model_validate()`.

Supports:
- scalar specs (int, float, str, bool)
- nested model specs (Report.title + Report.summary → Report instance)
- list[scalar] (multiple raw aliases)
- list[Model] (field-repeat detection groups aliases into instances)
- dict[str, V] (model.field-style → key-value pairs)
"""

from __future__ import annotations

import types as _types
from typing import Any, Union, get_args, get_origin

from .ast import Lact, Lvar, Program, RLvar
from .errors import InvalidConstructorError, MissingFieldError, MissingLvarError
from .types import ActionCall

NOTE_NAMESPACE = "note"


def _is_note_ref(ref: str) -> bool:
    """An OUT{} ref like 'note.draft' addresses the cross-round scratchpad."""
    return ref.startswith(f"{NOTE_NAMESPACE}.") and len(ref) > len(NOTE_NAMESPACE) + 1


def _note_key(ref: str) -> str:
    """Strip the 'note.' prefix from a ref like 'note.draft' → 'draft'."""
    return ref[len(NOTE_NAMESPACE) + 1 :]


def collect_notes(program: Program) -> dict[str, Any]:
    """Pull every <lvar note.X alias>...</lvar> out of a parsed program.

    Returns a dict keyed by the note name (without the 'note.' prefix). Used
    by the multi-round operate loop to populate the scratchpad each round —
    note values written this round become referenceable in later rounds.
    """
    notes: dict[str, Any] = {}
    for lv in program.lvars:
        if isinstance(lv, Lvar) and lv.model and lv.model.lower() == NOTE_NAMESPACE:
            field = getattr(lv, "field", None)
            if field:
                notes[field] = lv.content
    return notes


def _is_model_cls(t: Any) -> bool:
    return isinstance(t, type) and hasattr(t, "model_fields")


def _coerce_str_to_list(s: str) -> list[Any]:
    """Best-effort coerce a string to a list.

    Strict order:
    1. JSON array → use as-is.
    2. Python list literal → use as-is.
    3. Newline-separated lines → split on newlines.
    4. Bracketed comma list (``"[a, b, c]"``) → split inside brackets.
    5. Otherwise → return ``[s]``: treat the whole string as a single item.
       This avoids shredding prose by commas (e.g. "Step 1, then step 2, …"
       used to fragment into noise).
    """
    s = s.strip()
    if not s:
        return []
    # 1) JSON
    import json

    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return parsed
    except Exception:  # noqa: S110  # intentional: exhausts every conversion strategy before giving up
        pass
    # 2) Python literal
    import ast as _ast

    try:
        parsed = _ast.literal_eval(s)
        if isinstance(parsed, list):
            return parsed
    except Exception:  # noqa: S110  # intentional: exhausts every conversion strategy before giving up
        pass
    # 3) Newline-separated — recognise common bullet/numbered prefixes
    if "\n" in s:
        lines = [ln.rstrip() for ln in s.splitlines() if ln.strip()]
        if lines:
            return lines
    # 4) Explicit "[a, b, c]" envelope
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1]
        if inner.strip():
            items = [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
            if items:
                return items
    # 5) Conservative default — single-item list. Prose stays intact.
    return [s]


def _coerce_field_value(value: Any, field_type: Any) -> Any:
    """Coerce a single value to roughly match ``field_type``.

    Handles list[scalar] from a string. Other cases pass through —
    pydantic's own validators will do the rest.
    """
    field_type = _unwrap_optional(field_type)
    origin = get_origin(field_type)
    if origin is list and isinstance(value, str):
        args = get_args(field_type)
        if not args or not _is_model_cls(args[0]):
            return _coerce_str_to_list(value)
    return value


def _coerce_model_dict(value: dict, model_cls: type) -> dict:
    """Coerce each field of a dict to its declared type on the model class."""
    coerced: dict = {}
    fields = getattr(model_cls, "model_fields", None) or {}
    for k, v in value.items():
        if k in fields:
            coerced[k] = _coerce_field_value(v, fields[k].annotation)
        else:
            coerced[k] = v
    return coerced


def _unwrap_optional(t: Any) -> Any:
    """If ``t`` is Optional[X] / X | None, return X. Else return ``t``.

    Handles ``Union[X, None]``, ``Optional[X]``, and PEP 604 ``X | None``.
    For unions of multiple non-None types, returns the first model type if any,
    else the first non-None type.
    """
    origin = get_origin(t)
    if origin is Union or origin is _types.UnionType:
        args = [a for a in get_args(t) if a is not type(None)]
        if not args:
            return t
        for a in args:
            if _is_model_cls(a):
                return a
        return args[0]
    return t


def _alias_field(node: Lvar | RLvar | Lact) -> str | None:
    """Return the field name encoded in this alias (None if raw)."""
    if isinstance(node, (Lvar, Lact)):
        return getattr(node, "field", None)
    return None


def build_action_call(alias: str, node: Lact) -> ActionCall:
    """Parse a lact node's call text into an ``ActionCall`` placeholder.

    Shared by ``_alias_value`` (OUT-gated resolution) and callers executing
    lacts directly (e.g. a Continue-round tool pass with no OUT{} to gate
    against). Raises ``InvalidConstructorError`` when the call text isn't a
    parseable ``fn(args)`` expression.
    """
    from ._parse_function_call import parse_function_call, qualified_name

    try:
        parsed = parse_function_call(node.call)
    except Exception as e:
        raise InvalidConstructorError(
            f"lact '{alias}' body is not a parseable function call: {node.call!r}"
        ) from e
    return ActionCall(
        name=alias,
        function=qualified_name(parsed),
        arguments=parsed["arguments"],
        raw_call=node.call,
    )


def _alias_value(
    alias: str,
    lvars_by_alias: dict[str, Lvar | RLvar],
    lacts_by_alias: dict[str, Lact],
    action_results: dict[str, Any] | None,
    scratchpad: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    """Return (found, value) for an alias.

    For a lact alias:
    - if action_results has its result → return the result
    - else return the ActionCall placeholder (caller will execute later)

    For an OUT-side ``note.X`` ref: pull the value from ``scratchpad``.

    An alias not declared this round (neither an lvar nor a lact here) but
    present in ``action_results`` resolves to that historical result — a
    later round's OUT{} can reference a lact executed in an earlier round
    without re-declaring it, matching "tool results already in history".

    Raises ``MissingLvarError`` when the alias is not declared anywhere
    (this round's lvars/lacts) and has no historical result either.
    """
    if _is_note_ref(alias):
        if scratchpad is not None:
            key = _note_key(alias)
            if key in scratchpad:
                return True, scratchpad[key]
        return False, None
    if alias in lacts_by_alias:
        if action_results and alias in action_results:
            return True, action_results[alias]
        return True, build_action_call(alias, lacts_by_alias[alias])
    if alias in lvars_by_alias:
        node = lvars_by_alias[alias]
        return True, node.content
    if action_results and alias in action_results:
        return True, action_results[alias]
    raise MissingLvarError(f"OUT{{}} references undeclared alias '{alias}'")


def assemble_spec_value(
    refs: list[str],
    target_type: Any,
    lvars_by_alias: dict[str, Lvar | RLvar],
    lacts_by_alias: dict[str, Lact],
    action_results: dict[str, Any] | None = None,
    scratchpad: dict[str, Any] | None = None,
) -> Any:
    """Resolve OUT{}-listed aliases into a value matching ``target_type``."""
    parts: list[tuple[str | None, Any]] = []
    for alias in refs:
        # Defensive: aliases must be hashable strings. Skip nested structures
        # or scalar literals that slipped through (e.g. a malformed OUT block).
        if not isinstance(alias, str):
            continue
        found, value = _alias_value(
            alias, lvars_by_alias, lacts_by_alias, action_results, scratchpad
        )
        if not found:
            continue
        node = lacts_by_alias.get(alias) or lvars_by_alias.get(alias)
        field = _alias_field(node) if node else None
        parts.append((field, value))

    if not parts:
        return None

    target_type = _unwrap_optional(target_type)
    origin = get_origin(target_type)
    args = get_args(target_type)

    # list[X]
    if origin is list:
        elem = args[0] if args else Any
        if _is_model_cls(elem):
            # list[Model] — handled below from the original refs list, since
            # we need to know if the OUT block used explicit nested groups.
            # The caller (assemble) routes nested groups to _assemble_grouped.
            ordered_fields = list(elem.model_fields.keys())
            items: list[dict] = []
            current: dict = {}

            def next_unset(d: dict) -> str | None:
                for f in ordered_fields:
                    if f not in d:
                        return f
                return None

            for field, value in parts:
                if field is None:
                    f = next_unset(current)
                    if f is None:
                        items.append(_coerce_model_dict(current, elem))
                        current = {}
                        f = next_unset(current)
                    if f is not None:
                        current[f] = value
                else:
                    if field in current:
                        items.append(_coerce_model_dict(current, elem))
                        current = {field: value}
                    else:
                        current[field] = value
            if current:
                items.append(_coerce_model_dict(current, elem))
            return items
        # list[scalar]: if a single string-encoded list was provided, try to coerce it
        if len(parts) == 1 and isinstance(parts[0][1], str):
            return _coerce_str_to_list(parts[0][1])
        return [value for _, value in parts]

    # dict[K, V]
    if origin is dict:
        return {field: value for field, value in parts if field is not None}

    # Pydantic nested model — same fill rule as list[Model]:
    # raw aliases (no field) fill un-set fields in declaration order.
    if _is_model_cls(target_type):
        ordered_fields = list(target_type.model_fields.keys())
        d: dict = {}
        for field, value in parts:
            if field is None:
                for f in ordered_fields:
                    if f not in d:
                        d[f] = value
                        break
            else:
                d[field] = value
        return _coerce_model_dict(d, target_type)

    # Scalar — single alias is the typical case
    if len(parts) == 1:
        return parts[0][1]
    return [value for _, value in parts]


def _assemble_grouped_list(
    groups: list[list[str]],
    elem_type: Any,
    lvars_by_alias: dict[str, Lvar | RLvar],
    lacts_by_alias: dict[str, Lact],
    action_results: dict[str, Any] | None,
    scratchpad: dict[str, Any] | None = None,
) -> list[Any]:
    """Assemble explicit nested groups: ``[[n1, s1], [n2, s2]]`` → 2 items.

    Each inner list is one item; aliases inside are assembled as a single value
    of ``elem_type``. Inner items that turn out to be string literals (not
    declared aliases) get dropped onto the model's first string-typed field —
    a fallback for when the model writes ``[["raw text 1"], ["raw text 2"]]``
    instead of nested aliases.
    """
    items: list[Any] = []
    for group in groups:
        if not isinstance(group, list):
            # Mixed: a flat alias outside a group — promote into singleton group
            group = [group]
        try:
            value = assemble_spec_value(
                group,
                elem_type,
                lvars_by_alias,
                lacts_by_alias,
                action_results,
                scratchpad,
            )
        except MissingLvarError:
            # None of this group's entries resolved as declared aliases —
            # the model likely wrote raw string literals directly instead of
            # nested aliases. Fall through to the salvage fallback below
            # rather than treating a within-group literal as a real error.
            value = None
        if value is None and _is_model_cls(elem_type):
            # All entries in the group were string literals (none of them
            # registered aliases). Salvage by piping the joined text into
            # the first string-typed field on the model.
            target_field = None
            for fname, finfo in elem_type.model_fields.items():
                if _unwrap_optional(finfo.annotation) is str:
                    target_field = fname
                    break
            if target_field:
                joined = " ".join(str(g) for g in group if isinstance(g, str))
                value = {target_field: joined}
        items.append(value)
    return items


def assemble(
    program: Program,
    target: Any,
    action_results: dict[str, Any] | None = None,
    scratchpad: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a dict from a parsed LNDL program suitable for ``target.model_validate``.

    Returns a dict keyed by spec name (matching ``target.model_fields``).
    Values may be:
    - resolved scalars / lists / dicts / nested-model dicts
    - ``ActionCall`` placeholders if a lact has not been executed yet (caller
      should walk the result, execute, then call assemble again with results)
    """
    if not program.out_block:
        return {}

    lvars_by_alias = {lv.alias: lv for lv in program.lvars}
    lacts_by_alias = {la.alias: la for la in program.lacts}
    model_fields = getattr(target, "model_fields", None)

    # Merge in any new note.X declarations from this round so their values
    # are immediately available to OUT{} resolution.
    scratchpad = dict(scratchpad) if scratchpad else {}
    for k, v in collect_notes(program).items():
        scratchpad[k] = v

    out: dict[str, Any] = {}
    for spec_name, refs in program.out_block.fields.items():
        if isinstance(refs, list):
            field_type: Any = Any
            if model_fields and spec_name in model_fields:
                field_type = model_fields[spec_name].annotation

            # Detect explicit nested groups: [[...], [...]]
            has_groups = bool(refs) and any(isinstance(r, list) for r in refs)
            if has_groups:
                inner_t = _unwrap_optional(field_type)
                origin = get_origin(inner_t)
                if origin is list:
                    elem_t = get_args(inner_t)[0] if get_args(inner_t) else Any
                else:
                    elem_t = inner_t
                out[spec_name] = _assemble_grouped_list(
                    refs,
                    elem_t,
                    lvars_by_alias,
                    lacts_by_alias,
                    action_results,
                    scratchpad,
                )
                continue

            out[spec_name] = assemble_spec_value(
                refs,
                field_type,
                lvars_by_alias,
                lacts_by_alias,
                action_results,
                scratchpad,
            )
        else:
            # Bare scalar literal in OUT — but if it's a note.X ref, resolve.
            if isinstance(refs, str) and _is_note_ref(refs):
                key = _note_key(refs)
                if key in scratchpad:
                    out[spec_name] = scratchpad[key]
                    continue
            out[spec_name] = refs

    if model_fields:
        required = {name for name, info in model_fields.items() if info.is_required()}
        missing = required - out.keys()
        if missing:
            raise MissingFieldError(
                f"OUT{{}} is missing required field(s): {', '.join(sorted(missing))}"
            )
    return out


def collect_actions(value: Any) -> list[ActionCall]:
    """Walk a structure produced by ``assemble`` and gather all ActionCall placeholders."""
    found: list[ActionCall] = []

    def walk(v: Any) -> None:
        if isinstance(v, ActionCall):
            found.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return found


def replace_actions(value: Any, results_by_name: dict[str, Any]) -> Any:
    """Substitute ActionCall placeholders with their executed results.

    ``results_by_name`` maps the ActionCall.name (alias) → result value.
    """
    if isinstance(value, ActionCall):
        if value.name in results_by_name:
            return results_by_name[value.name]
        return value
    if isinstance(value, dict):
        return {k: replace_actions(v, results_by_name) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_actions(v, results_by_name) for v in value]
    return value


__all__ = (
    "NOTE_NAMESPACE",
    "assemble",
    "assemble_spec_value",
    "build_action_call",
    "collect_actions",
    "collect_notes",
    "replace_actions",
)
