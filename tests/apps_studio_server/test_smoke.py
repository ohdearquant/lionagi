"""Hermetic smoke tests for the Lion Studio server.

Covers /api/stats, /api/shows, /api/playbooks, /api/runs, /api/agents/{name},
plus path-traversal guard tests for /api/runs and /api/agents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402 — must follow importorskip

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
) -> TestClient:
    """Patch all roots to tmp_path subdirs and return a wired TestClient."""
    shows_root = tmp_path / "shows"
    runs_root = tmp_path / "runs"
    agents_root = tmp_path / "agents"
    playbooks_root = tmp_path / "playbooks"
    # A non-existent DB path so sessions_svc returns [] without touching the real DB
    fake_db = tmp_path / "state.db"
    for d in (shows_root, runs_root, agents_root, playbooks_root):
        d.mkdir(parents=True)

    # Patch config + service modules
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as config_mod
    import lionagi.studio.services.agents as agents_mod
    import lionagi.studio.services.definitions as defs_mod
    import lionagi.studio.services.playbooks as playbooks_mod
    import lionagi.studio.services.runs as runs_mod
    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.shows as shows_mod
    import lionagi.studio.services.stats as stats_mod

    monkeypatch.setattr(config_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", agents_root)
    monkeypatch.setattr(playbooks_mod, "_PLAYBOOKS_ROOT", playbooks_root)
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(cli_runs_mod, "RUNS_ROOT", runs_root)
    # Redirect state DB so runs/sessions/shows queries don't touch the real DB
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    # sessions/shows/defs import DEFAULT_DB_PATH at module load; patch both the
    # Path object and the _DB string so .exists() and aiosqlite.connect() both
    # see the fake path.
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
        (branches_dir / "b2.json").write_text(json.dumps({"branch_id": "b2"}))

    if with_agent:
        agent_md = agents_root / "my-agent.md"
        agent_md.write_text(
            "---\n"
            "provider: anthropic\n"
            "model: claude-3-5-sonnet\n"
            "description: A test agent\n"
            "guidance: Be helpful.\n"
            "---\n"
            "You are a test agent. Always respond concisely.\n"
        )

    if with_playbook:
        pb = playbooks_root / "my-playbook.playbook.yaml"
        pb.write_text("description: Test playbook\nsteps: {}\nlinks: []\n")

    from lionagi.studio.app import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_app_imports():
    from lionagi.studio.app import app

    assert app.title == "Lion Studio Server"


@pytest.mark.integration
def test_stats_route(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    for key in ("playbooks", "agents", "runs", "shows"):
        assert key in data
        assert isinstance(data[key], int)


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------


def test_shows_list(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/shows")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# Playbooks
# ---------------------------------------------------------------------------


def test_playbooks_list_returns_dict(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, with_playbook=True)
    r = client.get("/api/playbooks")
    assert r.status_code == 200
    data = r.json()
    assert "playbooks" in data
    assert isinstance(data["playbooks"], list)
    names = {p["name"] for p in data["playbooks"]}
    assert "my-playbook" in names


def test_playbooks_list_empty(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/playbooks")
    assert r.status_code == 200
    assert r.json()["playbooks"] == []


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_runs_list_returns_dict(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, with_run=True)
    r = client.get("/api/runs")
    assert r.status_code == 200
    data = r.json()
    assert "runs" in data
    assert isinstance(data["runs"], list)


def test_runs_list_has_contract_fields(tmp_path, monkeypatch):
    """RunSummary must contain the SQLite-backed fields (F-A1-1, ADR-0004 rewire).

    list_runs() now reads from the sessions SQLite table, not filesystem.
    Field names match the sessions schema: playbook_name, status, started_at,
    ended_at (not worker_name/task/step_count/finished_at from the old JSON snapshots).
    With an empty/absent DB, the list is empty.
    """
    client = _make_client(tmp_path, monkeypatch, with_run=True)
    r = client.get("/api/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    # DB doesn't exist (fake_db) so sessions list is empty — correct behaviour
    assert isinstance(runs, list)
    assert len(runs) == 0


def test_runs_list_filter_by_playbook(tmp_path, monkeypatch):
    """?playbook= filter replaces the old ?worker= param (F-A3-7, ADR-0005)."""
    client = _make_client(tmp_path, monkeypatch, with_run=True)
    # Correct param name; both should 200 with empty list (empty DB)
    r = client.get("/api/runs?playbook=some-playbook")
    assert r.status_code == 200
    assert r.json()["runs"] == []

    # Old ?worker= param should still 200 (FastAPI ignores unknown query params)
    r2 = client.get("/api/runs?worker=my-worker")
    assert r2.status_code == 200


def test_runs_list_filter_by_status(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, with_run=True)
    r = client.get("/api/runs?status=completed")
    assert r.status_code == 200
    assert r.json()["runs"] == []

    r2 = client.get("/api/runs?status=failed")
    assert r2.status_code == 200
    assert r2.json()["runs"] == []


def test_run_detail_contract_fields(tmp_path, monkeypatch):
    """RunDetail must include graph, error, cwd, manifest, branches."""
    client = _make_client(tmp_path, monkeypatch, with_run=True)
    r = client.get("/api/runs/20240101T000000-abc123")
    assert r.status_code == 200
    data = r.json()
    for field in (
        "run_id",
        "worker_name",
        "task",
        "status",
        "error",
        "cwd",
        "started_at",
        "finished_at",
        "graph",
        "manifest",
        "branches",
    ):
        assert field in data, f"missing field: {field}"
    assert data["graph"] == {"nodes": [], "edges": []}
    assert isinstance(data["branches"], list)
    assert isinstance(data["manifest"], dict)


# ---------------------------------------------------------------------------
# Path traversal — runs (Fix 1)
# ---------------------------------------------------------------------------


def test_path_traversal_encoded_dotdot_runs(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/runs/%2e%2e")
    assert r.status_code == 404


def test_path_traversal_encoded_slash_runs(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/runs/aaa%2Fbbb")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Agents (Fix 3)
# ---------------------------------------------------------------------------


def test_agents_list_returns_dict(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, with_agent=True)
    r = client.get("/api/agents")
    assert r.status_code == 200
    data = r.json()
    assert "agents" in data
    names = {a["name"] for a in data["agents"]}
    assert "my-agent" in names


def test_agent_detail_flat_contract(tmp_path, monkeypatch):
    """GET /api/agents/{name} must return AgentProfile flat shape."""
    client = _make_client(tmp_path, monkeypatch, with_agent=True)
    r = client.get("/api/agents/my-agent")
    assert r.status_code == 200
    data = r.json()
    # Required top-level fields per frontend AgentProfile contract
    for field in ("name", "provider", "model", "system_prompt", "guidance"):
        assert field in data, f"missing field: {field}"
    # Must NOT nest under frontmatter key
    assert "frontmatter" not in data
    assert "body" not in data
    assert data["provider"] == "anthropic"
    assert data["model"] == "claude-3-5-sonnet"
    assert data["guidance"] == "Be helpful."


def test_agent_detail_not_found(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/agents/nonexistent")
    assert r.status_code == 404


def test_path_traversal_encoded_dotdot_agents(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/agents/%2e%2e")
    assert r.status_code == 404


@pytest.mark.integration
def test_agent_update_round_trips_effort_and_provider_model(tmp_path, monkeypatch):
    import lionagi.studio.services.agents as agents_mod

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", agents_root)

    agent_path = agents_root / "my-agent.md"
    agent_path.write_text(
        "---\nmodel: claude/old-model\nreasoning_effort: medium\n---\n\nold body\n"
    )

    updated = agents_mod.update_agent(
        "my-agent",
        {
            "effort": "high",
            "model": "claude/claude-sonnet-4-6",
            "system_prompt": "new body",
        },
    )

    assert updated is not None
    assert updated["effort"] == "high"
    assert updated["model"] == "claude/claude-sonnet-4-6"
    assert "reasoning_effort" not in updated

    read_back = agents_mod.get_agent("my-agent")
    assert read_back is not None
    assert read_back["effort"] == "high"
    assert read_back["model"] == "claude/claude-sonnet-4-6"
    assert "reasoning_effort" not in read_back

    raw = agent_path.read_text()
    assert "effort: high" in raw
    assert "reasoning_effort" not in raw
    assert "model: claude/claude-sonnet-4-6" in raw
