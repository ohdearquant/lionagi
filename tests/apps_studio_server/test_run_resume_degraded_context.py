# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""End-to-end degraded-context refusal, through the real detached-launch pipeline.

test_run_resume_dispatch.py's degraded-context coverage stubs
``launch_detached_argv`` itself, so it only proves the flag is appended (or
withheld) in the built argv — it never exercises what happens to a refusal
once a detached process actually runs and fails. This module keeps
``_launches.launch_detached_argv`` and ``_spawn_detached`` real (only the
OS-level subprocess spawn — ``spawn_and_wait`` — is stubbed, the same
boundary test_launches_api.py stubs at), so a checkpoint with multiple
pending inherited-context operations drives the full path: dispatch ->
detached launch -> subprocess failure -> ``_failure_reason_summary`` ->
persisted invocation row -> the summary a client would read.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _seed_flow_session(db_path: Path, *, session_id: str, cli_run_id: str) -> None:
    async with StateDB(db_path) as db:
        progression_id = f"{session_id}-progression"
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression_id,
                "name": f"run-{session_id}",
                "status": "completed",
                "invocation_kind": "flow",
                "node_metadata": {"run_id": cli_run_id},
            }
        )


# Two pending ops that need inherited conversational context resume cannot
# restore (v1 restores results-context only) -- the exact scenario
# `_apply_checkpoint_precompletion` (lionagi/cli/orchestrate/flow.py) refuses
# unless --allow-degraded-context is passed.
_PENDING_INHERIT_CONTEXT_OPS = ["reviewer-1", "reviewer-2"]


def _write_degraded_checkpoint(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    plan = [
        {
            "agent_id": op_id,
            "assignee": "worker",
            "dep_indices": [],
            "inherit_context": True,
        }
        for op_id in _PENDING_INHERIT_CONTEXT_OPS
    ]
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 2,
                "session_id": "irrelevant-to-studio",
                "prompt": "original prompt",
                "plan": plan,
                "flow_context": {},
                # Empty: neither op has completed/failed yet -> both are
                # still pending inherit_context ops from resume's view.
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )


def _refusal_message(pending: list[str]) -> str:
    """Mirrors the exact wording `_apply_checkpoint_precompletion` raises."""
    return (
        "Resume refused: pending op(s) "
        f"{', '.join(pending)} expect inherited conversational context "
        "that resume cannot restore (v1 restores results-context only). "
        "Pass --allow-degraded-context to run them against an empty "
        "branch instead."
    )


@pytest.fixture
def real_launch_harness(tmp_path: Path, monkeypatch: Any):
    """Same shape as flow_resume_harness (test_run_resume_dispatch.py) but
    leaves launch_detached_argv/_spawn_detached real; only the OS-level
    subprocess spawn is stubbed, driven by the checkpoint actually on disk so
    the refusal text is genuinely computed from the plan, not hardcoded."""
    import lionagi.cli.orchestrate._checkpoint as ckmod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.scheduler.subprocess as subprocess_mod
    import lionagi.studio.services.invocations as invocations_svc
    import lionagi.studio.services.run_resume as resume_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    monkeypatch.setattr(
        resume_svc._subprocess,
        "resolve_li_executable",
        lambda: (["/opt/lionagi/bin/li"], None),
    )

    async def _fake_spawn_and_wait(
        argv: list[str],
        invocation_id: str,
        *,
        tmp_path: str | None = None,
        cwd: str | None = None,
        action_kind: str | None = None,
    ) -> tuple[int, str]:
        # The real CLI reads the checkpoint off disk to decide whether
        # pending inherit_context ops block the resume; this stub does the
        # same read (against the checkpoint this test wrote), so the
        # decision is driven by the on-disk plan rather than by argv alone.
        target = argv[-1] if "--allow-degraded-context" not in argv else argv[-2]
        run_dir = runs_root / target
        if not run_dir.exists():
            # target was the Studio session id; resolve via the DB the same
            # way _checkpoint.resolve_checkpoint_target does for a
            # session/run id that isn't itself a run directory name.
            async with StateDB(db_path) as db:
                session = await db.get_session(target)
            cli_run_id = (session or {}).get("node_metadata", {}).get("run_id")
            run_dir = runs_root / cli_run_id
        checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
        ops = checkpoint.get("ops", {})
        pending = [
            entry["agent_id"]
            for entry in checkpoint.get("plan", [])
            if entry["agent_id"] not in ops and entry.get("inherit_context")
        ]
        allow_degraded = "--allow-degraded-context" in argv
        if pending and not allow_degraded:
            return 1, _refusal_message(pending)
        return 0, "flow resumed"

    monkeypatch.setattr(subprocess_mod, "spawn_and_wait", _fake_spawn_and_wait)

    from lionagi.studio.app import create_app

    # Entered as a context manager (unlike flow_resume_harness's bare
    # construction) so TestClient reuses ONE portal/background loop across
    # every .post() in the test instead of tearing one down -- and
    # cancelling whatever it left running -- after each individual request.
    # That reuse is required here because, unlike flow_resume_harness (which
    # fakes launch_detached_argv itself and never spawns a real background
    # task), this harness leaves the real detached-task machinery running
    # past the point the 202 response is returned.
    with TestClient(
        create_app(),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8765",
    ) as client:
        yield resume_svc, invocations_svc, db_path, runs_root, client


async def _await_invocation_terminal(inv_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Poll for the detached task's terminal DB write.

    TestClient runs the app (and the detached task it spawns) on its own
    background loop/thread, so a task object grabbed from this test's loop
    cannot be awaited directly -- polling the persisted row is the only
    cross-loop-safe way to wait for it.
    """
    import lionagi.studio.services.invocations as invocations_svc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        invocation = await invocations_svc.get_invocation(inv_id)
        if invocation is not None and invocation["status"] != "running":
            return invocation
        await asyncio.sleep(0.02)
    raise AssertionError(f"invocation {inv_id!r} did not reach a terminal status in {timeout}s")


def test_degraded_context_refusal_surfaces_named_ops_in_the_invocation(real_launch_harness):
    _svc, invocations_svc, db_path, runs_root, client = real_launch_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(_seed_flow_session(db_path, session_id=session_id, cli_run_id=cli_run_id))
    _write_degraded_checkpoint(runs_root / cli_run_id)

    # No allow_degraded_context: the default must never auto-degrade.
    response = client.post(f"/api/runs/{session_id}/resume", json={})
    assert response.status_code == 202, response.text
    inv_id = response.json()["invocation_id"]

    invocation = _run(_await_invocation_terminal(inv_id))
    assert invocation["status"] == "failed"
    summary = invocation["status_reason_summary"]
    for op_id in _PENDING_INHERIT_CONTEXT_OPS:
        assert op_id in summary, f"{op_id!r} missing from surfaced failure: {summary!r}"
    assert "inherited conversational context" in summary
    assert "empty branch" in summary
    assert "--allow-degraded-context" in summary


def test_explicit_degraded_context_opt_in_then_succeeds(real_launch_harness):
    _svc, invocations_svc, db_path, runs_root, client = real_launch_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(_seed_flow_session(db_path, session_id=session_id, cli_run_id=cli_run_id))
    _write_degraded_checkpoint(runs_root / cli_run_id)

    # First: default request is refused (asserted in detail above; re-drain
    # its task here only so it can't race the second launch's admission
    # guard, which keys on run_id regardless of allow_degraded_context).
    first = client.post(f"/api/runs/{session_id}/resume", json={})
    assert first.status_code == 202, first.text
    _run(_await_invocation_terminal(first.json()["invocation_id"]))

    # Second: the SAME request but with the explicit opt-in must succeed --
    # a second, distinct request the user made on purpose, not an automatic
    # retry with the flag silently added.
    second = client.post(
        f"/api/runs/{session_id}/resume",
        json={"allow_degraded_context": True},
    )
    assert second.status_code == 202, second.text
    inv_id = second.json()["invocation_id"]
    assert inv_id != first.json()["invocation_id"]

    invocation = _run(_await_invocation_terminal(inv_id))
    assert invocation["status"] == "completed"
