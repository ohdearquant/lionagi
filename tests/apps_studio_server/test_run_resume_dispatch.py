# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Invocation-kind-aware resume dispatch: POST /api/runs/{run_id}/resume.

Covers the behavior test_run_resume.py does not: routing by invocation_kind
to either the (untouched) agent path or a checkpointed flow-resume path, the
NULL/unknown-kind refusal, the no-checkpoint/empty-checkpoint third states,
degraded-context opt-in propagation, and that the resume:agent and
resume:flow active-resume guards never mask each other.
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


async def _seed_session(
    db_path: Path,
    *,
    session_id: str,
    invocation_kind: str | None,
    status: str = "completed",
    node_metadata: dict[str, Any] | None = None,
) -> None:
    async with StateDB(db_path) as db:
        progression_id = f"{session_id}-progression"
        await db.create_progression(progression_id)
        session: dict[str, Any] = {
            "id": session_id,
            "progression_id": progression_id,
            "name": f"run-{session_id}",
            "status": status,
            "invocation_kind": invocation_kind,
        }
        if node_metadata is not None:
            # create_session binds node_metadata through SQLAlchemy's JSON
            # type, which serializes a native object itself — passing an
            # already-json.dumps'd string here would double-encode it.
            session["node_metadata"] = node_metadata
        await db.create_session(session)


def _write_checkpoint(run_dir: Path, *, plan: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 2,
                "session_id": "irrelevant-to-studio",
                "prompt": "original prompt",
                "plan": plan,
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )


_SOME_PLAN = [{"agent_id": "worker-1", "assignee": "worker", "dep_indices": []}]


@pytest.fixture
def flow_resume_harness(tmp_path: Path, monkeypatch: Any):
    import lionagi.cli.orchestrate._checkpoint as ckmod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.run_resume as resume_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    launched: list[tuple[list[str], dict[str, Any]]] = []

    async def _fake_launch(argv: list[str], **kwargs: Any) -> str:
        launched.append((argv, kwargs))
        inv_id = f"resumeinv-{len(launched)}"
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": inv_id,
                    "skill": kwargs["skill"],
                    "plugin": kwargs["plugin"],
                    "prompt": kwargs["prompt"],
                    "started_at": time.time(),
                    "status": "running",
                    "node_metadata": kwargs.get("node_metadata"),
                }
            )
        return inv_id

    monkeypatch.setattr(resume_svc._launches, "launch_detached_argv", _fake_launch)
    monkeypatch.setattr(
        resume_svc._subprocess,
        "resolve_li_executable",
        lambda: (["/opt/lionagi/bin/li"], None),
    )

    import lionagi.cli._runs as cli_runs

    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()

    def _find_snapshot(branch_id: str):
        from lionagi.ln import json_dumps
        from lionagi.service.manager import iModel
        from lionagi.session.branch import Branch

        snapshot = snapshots / f"{branch_id}.json"
        if not snapshot.exists():
            branch = Branch(
                chat_model=iModel(provider="claude_code", model="sonnet", api_key="dummy")
            )
            serialized = branch.to_dict()
            serialized["id"] = branch_id
            snapshot.write_text(json_dumps(serialized))
        return ("fixture-run", snapshot)

    monkeypatch.setattr(cli_runs, "find_branch", _find_snapshot)

    from lionagi.studio.app import create_app

    client = TestClient(
        create_app(),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8765",
    )
    try:
        yield resume_svc, db_path, runs_root, client, launched
    finally:
        client.close()


# ── Parameterized dispatch across every invocation_kind ──────────────────────


@pytest.mark.parametrize("kind", ["play", "flow", "show-play", "fanout"])
def test_flow_kind_resume_dispatches_to_checkpoint_replay_argv(flow_resume_harness, kind):
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind=kind,
            node_metadata={"run_id": cli_run_id},
        )
    )
    _write_checkpoint(runs_root / cli_run_id, plan=_SOME_PLAN)

    response = client.post(f"/api/runs/{session_id}/resume", json={})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["run_id"] == session_id
    assert body["invocation_kind"] == kind
    assert body["checkpoint_run_id"] == cli_run_id
    assert len(launched) == 1
    argv, kwargs = launched[0]
    assert argv == [
        "/opt/lionagi/bin/li",
        "orchestrate",
        "flow",
        "--resume",
        session_id,
    ]
    assert kwargs["skill"] == "resume:flow"
    assert kwargs["action_kind"] == "flow"
    assert kwargs["prompt"] is None
    assert kwargs["node_metadata"]["run_id"] == session_id
    assert kwargs["node_metadata"]["invocation_kind"] == kind
    assert kwargs["node_metadata"]["allow_degraded_context"] is False


def test_agent_kind_resume_still_uses_the_untouched_agent_argv(flow_resume_harness):
    """Same harness, agent kind — the dispatcher must still hit the byte-for-byte agent path."""
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id=run_id, invocation_kind="agent"))

    async def _add_branch() -> None:
        async with StateDB(db_path) as db:
            branch_progression_id = f"{branch_id}-progression"
            await db.create_progression(branch_progression_id)
            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": 1.0,
                    "name": "branch-1",
                    "session_id": run_id,
                    "progression_id": branch_progression_id,
                    "model": "claude_code/sonnet",
                    "provider": "claude_code",
                }
            )

    _run(_add_branch())

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"instruction": "Continue with the next step."},
    )

    assert response.status_code == 202, response.text
    assert len(launched) == 1
    assert response.json() == {
        "run_id": run_id,
        "branch_id": branch_id,
        "invocation_id": "resumeinv-1",
    }
    argv, kwargs = launched[0]
    assert argv == [
        "/opt/lionagi/bin/li",
        "agent",
        "-r",
        branch_id,
        "--prompt",
        "Continue with the next step.",
    ]
    assert kwargs["skill"] == "resume:agent"
    assert kwargs["action_kind"] == "agent"


def test_null_invocation_kind_refuses_before_launch_with_actual_kind(flow_resume_harness):
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    run_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id=run_id, invocation_kind=None))

    response = client.post(f"/api/runs/{run_id}/resume", json={})

    assert response.status_code == 409, response.text
    assert "None" in response.json()["detail"]
    assert "does not support resume" in response.json()["detail"]
    assert launched == []


def test_unknown_invocation_kind_refuses_and_never_silently_picks_a_path():
    """The DB CHECK constraint (schema.sql) forbids any value outside the
    known vocabulary from ever landing in a real row, so the "arbitrary
    unknown kind" case is exercised directly against the dispatcher with a
    synthetic session dict rather than through the DB/HTTP layer."""
    import lionagi.studio.services.run_resume as resume_svc

    with pytest.raises(resume_svc.RunResumeUnsupportedKindError) as excinfo:
        _run(
            resume_svc._dispatch_resume_by_kind(
                "some-run",
                {"invocation_kind": "totally-unrecognized"},
                instruction=None,
                branch_id=None,
                model=None,
                allow_degraded_context=False,
            )
        )
    assert "totally-unrecognized" in str(excinfo.value)


# ── Request contract: flow kinds reject instruction/branch/model ─────────────


@pytest.mark.parametrize(
    "body",
    [
        {"instruction": "do something else"},
        {"branch_id": "some-branch"},
        {"model": "codex/gpt-5.3-codex"},
    ],
)
def test_flow_kind_rejects_agent_only_fields(flow_resume_harness, body):
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    _write_checkpoint(runs_root / cli_run_id, plan=_SOME_PLAN)

    response = client.post(f"/api/runs/{session_id}/resume", json=body)

    assert response.status_code == 422, response.text
    assert launched == []


# ── No-checkpoint / empty-checkpoint: a distinct third state ────────────────


def test_no_checkpoint_is_a_distinct_state_not_a_generic_launch_failure(flow_resume_harness):
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    # No checkpoint.json ever written for cli_run_id.

    response = client.post(f"/api/runs/{session_id}/resume", json={})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "no_checkpoint"
    assert "No checkpoint.json found" in detail["message"]
    assert launched == []


def test_empty_checkpoint_plan_is_distinguished_from_no_checkpoint(flow_resume_harness):
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    _write_checkpoint(runs_root / cli_run_id, plan=[])

    response = client.post(f"/api/runs/{session_id}/resume", json={})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "empty_checkpoint"
    assert launched == []


def test_unresolvable_target_reports_target_not_found(flow_resume_harness):
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    # node_metadata carries no run_id at all -> _resolve_any_target resolves
    # the session but _resolve_primary_session/run_id extraction fails.
    _run(_seed_session(db_path, session_id=session_id, invocation_kind="play"))

    response = client.post(f"/api/runs/{session_id}/resume", json={})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "no_run_id"
    assert launched == []


def test_ambiguous_checkpoint_target_is_a_distinct_state(flow_resume_harness):
    """A short-id-style collision under RUNS_ROOT must read as its own
    resumability state, not a generic 422/500 from an uncaught
    AmbiguousIdError (both AmbiguousIdError and json.JSONDecodeError are
    ValueError subclasses, so an uncaught one would previously have been
    swallowed by the route's generic ValueError -> 422 handler)."""
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id=session_id, invocation_kind="flow"))
    # Two run directories whose names both start with the raw session_id --
    # the id-prefix resolver has no unambiguous exact match to fall back to.
    (runs_root / f"{session_id}-a").mkdir(parents=True)
    (runs_root / f"{session_id}-b").mkdir(parents=True)

    response = client.post(f"/api/runs/{session_id}/resume", json={})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "ambiguous_target"
    assert launched == []


def test_invalid_checkpoint_json_is_a_distinct_state(flow_resume_harness):
    """A checkpoint.json that exists but fails to parse (truncated write,
    hand-edited, disk corruption) must not surface as an unhandled
    JSONDecodeError / generic 500, and must not be conflated with the
    no-checkpoint state."""
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    run_dir = runs_root / cli_run_id
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text("{this is not valid json")

    response = client.post(f"/api/runs/{session_id}/resume", json={})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "invalid_checkpoint"
    assert launched == []


# ── Resumability precheck: GET /runs/{run_id}/resume ─────────────────────────


def test_resume_availability_agent_kind_reports_the_branch(flow_resume_harness):
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id=run_id, invocation_kind="agent"))

    async def _add_branch() -> None:
        async with StateDB(db_path) as db:
            branch_progression_id = f"{branch_id}-progression"
            await db.create_progression(branch_progression_id)
            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": 1.0,
                    "name": "branch-1",
                    "session_id": run_id,
                    "progression_id": branch_progression_id,
                    "model": "claude_code/sonnet",
                    "provider": "claude_code",
                }
            )

    _run(_add_branch())

    response = client.get(f"/api/runs/{run_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "run_id": run_id,
        "invocation_kind": "agent",
        "resumable": True,
        "branch_id": branch_id,
    }
    assert launched == []


def test_resume_availability_agent_kind_with_no_branch_is_explicit(flow_resume_harness):
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    run_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id=run_id, invocation_kind="agent"))

    response = client.get(f"/api/runs/{run_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumable"] is False
    assert body["reason"] == "branch_conflict"
    assert launched == []


def test_resume_availability_flow_kind_reports_the_checkpoint(flow_resume_harness):
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    _write_checkpoint(runs_root / cli_run_id, plan=_SOME_PLAN)

    response = client.get(f"/api/runs/{session_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "run_id": session_id,
        "invocation_kind": "flow",
        "resumable": True,
        "checkpoint_run_id": cli_run_id,
    }
    assert launched == []


def test_resume_availability_flow_kind_no_checkpoint_is_explicit(flow_resume_harness):
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )

    response = client.get(f"/api/runs/{session_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumable"] is False
    assert body["reason"] == "no_checkpoint"
    assert launched == []


def test_resume_availability_null_kind_is_explicit_unsupported(flow_resume_harness):
    _svc, db_path, _runs_root, client, launched = flow_resume_harness
    run_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id=run_id, invocation_kind=None))

    response = client.get(f"/api/runs/{run_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumable"] is False
    assert body["reason"] == "unsupported_kind"
    assert launched == []


def test_resume_availability_unknown_run_404s(flow_resume_harness):
    _svc, _db_path, _runs_root, client, _launched = flow_resume_harness

    response = client.get(f"/api/runs/{uuid.uuid4()}/resume")

    assert response.status_code == 404, response.text


# ── Degraded-context opt-in ───────────────────────────────────────────────────


def test_allow_degraded_context_is_never_defaulted_and_only_set_on_opt_in(flow_resume_harness):
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    _write_checkpoint(runs_root / cli_run_id, plan=_SOME_PLAN)

    default_response = client.post(f"/api/runs/{session_id}/resume", json={})
    assert default_response.status_code == 202, default_response.text
    assert "--allow-degraded-context" not in launched[0][0]
    assert launched[0][1]["node_metadata"]["allow_degraded_context"] is False


def test_explicit_opt_in_appends_the_flag(flow_resume_harness):
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    _write_checkpoint(runs_root / cli_run_id, plan=_SOME_PLAN)

    response = client.post(
        f"/api/runs/{session_id}/resume",
        json={"allow_degraded_context": True},
    )

    assert response.status_code == 202, response.text
    assert launched[0][0][-1] == "--allow-degraded-context"
    assert launched[0][1]["node_metadata"]["allow_degraded_context"] is True


# ── Guard isolation: resume:agent and resume:flow never mask each other ──────


def test_agent_and_flow_active_resume_guards_do_not_mask_each_other(flow_resume_harness):
    _svc, db_path, runs_root, client, launched = flow_resume_harness

    agent_run_id = str(uuid.uuid4())
    agent_branch_id = str(uuid.uuid4())
    flow_session_id = str(uuid.uuid4())
    flow_cli_run_id = f"cli-run-{uuid.uuid4()}"

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            agent_progression = f"{agent_run_id}-progression"
            await db.create_progression(agent_progression)
            await db.create_session(
                {
                    "id": agent_run_id,
                    "progression_id": agent_progression,
                    "name": "agent-run",
                    "status": "completed",
                    "invocation_kind": "agent",
                }
            )
            branch_progression = f"{agent_branch_id}-progression"
            await db.create_progression(branch_progression)
            await db.create_branch(
                {
                    "id": agent_branch_id,
                    "created_at": 1.0,
                    "name": "branch-1",
                    "session_id": agent_run_id,
                    "progression_id": branch_progression,
                    "model": "claude_code/sonnet",
                    "provider": "claude_code",
                }
            )
            flow_progression = f"{flow_session_id}-progression"
            await db.create_progression(flow_progression)
            await db.create_session(
                {
                    "id": flow_session_id,
                    "progression_id": flow_progression,
                    "name": "flow-run",
                    "status": "completed",
                    "invocation_kind": "flow",
                    "node_metadata": {"run_id": flow_cli_run_id},
                }
            )
            # An active resume:agent invocation for the agent branch.
            await db.create_invocation(
                {
                    "id": "active-agent-inv",
                    "skill": "resume:agent",
                    "plugin": "studio_run_resume",
                    "prompt": "Continue with the next step.",
                    "started_at": time.time(),
                    "status": "running",
                    "node_metadata": {
                        "run_id": agent_run_id,
                        "branch_id": agent_branch_id,
                        "resume": True,
                        "queued_for_terminal": False,
                        "model": None,
                    },
                }
            )
            # An active resume:flow invocation for the flow session.
            await db.create_invocation(
                {
                    "id": "active-flow-inv",
                    "skill": "resume:flow",
                    "plugin": "studio_run_resume",
                    "prompt": None,
                    "started_at": time.time(),
                    "status": "running",
                    "node_metadata": {
                        "run_id": flow_session_id,
                        "invocation_kind": "flow",
                        "resume": True,
                        "allow_degraded_context": False,
                        "checkpoint_run_id": flow_cli_run_id,
                    },
                }
            )

    _run(_seed())
    _write_checkpoint(runs_root / flow_cli_run_id, plan=_SOME_PLAN)

    # Coalesces to the existing agent invocation — unaffected by the
    # unrelated resume:flow row for a different source.
    agent_response = client.post(
        f"/api/runs/{agent_run_id}/resume",
        json={"instruction": "Continue with the next step."},
    )
    assert agent_response.status_code == 202, agent_response.text
    assert agent_response.json()["invocation_id"] == "active-agent-inv"

    # Coalesces to the existing flow invocation — unaffected by the
    # unrelated resume:agent row for a different source.
    flow_response = client.post(f"/api/runs/{flow_session_id}/resume", json={})
    assert flow_response.status_code == 202, flow_response.text
    assert flow_response.json()["invocation_id"] == "active-flow-inv"

    # Neither request actually launched a new subprocess: both coalesced.
    assert launched == []


def test_guard_helpers_stay_isolated_when_target_identity_overlaps(flow_resume_harness):
    """MINOR-5: the route-level test above only proves two UNRELATED
    identifiers don't coalesce. This exercises both admission-guard helpers
    directly against the SAME logical target identity across the two
    skills, so a bug that queried/matched `_active_flow_resume` through the
    `resume:agent` skill (or vice versa) fails this test even though the
    identifiers themselves overlap."""
    svc, db_path, _runs_root, _client, _launched = flow_resume_harness
    shared_id = str(uuid.uuid4())

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "agent-inv",
                    "skill": "resume:agent",
                    "plugin": "studio_run_resume",
                    "prompt": "Continue with the next step.",
                    "started_at": time.time(),
                    "status": "running",
                    # Both identity fields carry the SAME value a mis-keyed
                    # query could accidentally match against.
                    "node_metadata": {"branch_id": shared_id, "run_id": shared_id},
                }
            )
            await db.create_invocation(
                {
                    "id": "flow-inv",
                    "skill": "resume:flow",
                    "plugin": "studio_run_resume",
                    "prompt": None,
                    "started_at": time.time(),
                    "status": "running",
                    "node_metadata": {"run_id": shared_id, "invocation_kind": "flow"},
                }
            )

    _run(_seed())

    agent_active = _run(svc._active_resume_for_branch(shared_id))
    assert agent_active is not None
    assert agent_active["id"] == "agent-inv"

    flow_active = _run(svc._active_flow_resume(shared_id))
    assert flow_active is not None
    assert flow_active["id"] == "flow-inv"


def test_conflicting_flow_resume_while_active_returns_409(flow_resume_harness):
    _svc, db_path, runs_root, client, launched = flow_resume_harness
    session_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    _run(
        _seed_session(
            db_path,
            session_id=session_id,
            invocation_kind="flow",
            node_metadata={"run_id": cli_run_id},
        )
    )
    _write_checkpoint(runs_root / cli_run_id, plan=_SOME_PLAN)

    async def _seed_active() -> None:
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "active-flow-inv",
                    "skill": "resume:flow",
                    "plugin": "studio_run_resume",
                    "prompt": None,
                    "started_at": time.time(),
                    "status": "running",
                    "node_metadata": {
                        "run_id": session_id,
                        "invocation_kind": "flow",
                        "resume": True,
                        "allow_degraded_context": False,
                        "checkpoint_run_id": cli_run_id,
                    },
                }
            )

    _run(_seed_active())

    # Different allow_degraded_context than the active row -> conflict, not coalesce.
    response = client.post(
        f"/api/runs/{session_id}/resume",
        json={"allow_degraded_context": True},
    )

    assert response.status_code == 409, response.text
    assert "already has a flow resume in progress" in response.json()["detail"]
    assert launched == []
