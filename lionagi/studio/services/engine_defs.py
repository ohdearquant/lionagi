# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Engine definitions service — backs /api/engine-defs endpoints."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from typing import Any

from fastapi import HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from lionagi.state.db import DEFAULT_DB_PATH, StateDB

from ..registry import studio_route


class NameConflictError(Exception):
    """Engine definition name is already taken."""


# Closed set of engine kinds — must equal set(lionagi.cli.engine._KIND_META).
_VALID_ENGINE_KINDS: frozenset[str] = frozenset(
    {"research", "review", "coding", "hypothesis", "planning"}
)

# Allowed keys in the options JSON object.
_ALLOWED_OPTIONS_KEYS: frozenset[str] = frozenset({"test_cmd", "export_dir"})

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
    """A max_depth/max_agents override must be an int in [1, 100] (or absent)."""
    if val is None:
        return
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError(f"{field} must be an integer")
    if not (1 <= val <= 100):
        raise ValueError(f"{field} must be in [1, 100], got {val}")


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
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        return await db.list_engine_defs(kind=kind, limit=limit, offset=offset)


async def get_engine_def(def_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        return await db.get_engine_def(def_id)


async def get_engine_def_by_name(name: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
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

    def_id = uuid.uuid4().hex[:12]
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
    name: str
    kind: str
    model: str | None = None
    max_depth: int | None = None
    max_agents: int | None = None
    options: dict[str, str] | None = None
    description: str | None = None


class UpdateEngineDefRequest(BaseModel):
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
