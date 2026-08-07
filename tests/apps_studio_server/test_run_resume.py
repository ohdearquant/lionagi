# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused contract tests for POST /api/runs/{run_id}/resume."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402
from lionagi.state.reasons import RunReasons  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _seed_run(
    db_path: Path,
    *,
    run_id: str,
    status: str = "completed",
    branch_ids: list[str] | None = None,
) -> None:
    async with StateDB(db_path) as db:
        session_progression_id = f"{run_id}-progression"
        await db.create_progression(session_progression_id)
        await db.create_session(
            {
                "id": run_id,
                "progression_id": session_progression_id,
                "name": f"run-{run_id}",
                "status": status,
                "invocation_kind": "agent",
            }
        )
        for index, branch_id in enumerate(branch_ids or []):
            branch_progression_id = f"{branch_id}-progression"
            await db.create_progression(branch_progression_id)
            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": float(index + 1),
                    "name": f"branch-{index + 1}",
                    "session_id": run_id,
                    "progression_id": branch_progression_id,
                    "model": "claude_code/sonnet",
                    "provider": "claude_code",
                }
            )


@pytest.fixture
def resume_harness(tmp_path: Path, monkeypatch: Any):
    import lionagi.cli._runs as cli_runs
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.run_resume as resume_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    launched: list[tuple[list[str], dict[str, Any]]] = []

    async def _fake_launch(argv: list[str], **kwargs: Any) -> str:
        launched.append((argv, kwargs))
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "resumeinv123",
                    "skill": kwargs["skill"],
                    "plugin": kwargs["plugin"],
                    "prompt": kwargs["prompt"],
                    "started_at": time.time(),
                    "status": "running",
                    "node_metadata": kwargs.get("node_metadata"),
                }
            )
        return "resumeinv123"

    monkeypatch.setattr(resume_svc._launches, "launch_detached_argv", _fake_launch)
    monkeypatch.setattr(
        resume_svc._subprocess,
        "resolve_li_executable",
        lambda: (["/opt/lionagi/bin/li"], None),
    )
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()

    def _find_snapshot(branch_id: str):
        from lionagi.ln import json_dumps
        from lionagi.service.manager import iModel
        from lionagi.session.branch import Branch

        snapshot = snapshots / f"{branch_id}.json"
        if not snapshot.exists():
            branch = Branch(
                chat_model=iModel(
                    provider="claude_code",
                    model="sonnet",
                    api_key="dummy",
                )
            )
            serialized = branch.to_dict()
            serialized["id"] = branch_id
            snapshot.write_text(json_dumps(serialized))
        return ("fixture-run", snapshot)

    monkeypatch.setattr(
        cli_runs,
        "find_branch",
        _find_snapshot,
    )

    from lionagi.studio.app import create_app

    client = TestClient(
        create_app(),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8765",
    )
    try:
        yield resume_svc, db_path, client, launched
    finally:
        client.close()


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled", "timed_out"])
def test_resume_terminal_run_launches_exact_cli_contract(resume_harness, terminal_status):
    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(
        _seed_run(
            db_path,
            run_id=run_id,
            status=terminal_status,
            branch_ids=[branch_id],
        )
    )

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue with the next step."},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "run_id": run_id,
        "branch_id": branch_id,
        "invocation_id": "resumeinv123",
    }
    assert launched == [
        (
            [
                "/opt/lionagi/bin/li",
                "agent",
                "-r",
                branch_id,
                "--prompt",
                "Continue with the next step.",
            ],
            {
                "skill": "resume:agent",
                "plugin": "studio_run_resume",
                "prompt": "Continue with the next step.",
                "tmp_path": None,
                "action_kind": "agent",
                "node_metadata": {
                    "run_id": run_id,
                    "branch_id": branch_id,
                    "resume": True,
                    "queued_for_terminal": False,
                    "model": None,
                },
            },
        )
    ]
    assert "--bypass" not in launched[0][0]
    assert "--yolo" not in launched[0][0]


def test_resume_running_run_queues_until_terminal_and_coalesces_duplicate(
    resume_harness, monkeypatch
):
    import lionagi.cli._runs as cli_runs

    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(
        _seed_run(
            db_path,
            run_id=run_id,
            status="running",
            branch_ids=[branch_id],
        )
    )

    def _snapshot_must_not_be_read_while_active(_branch_id: str):
        raise AssertionError("active branch snapshot was read before terminal hand-off")

    monkeypatch.setattr(cli_runs, "find_branch", _snapshot_must_not_be_read_while_active)
    body = {"instruction": "Continue after the active leg finishes."}
    first = client.post(f"/api/runs/{run_id}/resume", json=body)
    assert first.status_code == 202, first.text
    assert first.json() == {
        "run_id": run_id,
        "branch_id": branch_id,
        "invocation_id": "resumeinv123",
    }
    assert len(launched) == 1
    worker_argv, worker_kwargs = launched[0]
    assert worker_argv[:4] == [
        sys.executable,
        "-m",
        "lionagi.studio.services.run_resume_worker",
        "--config",
    ]
    config_path = worker_argv[4]
    config = json.loads(Path(config_path).read_text())
    assert config == {
        "run_id": run_id,
        "branch_id": branch_id,
        "instruction": body["instruction"],
        "model": None,
        "executable_prefix": ["/opt/lionagi/bin/li"],
    }
    assert worker_kwargs["tmp_path"] == config_path
    assert worker_kwargs["node_metadata"]["queued_for_terminal"] is True

    repeated = client.post(f"/api/runs/{run_id}/resume", json=body)
    assert repeated.status_code == 202
    assert repeated.json() == first.json()
    assert len(launched) == 1

    conflict = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "A different concurrent continuation."},
    )
    assert conflict.status_code == 409
    assert "already has a resume in progress" in conflict.json()["detail"]
    assert len(launched) == 1
    os.unlink(config_path)


@pytest.mark.asyncio
async def test_queued_resume_worker_waits_for_terminal_snapshot_before_spawning(
    tmp_path, monkeypatch
):
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.run_resume as resume_svc
    import lionagi.studio.services.run_resume_worker as worker

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    await _seed_run(
        db_path,
        run_id=run_id,
        status="running",
        branch_ids=[branch_id],
    )
    events: list[tuple[str, Any]] = []

    async def ensure_snapshot(value: str) -> None:
        events.append(("snapshot", value))

    class Process:
        async def wait(self) -> int:
            events.append(("wait", None))
            return 0

    async def create_subprocess_exec(*argv: str):
        events.append(("spawn", list(argv)))
        return Process()

    monkeypatch.setattr(
        resume_svc,
        "_ensure_branch_snapshot_available",
        ensure_snapshot,
    )
    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", create_subprocess_exec)
    task = asyncio.create_task(
        worker.run_worker(
            {
                "run_id": run_id,
                "branch_id": branch_id,
                "instruction": "Continue only after hand-off.",
                "model": None,
                "executable_prefix": ["/opt/lionagi/bin/li"],
            }
        )
    )
    await asyncio.sleep(0.05)
    assert events == []

    async with StateDB(db_path) as db:
        await db.update_session(
            run_id,
            status="completed",
            ended_at=time.time(),
            reason_code=RunReasons.COMPLETED_OK,
        )
    assert await asyncio.wait_for(task, timeout=1) == 0
    assert events == [
        ("snapshot", branch_id),
        (
            "spawn",
            [
                "/opt/lionagi/bin/li",
                "agent",
                "-r",
                branch_id,
                "--prompt",
                "Continue only after hand-off.",
            ],
        ),
        ("wait", None),
    ]


def test_explicit_member_branch_and_model_override(resume_harness):
    resume_svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    first_branch = str(uuid.uuid4())
    second_branch = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[first_branch, second_branch]))
    monkey_prefix = ["/opt/lionagi/bin/python", "-m", "lionagi.cli"]
    resume_svc._subprocess.resolve_li_executable = lambda: (monkey_prefix, None)

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={
            "instruction": "Use the alternate branch.",
            "branch_id": second_branch,
            "model": "codex/gpt-5.3-codex",
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["branch_id"] == second_branch
    assert launched[0][0] == [
        *monkey_prefix,
        "agent",
        "-r",
        second_branch,
        "codex/gpt-5.3-codex",
        "--prompt",
        "Use the alternate branch.",
    ]


def test_multiple_branches_require_explicit_selection(resume_harness):
    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    _run(
        _seed_run(
            db_path,
            run_id=run_id,
            branch_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
        )
    )

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue."},
    )

    assert response.status_code == 409, response.text
    assert "branch_id is required" in response.json()["detail"]
    assert launched == []


def test_branch_must_belong_to_run(resume_harness):
    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    foreign_run_id = str(uuid.uuid4())
    member_branch = str(uuid.uuid4())
    foreign_branch = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[member_branch]))
    _run(_seed_run(db_path, run_id=foreign_run_id, branch_ids=[foreign_branch]))

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue.", "branch_id": foreign_branch},
    )

    assert response.status_code == 422, response.text
    assert "does not belong" in response.json()["detail"]
    assert launched == []


def test_missing_run_returns_404(resume_harness):
    _svc, _db_path, client, launched = resume_harness

    response = client.post(
        f"/api/runs/{uuid.uuid4()}/resume",
        json={"instruction": "Continue."},
    )

    assert response.status_code == 404, response.text
    assert launched == []


def test_run_without_branch_returns_conflict(resume_harness):
    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[]))

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue."},
    )

    assert response.status_code == 409, response.text
    assert "no branch to resume" in response.json()["detail"]
    assert launched == []


def test_unresolved_installed_cli_returns_503(resume_harness):
    resume_svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[branch_id]))
    resume_svc._subprocess.resolve_li_executable = lambda: (
        None,
        "no console script registered",
    )

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue."},
    )

    assert response.status_code == 503, response.text
    assert "reinstall LionAGI with the Studio extra" in response.json()["detail"]
    assert "no console script registered" not in response.json()["detail"]
    assert launched == []


def test_missing_cli_branch_snapshot_returns_conflict_before_launch(resume_harness, monkeypatch):
    import lionagi.cli._runs as cli_runs

    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[branch_id]))

    def _missing_snapshot(_branch_id: str):
        raise FileNotFoundError("snapshot was pruned")

    monkeypatch.setattr(cli_runs, "find_branch", _missing_snapshot)

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue."},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"Branch {branch_id!r} has no available CLI snapshot and cannot be resumed"
    )
    assert launched == []


def test_get_and_post_agree_when_the_branch_snapshot_is_missing(resume_harness, monkeypatch):
    """GET (resume_availability) used to answer "resumable" off branch
    membership alone, never checking that the branch's CLI snapshot actually
    exists — the same prerequisite POST's dispatch enforces via
    _ensure_branch_snapshot_available and 409s on when missing. That let the
    button light up and then fail on click, the exact defect class this
    resume-dispatch work exists to kill. Both routes must now share the same
    prerequisite check, so a session with a resolvable branch but no snapshot
    reads as not-resumable before the button is ever offered, not just when
    it's pressed.
    """
    import lionagi.cli._runs as cli_runs

    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[branch_id]))

    def _missing_snapshot(_branch_id: str):
        raise FileNotFoundError("snapshot was pruned")

    monkeypatch.setattr(cli_runs, "find_branch", _missing_snapshot)

    get_response = client.get(f"/api/runs/{run_id}/resume")
    assert get_response.status_code == 200, get_response.text
    get_body = get_response.json()
    assert get_body["resumable"] is False
    assert get_body["reason"] == "snapshot_unavailable"

    post_response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue."},
    )
    assert post_response.status_code == 409, post_response.text

    # Same underlying check, same exception text — GET's explanation and
    # POST's refusal must describe the identical fact, not two different ones
    # that merely happen to both be negative.
    assert get_body["message"] == post_response.json()["detail"]
    assert launched == []


def test_corrupt_cli_branch_snapshot_returns_conflict_before_launch(
    resume_harness, monkeypatch, tmp_path
):
    import lionagi.cli._runs as cli_runs

    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[branch_id]))
    snapshot = tmp_path / f"{branch_id}.json"
    snapshot.write_text("{not-json")
    monkeypatch.setattr(
        cli_runs,
        "find_branch",
        lambda _branch_id: ("fixture-run", snapshot),
    )

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue."},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"Branch {branch_id!r} has an invalid CLI snapshot and cannot be resumed"
    )
    assert launched == []


def test_option_looking_instruction_cannot_become_bypass_flag(resume_harness):
    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[branch_id]))

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "--bypass is text to discuss, not a CLI option"},
    )

    assert response.status_code == 202, response.text
    assert launched[0][0][-1] == "--prompt=--bypass is text to discuss, not a CLI option"
    assert "--bypass" not in launched[0][0]


@pytest.mark.parametrize(
    "body",
    [
        {"instruction": "   "},
        {"instruction": "--"},
        {"instruction": "Continue.", "model": "--bypass"},
        {"instruction": "Continue.", "branch_id": "--foreign"},
    ],
)
def test_invalid_resume_input_returns_422_without_launch(resume_harness, body):
    _svc, db_path, client, launched = resume_harness
    run_id = str(uuid.uuid4())
    _run(_seed_run(db_path, run_id=run_id, branch_ids=[str(uuid.uuid4())]))

    response = client.post(f"/api/runs/{run_id}/resume", json=body)

    assert response.status_code == 422, response.text
    assert launched == []
