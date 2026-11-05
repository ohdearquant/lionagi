# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""HTTP coverage for the read-only engine blueprint and preset catalog."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


@pytest.fixture
def catalog_app(tmp_path, monkeypatch):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    httpx = pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.state.db as db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    from lionagi.studio.app import app

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
    )
    return db_path, client


async def _initialize(db_path: Path) -> None:
    from lionagi.state.db import StateDB

    async with StateDB(db_path):
        pass


def _insert(db_path: Path, rows: list[tuple[str | None, str, str | bytes, object]]) -> None:
    """Seed legacy raw values without the validated HTTP write path."""
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO engine_defs (id,name,kind,options,created_at,updated_at) "
            "VALUES (?,?,?,?,1,1)",
            rows,
        )


def _encode(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _encode_bytes(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


async def test_blueprints_describe_registered_runtime_inputs_and_options(catalog_app):
    from lionagi.cli._engine_definitions import KIND_META

    _, client = catalog_app
    async with client as ac:
        response = await ac.get("/api/engine-catalog/blueprints")
        repeated = await ac.get("/api/engine-catalog/blueprints")
    assert response.status_code == 200, response.text
    assert repeated.status_code == 200
    assert repeated.json() == response.json()
    body = response.json()
    assert body["schema_version"] == 1
    items = body["items"]
    assert len(items) == len(KIND_META)
    assert {item["kind"] for item in items} == set(KIND_META)
    for item in items:
        meta = KIND_META[item["kind"]]
        assert item["input"] == {"name": meta["pos_arg"], "help": meta["pos_help"]}
        assert "topology" not in item
        assert set(item["common_fields"]) == {"model", "max_depth", "max_agents"}
        assert all({"name", "type", "required"} <= set(option) for option in item["options"])
    coding = next(item for item in items if item["kind"] == "coding")
    assert {o["name"]: (o["type"], o["required"]) for o in coding["options"]} == {
        "test_cmd": ("string", True),
        "export_dir": ("string", False),
    }


async def test_preset_limits_share_validator_blueprint_and_route_description(
    catalog_app, monkeypatch
):
    from lionagi.studio.services import engine_defs

    low, high = engine_defs._PRESET_BUDGET_BOUNDS
    _, client = catalog_app
    async with client as ac:
        schema = await ac.get("/openapi.json")
    assert schema.status_code == 200
    description = schema.json()["paths"]["/api/engine-catalog/presets"]["get"]["description"]
    assert description == engine_defs._CATALOG_PRESETS_DESCRIPTION
    assert f"{low} to {high}" in description
    assert f"{engine_defs._CATALOG_OPTIONS_MAX_DEPTH} JSON nesting" in description
    for key in (
        "stored_option_keys",
        "unused_option_keys",
        "missing_option_keys",
        "invalid_option_keys",
    ):
        assert key in description
    assert "null" in description and "[]" in description
    assert "id_exceeds_cursor_bound" in description
    for item in engine_defs.engine_catalog_blueprints()["items"]:
        for field in ("max_depth", "max_agents"):
            assert item["common_fields"][field]["minimum"] == low
            assert item["common_fields"][field]["maximum"] == high
    monkeypatch.setattr(engine_defs, "_PRESET_BUDGET_BOUNDS", (2, 3))
    engine_defs._validate_budget("max_depth", 2)
    with pytest.raises(ValueError):
        engine_defs._validate_budget("max_depth", 1)
    assert (
        engine_defs.engine_catalog_blueprints()["items"][0]["common_fields"]["max_depth"]["minimum"]
        == 2
    )


def test_required_option_detection_uses_registry_metadata(monkeypatch):
    from lionagi.studio.services import engine_defs

    meta = dict(engine_defs.KIND_META["research"])
    meta["preset_options"] = {
        "future_input": {"type": "string", "required": True},
        "another_input": {"type": "string", "required": True},
        "test_cmd": {"type": "string", "required": False},
    }
    monkeypatch.setitem(engine_defs.KIND_META, "research", meta)
    monkeypatch.setattr(
        engine_defs,
        "_ALLOWED_OPTIONS_KEYS",
        engine_defs._ALLOWED_OPTIONS_KEYS | {"future_input", "another_input"},
    )
    empty = engine_defs._catalog_annotate_row({"kind": "research", "options": "{}"})
    assert empty["reasons"] == ["required_option_missing"]
    assert empty["missing_option_keys"] == ["another_input", "future_input"]
    assert engine_defs._catalog_annotate_row(
        {"kind": "research", "options": '{"test_cmd":"pytest"}'}
    )["reasons"] == ["required_option_missing"]
    assert (
        engine_defs._catalog_annotate_row(
            {"kind": "research", "options": '{"future_input":"ready","another_input":"yes"}'}
        )["reasons"]
        == []
    )


async def test_http_id_roundtrip_empty_punctuation_unicode_and_maximum(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    ids = ["", "a/b%?#雪", "é" * 2048]
    _insert(db_path, [(value, f"preset-{i}", "research", "{}") for i, value in enumerate(ids)])
    async with client as ac:
        for value in ids:
            response = await ac.get("/api/engine-catalog/preset", params={"id": value})
            assert response.status_code == 200, response.text
            assert response.json()["id"] == value
        missing_parameter = await ac.get("/api/engine-catalog/preset")
        unknown_id = await ac.get("/api/engine-catalog/preset", params={"id": "missing"})
    assert missing_parameter.status_code == 422
    assert unknown_id.status_code == 404


@pytest.mark.parametrize("endpoint", ["list", "detail", "oversized"])
async def test_catalog_annotations_run_in_one_worker_call(catalog_app, monkeypatch, endpoint):
    from lionagi.studio.services import engine_defs

    db_path, client = catalog_app
    await _initialize(db_path)
    if endpoint == "oversized":
        ids = ["a" * 4097, "b" * 4097, "z"]
    else:
        ids = ["a", "b"]
    _insert(db_path, [(value, f"preset-{i}", "research", "{}") for i, value in enumerate(ids)])
    loop_thread = threading.get_ident()
    annotation_threads = []
    annotation_ids = []
    worker_calls = []
    original_annotate = engine_defs._catalog_annotate_row
    original_to_thread = asyncio.to_thread

    def record_annotation(row):
        annotation_threads.append(threading.get_ident())
        annotation_ids.append(row["id"])
        return original_annotate(row)

    async def record_worker_call(func, *args, **kwargs):
        before = len(annotation_threads)
        try:
            return await original_to_thread(func, *args, **kwargs)
        finally:
            if len(annotation_threads) > before:
                worker_calls.append(func)

    monkeypatch.setattr(engine_defs, "_catalog_annotate_row", record_annotation)
    monkeypatch.setattr(asyncio, "to_thread", record_worker_call)
    async with client as ac:
        if endpoint == "list":
            response = await ac.get("/api/engine-catalog/presets")
            expected_ids = ["a", "b"]
            assert [row["id"] for row in response.json()["items"]] == expected_ids
        elif endpoint == "oversized":
            response = await ac.get("/api/engine-catalog/presets", params={"limit": 2})
            expected_ids = ids[:2]
            body = response.json()
            assert [row["id"] for row in body["items"]] == expected_ids
            assert body["next_cursor"] is None
            assert body["stop_reason"] == "id_exceeds_cursor_bound"
        else:
            response = await ac.get("/api/engine-catalog/preset", params={"id": "a"})
            expected_ids = ["a"]
            assert response.json()["id"] == "a"
    assert response.status_code == 200
    assert annotation_ids == expected_ids
    assert annotation_threads and all(thread != loop_thread for thread in annotation_threads)
    assert len(worker_calls) == 1


async def test_list_pages_more_than_200_and_binds_filter(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    ids = [f"item-{i:03}" for i in range(231)]
    _insert(
        db_path,
        [
            (value, f"name-{i:03}", "research" if i % 2 else "review", "{}")
            for i, value in enumerate(ids)
        ],
    )
    seen = []
    cursor = None
    async with client as ac:
        for _ in range(4):
            params = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            response = await ac.get("/api/engine-catalog/presets", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            seen.extend(row["id"] for row in page["items"])
            next_cursor = page["next_cursor"]
            assert next_cursor != cursor or next_cursor is None
            cursor = next_cursor
            if cursor is None:
                break
        assert cursor is None
        assert seen == ids
        first = await ac.get("/api/engine-catalog/presets", params={"kind": "research", "limit": 3})
        assert first.status_code == 200
        assert all(row["kind"] == "research" for row in first.json()["items"])
        token = first.json()["next_cursor"]
        assert token
        wrong_filter = await ac.get(
            "/api/engine-catalog/presets", params={"kind": "review", "cursor": token}
        )
        assert wrong_filter.status_code == 422
        assert token not in wrong_filter.text


async def test_empty_id_is_a_cursor_position(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    _insert(db_path, [("", "empty", "research", "{}"), ("a", "next", "research", "{}")])
    async with client as ac:
        first = await ac.get("/api/engine-catalog/presets", params={"limit": 1})
        assert first.status_code == 200
        assert [r["id"] for r in first.json()["items"]] == [""]
        token = first.json()["next_cursor"]
        assert token
        second = await ac.get("/api/engine-catalog/presets", params={"limit": 1, "cursor": token})
        assert second.status_code == 200
        assert [r["id"] for r in second.json()["items"]] == ["a"]
        assert second.json()["next_cursor"] is None
        explicit = await ac.get(
            "/api/engine-catalog/presets",
            params={"cursor": _encode({"v": 1, "after": "", "kind": None})},
        )
        assert explicit.status_code == 200
        assert [r["id"] for r in explicit.json()["items"]] == ["a"]


@pytest.mark.parametrize(
    "token",
    [
        "!not-base64!",
        _encode_bytes(b"\xff"),
        _encode_bytes(b"{"),
        _encode_bytes(b'{"v":1,"v":1,"after":"a","kind":null}'),
        _encode({"v": True, "after": "a", "kind": None}),
        _encode({"v": 2, "after": "a", "kind": None}),
        _encode({"v": 1, "after": 5, "kind": None}),
        _encode({"v": 1, "after": "a"}),
        _encode({"v": 1, "after": "a", "kind": None, "extra": 1}),
        _encode({"v": 1, "after": "a", "kind": "unknown"}),
        _encode({"v": 1, "after": "x" * 4097, "kind": None}),
        _encode({"v": 1, "after": "a", "kind": None}) + "=",
    ],
)
async def test_invalid_cursor_is_generic_422_even_when_store_absent(catalog_app, token):
    _, client = catalog_app
    async with client as ac:
        response = await ac.get("/api/engine-catalog/presets", params={"cursor": token})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid engine catalog cursor"}
    assert token not in response.text


@pytest.mark.parametrize("oversized_position", [0, 1, 2], ids=["first", "middle", "last"])
async def test_page_alignment_does_not_hide_rows_after_oversized_id(
    catalog_app, oversized_position
):
    db_path, client = catalog_app
    await _initialize(db_path)
    ids = ["a", "b", "c", "d", "e"]
    ids[oversized_position] *= 4097
    _insert(db_path, [(value, f"oversize-{i:03}", "research", "{}") for i, value in enumerate(ids)])
    seen = []
    cursor = None
    seen_cursors = set()
    async with client as ac:
        for _ in range(8):
            params = {"limit": 3}
            if cursor is not None:
                assert cursor not in seen_cursors
                seen_cursors.add(cursor)
                params["cursor"] = cursor
            response = await ac.get("/api/engine-catalog/presets", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            seen.extend(row["id"] for row in page["items"])
            assert "stop_reason" not in page
            cursor = page["next_cursor"]
            if cursor is None:
                break
        else:
            pytest.fail("pagination never terminated")
    assert seen == ids


@pytest.mark.parametrize("has_later_row", [False, True], ids=["at-end", "more-rows"])
async def test_entire_oversized_page_reports_stop_only_when_more_rows_exist(
    catalog_app, has_later_row
):
    db_path, client = catalog_app
    await _initialize(db_path)
    oversized = [character * 4097 for character in "abc"]
    ids = oversized + (["z"] if has_later_row else [])
    _insert(db_path, [(value, f"preset-{i}", "research", "{}") for i, value in enumerate(ids)])
    async with client as ac:
        response = await ac.get("/api/engine-catalog/presets", params={"limit": 3})
    assert response.status_code == 200, response.text
    page = response.json()
    assert [row["id"] for row in page["items"]] == oversized
    assert page["next_cursor"] is None
    if has_later_row:
        assert page["stop_reason"] == "id_exceeds_cursor_bound"
    else:
        assert "stop_reason" not in page


async def test_single_trailing_oversized_id_has_no_stop_reason(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    oversized = "z" * 4097
    _insert(db_path, [("a", "first", "research", "{}"), (oversized, "last", "research", "{}")])
    async with client as ac:
        first = await ac.get("/api/engine-catalog/presets", params={"limit": 1})
        assert first.status_code == 200, first.text
        assert [row["id"] for row in first.json()["items"]] == ["a"]
        last = await ac.get(
            "/api/engine-catalog/presets",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )
    assert last.status_code == 200, last.text
    assert [row["id"] for row in last.json()["items"]] == [oversized]
    assert last.json()["next_cursor"] is None
    assert "stop_reason" not in last.json()


@pytest.mark.parametrize("endpoint", ["detail", "cursor"])
async def test_nul_id_and_cursor_are_rejected_before_opening_store(
    catalog_app, monkeypatch, endpoint
):
    import lionagi.state.db as db_mod
    from lionagi.studio.services import engine_defs

    db_path, client = catalog_app
    await _initialize(db_path)
    store = Mock(side_effect=AssertionError("store must not open"))
    lookup = AsyncMock(side_effect=AssertionError("store lookup must not run"))
    listing = AsyncMock(side_effect=AssertionError("store query must not run"))
    monkeypatch.setattr(engine_defs, "StateDB", store)
    monkeypatch.setattr(db_mod, "_get_engine_catalog_row", lookup)
    monkeypatch.setattr(db_mod, "_list_engine_catalog_rows", listing)
    token = _encode({"v": 1, "after": "bad\u0000id", "kind": None})
    async with client as ac:
        if endpoint == "detail":
            response = await ac.get("/api/engine-catalog/preset", params={"id": "bad\u0000id"})
            assert response.status_code == 404
        else:
            response = await ac.get("/api/engine-catalog/presets", params={"cursor": token})
            assert response.status_code == 422
            assert response.json() == {"detail": "Invalid engine catalog cursor"}
            assert token not in response.text
    store.assert_not_called()
    lookup.assert_not_awaited()
    listing.assert_not_awaited()


async def test_required_registry_options_control_availability_in_list_and_detail(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    _insert(
        db_path,
        [
            ("a", "empty", "coding", "{}"),
            ("b", "null", "coding", None),
            ("c", "valid", "coding", '{"test_cmd":"pytest"}'),
            ("d", "malformed", "coding", "{"),
        ],
    )
    async with client as ac:
        response = await ac.get("/api/engine-catalog/presets")
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()["items"]}
        for row_id, expected in [
            ("a", ["required_option_missing"]),
            ("b", ["required_option_missing"]),
            ("c", []),
            ("d", ["options_malformed"]),
        ]:
            assert rows[row_id]["reasons"] == expected
            assert rows[row_id]["availability"] == ("unavailable" if expected else "available")
            assert rows[row_id]["missing_option_keys"] == (
                ["test_cmd"] if row_id in ("a", "b") else None if row_id == "d" else []
            )
            assert rows[row_id]["invalid_option_keys"] == (None if row_id == "d" else [])
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200
            assert detail.json() == rows[row_id]


async def test_invalid_coding_option_values_are_unavailable_and_not_echoed(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    sentinel = "-x-unique-secret"
    combined_sentinel = "-x-combined-sentinel"
    _insert(
        db_path,
        [
            ("a", "boolean", "coding", '{"test_cmd":true}'),
            ("b", "unsafe", "coding", '{"test_cmd":"' + sentinel + '"}'),
            ("c", "valid", "coding", '{"test_cmd":"pytest"}'),
            ("d", "missing-and-invalid", "coding", '{"export_dir":"' + combined_sentinel + '"}'),
            ("e", "invalid-keys", "research", '{"z_option":"value","a_option":"value"}'),
        ],
    )
    async with client as ac:
        response = await ac.get("/api/engine-catalog/presets")
        assert response.status_code == 200, response.text
        assert sentinel not in response.text
        assert combined_sentinel not in response.text
        rows = {row["id"]: row for row in response.json()["items"]}
        for row_id in ("a", "b"):
            assert rows[row_id]["availability"] == "unavailable"
            assert rows[row_id]["reasons"] == ["options_invalid"]
            assert rows[row_id]["options"] is None
            assert rows[row_id]["invalid_option_keys"] == ["test_cmd"]
        assert rows["c"]["availability"] == "available"
        assert rows["c"]["reasons"] == []
        assert rows["c"]["options"] == {"test_cmd": "pytest"}
        assert rows["c"]["missing_option_keys"] == []
        assert rows["c"]["invalid_option_keys"] == []
        assert rows["a"]["missing_option_keys"] == []
        assert rows["b"]["missing_option_keys"] == []
        assert rows["d"]["availability"] == "unavailable"
        assert rows["d"]["reasons"] == ["required_option_missing", "options_invalid"]
        assert rows["d"]["options"] is None
        assert rows["d"]["missing_option_keys"] == ["test_cmd"]
        assert rows["d"]["invalid_option_keys"] == ["export_dir"]
        assert rows["e"]["missing_option_keys"] == []
        assert rows["e"]["invalid_option_keys"] == ["a_option", "z_option"]
        for row_id in ("a", "b", "c", "d", "e"):
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200, detail.text
            assert sentinel not in detail.text
            assert combined_sentinel not in detail.text
            assert detail.json() == rows[row_id]


async def test_legacy_option_shapes_are_isolated_and_unknown_kinds_remain_visible(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    secret = "never-echo-malformed-content"
    deep = "[" * 1100 + "0" + "]" * 1100
    _insert(
        db_path,
        [
            ("a", "valid", "research", '{"test_cmd":"ignored","export_dir":"ignored"}'),
            ("b", "json-null", "research", "null"),
            ("c", "sql-null", "research", None),
            ("d", "malformed", "research", '{"secret":"' + secret),
            ("e", "array", "research", '["not-a-map"]'),
            ("f", "bytes", "research", b"\xff\xfe"),
            ("g", "deep", "research", deep),
            ("h", "unknown", "future-kind", '{"old":"value"}'),
            ("i", "unknown-and-broken", "future-kind", '{"old":'),
        ],
    )
    async with client as ac:
        response = await ac.get("/api/engine-catalog/presets")
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["items"]}
        assert set(rows) == set("abcdefghi")
        assert rows["a"]["availability"] == "available"
        assert rows["a"]["reasons"] == []
        assert rows["a"]["stored_option_keys"] == ["export_dir", "test_cmd"]
        assert rows["a"]["unused_option_keys"] == ["export_dir", "test_cmd"]
        for row_id in ("b", "c"):
            assert rows[row_id]["availability"] == "available"
            assert rows[row_id]["options"] == {}
            assert rows[row_id]["stored_option_keys"] == []
        for row_id, reason in (
            ("d", "options_malformed"),
            ("e", "options_not_object"),
            ("f", "options_undecodable"),
        ):
            assert rows[row_id]["availability"] == "unavailable"
            assert rows[row_id]["reasons"] == [reason]
            assert rows[row_id]["options"] is None
            assert rows[row_id]["stored_option_keys"] is None
            assert rows[row_id]["unused_option_keys"] is None
        assert rows["g"]["availability"] == "unavailable"
        assert rows["g"]["options"] is None
        assert rows["g"]["reasons"] == ["options_undecodable"]
        assert rows["h"]["availability"] == "unavailable"
        assert rows["h"]["reasons"] == ["kind_not_registered"]
        assert rows["h"]["options"] == {"old": "value"}
        assert rows["h"]["missing_option_keys"] is None
        assert rows["h"]["invalid_option_keys"] is None
        assert rows["h"]["stored_option_keys"] == ["old"]
        assert rows["h"]["unused_option_keys"] is None
        assert rows["i"]["availability"] == "unavailable"
        assert rows["i"]["reasons"] == ["kind_not_registered", "options_malformed"]
        assert rows["i"]["options"] is None
        assert rows["i"]["stored_option_keys"] is None
        assert rows["i"]["unused_option_keys"] is None
        for row_id in "defgi":
            assert rows[row_id]["missing_option_keys"] is None
            assert rows[row_id]["invalid_option_keys"] is None
        for row_id in "abc":
            assert rows[row_id]["missing_option_keys"] == []
            assert rows[row_id]["invalid_option_keys"] == []
        assert secret not in response.text
        for row_id in "defghi":
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200
            assert detail.json() == rows[row_id]
            assert secret not in detail.text


async def test_existing_http_create_with_omitted_options_is_available(catalog_app):
    _, client = catalog_app
    async with client as ac:
        created = await ac.post(
            "/api/engine-defs/", json={"name": "omitted-options", "kind": "research"}
        )
        assert created.status_code == 201, created.text
        def_id = created.json()["id"]
        response = await ac.get("/api/engine-catalog/preset", params={"id": def_id})
        listing = await ac.get("/api/engine-catalog/presets")
    assert response.status_code == 200, response.text
    assert listing.status_code == 200, listing.text
    row = response.json()
    assert row["availability"] == "available"
    assert row["reasons"] == []
    assert row["options"] == {}
    assert next(item for item in listing.json()["items"] if item["id"] == def_id) == row


async def test_catalog_excludes_legacy_null_ids_but_keeps_empty_id(catalog_app):
    """Legacy SQLite permits NULL primary keys, which the catalog excludes."""
    db_path, client = catalog_app
    schema = Path(__file__).resolve().parents[2] / "lionagi/state/schema.sql"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema.read_text())
    _insert(
        db_path,
        [
            (None, "legacy-null-one", "research", "{}"),
            (None, "legacy-null-two", "research", "{}"),
            ("", "supported-empty-id", "research", "{}"),
        ],
    )
    async with client as ac:
        page = await ac.get("/api/engine-catalog/presets")
        detail = await ac.get("/api/engine-catalog/preset", params={"id": ""})
    assert page.status_code == 200, page.text
    assert [row["id"] for row in page.json()["items"]] == [""]
    assert detail.status_code == 200
    assert detail.json()["id"] == ""


async def test_legacy_blob_kind_is_redacted_and_does_not_break_valid_neighbor(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    raw_kind = b"never-echo-kind-\xff"
    _insert(
        db_path,
        [("a", "valid", "research", "{}"), ("b", "blob-kind", raw_kind, "{}")],
    )
    async with client as ac:
        listing = await ac.get("/api/engine-catalog/presets")
        assert listing.status_code == 200, listing.text
        rows = {row["id"]: row for row in listing.json()["items"]}
        assert rows["a"]["availability"] == "available"
        assert rows["a"]["reasons"] == []
        assert rows["b"]["availability"] == "unavailable"
        assert rows["b"]["reasons"] == ["kind_not_text"]
        assert rows["b"]["kind"] is None
        assert rows["b"]["missing_option_keys"] is None
        assert rows["b"]["invalid_option_keys"] is None
        assert "never-echo-kind" not in listing.text
        detail = await ac.get("/api/engine-catalog/preset", params={"id": "b"})
        assert detail.status_code == 200, detail.text
        assert detail.json() == rows["b"]
        assert "never-echo-kind" not in detail.text
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT kind, typeof(kind) FROM engine_defs WHERE id = ?", ("b",)
        ).fetchone() == (
            raw_kind,
            "blob",
        )


async def test_cursor_id_bound_counts_decoded_utf8_bytes_not_encoded_length(catalog_app):
    _, client = catalog_app
    within_bound = _encode({"v": 1, "after": "é" * 2048, "kind": None})
    over_bound = _encode({"v": 1, "after": "é" * 2049, "kind": None})
    assert len(within_bound) > 4096
    async with client as ac:
        good = await ac.get("/api/engine-catalog/presets", params={"cursor": within_bound})
        bad = await ac.get("/api/engine-catalog/presets", params={"cursor": over_bound})
    assert good.status_code == 200, good.text
    assert bad.status_code == 422
    assert over_bound not in bad.text


async def test_unknown_kind_filter_is_refused(catalog_app):
    _, client = catalog_app
    async with client as ac:
        response = await ac.get("/api/engine-catalog/presets", params={"kind": "future-kind"})
    assert response.status_code == 422


async def test_noncanonical_base64_and_bad_payloads_use_same_generic_error(catalog_app):
    """Unused base64 pad bits must not create a second encoding of the same cursor."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    after = "id"
    valid = _encode({"v": 1, "after": after, "kind": None})
    while len(valid) % 4 == 0:
        after += "x"
        valid = _encode({"v": 1, "after": after, "kind": None})
    last = alphabet.index(valid[-1])
    noncanonical = valid[:-1] + alphabet[last ^ 1]
    pad = "=" * (-len(valid) % 4)
    assert base64.urlsafe_b64decode(valid + pad) == base64.urlsafe_b64decode(noncanonical + pad)
    _, client = catalog_app
    async with client as ac:
        responses = [
            await ac.get("/api/engine-catalog/presets", params={"cursor": token})
            for token in (
                noncanonical,
                "not-a-cursor",
                _encode({"v": True, "after": "a", "kind": None}),
            )
        ]
    assert all(response.status_code == 422 for response in responses)
    assert responses[0].json() == responses[1].json() == responses[2].json()


async def test_legacy_unserializable_options_do_not_break_neighbor_or_detail(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    deep = '{"legacy":' + "[" * 992 + "0" + "]" * 992 + "}"
    _insert(
        db_path,
        [
            ("a", "healthy", "research", "{}"),
            ("b", "nan", "research", '{"legacy":NaN}'),
            ("c", "overflow", "research", '{"legacy":1e1000}'),
            ("d", "deep-wrapper", "research", deep),
            ("e", "infinity", "research", '{"legacy":Infinity}'),
            ("f", "surrogate", "research", r'{"legacy":"\ud800"}'),
        ],
    )
    async with client as ac:
        listing = await ac.get("/api/engine-catalog/presets")
        assert listing.status_code == 200, listing.text
        rows = {row["id"]: row for row in listing.json()["items"]}
        assert rows["a"]["availability"] == "available"
        for row_id, reason in (
            ("b", "options_malformed"),
            ("c", "options_undecodable"),
            ("d", "options_undecodable"),
            ("e", "options_malformed"),
            ("f", "options_undecodable"),
        ):
            assert rows[row_id]["availability"] == "unavailable"
            assert rows[row_id]["reasons"] == [reason]
            assert rows[row_id]["options"] is None
            assert rows[row_id]["stored_option_keys"] is None
            assert rows[row_id]["unused_option_keys"] is None
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200, detail.text
            assert detail.json() == rows[row_id]


async def test_options_depth_boundary_and_quoted_brackets(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)

    def nested(depth):
        return '{"legacy":' + "[" * (depth - 1) + "0" + "]" * (depth - 1) + "}"

    # The object counts as depth 1. Brackets and escaped quotes inside strings do not count.
    quoted = json.dumps({"legacy": ('[[[\\"' * 100) + "]]]"})
    _insert(
        db_path,
        [
            ("a", "depth-32", "future-kind", nested(32)),
            ("b", "depth-33", "future-kind", nested(33)),
            ("c", "quoted-brackets", "future-kind", quoted),
            ("d", "valid", "research", "{}"),
        ],
    )
    async with client as ac:
        response = await ac.get("/api/engine-catalog/presets")
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["items"]}
        assert rows["a"]["reasons"] == ["kind_not_registered"]
        assert rows["a"]["options"] == json.loads(nested(32))
        assert rows["b"]["availability"] == "unavailable"
        assert rows["b"]["reasons"] == ["kind_not_registered", "options_undecodable"]
        assert rows["b"]["options"] is None
        assert rows["c"]["reasons"] == ["kind_not_registered"]
        assert rows["c"]["options"] == json.loads(quoted)
        assert rows["d"]["availability"] == "available"
        for row_id in "abcd":
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200, detail.text
            assert detail.json() == rows[row_id]


@pytest.mark.parametrize("field", ["max_depth", "max_agents"])
async def test_stored_budget_bounds_affect_list_and_detail(catalog_app, monkeypatch, field):
    import lionagi.state.db as db_mod

    db_path, client = catalog_app
    await _initialize(db_path)
    values = [
        ("zero", 0),
        ("high", 101),
        ("boolean", 1),
        ("fraction", 1.5),
        ("text", "not-int"),
        ("low", 1),
        ("top", 100),
        ("null", None),
    ]
    _insert(db_path, [(name, name, "research", "{}") for name, _ in values])
    updates = {
        "max_depth": "UPDATE engine_defs SET max_depth=? WHERE id=?",
        "max_agents": "UPDATE engine_defs SET max_agents=? WHERE id=?",
    }
    with sqlite3.connect(db_path) as conn:
        conn.executemany(updates[field], [(value, name) for name, value in values])

    # SQLite INTEGER affinity turns bool into 0/1; emulate a bool-bearing driver row
    # while still exercising the real HTTP routes and the real read helpers.
    list_rows = db_mod._list_engine_catalog_rows
    get_row = db_mod._get_engine_catalog_row

    async def with_boolean_on_list(*args, **kwargs):
        rows = await list_rows(*args, **kwargs)
        for row in rows:
            if row["id"] == "boolean":
                row[field] = True
        return rows

    async def with_boolean_on_detail(*args, **kwargs):
        row = await get_row(*args, **kwargs)
        if row is not None and row["id"] == "boolean":
            row[field] = True
        return row

    monkeypatch.setattr(db_mod, "_list_engine_catalog_rows", with_boolean_on_list)
    monkeypatch.setattr(db_mod, "_get_engine_catalog_row", with_boolean_on_detail)
    async with client as ac:
        listing = await ac.get("/api/engine-catalog/presets")
        assert listing.status_code == 200
        rows = {row["id"]: row for row in listing.json()["items"]}
        assert set(rows) == {name for name, _ in values}
        for row_id, _ in values:
            expected = (
                ["budget_invalid"]
                if row_id in {"zero", "high", "boolean", "fraction", "text"}
                else []
            )
            assert rows[row_id]["reasons"] == expected
            assert rows[row_id]["availability"] == ("unavailable" if expected else "available")
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200
            assert detail.json() == rows[row_id]


async def test_budget_reason_follows_kind_and_option_reasons(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    _insert(
        db_path,
        [("a", "unknown", "future-kind", "{"), ("b", "missing", "coding", "{}")],
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE engine_defs SET max_depth=0")
    async with client as ac:
        listing = await ac.get("/api/engine-catalog/presets")
        assert listing.status_code == 200
        rows = {row["id"]: row for row in listing.json()["items"]}
        assert rows["a"]["reasons"] == [
            "kind_not_registered",
            "options_malformed",
            "budget_invalid",
        ]
        assert rows["b"]["reasons"] == ["required_option_missing", "budget_invalid"]
        for row_id in ("a", "b"):
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200
            assert detail.json() == rows[row_id]


def test_option_size_limit_checks_utf8_bytes_before_scan_and_parse(monkeypatch):
    from lionagi.studio.services import engine_defs

    class ScanSpy(str):
        scanned = False

        def __iter__(self):
            self.scanned = True
            return super().__iter__()

    # Multibyte text keeps the character count below the byte limit.
    exact = '{"x":"' + "é" * 32764 + '"}'
    oversized = ScanSpy(exact[:-2] + 'a"}')
    assert len(exact.encode("utf-8")) == 65536
    assert len(oversized.encode("utf-8")) == 65537
    assert len(oversized) < 65536
    spy = Mock(wraps=json.loads)
    monkeypatch.setattr(json, "loads", spy)

    assert engine_defs._catalog_options(oversized) == (None, "options_undecodable")
    assert not oversized.scanned
    spy.assert_not_called()
    parsed, reason = engine_defs._catalog_options(exact)
    assert reason is None
    assert parsed == {"x": "é" * 32764}
    spy.assert_called_once()


async def test_oversized_utf8_options_are_isolated_in_http(catalog_app):
    db_path, client = catalog_app
    await _initialize(db_path)
    exact = '{"x":"' + "é" * 32764 + '"}'
    oversized = exact[:-2] + 'a"}'
    assert len(exact.encode("utf-8")) == 65536
    assert len(oversized.encode("utf-8")) == 65537
    _insert(
        db_path,
        [
            ("a", "at-bound", "future-kind", exact),
            ("b", "over-bound", "future-kind", oversized),
            ("c", "healthy", "research", "{}"),
            ("d", "over-bound-bad-budget", "research", oversized),
        ],
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE engine_defs SET max_depth=0 WHERE id='d'")
    async with client as ac:
        listing = await ac.get("/api/engine-catalog/presets")
        assert listing.status_code == 200
        rows = {row["id"]: row for row in listing.json()["items"]}
        assert rows["a"]["reasons"] == ["kind_not_registered"]
        assert rows["a"]["options"] == {"x": "é" * 32764}
        assert rows["b"]["reasons"] == ["kind_not_registered", "options_undecodable"]
        assert rows["b"]["options"] is None
        assert rows["c"]["availability"] == "available"
        assert rows["d"]["reasons"] == ["options_undecodable", "budget_invalid"]
        assert rows["d"]["options"] is None
        assert rows["d"]["availability"] == "unavailable"
        for row_id in ("a", "b", "c", "d"):
            detail = await ac.get("/api/engine-catalog/preset", params={"id": row_id})
            assert detail.status_code == 200
            assert detail.json() == rows[row_id]
