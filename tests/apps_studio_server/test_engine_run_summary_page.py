from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB


@pytest.fixture
def engine_app(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    from lionagi.studio.app import app

    return db_path, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
    )


async def _insert(db_path: Path, run_id: str, started_at: float, spec: str) -> None:
    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=run_id,
            kind="research",
            spec_json={"topic": spec},
            started_at=started_at,
        )


async def test_engine_run_list_is_bounded_page_without_raw_spec(engine_app) -> None:
    db_path, client = engine_app
    sentinel = "sk-list-must-not-leak-1234567890"
    await _insert(db_path, "run-a", 100.0, sentinel + ("x" * 1_000_000))

    async with client as ac:
        response = await ac.get("/api/engine-runs/?limit=20")

    assert response.status_code == 200
    page = response.json()
    assert page["version"] == 1
    assert page["next_cursor"] is None
    assert [item["id"] for item in page["items"]] == ["run-a"]
    assert "spec_json" not in page["items"][0]
    assert sentinel not in response.text
    assert len(response.content) < 16_384


async def test_engine_run_cursor_is_stable_when_new_rows_arrive_ahead(engine_app) -> None:
    db_path, client = engine_app
    await _insert(db_path, "run-a", 100.0, "a")
    await _insert(db_path, "run-b", 100.0, "b")
    await _insert(db_path, "run-old", 90.0, "old")

    async with client as ac:
        first = (await ac.get("/api/engine-runs/?limit=1")).json()
        await _insert(db_path, "run-new", 200.0, "new")
        second = (
            await ac.get(
                "/api/engine-runs/",
                params={"limit": 2, "cursor": first["next_cursor"]},
            )
        ).json()

    assert [item["id"] for item in first["items"]] == ["run-b"]
    assert [item["id"] for item in second["items"]] == ["run-a", "run-old"]
    assert "run-new" not in {item["id"] for item in second["items"]}


async def test_engine_run_cursor_rejects_malformed_input(engine_app) -> None:
    _, client = engine_app
    async with client as ac:
        response = await ac.get("/api/engine-runs/", params={"cursor": "%%%not-base64"})

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid engine-run cursor"}


async def test_engine_detail_requires_explicit_spec_reveal_and_redacts_credentials(
    engine_app,
) -> None:
    db_path, client = engine_app
    sentinel = "sk-detail-secret-1234567890"
    await _insert(db_path, "run-a", 100.0, sentinel)

    async with client as ac:
        default = await ac.get("/api/engine-runs/run-a")
        revealed = await ac.get("/api/engine-runs/run-a?include_spec=true")

    assert default.status_code == 200
    assert default.json()["spec_json"] is None
    assert default.json()["spec_preview"] == {"topic": "[redacted]"}
    assert revealed.status_code == 200
    assert revealed.json()["spec_json"] == {"topic": "[redacted]"}
    assert sentinel not in revealed.text
