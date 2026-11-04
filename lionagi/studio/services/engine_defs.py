# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Engine definitions service — backs /api/engine-defs endpoints."""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError as SAIntegrityError

import lionagi.cli._engine_definitions as _engine_registry
from lionagi.ln import short_id
from lionagi.state.db import StateDB, state_db_known_absent

from ..registry import studio_route

KIND_META = _engine_registry.KIND_META
is_registered_engine_kind = _engine_registry._is_registered_engine_kind


class NameConflictError(Exception):
    """Engine definition name is already taken."""


# Closed set of engine kinds — must equal set(lionagi.cli._engine_definitions.KIND_META).
_VALID_ENGINE_KINDS: frozenset[str] = frozenset(
    {"research", "review", "coding", "hypothesis", "planning"}
)

# Allowed keys in the options JSON object.
_ALLOWED_OPTIONS_KEYS: frozenset[str] = frozenset({"test_cmd", "export_dir"})
_PRESET_BUDGET_BOUNDS = (1, 100)

# Safe shell-token pattern: no leading '-', no shell metacharacters.
_SAFE_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_./:@\-\+= ]+$")


def _validate_kind(kind: str) -> None:
    if kind not in _VALID_ENGINE_KINDS:
        raise ValueError(
            f"Invalid engine kind {kind!r}. Valid kinds: {sorted(_VALID_ENGINE_KINDS)}"
        )


def _validate_options(options: dict[str, Any] | None) -> None:
    if not options:
        return
    bad_keys = set(options) - _ALLOWED_OPTIONS_KEYS
    if bad_keys:
        raise ValueError(
            f"options contains disallowed keys {sorted(bad_keys)}. "
            f"Allowed: {sorted(_ALLOWED_OPTIONS_KEYS)}"
        )
    for key, val in options.items():
        if not isinstance(val, str):
            raise ValueError(f"options.{key} must be a string, got {type(val).__name__}")
        if val.startswith("-"):
            raise ValueError(f"options.{key} {val!r} starts with '-' and would inject a CLI flag")
        if not _SAFE_TOKEN_RE.match(val):
            raise ValueError(
                f"options.{key} {val!r} contains shell metacharacters not allowed in "
                "engine option values"
            )


def _validate_kind_options(kind: str, options: dict[str, Any] | None) -> None:
    """Reject kind/options combinations the engine CLI would refuse to run."""
    test_cmd = (options or {}).get("test_cmd")
    if kind == "coding" and not str(test_cmd or "").strip():
        raise ValueError(
            "the 'coding' engine kind requires a non-blank options.test_cmd "
            "(e.g. {'test_cmd': 'pytest tests/'})"
        )


def _validate_budget(field: str, val: Any) -> None:
    """Validate an optional max_depth/max_agents override against the preset bounds."""
    if val is None:
        return
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError(f"{field} must be an integer")
    low, high = _PRESET_BUDGET_BOUNDS
    if not (low <= val <= high):
        raise ValueError(f"{field} must be in [{low}, {high}], got {val}")


def _svc_validate_action_model(model: str | None) -> None:
    if not model:
        return
    from lionagi.studio.scheduler.subprocess import _validate_action_model

    _validate_action_model(model)


def _svc_validate_identifier(value: str | None, field_name: str) -> None:
    if not value:
        return
    from lionagi.studio.scheduler.subprocess import _validate_identifier

    _validate_identifier(value, field_name)


async def list_engine_defs(
    *,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if state_db_known_absent():
        return []
    async with StateDB() as db:
        return await db.list_engine_defs(kind=kind, limit=limit, offset=offset)


async def get_engine_def(def_id: str) -> dict[str, Any] | None:
    if state_db_known_absent():
        return None
    async with StateDB() as db:
        return await db.get_engine_def(def_id)


async def get_engine_def_by_name(name: str) -> dict[str, Any] | None:
    if state_db_known_absent():
        return None
    async with StateDB() as db:
        return await db.get_engine_def_by_name(name)


async def create_engine_def(data: dict[str, Any]) -> dict[str, Any]:
    name = data.get("name", "").strip()
    if not name:
        raise ValueError("Engine definition name is required")
    _svc_validate_identifier(name, "name")

    kind = data.get("kind", "")
    _validate_kind(kind)

    _svc_validate_action_model(data.get("model"))

    for field in ("max_depth", "max_agents"):
        _validate_budget(field, data.get(field))

    _validate_options(data.get("options"))
    _validate_kind_options(kind, data.get("options"))

    def_id = short_id(12)
    now = time.time()
    defn = {
        "id": def_id,
        "name": name,
        "kind": kind,
        "model": data.get("model"),
        "max_depth": data.get("max_depth"),
        "max_agents": data.get("max_agents"),
        "options": data.get("options"),
        "description": data.get("description"),
        "created_at": now,
        "updated_at": now,
    }
    async with StateDB() as db:
        try:
            await db.create_engine_def(defn)
        except (sqlite3.IntegrityError, SAIntegrityError) as exc:
            raise NameConflictError(f"Engine definition name {name!r} already exists") from exc
    return {"id": def_id, "name": name, "created_at": now}


async def update_engine_def(def_id: str, fields: dict[str, Any]) -> dict[str, Any] | bool:
    async with StateDB() as db:
        existing = await db.get_engine_def(def_id)
        if not existing:
            return False
        if not fields:
            return existing

        if "name" in fields:
            _svc_validate_identifier(fields["name"], "name")
        if "kind" in fields:
            _validate_kind(fields["kind"])
        if "model" in fields:
            _svc_validate_action_model(fields["model"])
        for field in ("max_depth", "max_agents"):
            if field in fields:
                _validate_budget(field, fields[field])
        if "options" in fields:
            _validate_options(fields["options"])

        # Validate the EFFECTIVE merged definition, not just the patch — a kind
        # change to 'coding' or dropping test_cmd would otherwise store a def the CLI rejects.
        effective_kind = fields.get("kind", existing.get("kind"))
        effective_options = fields["options"] if "options" in fields else existing.get("options")
        _validate_kind_options(effective_kind, effective_options)

        try:
            await db.update_engine_def(def_id, **fields)
        except (sqlite3.IntegrityError, SAIntegrityError) as exc:
            raise NameConflictError(
                f"Engine definition name {fields.get('name')!r} already exists"
            ) from exc
    return True


async def delete_engine_def(def_id: str) -> bool:
    async with StateDB() as db:
        return await db.delete_engine_def(def_id)


class CreateEngineDefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    model: str | None = None
    max_depth: int | None = None
    max_agents: int | None = None
    options: dict[str, str] | None = None
    description: str | None = None


class UpdateEngineDefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    kind: str | None = None
    model: str | None = None
    max_depth: int | None = None
    max_agents: int | None = None
    options: dict[str, str] | None = None
    description: str | None = None


@studio_route("/engine-defs/", method="GET", area="engine-defs", name="list_engine_defs")
async def list_engine_defs_route(
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await list_engine_defs(kind=kind, limit=limit, offset=offset)


@studio_route(
    "/engine-defs/", method="POST", area="engine-defs", status_code=201, name="create_engine_def"
)
async def create_engine_def_route(body: CreateEngineDefRequest) -> dict[str, Any]:
    try:
        return await create_engine_def(body.model_dump(exclude_none=True))
    except NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@studio_route("/engine-defs/{def_id}", method="GET", area="engine-defs", name="get_engine_def")
async def get_engine_def_route(def_id: str) -> dict[str, Any]:
    data = await get_engine_def(def_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Engine definition '{def_id}' not found")
    return data


@studio_route("/engine-defs/{def_id}", method="PUT", area="engine-defs", name="update_engine_def")
async def update_engine_def_route(def_id: str, body: UpdateEngineDefRequest) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    try:
        ok = await update_engine_def(def_id, fields)
    except NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Engine definition '{def_id}' not found")
    return {"ok": True}


@studio_route(
    "/engine-defs/{def_id}", method="DELETE", area="engine-defs", name="delete_engine_def"
)
async def delete_engine_def_route(def_id: str) -> dict[str, Any]:
    ok = await delete_engine_def(def_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Engine definition '{def_id}' not found")
    return {"ok": True}


_CATALOG_CURSOR_ID_BYTES = 4096
_CATALOG_OPTIONS_MAX_DEPTH = 32
_CATALOG_JSON_STRING = r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
_CATALOG_CURSOR_SHAPE = re.compile(
    rf'\{{"v":1,"after":{_CATALOG_JSON_STRING},"kind":(?:null|{_CATALOG_JSON_STRING})\}}'
)


def _catalog_encode_cursor(after: str, kind: str | None) -> str:
    import base64
    import json

    payload = json.dumps(
        {"v": 1, "after": after, "kind": kind}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _catalog_decode_cursor(cursor: str, kind: str | None) -> str:
    import base64
    import binascii
    import json

    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError("invalid encoding")
        data = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = data.decode("utf-8")
        # The shape check forbids nested containers and duplicate keys before JSON
        # decoding, so hostile nesting cannot trigger decoder recursion.
        if _CATALOG_CURSOR_SHAPE.fullmatch(payload) is None:
            raise ValueError("invalid shape")
        obj = json.loads(payload)
        after = obj["after"]
        if (
            not isinstance(after, str)
            or obj["kind"] != kind
            or len(after.encode("utf-8")) > _CATALOG_CURSOR_ID_BYTES
        ):
            raise ValueError("invalid cursor")
        if _catalog_encode_cursor(after, kind) != cursor:
            raise ValueError("noncanonical cursor")
        return after
    except (binascii.Error, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail="Invalid engine catalog cursor") from exc


def _catalog_options(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    import json

    if raw is None:
        return {}, None
    if isinstance(raw, bytes | bytearray | memoryview):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None, "options_undecodable"
    if isinstance(raw, str):
        # Check depth before decoding: json.loads itself can recurse on deeply nested input.
        depth = 0
        quoted = False
        escaped = False
        for char in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > _CATALOG_OPTIONS_MAX_DEPTH:
                    return None, "options_undecodable"
            elif char in "]}":
                depth -= 1

        class NonJsonConstantError(ValueError):
            pass

        def reject_constant(_constant: str) -> None:
            raise NonJsonConstantError("non-JSON constant")

        try:
            value = json.loads(raw, parse_constant=reject_constant)
        except (json.JSONDecodeError, NonJsonConstantError):
            return None, "options_malformed"
        except (RecursionError, UnicodeError, ValueError):
            return None, "options_undecodable"
    else:
        value = raw
    try:
        # The response must be encodable: reject lone surrogates and non-finite values.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (RecursionError, UnicodeError, ValueError, TypeError):
        return None, "options_undecodable"
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, "options_not_object"
    return value, None


def _catalog_annotate_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    options, options_reason = _catalog_options(result.pop("options", None))
    result["options"] = options
    result["unused_option_keys"] = None
    result["stored_option_keys"] = None
    reasons = []
    kind = result.get("kind")
    registered = is_registered_engine_kind(kind)
    if not isinstance(kind, str):
        result["kind"] = None
        reasons.append("kind_not_text")
    elif not registered:
        reasons.append("kind_not_registered")
    if options_reason is not None:
        reasons.append(options_reason)
    if options is not None:
        keys = sorted(options)
        result["stored_option_keys"] = keys
        if registered:
            definitions = KIND_META[kind]["preset_options"]
            result["unused_option_keys"] = sorted(set(keys) - set(definitions))
            missing_keys = sorted(
                name
                for name, definition in definitions.items()
                if definition.get("required") and not str(options.get(name) or "").strip()
            )
            if missing_keys:
                result["missing_option_keys"] = missing_keys
                reasons.append("required_option_missing")
            try:
                _validate_options(options)
            except ValueError:
                invalid_keys = []
                for name in keys:
                    try:
                        _validate_options({name: options[name]})
                    except ValueError:
                        invalid_keys.append(name)
                # Validator messages can include values; only expose the offending keys.
                result["options"] = None
                result["invalid_option_keys"] = invalid_keys
                reasons.append("options_invalid")
    result["availability"] = "unavailable" if reasons else "available"
    result["reasons"] = reasons
    return result


def _catalog_validate_kind(kind: str | None) -> None:
    if kind is not None and not is_registered_engine_kind(kind):
        raise HTTPException(status_code=422, detail="Invalid engine catalog kind")


def engine_catalog_blueprints() -> dict[str, Any]:
    """Describe preset inputs from the runtime engine registry."""
    items = []
    for kind, meta in KIND_META.items():
        items.append(
            {
                "kind": kind,
                "input": {"name": meta["pos_arg"], "help": meta["pos_help"]},
                "options": [
                    {"name": name, **option} for name, option in meta["preset_options"].items()
                ],
                "common_fields": {
                    "model": {"type": "string", "nullable": True, "scope": "preset"},
                    "max_depth": {
                        "type": "integer",
                        "nullable": True,
                        "minimum": _PRESET_BUDGET_BOUNDS[0],
                        "maximum": _PRESET_BUDGET_BOUNDS[1],
                        "scope": "preset",
                    },
                    "max_agents": {
                        "type": "integer",
                        "nullable": True,
                        "minimum": _PRESET_BUDGET_BOUNDS[0],
                        "maximum": _PRESET_BUDGET_BOUNDS[1],
                        "scope": "preset",
                    },
                },
            }
        )
    return {"schema_version": 1, "items": items}


async def list_engine_catalog_presets(
    *, kind: str | None = None, limit: int = 100, cursor: str | None = None
) -> dict[str, Any]:
    """List non-NULL-ID presets by ID, without snapshot isolation.

    An insertion before the cursor can be missed; deletes and renames preserve the ID order.
    Rows with NULL or non-text IDs are outside this catalog.
    """
    from lionagi.state.db import _list_engine_catalog_rows

    _catalog_validate_kind(kind)
    after = _catalog_decode_cursor(cursor, kind) if cursor is not None else None
    if state_db_known_absent():
        return {"items": [], "next_cursor": None}
    async with StateDB() as db:
        rows = await _list_engine_catalog_rows(db, kind=kind, after=after, limit=limit + 1)
    visible = rows[:limit]
    has_more = len(rows) > limit
    if visible and len(visible[0]["id"].encode("utf-8")) > _CATALOG_CURSOR_ID_BYTES:
        return {
            "items": [_catalog_annotate_row(visible[0])],
            "next_cursor": None,
            "stop_reason": "id_exceeds_cursor_bound",
        }
    # A trailing oversized ID cannot be encoded as the next seek position.
    while visible and len(visible[-1]["id"].encode("utf-8")) > _CATALOG_CURSOR_ID_BYTES:
        visible.pop()
        has_more = True
    next_cursor = _catalog_encode_cursor(visible[-1]["id"], kind) if has_more else None
    return {"items": [_catalog_annotate_row(row) for row in visible], "next_cursor": next_cursor}


async def get_engine_catalog_preset(def_id: str) -> dict[str, Any] | None:
    """Return a text-ID preset independently of the loaded page."""
    from lionagi.state.db import _get_engine_catalog_row

    if state_db_known_absent():
        return None
    async with StateDB() as db:
        row = await _get_engine_catalog_row(db, def_id)
    return _catalog_annotate_row(row) if row is not None else None


@studio_route(
    "/engine-catalog/blueprints", method="GET", area="engine-defs", name="engine_catalog_blueprints"
)
async def engine_catalog_blueprints_route() -> dict[str, Any]:
    return engine_catalog_blueprints()


_CATALOG_PRESETS_DESCRIPTION = (
    "List text-ID presets; paging is not a snapshot. Rows with NULL or non-text IDs "
    "are not listed. When an ID exceeds the cursor bound, that row is returned alone "
    "and rows after it cannot be reached by paging.\n\n"
    f"Options over {_CATALOG_OPTIONS_MAX_DEPTH} JSON nesting levels are unavailable. "
    "Preset inputs are flat key/value pairs, so this limit does not exclude normal "
    "preset options. "
    f"Preset budget overrides accept integers from {_PRESET_BUDGET_BOUNDS[0]} to "
    f"{_PRESET_BUDGET_BOUNDS[1]}, or null."
)


@studio_route(
    "/engine-catalog/presets", method="GET", area="engine-defs", name="list_engine_catalog_presets"
)
async def list_engine_catalog_presets_route(
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    """List text-ID presets with documented pagination and preset limits."""
    return await list_engine_catalog_presets(kind=kind, limit=limit, cursor=cursor)


list_engine_catalog_presets_route.__doc__ = _CATALOG_PRESETS_DESCRIPTION


@studio_route(
    "/engine-catalog/preset", method="GET", area="engine-defs", name="get_engine_catalog_preset"
)
async def get_engine_catalog_preset_route(def_id: str = Query(..., alias="id")) -> dict[str, Any]:
    """Look up a text-ID preset by query parameter, including an empty ID."""
    row = await get_engine_catalog_preset(def_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Engine preset not found")
    return row
