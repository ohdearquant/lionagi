# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the engine_defs Studio API."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


def _rid() -> str:
    return uuid.uuid4().hex[:12]


async def _seed_engine_def(
    db_path: Path,
    *,
    name: str | None = None,
    kind: str = "research",
    model: str | None = None,
    description: str | None = None,
    options: dict | None = None,
) -> str:
    import time

    from lionagi.state.db import StateDB

    def_id = _rid()
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_engine_def(
            {
                "id": def_id,
                "name": name or f"def-{def_id}",
                "kind": kind,
                "model": model,
                "options": options,
                "description": description,
                "created_at": now,
                "updated_at": now,
            }
        )
    return def_id


# ── Service layer ────────────────────────────────────────────────────────────


@pytest.fixture
def patched_svc(tmp_path: Path, monkeypatch):
    import lionagi.studio.services.engine_defs as svc
    from lionagi.state.db import DEFAULT_DB_PATH

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    # Also patch db module's DEFAULT_DB_PATH so StateDB() picks up the temp path.
    import lionagi.state.db as db_mod

    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    return svc, db_path


async def test_list_returns_empty_when_db_absent(patched_svc):
    svc, _ = patched_svc
    assert await svc.list_engine_defs() == []


async def test_create_and_list(patched_svc):
    svc, db_path = patched_svc
    result = await svc.create_engine_def({"name": "my-engine", "kind": "research"})
    assert "id" in result
    rows = await svc.list_engine_defs()
    assert any(r["name"] == "my-engine" for r in rows)


async def test_create_bad_kind_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="Invalid engine kind"):
        await svc.create_engine_def({"name": "bad", "kind": "nonexistent"})


async def test_create_bad_name_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError):
        await svc.create_engine_def({"name": "-badname", "kind": "research"})


async def test_create_flag_injection_in_model_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="starts with"):
        await svc.create_engine_def({"name": "ok", "kind": "coding", "model": "--inject"})


async def test_create_flag_injection_in_options_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="starts with"):
        await svc.create_engine_def(
            {
                "name": "ok2",
                "kind": "coding",
                "options": {"test_cmd": "--rm -rf /"},
            }
        )


async def test_create_bad_options_key_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="disallowed keys"):
        await svc.create_engine_def(
            {
                "name": "ok3",
                "kind": "research",
                "options": {"unknown_key": "value"},
            }
        )


async def test_create_name_conflict_raises(patched_svc):
    svc, _ = patched_svc
    await svc.create_engine_def({"name": "dup-name", "kind": "research"})
    with pytest.raises(Exception, match="already exists"):
        await svc.create_engine_def({"name": "dup-name", "kind": "review"})


async def test_get_returns_none_when_absent(patched_svc):
    svc, _ = patched_svc
    assert await svc.get_engine_def("nonexistent") is None


async def test_get_returns_row(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path, name="my-def", kind="coding", model="gpt-4o")
    row = await svc.get_engine_def(def_id)
    assert row is not None
    assert row["name"] == "my-def"
    assert row["kind"] == "coding"
    assert row["model"] == "gpt-4o"


async def test_update_returns_false_for_missing(patched_svc):
    svc, db_path = patched_svc
    # Ensure DB exists
    await _seed_engine_def(db_path)
    ok = await svc.update_engine_def("nonexistent", {"description": "x"})
    assert ok is False


async def test_update_changes_fields(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path, name="upd-def", kind="planning")
    ok = await svc.update_engine_def(def_id, {"description": "new desc"})
    assert ok is True
    row = await svc.get_engine_def(def_id)
    assert row["description"] == "new desc"


async def test_delete_returns_true(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path)
    ok = await svc.delete_engine_def(def_id)
    assert ok is True
    assert await svc.get_engine_def(def_id) is None


async def test_delete_returns_false_for_missing(patched_svc):
    svc, db_path = patched_svc
    await _seed_engine_def(db_path)
    ok = await svc.delete_engine_def("nonexistent")
    assert ok is False


async def test_max_depth_out_of_range_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="max_depth"):
        await svc.create_engine_def({"name": "x", "kind": "research", "max_depth": 0})


async def test_max_agents_out_of_range_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="max_agents"):
        await svc.create_engine_def({"name": "x", "kind": "research", "max_agents": 101})


# ── HTTP layer ───────────────────────────────────────────────────────────────


@pytest.fixture
def patched_app(tmp_path: Path, monkeypatch):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    httpx = pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.state.db as db_mod
    import lionagi.studio.services.engine_defs as ed_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
        headers={"Content-Type": "application/json"},
    )
    return app, db_path, client


async def test_list_endpoint_empty(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.get("/api/engine-defs/")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "my-engine", "kind": "research"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["name"] == "my-engine"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            "/api/engine-defs/",
            {"name": "stage-drift", "kind": "research", "stages": {"analyst": {}}},
        ),
        (
            "PUT",
            "/api/engine-defs/{def_id}",
            {"stages": {"analyst": {"model": "provider/model"}}},
        ),
    ],
)
async def test_engine_def_endpoints_reject_unknown_stage_overrides(
    patched_app, method, path, payload
):
    """Unsupported editor fields fail visibly instead of becoming successful no-ops."""
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="stage-drift-target")
    async with client as ac:
        resp = await ac.request(method, path.format(def_id=def_id), json=payload)
    assert resp.status_code == 422
    assert "stages" in resp.text


async def test_engine_def_supported_fields_round_trip_through_create_and_update(patched_app):
    """The closed write contract persists every admitted field without silent loss."""
    _, _, client = patched_app
    created = {
        "name": "round-trip-hypothesis",
        "kind": "hypothesis",
        "model": "provider/model-a",
        "max_depth": 3,
        "max_agents": 4,
        "options": {"export_dir": "artifacts/a"},
        "description": "first definition",
    }
    updated = {
        "name": "round-trip-coding",
        "kind": "coding",
        "model": "provider/model-b",
        "max_depth": 5,
        "max_agents": 6,
        "options": {"test_cmd": "pytest tests", "export_dir": "artifacts/b"},
        "description": "updated definition",
    }

    async with client as ac:
        create_resp = await ac.post("/api/engine-defs/", json=created)
        assert create_resp.status_code == 201, create_resp.text
        def_id = create_resp.json()["id"]

        first_get = await ac.get(f"/api/engine-defs/{def_id}")
        assert first_get.status_code == 200
        assert {key: first_get.json()[key] for key in created} == created

        update_resp = await ac.put(f"/api/engine-defs/{def_id}", json=updated)
        assert update_resp.status_code == 200, update_resp.text

        second_get = await ac.get(f"/api/engine-defs/{def_id}")
        assert second_get.status_code == 200
        assert {key: second_get.json()[key] for key in updated} == updated


async def test_create_endpoint_bad_kind_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "bad", "kind": "nonexistent"},
        )
    assert resp.status_code == 422


async def test_create_endpoint_bad_name_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "-inject", "kind": "research"},
        )
    assert resp.status_code == 422


async def test_create_endpoint_flag_injection_in_model_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "ok", "kind": "coding", "model": "--inject"},
        )
    assert resp.status_code == 422


async def test_create_endpoint_flag_injection_in_options_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "ok2", "kind": "coding", "options": {"test_cmd": "--rm -rf /"}},
        )
    assert resp.status_code == 422


async def test_create_endpoint_name_conflict_409(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        r1 = await ac.post("/api/engine-defs/", json={"name": "dup", "kind": "research"})
        assert r1.status_code == 201
        r2 = await ac.post("/api/engine-defs/", json={"name": "dup", "kind": "review"})
        assert r2.status_code == 409


async def test_get_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="get-me", kind="planning")
    async with client as ac:
        resp = await ac.get(f"/api/engine-defs/{def_id}")
    assert resp.status_code == 200
    assert resp.json()["kind"] == "planning"


async def test_get_endpoint_404(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.get("/api/engine-defs/nonexistent")
    assert resp.status_code == 404


async def test_update_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="upd-me", kind="hypothesis")
    async with client as ac:
        resp = await ac.put(
            f"/api/engine-defs/{def_id}",
            json={"description": "updated"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_update_endpoint_404(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.put("/api/engine-defs/nonexistent", json={"description": "x"})
    assert resp.status_code == 404


async def test_delete_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.delete(f"/api/engine-defs/{def_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_delete_endpoint_404(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.delete("/api/engine-defs/nonexistent")
    assert resp.status_code == 404


async def test_list_endpoint_filter_by_kind(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path, name="r1", kind="research")
    await _seed_engine_def(db_path, name="p1", kind="planning")
    async with client as ac:
        resp = await ac.get("/api/engine-defs/?kind=planning")
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r["kind"] == "planning" for r in rows)
    names = [r["name"] for r in rows]
    assert "p1" in names
    assert "r1" not in names


# ── coding kind requires options.test_cmd (engine CLI contract) ──────────────


async def test_create_coding_without_test_cmd_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="test_cmd"):
        await svc.create_engine_def({"name": "coder", "kind": "coding"})


async def test_create_coding_with_test_cmd_ok(patched_svc):
    svc, _ = patched_svc
    result = await svc.create_engine_def(
        {"name": "coder", "kind": "coding", "options": {"test_cmd": "uv run pytest"}}
    )
    assert "id" in result


async def test_update_kind_to_coding_without_test_cmd_raises(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path, name="was-research", kind="research")
    with pytest.raises(ValueError, match="test_cmd"):
        await svc.update_engine_def(def_id, {"kind": "coding"})


async def test_update_kind_to_coding_with_existing_test_cmd_ok(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(
        db_path, name="had-opts", kind="research", options={"test_cmd": "pytest"}
    )
    ok = await svc.update_engine_def(def_id, {"kind": "coding"})
    assert ok is True


async def test_update_options_dropping_test_cmd_on_coding_raises(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(
        db_path, name="coder2", kind="coding", options={"test_cmd": "pytest"}
    )
    with pytest.raises(ValueError, match="test_cmd"):
        await svc.update_engine_def(def_id, {"options": {"export_dir": "out"}})


async def test_create_coding_whitespace_test_cmd_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="test_cmd"):
        await svc.create_engine_def(
            {"name": "ws-coder", "kind": "coding", "options": {"test_cmd": "   "}}
        )


async def test_update_options_whitespace_test_cmd_on_coding_raises(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(
        db_path, name="ws-coder2", kind="coding", options={"test_cmd": "pytest"}
    )
    with pytest.raises(ValueError, match="test_cmd"):
        await svc.update_engine_def(def_id, {"options": {"test_cmd": "   "}})


async def test_update_empty_fields_existing_returns_truthy(patched_svc):
    """Empty fields dict on an existing def is a no-op, not a not-found."""
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path, name="noop-def", kind="research")
    result = await svc.update_engine_def(def_id, {})
    assert result, f"expected truthy (no-op success), got {result!r}"


async def test_update_empty_fields_missing_returns_false(patched_svc):
    """Empty fields dict on a missing def still signals not-found."""
    svc, db_path = patched_svc
    await _seed_engine_def(db_path)
    result = await svc.update_engine_def("nonexistent", {})
    assert result is False


async def test_update_endpoint_empty_body_existing_200(patched_app):
    """PUT {} on an existing def must return 200, not 404."""
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="empty-body-def", kind="review")
    async with client as ac:
        resp = await ac.put(f"/api/engine-defs/{def_id}", json={})
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"


async def test_update_endpoint_empty_body_missing_404(patched_app):
    """PUT {} on a missing def must still return 404."""
    _, db_path, client = patched_app
    await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.put("/api/engine-defs/nonexistent", json={})
    assert resp.status_code == 404


async def test_update_explicit_null_clears_model_override(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="clear-model", model="provider/model")
    async with client as ac:
        response = await ac.put(f"/api/engine-defs/{def_id}", json={"model": None})
        assert response.status_code == 200, response.text
        fresh = await ac.get(f"/api/engine-defs/{def_id}")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["model"] is None


async def test_create_refuses_option_unused_by_kind(patched_app):
    _, _, client = patched_app
    async with client as ac:
        response = await ac.post(
            "/api/engine-defs/",
            json={
                "name": "unsupported-option",
                "kind": "research",
                "options": {"export_dir": "OPTION_VALUE_SENTINEL"},
            },
        )
        assert response.status_code == 422, response.text
        assert "export_dir" in response.text
        assert "OPTION_VALUE_SENTINEL" not in response.text
        rows = await ac.get("/api/engine-defs/")
    assert rows.status_code == 200
    assert rows.json() == []


async def _raw_engine_row(db_path: Path, def_id: str) -> dict[str, Any]:
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        async with db._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM engine_defs WHERE id = :id"), {"id": def_id}
                    )
                )
                .mappings()
                .one()
            )
    return dict(row)


async def _replace_raw_options(db_path: Path, def_id: str, raw: str) -> None:
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE engine_defs SET options = :options WHERE id = :id"),
                {"id": def_id, "options": raw},
            )


@pytest.mark.parametrize("field", ["model", "max_depth", "max_agents", "description", "options"])
async def test_update_distinguishes_omitted_from_null_for_every_optional_field(patched_app, field):
    _, db_path, client = patched_app
    supplied = {
        "name": "clearable",
        "kind": "hypothesis",
        "model": "provider/model",
        "max_depth": 3,
        "max_agents": 4,
        "description": "keep unless submitted",
        "options": {"export_dir": "artifacts"},
    }
    async with client as ac:
        created = await ac.post("/api/engine-defs/", json=supplied)
        assert created.status_code == 201, created.text
        def_id = created.json()["id"]
        before = await _raw_engine_row(db_path, def_id)
        noop = await ac.put(f"/api/engine-defs/{def_id}", json={})
        assert noop.status_code == 200, noop.text
        assert await _raw_engine_row(db_path, def_id) == before
        response = await ac.put(f"/api/engine-defs/{def_id}", json={field: None})
        assert response.status_code == 200, response.text
        fresh = await ac.get(f"/api/engine-defs/{def_id}")
        assert fresh.status_code == 200, fresh.text
        row = fresh.json()
        assert row[field] == ({} if field == "options" else None)
        for other, value in supplied.items():
            if other != field:
                assert row[other] == value
        after = await _raw_engine_row(db_path, def_id)
        assert {k: v for k, v in after.items() if k not in (field, "updated_at")} == {
            k: v for k, v in before.items() if k not in (field, "updated_at")
        }


@pytest.mark.parametrize("patch", [{"name": None}, {"kind": None}, {"name": ""}, {"name": "   "}])
async def test_update_refuses_null_identity_or_blank_name_without_mutating_row(patched_app, patch):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="unchanged")
    before = await _raw_engine_row(db_path, def_id)
    async with client as ac:
        response = await ac.put(f"/api/engine-defs/{def_id}", json=patch)
    assert response.status_code == 422, response.text
    assert await _raw_engine_row(db_path, def_id) == before


async def test_update_trims_name_and_preserves_identity_and_conflict_atomicity(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="before-rename")
    await _seed_engine_def(db_path, name="already-taken")
    async with client as ac:
        renamed = await ac.put(f"/api/engine-defs/{def_id}", json={"name": "  after-rename  "})
        assert renamed.status_code == 200, renamed.text
        stored = await ac.get(f"/api/engine-defs/{def_id}")
        assert stored.status_code == 200
        assert stored.json()["name"] == "after-rename"
        assert stored.json()["id"] == def_id
        before_conflict = await _raw_engine_row(db_path, def_id)
        refused = await ac.put(
            f"/api/engine-defs/{def_id}",
            json={"name": "already-taken", "description": "must not be stored"},
        )
        assert refused.status_code == 409, refused.text
    assert await _raw_engine_row(db_path, def_id) == before_conflict


async def test_update_options_replace_the_whole_object(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(
        db_path,
        name="replace-options",
        kind="coding",
        options={"test_cmd": "pytest tests", "export_dir": "old-output"},
    )
    async with client as ac:
        response = await ac.put(
            f"/api/engine-defs/{def_id}", json={"options": {"test_cmd": "pytest unit"}}
        )
        assert response.status_code == 200, response.text
        row = await ac.get(f"/api/engine-defs/{def_id}")
    assert row.json()["options"] == {"test_cmd": "pytest unit"}


_OPTION_CASES = [
    ("coding", {"test_cmd": "pytest"}, None),
    ("coding", {"test_cmd": "pytest", "export_dir": "out"}, None),
    ("coding", {}, "test_cmd"),
    ("coding", {"export_dir": "out"}, "test_cmd"),
    ("hypothesis", {}, None),
    ("hypothesis", {"export_dir": "out"}, None),
    ("hypothesis", {"test_cmd": "OPTION_VALUE_SENTINEL"}, "test_cmd"),
    ("research", {"export_dir": "OPTION_VALUE_SENTINEL"}, "export_dir"),
    ("review", {"test_cmd": "OPTION_VALUE_SENTINEL"}, "test_cmd"),
    ("planning", {"export_dir": "OPTION_VALUE_SENTINEL"}, "export_dir"),
    ("research", {}, None),
    ("review", {}, None),
    ("planning", {}, None),
]


@pytest.mark.parametrize(("kind", "options", "bad_key"), _OPTION_CASES)
async def test_create_consumes_only_the_selected_kind_options(patched_app, kind, options, bad_key):
    _, _, client = patched_app
    async with client as ac:
        response = await ac.post(
            "/api/engine-defs/", json={"name": "option-contract", "kind": kind, "options": options}
        )
        assert response.status_code == (422 if bad_key else 201), response.text
        if bad_key:
            assert bad_key in response.text
            assert "OPTION_VALUE_SENTINEL" not in response.text
            rows = await ac.get("/api/engine-defs/")
            assert rows.json() == []
        else:
            stored = await ac.get(f"/api/engine-defs/{response.json()['id']}")
            assert stored.json()["options"] == options


@pytest.mark.parametrize(("kind", "options", "bad_key"), _OPTION_CASES)
async def test_update_consumes_only_the_selected_kind_options(patched_app, kind, options, bad_key):
    _, db_path, client = patched_app
    initial = {"test_cmd": "pytest"} if kind == "coding" else {}
    def_id = await _seed_engine_def(db_path, name="option-contract", kind=kind, options=initial)
    before = await _raw_engine_row(db_path, def_id)
    async with client as ac:
        response = await ac.put(f"/api/engine-defs/{def_id}", json={"options": options})
        assert response.status_code == (422 if bad_key else 200), response.text
        if bad_key:
            assert bad_key in response.text
            assert "OPTION_VALUE_SENTINEL" not in response.text
            assert await _raw_engine_row(db_path, def_id) == before
        else:
            stored = await ac.get(f"/api/engine-defs/{def_id}")
            assert stored.json()["options"] == options


@pytest.mark.parametrize("replacement", [{}, None])
async def test_kind_transition_checks_retained_options_and_accepts_same_request_clear(
    patched_app, replacement
):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(
        db_path, name="kind-transition", kind="coding", options={"test_cmd": "pytest"}
    )
    before = await _raw_engine_row(db_path, def_id)
    async with client as ac:
        refused = await ac.put(f"/api/engine-defs/{def_id}", json={"kind": "research"})
        assert refused.status_code == 422, refused.text
        assert "test_cmd" in refused.text
        assert await _raw_engine_row(db_path, def_id) == before
        accepted = await ac.put(
            f"/api/engine-defs/{def_id}", json={"kind": "research", "options": replacement}
        )
        assert accepted.status_code == 200, accepted.text
        stored = await ac.get(f"/api/engine-defs/{def_id}")
    assert stored.json()["kind"] == "research"
    assert stored.json()["options"] == {}


@pytest.mark.parametrize("include_equal_kind", [False, True])
@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("research", '{"test_cmd":"pytest","export_dir":"out"}'),
        ("coding", "{}"),
        ("coding", "null"),
        ("coding", "{"),
        ("coding", "[1]"),
        ("coding", "false"),
        ("coding", '["' + "x" * 65537 + '"]'),
        ("coding", "[" * 992 + "0" + "]" * 992),
    ],
    ids=[
        "unused",
        "missing-required",
        "null",
        "malformed",
        "array",
        "boolean",
        "oversized",
        "deep",
    ],
)
async def test_metadata_only_update_preserves_every_legacy_option_value(
    patched_app, kind, raw, include_equal_kind
):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="legacy-options", kind=kind)
    await _replace_raw_options(db_path, def_id, raw)
    before = await _raw_engine_row(db_path, def_id)
    patch = {"description": "metadata only"}
    if include_equal_kind:
        patch["kind"] = kind
    async with client as ac:
        response = await ac.put(f"/api/engine-defs/{def_id}", json=patch)
    assert response.status_code == 200, response.text
    after = await _raw_engine_row(db_path, def_id)
    assert after["options"] == before["options"] == raw
    assert after["description"] == "metadata only"
    assert {k: v for k, v in after.items() if k not in ("description", "updated_at")} == {
        k: v for k, v in before.items() if k not in ("description", "updated_at")
    }


async def test_explicit_options_replace_corrupt_legacy_value_without_decoding_it(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="replace-corrupt", kind="coding")
    await _replace_raw_options(db_path, def_id, "[" * 992 + "0" + "]" * 992)
    async with client as ac:
        response = await ac.put(
            f"/api/engine-defs/{def_id}", json={"options": {"test_cmd": "pytest"}}
        )
        assert response.status_code == 200, response.text
        stored = await ac.get(f"/api/engine-defs/{def_id}")
    assert stored.json()["options"] == {"test_cmd": "pytest"}


@pytest.mark.parametrize("kind", ["uninstalled-kind", b"\xff"])
async def test_unknown_or_unreadable_legacy_kind_is_read_only(patched_app, kind):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="unavailable", kind=kind)
    before = await _raw_engine_row(db_path, def_id)
    async with client as ac:
        for patch in ({"description": "not admitted"}, {"kind": "research", "options": {}}):
            response = await ac.put(f"/api/engine-defs/{def_id}", json=patch)
            assert response.status_code == 422, response.text
            assert await _raw_engine_row(db_path, def_id) == before
        noop = await ac.put(f"/api/engine-defs/{def_id}", json={})
    assert noop.status_code == 200, noop.text
    assert await _raw_engine_row(db_path, def_id) == before


async def test_create_and_kind_update_follow_registry_addition_and_removal(
    patched_app, monkeypatch
):
    import lionagi.cli._engine_definitions as registry

    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="registry-target", kind="review")
    original = dict(registry.KIND_META)
    try:
        with monkeypatch.context() as scoped:
            scoped.setitem(registry.KIND_META, "new-kind", dict(registry.KIND_META["research"]))
            async with client as ac:
                created = await ac.post(
                    "/api/engine-defs/", json={"name": "new-kind", "kind": "new-kind"}
                )
                assert created.status_code == 201, created.text
                updated = await ac.put(f"/api/engine-defs/{def_id}", json={"kind": "new-kind"})
                assert updated.status_code == 200, updated.text
                scoped.delitem(registry.KIND_META, "research")
                rejected_create = await ac.post(
                    "/api/engine-defs/", json={"name": "removed-kind", "kind": "research"}
                )
                assert rejected_create.status_code == 422, rejected_create.text
                before = await _raw_engine_row(db_path, def_id)
                rejected_update = await ac.put(
                    f"/api/engine-defs/{def_id}", json={"kind": "research"}
                )
            assert rejected_update.status_code == 422, rejected_update.text
            assert await _raw_engine_row(db_path, def_id) == before
    finally:
        # Restoring a deleted dict entry alone changes CLI choice order.
        registry.KIND_META.clear()
        registry.KIND_META.update(original)


async def test_write_options_follow_registry_option_addition_without_changing_legacy_wrapper(
    patched_app, monkeypatch
):
    import lionagi.cli._engine_definitions as registry
    from lionagi.studio.services.engine_defs import _validate_options

    meta = dict(registry.KIND_META["hypothesis"])
    meta["preset_options"] = {
        **meta["preset_options"],
        "new_option": {"type": "string", "required": False},
    }
    monkeypatch.setitem(registry.KIND_META, "hypothesis", meta)
    with pytest.raises(ValueError, match="disallowed keys"):
        _validate_options({"new_option": "value"})
    _, _, client = patched_app
    async with client as ac:
        created = await ac.post(
            "/api/engine-defs/",
            json={"name": "new-option", "kind": "hypothesis", "options": {"new_option": "first"}},
        )
        assert created.status_code == 201, created.text
        def_id = created.json()["id"]
        changed = await ac.put(
            f"/api/engine-defs/{def_id}", json={"options": {"new_option": "second"}}
        )
        assert changed.status_code == 200, changed.text
        stored = await ac.get(f"/api/engine-defs/{def_id}")
        assert stored.json()["options"] == {"new_option": "second"}
        before_invalid = await _raw_engine_row(patched_app[1], def_id)
        invalid = await ac.put(
            f"/api/engine-defs/{def_id}", json={"options": {"new_option": "--flag"}}
        )
    assert invalid.status_code == 422, invalid.text
    assert await _raw_engine_row(patched_app[1], def_id) == before_invalid


async def test_write_required_flags_follow_registry_without_changing_legacy_wrapper(
    patched_app, monkeypatch
):
    import lionagi.cli._engine_definitions as registry
    from lionagi.studio.services.engine_defs import _validate_kind_options

    meta = dict(registry.KIND_META["coding"])
    meta["preset_options"] = {
        **meta["preset_options"],
        "test_cmd": {"type": "string", "required": False},
    }
    monkeypatch.setitem(registry.KIND_META, "coding", meta)
    with pytest.raises(ValueError, match="test_cmd"):
        _validate_kind_options("coding", {})
    _, _, client = patched_app
    async with client as ac:
        created = await ac.post(
            "/api/engine-defs/", json={"name": "optional-command", "kind": "coding", "options": {}}
        )
        assert created.status_code == 201, created.text
        def_id = created.json()["id"]
        changed = await ac.put(f"/api/engine-defs/{def_id}", json={"options": {}})
    assert changed.status_code == 200, changed.text


@pytest.mark.parametrize(("supplied", "expected"), [(True, 1), ("3", 3), (1.0, 1)])
@pytest.mark.parametrize("field", ["max_depth", "max_agents"])
async def test_http_budget_coercion_remains_compatible_on_create_and_update(
    patched_app, field, supplied, expected
):
    _, _, client = patched_app
    async with client as ac:
        created = await ac.post(
            "/api/engine-defs/",
            json={"name": "budget-coercion", "kind": "research", field: supplied},
        )
        assert created.status_code == 201, created.text
        def_id = created.json()["id"]
        fresh = await ac.get(f"/api/engine-defs/{def_id}")
        assert fresh.json()[field] == expected
        changed = await ac.put(f"/api/engine-defs/{def_id}", json={field: supplied})
        assert changed.status_code == 200, changed.text
        fresh = await ac.get(f"/api/engine-defs/{def_id}")
    assert fresh.json()[field] == expected


def test_legacy_option_validation_does_not_repr_an_accepted_string():
    from lionagi.studio.services.engine_defs import _validate_options

    class AcceptedString(str):
        def __repr__(self):
            raise AssertionError("an accepted option does not need a representation")

    _validate_options({"test_cmd": AcceptedString("pytest tests")})


async def test_empty_update_preserves_legacy_numeric_options_without_decoding(
    patched_app, monkeypatch
):
    from types import SimpleNamespace

    from lionagi.studio.services import engine_defs

    def unused_decoder(*args, **kwargs):
        raise AssertionError("a read-only no-op must not decode stored options")

    # Instrument only the service boundary, leaving the HTTP JSON parser intact.
    # This pins absence of decoding independently of interpreter digit limits.
    monkeypatch.setattr(
        engine_defs,
        "json",
        SimpleNamespace(loads=unused_decoder, JSONDecodeError=ValueError),
        raising=False,
    )
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="numeric-options", kind="coding")
    await _replace_raw_options(db_path, def_id, '{"large":' + "9" * 5000 + "}")
    before = await _raw_engine_row(db_path, def_id)
    async with client as ac:
        response = await ac.put(f"/api/engine-defs/{def_id}", json={})
    assert response.status_code == 200, response.text
    assert await _raw_engine_row(db_path, def_id) == before
