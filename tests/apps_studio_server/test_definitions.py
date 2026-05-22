# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for save_definition() atomicity and DB-first ordering (H-BE-3)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

# _run kept for non-integration sync test paths (e.g. ValueError checks without fastapi)
from tests.apps_studio_server._helpers import run_async as _run  # noqa: E402

# ---------------------------------------------------------------------------
# H-BE-3: save_definition() writes DB first, then disk
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_definition_creates_db_row_and_file(tmp_path, monkeypatch):
    """save_definition() with a missing (fresh) DB path must create the DB,
    insert a row, then write the file.  It must NOT return success without a
    row in the definitions table.
    """
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    # Redirect LIONAGI_HOME → tmp dirs so no real agent/playbook dirs are needed
    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    playbooks_dir = fake_home / "playbooks"
    agents_dir.mkdir()
    playbooks_dir.mkdir()

    fake_db = tmp_path / "state.db"  # does NOT exist yet

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    result = await defs_mod.save_definition(
        "agent", "test-agent", "# Test Agent\nGuidance here.", "initial save"
    )

    # DB file must exist now (StateDB created it)
    assert fake_db.exists(), "DB file must be created by StateDB on first use"

    # Result must carry a valid version number
    assert result["version"] >= 1
    assert result["kind"] == "agent"
    assert result["name"] == "test-agent"
    assert "saved_at" in result

    # Disk file must also exist
    agent_file = agents_dir / "test-agent.md"
    assert agent_file.exists(), "Disk file must be written after DB row is committed"
    assert agent_file.read_text() == "# Test Agent\nGuidance here."

    # Verify the DB row was actually inserted
    import aiosqlite

    async with aiosqlite.connect(str(fake_db)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT version, name, kind FROM definitions"
            " WHERE kind = 'agent' AND name = 'test-agent'"
        )
        rows = await cur.fetchall()

    assert len(rows) == 1, "Exactly one DB row must exist after save"
    assert rows[0]["version"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_definition_increments_version(tmp_path, monkeypatch):
    """Calling save_definition() twice for the same (kind, name) must increment version."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    r1 = await defs_mod.save_definition("agent", "my-agent", "v1 content")
    r2 = await defs_mod.save_definition("agent", "my-agent", "v2 content")

    assert r1["version"] == 1
    assert r2["version"] == 2


@pytest.mark.asyncio
async def test_save_definition_unknown_kind_raises(tmp_path, monkeypatch):
    """save_definition() with an unknown kind must raise ValueError (not return success)."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    (fake_home / "agents").mkdir()
    (fake_home / "playbooks").mkdir()

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(
        defs_mod, "KIND_DIRS", {"agent": fake_home / "agents", "playbook": fake_home / "playbooks"}
    )

    with pytest.raises(ValueError, match="Unknown kind"):
        await defs_mod.save_definition("skill", "my-skill", "content")


# ---------------------------------------------------------------------------
# CRITICAL: path/glob injection — service boundary validation
# ---------------------------------------------------------------------------


def _make_patched_client(tmp_path, monkeypatch):
    """Return a TestClient with definitions service redirected to tmp_path."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    playbooks_dir = fake_home / "playbooks"
    agents_dir.mkdir()
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    from fastapi.testclient import TestClient

    from apps.studio.server.app import app

    return TestClient(app)


@pytest.mark.parametrize(
    "encoded_name",
    [
        "%2A",       # URL-encoded * (glob wildcard)
        "%2e%2e",    # URL-encoded .. (directory traversal)
        "foo%2Fbar", # URL-encoded / (path separator — ASGI may split before service)
        "foo%00bar", # NUL byte
        "foo%3Fbar", # URL-encoded ? (glob metachar)
        "%5B%5D",    # URL-encoded [] (glob metachar)
    ],
)
@pytest.mark.integration
def test_save_definition_rejects_unsafe_name_post(encoded_name, tmp_path, monkeypatch):
    """POST /api/definitions/agent/<unsafe_name> must NOT return 200.

    This covers the path/glob injection attack surface reported in PR #981
    round-2 review: URL-encoded metacharacters and traversal sequences are
    decoded by the ASGI layer before route parameters are populated, so the
    service layer must validate the already-decoded string.

    Note: %2F (slash) may be split at the ASGI level before the route handler
    is invoked, resulting in a 404 instead of a 422.  Both are acceptable
    rejections — the important invariant is that no 200 is returned.
    """
    client = _make_patched_client(tmp_path, monkeypatch)
    r = client.post(
        f"/api/definitions/agent/{encoded_name}",
        json={"content": "# injected"},
    )
    assert r.status_code in (400, 404, 422), (
        f"Expected 4xx for name={encoded_name!r}, got {r.status_code}"
    )


@pytest.mark.parametrize(
    "encoded_name",
    [
        "%2A",
        "%2e%2e",
        "foo%2Fbar",
        "foo%00bar",
    ],
)
def test_get_definition_rejects_unsafe_name(encoded_name, tmp_path, monkeypatch):
    """GET /api/definitions/agent/<unsafe_name> must return 4xx."""
    client = _make_patched_client(tmp_path, monkeypatch)
    r = client.get(f"/api/definitions/agent/{encoded_name}")
    assert r.status_code in (400, 404, 422), (
        f"Expected 4xx for name={encoded_name!r}, got {r.status_code}"
    )


@pytest.mark.parametrize(
    "encoded_name",
    [
        "%2A",
        "%2e%2e",
        "foo%2Fbar",
        "foo%00bar",
    ],
)
def test_rollback_definition_rejects_unsafe_name(encoded_name, tmp_path, monkeypatch):
    """POST /api/definitions/agent/<unsafe_name>/rollback must return 4xx."""
    client = _make_patched_client(tmp_path, monkeypatch)
    r = client.post(
        f"/api/definitions/agent/{encoded_name}/rollback",
        params={"version": 1},
    )
    assert r.status_code in (400, 404, 422), (
        f"Expected 4xx for name={encoded_name!r}, got {r.status_code}"
    )


@pytest.mark.parametrize("name", ["my-agent", "my_agent", "myagent", "agent-123"])
@pytest.mark.asyncio
async def test_save_definition_accepts_safe_names(name, tmp_path, monkeypatch):
    """Normal safe names must not be rejected by the validation layer."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", fake_home / "playbooks")
    monkeypatch.setattr(
        defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": fake_home / "playbooks"}
    )
    (fake_home / "playbooks").mkdir()

    result = await defs_mod.save_definition("agent", name, "# content")
    assert result["version"] >= 1
    assert result["name"] == name


@pytest.mark.parametrize("kind", ["agent", "playbook"])
@pytest.mark.asyncio
async def test_save_definition_accepts_valid_kinds(kind, tmp_path, monkeypatch):
    """Valid kind values ('agent', 'playbook') must pass the validation gate."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    result = await defs_mod.save_definition(kind, "test-def", "# content")
    assert result["version"] >= 1


# ---------------------------------------------------------------------------
# HIGH: concurrent save race — disk must reflect the HIGHER version's content
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_save_disk_reflects_highest_version(tmp_path, monkeypatch):
    """Two concurrent save_definition() calls for the same (kind, name) must
    leave the disk file with the content of the HIGHER committed version.

    Regression test for the save race described in PR #981 round-2 review:
    without a per-(kind, name) lock spanning both the DB write and the disk
    write, the lower-version caller can win the disk write after losing the DB
    race.
    """
    import asyncio

    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    # Reset module-level lock dict so this test starts clean.
    defs_mod._DEFINITION_LOCKS.clear()

    async def _run_concurrent():
        r1, r2 = await asyncio.gather(
            defs_mod.save_definition("agent", "race-agent", "content-A", "save A"),
            defs_mod.save_definition("agent", "race-agent", "content-B", "save B"),
        )
        return r1, r2

    r1, r2 = await _run_concurrent()

    versions = sorted([r1["version"], r2["version"]])
    assert versions == [1, 2], f"Expected versions [1, 2], got {versions}"

    # Determine which content corresponds to the higher version
    if r1["version"] > r2["version"]:
        expected_content = "content-A"
    else:
        expected_content = "content-B"

    disk_file = agents_dir / "race-agent.md"
    assert disk_file.exists(), "Disk file must exist after concurrent saves"
    actual_content = disk_file.read_text()
    assert actual_content == expected_content, (
        f"Disk content should match highest version ({r1['version']}/{r2['version']}); "
        f"expected {expected_content!r}, got {actual_content!r}"
    )


# ---------------------------------------------------------------------------
# MEDIUM: StateDB failure — no file written, exception propagates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_definition_db_failure_does_not_write_file(tmp_path, monkeypatch):
    """When StateDB.save_definition() raises, the service must NOT write the
    disk file and must propagate the exception (so the router can return 500).
    """
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    # Patch StateDB.save_definition to raise a RuntimeError simulating DB failure.
    async def _failing_save(self, **kwargs):
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(state_db_mod.StateDB, "save_definition", _failing_save)

    with pytest.raises(RuntimeError, match="simulated DB write failure"):
        await defs_mod.save_definition("agent", "db-fail-agent", "# content")

    # No disk file must have been written.
    for candidate in agents_dir.iterdir():
        assert "db-fail-agent" not in candidate.name, (
            f"Disk file was written despite DB failure: {candidate}"
        )


def test_save_definition_db_failure_returns_500_from_router(tmp_path, monkeypatch):
    """The router must surface a DB failure as HTTP 500 (not 200)."""
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    async def _failing_save(self, **kwargs):
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(state_db_mod.StateDB, "save_definition", _failing_save)

    from fastapi.testclient import TestClient

    from apps.studio.server.app import app

    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/api/definitions/agent/db-fail-agent",
        json={"content": "# content"},
    )
    assert r.status_code == 500, f"Expected 500, got {r.status_code}"


# ---------------------------------------------------------------------------
# HIGH-R3-BE-1: symlinked agent definitions must be readable + writable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_definition_follows_symlink(tmp_path, monkeypatch):
    """get_definition() must return content from a symlinked file.

    Regression test for HIGH-R3-BE-1: the R3 bounded scan used
    candidate.resolve().relative_to(base.resolve()) to validate results, which
    unconditionally rejected symlinks whose targets live outside base (the
    common case for ~/.lionagi/agents/*.md → firm/agents/*.md).

    Fix: security is maintained by validate_name_component() on the route
    parameter, not by resolving symlink targets.
    """
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    # Set up an "external" target directory (simulates firm/agents/)
    external_dir = tmp_path / "firm" / "agents"
    external_dir.mkdir(parents=True)
    target_file = external_dir / "test-link.md"
    target_file.write_text("# Linked Agent\nContent from external location.")

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    # Create a symlink inside agents_dir pointing outside it
    symlink = agents_dir / "test-link.md"
    symlink.symlink_to(target_file)

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    result = await defs_mod.get_definition("agent", "test-link")

    assert result is not None, "get_definition must not return None for a symlinked agent"
    assert result["content"] == "# Linked Agent\nContent from external location."
    assert result["name"] == "test-link"


@pytest.mark.asyncio
async def test_save_definition_writes_through_symlink(tmp_path, monkeypatch):
    """save_definition() must update the symlink target, not create a new file.

    Regression test for HIGH-R3-BE-1: after the fix, saving a definition whose
    current disk file is a symlink must write the target file (the symlink
    already exists, _find_definition_file returns it, and write_text follows
    symlinks by default on POSIX).
    """
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    external_dir = tmp_path / "firm" / "agents"
    external_dir.mkdir(parents=True)
    target_file = external_dir / "test-link.md"
    target_file.write_text("# Original content")

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    agents_dir.mkdir()
    playbooks_dir = fake_home / "playbooks"
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    symlink = agents_dir / "test-link.md"
    symlink.symlink_to(target_file)

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    result = await defs_mod.save_definition("agent", "test-link", "# Updated content")

    assert result["version"] >= 1
    # The symlink target must have been updated (write_text follows the link)
    assert target_file.read_text() == "# Updated content", (
        "save_definition must write through the symlink to the target file"
    )
    # The symlink itself must still be a symlink (not replaced with a regular file)
    assert symlink.is_symlink(), "symlink must remain a symlink after save"


# ---------------------------------------------------------------------------
# MEDIUM-R3-BE-2: missing kind directory must not 500
# ---------------------------------------------------------------------------


def test_find_definition_file_missing_base_returns_none(tmp_path):
    """_find_definition_file must return None (not raise) when base dir is absent.

    Regression test for MEDIUM-R3-BE-2: the slow path called base.iterdir()
    without an existence check, causing FileNotFoundError on a fresh home where
    the kind directory has not yet been created.
    """
    from apps.studio.server.services.definitions import _find_definition_file

    missing_base = tmp_path / "does_not_exist"
    # Must not raise FileNotFoundError
    result = _find_definition_file(missing_base, "my-agent")
    assert result is None


@pytest.mark.asyncio
async def test_save_definition_fresh_home_no_kind_dir(tmp_path, monkeypatch):
    """POST to a fresh home where agents/ doesn't exist must succeed (not 500).

    Regression test for MEDIUM-R3-BE-2: save_definition() calls
    _find_definition_file before mkdir, so a missing kind dir triggered a
    FileNotFoundError in the slow-path iterdir.  After the fix, it returns None
    and falls back to the default disk_file path, then creates the directory.
    """
    import apps.studio.server.services.definitions as defs_mod
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    # Deliberately do NOT create agents/ or playbooks/
    agents_dir = fake_home / "agents"
    playbooks_dir = fake_home / "playbooks"
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})

    # Must succeed even though agents_dir doesn't exist yet
    result = await defs_mod.save_definition("agent", "my-agent", "# content")

    assert result["version"] >= 1
    assert (agents_dir / "my-agent.md").exists(), "agents/ dir and file must be created"
