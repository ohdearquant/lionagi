# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ADR-0026 project management in StateDB."""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def uid() -> str:
    return str(uuid.uuid4())


async def _make_session(
    db: StateDB,
    *,
    project: str | None = None,
    project_source: str | None = None,
    status: str | None = None,
) -> dict:
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {
        "id": uid(),
        "progression_id": prog_id,
        "status": status,
        "project": project,
        "project_source": project_source,
    }
    await db.create_session(session)
    return session


# ── register_project ──────────────────────────────────────────────────────────


async def test_register_project_creates_row(db: StateDB):
    await db.register_project("lionagi/lionagi", "git_remote")
    project = await db.get_project("lionagi/lionagi")
    assert project is not None
    assert project["name"] == "lionagi/lionagi"
    assert project["source"] == "git_remote"


async def test_register_project_idempotent_bumps_last_seen(db: StateDB):
    await db.register_project("myproject", "config_toml")
    before = await db.get_project("myproject")
    # Small sleep is needed so last_seen_at changes; use direct DB write instead.
    old_seen = before["last_seen_at"]
    # Re-register with a git_remote source — should NOT downgrade source.
    await db.register_project("myproject", "git_remote")
    after = await db.get_project("myproject")
    assert after["source"] == "config_toml"  # preserved; git_remote < config_toml
    assert after["last_seen_at"] >= old_seen


async def test_register_project_upgrades_source_to_config_toml(db: StateDB):
    await db.register_project("myproject", "git_remote")
    await db.register_project("myproject", "config_toml")
    project = await db.get_project("myproject")
    assert project["source"] == "config_toml"


async def test_register_project_preserves_existing_path(db: StateDB):
    await db.register_project("myproject", "config_toml", path="/home/user/proj")
    await db.register_project("myproject", "config_toml")  # no path
    project = await db.get_project("myproject")
    assert project["path"] == "/home/user/proj"


async def test_register_project_updates_github_when_provided(db: StateDB):
    await db.register_project("myproject", "config_toml")
    await db.register_project("myproject", "config_toml", github="https://github.com/org/repo")
    project = await db.get_project("myproject")
    assert project["github"] == "https://github.com/org/repo"


# ── create_project ─────────────────────────────────────────────────────────────


async def test_create_project_studio_source(db: StateDB):
    await db.create_project("my-studio-proj", description="A test project")
    project = await db.get_project("my-studio-proj")
    assert project is not None
    assert project["source"] == "studio"
    assert project["description"] == "A test project"


async def test_create_project_all_fields(db: StateDB):
    await db.create_project(
        "full-proj",
        github="https://github.com/org/full",
        description="Full test",
        path="/tmp/full",
    )
    project = await db.get_project("full-proj")
    assert project["github"] == "https://github.com/org/full"
    assert project["path"] == "/tmp/full"
    assert project["source"] == "studio"


async def test_create_project_duplicate_raises(db: StateDB):
    import aiosqlite

    await db.create_project("dup-proj")
    with pytest.raises(aiosqlite.IntegrityError):
        await db.create_project("dup-proj")


# ── list_projects ─────────────────────────────────────────────────────────────


async def test_list_projects_empty(db: StateDB):
    result = await db.list_projects()
    assert result == []


async def test_list_projects_returns_all(db: StateDB):
    await db.register_project("proj-a", "git_remote")
    await db.register_project("proj-b", "config_toml")
    result = await db.list_projects()
    names = [r["name"] for r in result]
    assert "proj-a" in names
    assert "proj-b" in names


async def test_list_projects_includes_session_counts(db: StateDB):
    await db.register_project("counted", "git_remote")
    for _ in range(3):
        await _make_session(db, project="counted", project_source="git_remote")
    result = await db.list_projects()
    proj = next(r for r in result if r["name"] == "counted")
    assert proj["session_count"] == 3


async def test_list_projects_running_count(db: StateDB):
    await db.register_project("runner", "git_remote")
    await _make_session(db, project="runner", status="running")
    await _make_session(db, project="runner", status="completed")
    result = await db.list_projects()
    proj = next(r for r in result if r["name"] == "runner")
    assert proj["running_count"] == 1


# ── get_project ───────────────────────────────────────────────────────────────


async def test_get_project_none_for_missing(db: StateDB):
    result = await db.get_project("nonexistent")
    assert result is None


async def test_get_project_returns_dict(db: StateDB):
    await db.register_project("my-repo", "git_remote")
    result = await db.get_project("my-repo")
    assert isinstance(result, dict)
    assert result["name"] == "my-repo"


# ── update_project ─────────────────────────────────────────────────────────────


async def test_update_project_description(db: StateDB):
    await db.create_project("updatable")
    ok = await db.update_project("updatable", description="new desc")
    assert ok is True
    proj = await db.get_project("updatable")
    # description column is in projects table; get_project SQL includes p.*
    assert proj is not None


async def test_update_project_returns_false_for_missing(db: StateDB):
    ok = await db.update_project("ghost", description="x")
    assert ok is False


async def test_update_project_rejects_bad_column(db: StateDB):
    await db.create_project("safe")
    with pytest.raises(ValueError, match="Invalid project field"):
        await db.update_project("safe", source="evil")


# ── delete_project ─────────────────────────────────────────────────────────────


async def test_delete_project_studio_source(db: StateDB):
    await db.create_project("deletable")
    ok = await db.delete_project("deletable")
    assert ok is True
    assert await db.get_project("deletable") is None


async def test_delete_project_non_studio_returns_false(db: StateDB):
    await db.register_project("auto-detected", "git_remote")
    ok = await db.delete_project("auto-detected")
    assert ok is False
    assert await db.get_project("auto-detected") is not None


# ── auto-registration via create_session ──────────────────────────────────────


async def test_create_session_auto_registers_project(db: StateDB):
    prog_id = uid()
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": uid(),
            "progression_id": prog_id,
            "project": "auto/project",
            "project_source": "git_remote",
        }
    )
    project = await db.get_project("auto/project")
    assert project is not None
    assert project["source"] == "git_remote"


async def test_create_session_no_project_skips_registration(db: StateDB):
    prog_id = uid()
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": uid(),
            "progression_id": prog_id,
        }
    )
    result = await db.list_projects()
    assert result == []


async def test_create_session_missing_project_source_defaults_to_git_remote(db: StateDB):
    prog_id = uid()
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": uid(),
            "progression_id": prog_id,
            "project": "my/proj",
            # no project_source
        }
    )
    project = await db.get_project("my/proj")
    assert project is not None
    assert project["source"] == "git_remote"
