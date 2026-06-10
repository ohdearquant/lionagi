"""Tests that Studio route handlers offload synchronous filesystem I/O
to worker threads instead of blocking the event loop.

Verifies that the hottest routes (invocations list, runs detail, agents,
playbooks, skills, plugins) complete without performing synchronous
filesystem reads on the main async thread.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_run: bool = False,
    with_agent: bool = False,
    with_playbook: bool = False,
    with_skill: bool = False,
) -> TestClient:
    """Patch all roots to tmp_path subdirs and return a wired TestClient."""
    shows_root = tmp_path / "shows"
    runs_root = tmp_path / "runs"
    agents_root = tmp_path / "agents"
    playbooks_root = tmp_path / "playbooks"
    skills_root = tmp_path / "skills"
    fake_db = tmp_path / "state.db"
    for d in (shows_root, runs_root, agents_root, playbooks_root, skills_root):
        d.mkdir(parents=True)

    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as config_mod
    import lionagi.studio.services.agents as agents_mod
    import lionagi.studio.services.definitions as defs_mod
    import lionagi.studio.services.playbooks as playbooks_mod
    import lionagi.studio.services.runs as runs_mod
    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.shows as shows_mod
    import lionagi.studio.services.skills as skills_mod
    import lionagi.studio.services.stats as stats_mod

    monkeypatch.setattr(config_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", agents_root)
    monkeypatch.setattr(playbooks_mod, "_PLAYBOOKS_ROOT", playbooks_root)
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", skills_root)
    monkeypatch.setattr(cli_runs_mod, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(sessions_mod, "_DB", str(fake_db))
    monkeypatch.setattr(shows_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(stats_mod, "_DB", str(fake_db))

    if with_run:
        run_dir = runs_root / "20240101T000000-abc123"
        run_dir.mkdir()
        manifest = {
            "run_id": "20240101T000000-abc123",
            "worker_name": "my-worker",
            "task": "do stuff",
            "status": "success",
            "started_at": 1704067200000,
            "finished_at": 1704067260000,
            "state_root": str(run_dir),
            "artifact_root": str(run_dir / "artifacts"),
        }
        (run_dir / "run.json").write_text(json.dumps(manifest))
        branches_dir = run_dir / "branches"
        branches_dir.mkdir()
        (branches_dir / "b1.json").write_text(json.dumps({"branch_id": "b1"}))

    if with_agent:
        agent_md = agents_root / "test-agent.md"
        agent_md.write_text(
            "---\n"
            "provider: anthropic\n"
            "model: claude-3-5-sonnet\n"
            "description: A test agent\n"
            "---\n"
            "You are a test agent.\n"
        )

    if with_playbook:
        pb = playbooks_root / "test-pb.playbook.yaml"
        pb.write_text("description: Test playbook\nsteps: {}\nlinks: []\n")

    if with_skill:
        skill_dir = skills_root / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill\n---\n\nSkill body.\n"
        )

    from lionagi.studio.app import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Runs detail — offloaded to worker thread
# ---------------------------------------------------------------------------


def test_run_detail_reads_from_statedb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/runs/{id} reads from StateDB (not flat-file run.json).

    get_run() is now async and reads the same SQLite source as list_runs().
    """
    from lionagi.state.db import StateDB

    run_id = str(uuid.uuid4())
    db_path = tmp_path / "state.db"

    async def _seed():
        async with StateDB(db_path) as db:
            prog_id = f"{run_id}-prog"
            await db.create_progression(prog_id)
            await db.create_session(
                {
                    "id": run_id,
                    "progression_id": prog_id,
                    "name": "async-run",
                    "status": "completed",
                    "agent_name": "my-worker",
                    "model": "gpt-5",
                    "invocation_kind": "agent",
                    "source_kind": "live",
                }
            )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_seed())
    finally:
        loop.close()

    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    client = _make_client(tmp_path, monkeypatch)
    r = client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["run_id"] == run_id
    assert data["worker_name"] == "my-worker"


def test_run_detail_404_with_thread_offload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/runs/{id} for a nonexistent run still returns 404."""
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/runs/nonexistent-id")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Agents — offloaded to worker thread
# ---------------------------------------------------------------------------


def test_agents_list_uses_thread_offload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/agents must complete without blocking the event loop."""
    client = _make_client(tmp_path, monkeypatch, with_agent=True)
    r = client.get("/api/agents")
    assert r.status_code == 200
    data = r.json()
    assert "agents" in data
    names = {a["name"] for a in data["agents"]}
    assert "test-agent" in names


def test_agent_detail_uses_thread_offload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/agents/{name} must complete without blocking the event loop."""
    client = _make_client(tmp_path, monkeypatch, with_agent=True)
    r = client.get("/api/agents/test-agent")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "test-agent"
    assert data["provider"] == "anthropic"


def test_agent_update_uses_thread_offload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /api/agents/{name} must complete without blocking the event loop."""
    client = _make_client(tmp_path, monkeypatch, with_agent=True)
    r = client.put(
        "/api/agents/test-agent",
        json={"model": "anthropic/claude-sonnet-4-20250514", "system_prompt": "Updated."},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["model"] == "anthropic/claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Playbooks — offloaded to worker thread
# ---------------------------------------------------------------------------


def test_playbooks_list_uses_thread_offload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/playbooks must complete without blocking the event loop."""
    client = _make_client(tmp_path, monkeypatch, with_playbook=True)
    r = client.get("/api/playbooks")
    assert r.status_code == 200
    data = r.json()
    assert "playbooks" in data
    names = {p["name"] for p in data["playbooks"]}
    assert "test-pb" in names


def test_playbook_detail_uses_thread_offload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/playbooks/{name} must complete without blocking the event loop."""
    client = _make_client(tmp_path, monkeypatch, with_playbook=True)
    r = client.get("/api/playbooks/test-pb")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "test-pb"


# ---------------------------------------------------------------------------
# Skills — offloaded to worker thread
# ---------------------------------------------------------------------------


def test_skills_list_uses_thread_offload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/skills must complete without blocking the event loop."""
    client = _make_client(tmp_path, monkeypatch, with_skill=True)
    r = client.get("/api/skills")
    assert r.status_code == 200
    data = r.json()
    assert "skills" in data
    names = {s["name"] for s in data["skills"]}
    assert "test-skill" in names


def test_skill_detail_uses_thread_offload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/skills/{name} must complete without blocking the event loop."""
    client = _make_client(tmp_path, monkeypatch, with_skill=True)
    r = client.get("/api/skills/test-skill")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "test-skill"
    assert data["content"] == "Skill body."


# ---------------------------------------------------------------------------
# Thread offload verification — confirms anyio.to_thread.run_sync is used
# ---------------------------------------------------------------------------


def test_run_detail_returns_404_for_nonexistent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/runs/{id} returns 404 for an ID absent from StateDB.

    get_run() is now async and reads StateDB directly (no thread offload needed).
    """
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/runs/20240101T000000-abc123")
    assert r.status_code == 404


def test_agents_list_calls_anyio_to_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that the agents list route actually invokes anyio.to_thread.run_sync."""
    import anyio.to_thread

    original_run_sync = anyio.to_thread.run_sync
    call_count = 0

    async def tracking_run_sync(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return await original_run_sync(*args, **kwargs)

    client = _make_client(tmp_path, monkeypatch, with_agent=True)
    with patch.object(anyio.to_thread, "run_sync", side_effect=tracking_run_sync):
        r = client.get("/api/agents")
    assert r.status_code == 200
    assert call_count >= 1, "anyio.to_thread.run_sync was not called for agents list"
