# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the workflow_defs Studio API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
pytest.importorskip("fastapi", reason="studio extra not installed")


def _spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "version": 1,
        "nodes": [
            {"id": "n1", "kind": "input", "label": "Input", "pos": {"x": 0, "y": 0}},
            {
                "id": "n2",
                "kind": "engine",
                "label": "Research",
                "pos": {"x": 200, "y": 0},
                "config": {"engine_def_id": "def-1"},
            },
        ],
        "edges": [{"id": "e1", "from": "n1", "to": "n2"}],
        "inputs": ["query"],
        "outputs": ["report"],
    }
    spec.update(overrides)
    return spec


@pytest.fixture
def patched_svc(tmp_path: Path, monkeypatch):
    import lionagi.state.db as db_mod
    import lionagi.studio.services.workflow_defs as svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    return svc, db_path


async def test_list_returns_empty_when_db_absent(patched_svc):
    svc, _ = patched_svc
    assert await svc.list_workflow_defs() == []


async def test_create_and_list(patched_svc):
    svc, _ = patched_svc
    result = await svc.create_workflow_def({"name": "my-flow", "spec_json": _spec()})
    assert "id" in result
    rows = await svc.list_workflow_defs()
    assert any(r["name"] == "my-flow" for r in rows)


async def test_create_without_spec(patched_svc):
    svc, _ = patched_svc
    result = await svc.create_workflow_def({"name": "empty-flow"})
    row = await svc.get_workflow_def(result["id"])
    assert row is not None
    assert row["spec_json"] is None


async def test_spec_round_trips_as_object(patched_svc):
    svc, _ = patched_svc
    result = await svc.create_workflow_def({"name": "rt-flow", "spec_json": _spec()})
    row = await svc.get_workflow_def(result["id"])
    assert row["spec_json"] == _spec()


async def test_create_empty_name_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="name"):
        await svc.create_workflow_def({"name": "   "})


async def test_create_name_conflict_raises(patched_svc):
    svc, _ = patched_svc
    await svc.create_workflow_def({"name": "dup-flow"})
    with pytest.raises(svc.NameConflictError, match="already exists"):
        await svc.create_workflow_def({"name": "dup-flow"})


async def test_bad_version_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="version"):
        await svc.create_workflow_def({"name": "v2", "spec_json": _spec(version=2)})


async def test_bad_node_kind_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["nodes"][0]["kind"] = "teleport"
    with pytest.raises(ValueError, match="invalid kind"):
        await svc.create_workflow_def({"name": "badkind", "spec_json": spec})


async def test_duplicate_node_id_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["nodes"][1]["id"] = "n1"
    spec["edges"] = []
    with pytest.raises(ValueError, match="duplicate node id"):
        await svc.create_workflow_def({"name": "dupnode", "spec_json": spec})


async def test_dangling_edge_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["edges"][0]["to"] = "missing"
    with pytest.raises(ValueError, match="unknown node"):
        await svc.create_workflow_def({"name": "dangling", "spec_json": spec})


async def test_non_numeric_pos_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["nodes"][0]["pos"] = {"x": "left", "y": 0}
    with pytest.raises(ValueError, match="pos"):
        await svc.create_workflow_def({"name": "badpos", "spec_json": spec})


async def test_update_spec_and_name(patched_svc):
    svc, _ = patched_svc
    result = await svc.create_workflow_def({"name": "upd-flow", "spec_json": _spec()})
    new_spec = _spec(outputs=["summary"])
    ok = await svc.update_workflow_def(result["id"], {"name": "renamed", "spec_json": new_spec})
    assert ok is True
    row = await svc.get_workflow_def(result["id"])
    assert row["name"] == "renamed"
    assert row["spec_json"]["outputs"] == ["summary"]


async def test_update_missing_returns_false(patched_svc):
    svc, _ = patched_svc
    await svc.create_workflow_def({"name": "seed"})
    assert await svc.update_workflow_def("nonexistent", {"description": "x"}) is False


async def test_update_invalid_spec_raises(patched_svc):
    svc, _ = patched_svc
    result = await svc.create_workflow_def({"name": "inv-upd", "spec_json": _spec()})
    with pytest.raises(ValueError, match="version"):
        await svc.update_workflow_def(result["id"], {"spec_json": {"version": 9}})


async def test_delete(patched_svc):
    svc, _ = patched_svc
    result = await svc.create_workflow_def({"name": "del-flow"})
    assert await svc.delete_workflow_def(result["id"]) is True
    assert await svc.get_workflow_def(result["id"]) is None
    assert await svc.delete_workflow_def(result["id"]) is False


def test_route_registration():
    from lionagi.studio.registry import iter_studio_routes, load_studio_route_modules

    load_studio_route_modules()
    paths = {(r.method, r.path) for r in iter_studio_routes(area="workflow-defs")}
    assert ("GET", "/workflow-defs/") in paths
    assert ("POST", "/workflow-defs/") in paths
    assert ("GET", "/workflow-defs/{def_id}") in paths
    assert ("PUT", "/workflow-defs/{def_id}") in paths
    assert ("DELETE", "/workflow-defs/{def_id}") in paths
    assert ("POST", "/workflow-defs/{def_id}/run") in paths


# ─── gate-kind removal (workflow-exec Fork 1) ───────────────────────────────


async def test_gate_kind_rejected_at_create(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["nodes"][0]["kind"] = "gate"
    with pytest.raises(ValueError, match="invalid kind"):
        await svc.create_workflow_def({"name": "gate-create", "spec_json": spec})


async def test_gate_node_load_guard_names_the_node(patched_svc):
    """A legacy row saved before the validator change fails GET with an actionable error."""
    svc, _ = patched_svc
    # Seed the db/tables, then write a legacy row directly (bypassing
    # _validate_spec, which now rejects 'gate' — this simulates data saved
    # before the kind was removed).
    await svc.create_workflow_def({"name": "seed"})

    import time
    import uuid

    from fastapi import HTTPException

    from lionagi.state.db import StateDB

    spec = _spec()
    spec["nodes"][0]["kind"] = "gate"
    def_id = uuid.uuid4().hex[:12]
    now = time.time()
    async with StateDB() as db:
        await db.create_workflow_def(
            {
                "id": def_id,
                "name": "legacy-gate",
                "description": None,
                "spec_json": spec,
                "created_at": now,
                "updated_at": now,
            }
        )

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_workflow_def_route(def_id)
    assert exc_info.value.status_code == 422
    assert "n1" in str(exc_info.value.detail)
    assert "gate" in str(exc_info.value.detail)


# ─── chat node config (WorkflowChatConfig) ──────────────────────────────────


def _spec_with_chat(config: dict[str, Any]) -> dict[str, Any]:
    spec = _spec()
    spec["nodes"].append(
        {"id": "n3", "kind": "chat", "label": "Chat", "pos": {"x": 100, "y": 0}, "config": config}
    )
    return spec


async def test_chat_node_missing_prompt_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="prompt"):
        await svc.create_workflow_def({"name": "nochatprompt", "spec_json": _spec_with_chat({})})


async def test_chat_node_non_string_prompt_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec_with_chat({"prompt": 5})
    with pytest.raises(ValueError, match="prompt"):
        await svc.create_workflow_def({"name": "badprompt", "spec_json": spec})


async def test_chat_node_non_string_model_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec_with_chat({"prompt": "hi", "model": 5})
    with pytest.raises(ValueError, match="model"):
        await svc.create_workflow_def({"name": "badmodel", "spec_json": spec})


async def test_chat_node_valid_config_passes(patched_svc):
    svc, _ = patched_svc
    spec = _spec_with_chat({"prompt": "hi", "model": "gpt-4"})
    result = await svc.create_workflow_def({"name": "goodchat", "spec_json": spec})
    assert "id" in result


# ─── edge condition field (WorkflowEdge.condition) ──────────────────────────


async def test_edge_condition_empty_string_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["edges"][0]["condition"] = "   "
    with pytest.raises(ValueError, match="condition"):
        await svc.create_workflow_def({"name": "badcond", "spec_json": spec})


async def test_edge_condition_non_string_raises(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["edges"][0]["condition"] = 42
    with pytest.raises(ValueError, match="condition"):
        await svc.create_workflow_def({"name": "badcond2", "spec_json": spec})


async def test_edge_condition_valid_string_passes(patched_svc):
    svc, _ = patched_svc
    spec = _spec()
    spec["edges"][0]["condition"] = "result == 'go'"
    result = await svc.create_workflow_def({"name": "goodcond", "spec_json": spec})
    assert "id" in result
