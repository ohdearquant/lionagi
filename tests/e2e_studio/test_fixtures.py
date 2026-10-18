"""Unit coverage for the seed-data builder and the harness safety assertion.

These run in-process against an in-memory StateDB (no subprocess, no
browser) -- the Playwright specs are the actual end-to-end coverage; this
file just pins the fixture contract the specs rely on and proves the
temp-dir safety assertion actually fires on a bad path.
"""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from lionagi.state.db import StateDB

from . import harness
from .fixtures import (
    SESSION_STATUSES,
    SMOKE_EXECUTION_GRAPH,
    SMOKE_FAILING_SCHEDULE_NAME,
    SMOKE_SCHEDULE_NAME,
    SMOKE_SESSION_NAME,
    seed_filesystem_fixtures,
    seed_state_db,
)
from .harness import SeededDaemon, _seed


@pytest_asyncio.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def test_seed_state_db_covers_every_session_status(db):
    manifest = await seed_state_db(db)

    for status in SESSION_STATUSES:
        session = await db.get_session(manifest["sessions"][status])
        assert session["status"] == status

    completed = await db.get_session(manifest["sessions"]["completed"])
    assert completed["name"] == SMOKE_SESSION_NAME
    assert completed["node_metadata"] == {"early_graph": SMOKE_EXECUTION_GRAPH}


async def test_seed_state_db_phantom_session_is_stale_running(db):
    manifest = await seed_state_db(db)
    phantom = await db.get_session(manifest["sessions"]["phantom"])
    assert phantom["status"] == "running"
    assert phantom["name"] == "e2e-smoke-session-phantom-stale"


async def test_seed_state_db_failed_invocation_has_reason(db):
    manifest = await seed_state_db(db)
    invocation = await db.get_invocation(manifest["invocations"]["failed"])
    assert invocation["status"] == "failed"
    assert invocation["status_reason_code"] == "run.failed.exit_nonzero"


async def test_seed_state_db_schedules_are_disabled(db):
    await seed_state_db(db)
    spent = await db.get_schedule_by_name(SMOKE_SCHEDULE_NAME)
    failing = await db.get_schedule_by_name(SMOKE_FAILING_SCHEDULE_NAME)
    assert spent["enabled"] == 0
    assert failing["enabled"] == 0

    runs = await db.list_schedule_runs(failing["id"])
    assert len(runs) == 3
    assert all(run["status"] == "failed" for run in runs)


def test_seed_filesystem_fixtures_writes_agent_and_playbook(tmp_path: Path):
    seed_filesystem_fixtures(tmp_path)
    assert (tmp_path / "agents" / "e2e-smoke-reviewer.md").exists()
    assert (tmp_path / "playbooks" / "e2e-smoke-release-checklist.playbook.yaml").exists()


async def test_seed_refuses_a_db_path_outside_the_temp_dir():
    with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as other_dir:
        outside_db_path = Path(other_dir) / "state.db"
        with pytest.raises(RuntimeError, match="outside the temp dir"):
            await _seed(outside_db_path, Path(tmp_dir))


def test_seeded_daemon_start_reports_an_early_child_exit(monkeypatch) -> None:
    async def skip_seed(_db_path: Path, _tmp_dir: Path) -> None:
        pass

    class ExitedProcess:
        returncode = 1

        def poll(self) -> int:
            return self.returncode

    with socket.socket() as occupied_port:
        occupied_port.bind(("127.0.0.1", 0))
        port = occupied_port.getsockname()[1]

        def exit_before_start(_args, *, env, stdout, **_kwargs):
            assert env["LIONAGI_STUDIO_PORT"] == str(port)
            stdout.write("address already in use\n")
            return ExitedProcess()

        monkeypatch.setattr(harness, "_seed", skip_seed)
        monkeypatch.setattr(harness.subprocess, "Popen", exit_before_start)

        with pytest.raises(RuntimeError, match="seeded daemon exited early") as exc_info:
            SeededDaemon.start(port=port)

    assert "address already in use" in str(exc_info.value)
