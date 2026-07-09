# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Workflow definitions service — backs /api/workflow-defs endpoints."""

from __future__ import annotations

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
    """Workflow definition name is already taken."""


# Closed set of node kinds — must match the Designer frontend's WorkflowNodeKind.
# 'gate' was removed (conditions now live on edges, not gate nodes — see the
# workflow-exec spec, Fork 1). A saved def still carrying a 'gate' node fails
# validation here (create/update) and load (get_workflow_def_route) with an
# actionable, node-naming error rather than a generic 422.
_VALID_NODE_KINDS: frozenset[str] = frozenset({"input", "chat", "parse", "fanout", "engine"})

_MAX_NODES = 200
_MAX_EDGES = 400


def _validate_spec(spec: dict[str, Any] | None) -> None:
    """Validate a workflow spec's graph shape; raises ValueError on problems."""
    if spec is None:
        return
    if not isinstance(spec, dict):
        raise ValueError("spec_json must be an object")
    if spec.get("version") != 1:
        raise ValueError(f"spec_json.version must be 1, got {spec.get('version')!r}")

    nodes = spec.get("nodes", [])
    edges = spec.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("spec_json.nodes and spec_json.edges must be arrays")
    if len(nodes) > _MAX_NODES:
        raise ValueError(f"spec_json.nodes exceeds the {_MAX_NODES}-node limit")
    if len(edges) > _MAX_EDGES:
        raise ValueError(f"spec_json.edges exceeds the {_MAX_EDGES}-edge limit")

    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("each node must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("each node requires a non-empty string id")
        if node_id in node_ids:
            raise ValueError(f"duplicate node id {node_id!r}")
        node_ids.add(node_id)
        kind = node.get("kind")
        if kind not in _VALID_NODE_KINDS:
            raise ValueError(
                f"node {node_id!r} has invalid kind {kind!r}. "
                f"Valid kinds: {sorted(_VALID_NODE_KINDS)}"
            )
        if kind == "chat":
            _validate_chat_config(node_id, node.get("config"))
        pos = node.get("pos")
        if not isinstance(pos, dict) or not all(
            isinstance(pos.get(axis), int | float) and not isinstance(pos.get(axis), bool)
            for axis in ("x", "y")
        ):
            raise ValueError(f"node {node_id!r} requires numeric pos.x and pos.y")

    edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("each edge must be an object")
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id:
            raise ValueError("each edge requires a non-empty string id")
        if edge_id in edge_ids:
            raise ValueError(f"duplicate edge id {edge_id!r}")
        edge_ids.add(edge_id)
        for endpoint in ("from", "to"):
            target = edge.get(endpoint)
            if target not in node_ids:
                raise ValueError(f"edge {edge_id!r} {endpoint} references unknown node {target!r}")
        condition = edge.get("condition")
        if condition is not None and (not isinstance(condition, str) or not condition.strip()):
            raise ValueError(f"edge {edge_id!r} condition must be a non-empty string")

    for field in ("inputs", "outputs"):
        vals = spec.get(field, [])
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            raise ValueError(f"spec_json.{field} must be an array of strings")


def _validate_chat_config(node_id: str, config: Any) -> None:
    """WorkflowChatConfig: {prompt: str (required), model?: str}."""
    if not isinstance(config, dict) or not config.get("prompt"):
        raise ValueError(f"node {node_id!r} (kind 'chat') requires config.prompt")
    if not isinstance(config["prompt"], str):
        raise ValueError(f"node {node_id!r} config.prompt must be a string")
    model = config.get("model")
    if model is not None:
        if not isinstance(model, str):
            raise ValueError(f"node {node_id!r} config.model must be a string")
        if "/" not in model:
            raise ValueError(
                f"node {node_id!r} config.model must be provider-prefixed "
                f"(provider/name, e.g. openai/gpt-4.1-mini); got {model!r}. "
                "Use provider/model, e.g. openai/gpt-4.1-mini."
            )


def _find_gate_node_ids(spec: dict[str, Any] | None) -> list[str]:
    """Ids of any 'gate' nodes in *spec* — the kind was removed; see _VALID_NODE_KINDS."""
    if not isinstance(spec, dict):
        return []
    return [
        node.get("id")
        for node in spec.get("nodes", [])
        if isinstance(node, dict) and node.get("kind") == "gate"
    ]


def _validate_name(name: str) -> None:
    if not name or len(name) > 120:
        raise ValueError("name must be 1-120 characters")


async def list_workflow_defs(*, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        return await db.list_workflow_defs(limit=limit, offset=offset)


async def get_workflow_def(def_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        return await db.get_workflow_def(def_id)


async def create_workflow_def(data: dict[str, Any]) -> dict[str, Any]:
    name = data.get("name", "").strip()
    _validate_name(name)
    _validate_spec(data.get("spec_json"))

    def_id = uuid.uuid4().hex[:12]
    now = time.time()
    defn = {
        "id": def_id,
        "name": name,
        "description": data.get("description"),
        "spec_json": data.get("spec_json"),
        "created_at": now,
        "updated_at": now,
    }
    async with StateDB() as db:
        try:
            await db.create_workflow_def(defn)
        except (sqlite3.IntegrityError, SAIntegrityError) as exc:
            raise NameConflictError(f"Workflow definition name {name!r} already exists") from exc
    return {"id": def_id, "name": name, "created_at": now}


async def update_workflow_def(def_id: str, fields: dict[str, Any]) -> bool:
    async with StateDB() as db:
        existing = await db.get_workflow_def(def_id)
        if not existing:
            return False
        if not fields:
            return True

        if "name" in fields:
            fields["name"] = fields["name"].strip()
            _validate_name(fields["name"])
        if "spec_json" in fields:
            _validate_spec(fields["spec_json"])

        try:
            await db.update_workflow_def(def_id, **fields)
        except (sqlite3.IntegrityError, SAIntegrityError) as exc:
            raise NameConflictError(
                f"Workflow definition name {fields.get('name')!r} already exists"
            ) from exc
    return True


async def delete_workflow_def(def_id: str) -> bool:
    async with StateDB() as db:
        return await db.delete_workflow_def(def_id)


class CreateWorkflowDefRequest(BaseModel):
    name: str
    description: str | None = None
    spec_json: dict[str, Any] | None = None


class UpdateWorkflowDefRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    spec_json: dict[str, Any] | None = None


@studio_route("/workflow-defs/", method="GET", area="workflow-defs", name="list_workflow_defs")
async def list_workflow_defs_route(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await list_workflow_defs(limit=limit, offset=offset)


@studio_route(
    "/workflow-defs/",
    method="POST",
    area="workflow-defs",
    status_code=201,
    name="create_workflow_def",
)
async def create_workflow_def_route(body: CreateWorkflowDefRequest) -> dict[str, Any]:
    try:
        return await create_workflow_def(body.model_dump(exclude_none=True))
    except NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@studio_route(
    "/workflow-defs/{def_id}", method="GET", area="workflow-defs", name="get_workflow_def"
)
async def get_workflow_def_route(def_id: str) -> dict[str, Any]:
    data = await get_workflow_def(def_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Workflow definition '{def_id}' not found")
    gate_ids = _find_gate_node_ids(data.get("spec_json"))
    if gate_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Workflow definition {def_id!r} contains removed 'gate' node(s) "
                f"{gate_ids!r}. The 'gate' node kind was removed — conditions now "
                "live on edges (WorkflowEdge.condition). Edit the definition to "
                "remove or replace these nodes before it can be loaded."
            ),
        )
    return data


@studio_route(
    "/workflow-defs/{def_id}", method="PUT", area="workflow-defs", name="update_workflow_def"
)
async def update_workflow_def_route(def_id: str, body: UpdateWorkflowDefRequest) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    try:
        ok = await update_workflow_def(def_id, fields)
    except NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Workflow definition '{def_id}' not found")
    return {"ok": True}


@studio_route(
    "/workflow-defs/{def_id}", method="DELETE", area="workflow-defs", name="delete_workflow_def"
)
async def delete_workflow_def_route(def_id: str) -> dict[str, Any]:
    ok = await delete_workflow_def(def_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Workflow definition '{def_id}' not found")
    return {"ok": True}


class RunWorkflowDefRequest(BaseModel):
    inputs: dict[str, Any] | None = None


@studio_route(
    "/workflow-defs/{def_id}/run",
    method="POST",
    area="workflow-defs",
    status_code=202,
    name="run_workflow_def",
)
async def run_workflow_def_route(def_id: str, body: RunWorkflowDefRequest) -> dict[str, Any]:
    """Compile *def_id* and execute it via Session.flow; returns {run_id, status}.

    run_id is the same id GET /api/sessions/{id} and Fleet/History already read —
    the run appears there like any other flow run (no new telemetry surface).
    """
    from .workflow_compile import WorkflowCompileError
    from .workflow_run import WorkflowNotFoundError, run_workflow_def

    try:
        return await run_workflow_def(def_id, body.inputs)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowCompileError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
