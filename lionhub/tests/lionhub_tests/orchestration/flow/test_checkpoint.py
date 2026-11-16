# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Checkpoint writer and checkpoint-location resolution contracts."

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionhub.orchestration.flow.checkpoint import (
    CheckpointWriter,
    FlowResumeError,
    load_checkpoint,
    resolve_checkpoint_target,
)
from lionhub.serve.cli.orchestrate._orchestration import start_live_persist, stop_live_persist
from tests._checkpoint_test_support import _minimal_real_env as _minimal_real_env
from tests._checkpoint_test_support import temp_db_path as temp_db_path
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


async def test_checkpoint_writer_record_writes_valid_schema_no_leftover_tmp(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(
        path=path,
        session_id="sess-1",
        prompt="do the thing",
        plan=[
            {
                "task": "do it",
                "assignee": "worker",
                "agent_id": "worker",
                "dep_indices": [],
            }
        ],
        config={"model_spec": "claude"},
    )

    await writer.record("worker", status="completed", response="result-1")

    assert path.exists()
    assert not list(tmp_path.glob("checkpoint.*.tmp"))

    data = load_checkpoint(path)
    assert data["version"] == 2
    assert data["session_id"] == "sess-1"
    assert data["prompt"] == "do the thing"
    assert data["ops"]["worker"] == {
        "agent_id": "worker",
        "status": "completed",
        "response": "result-1",
    }
    assert data["plan"][0]["agent_id"] == "worker"


async def test_checkpoint_writer_second_record_preserves_prior_ops(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="sess-1", prompt="p", plan=[], config={})

    await writer.record("worker-1", status="completed", response="r1")
    await writer.record("worker-2", status="failed", response=None)

    data = load_checkpoint(path)
    assert set(data["ops"]) == {"worker-1", "worker-2"}
    assert data["ops"]["worker-1"]["status"] == "completed"
    assert data["ops"]["worker-2"]["status"] == "failed"
    assert data["ops"]["worker-2"]["response"] is None


async def test_checkpoint_writer_concurrent_records_stay_valid_and_lose_nothing(tmp_path: Path):
    """asyncio.Lock around serialize-write-rename must serialize concurrent
    record() calls — no torn file, no leftover temp files, every op lands."""
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    await asyncio.gather(
        *(writer.record(f"agent-{i}", status="completed", response=i) for i in range(20))
    )

    assert not list(tmp_path.glob("checkpoint.*.tmp"))
    data = load_checkpoint(path)
    assert len(data["ops"]) == 20
    assert writer._seq == 20


async def test_checkpoint_writer_flush_persists_without_touching_ops(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(
        path=path,
        session_id="s",
        prompt="p",
        plan=[],
        config={},
        ops={"seeded": {"agent_id": "seeded", "status": "completed", "response": "x"}},
    )

    await writer.flush()

    data = load_checkpoint(path)
    assert data["ops"] == {"seeded": {"agent_id": "seeded", "status": "completed", "response": "x"}}


async def test_checkpoint_writer_record_flow_context_updates_running_snapshot(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    await writer.record("a", status="completed", response="r1", flow_context={"k": 1})
    await writer.record("b", status="completed", response="r2")  # no flow_context this time
    await writer.record("c", status="completed", response="r3", flow_context={"k": 1, "j": 2})

    data = load_checkpoint(path)
    # The middle record (no flow_context passed) must not reset it to {} --
    # only an explicit non-None flow_context updates the running snapshot.
    assert data["flow_context"] == {"k": 1, "j": 2}


async def test_checkpoint_writer_record_spawned_keeps_separate_from_ops_and_dedups_by_node_id(
    tmp_path: Path,
):
    """Spawned nodes use a separate `spawned` keyspace from planned `ops` (a spawned branch can share a planned agent_id's name); re-recording the same node id updates in place, and CHECKPOINT_VERSION 2 reconstruction fields round-trip, defaulting to None when omitted."""
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    await writer.record("critic", status="completed", response="planned-result")
    await writer.record_spawned("spawned-1", status="completed", response="child-result-v1")
    await writer.record_spawned(
        "spawned-1",
        status="completed",
        response="child-result-v2",
        operation="operate",
        assignee="critic",
        instruction="critique the draft",
        parent_id="node-0",
        spawn_id="spawn-3",
    )

    data = load_checkpoint(path)
    assert data["ops"] == {
        "critic": {"agent_id": "critic", "status": "completed", "response": "planned-result"}
    }
    assert data["spawned"] == [
        {
            "node_id": "spawned-1",
            "status": "completed",
            "response": "child-result-v2",
            "operation": "operate",
            "assignee": "critic",
            "instruction": "critique the draft",
            "parent_id": "node-0",
            "spawn_id": "spawn-3",
            "context": None,
        }
    ]


async def test_checkpoint_writer_record_spawned_captures_context_payload(tmp_path: Path):
    """context (added alongside CHECKPOINT_VERSION 2's other reconstruction
    fields) round-trips independently of instruction — a team round op's
    prior_team_messages lives in context, not instruction, so it must be
    captured the same way the other reconstruction fields are."""
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    context_payload = [
        {"original_task": "t"},
        {"prior_team_messages": {"total_count": 1, "messages": [{"from": "bob", "content": "hi"}]}},
    ]
    await writer.record_spawned(
        "spawned-2",
        status="completed",
        response="child-result",
        operation="operate",
        assignee="alice",
        instruction="Team follow-up round: teammates left you new message(s)...",
        parent_id="node-0",
        spawn_id="alice-round1",
        context=context_payload,
    )

    data = load_checkpoint(path)
    assert data["spawned"][0]["context"] == context_payload


async def test_resolve_checkpoint_target_exact_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionhub.orchestration.flow.checkpoint as ckmod

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "20260703T000000-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "s1",
                "prompt": "p",
                "plan": [],
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    resolved_run_dir, checkpoint = await resolve_checkpoint_target("20260703T000000-abc123")
    assert resolved_run_dir.run_id == "20260703T000000-abc123"
    assert checkpoint["session_id"] == "s1"


async def test_resolve_checkpoint_target_prefix_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionhub.orchestration.flow.checkpoint as ckmod

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "20260703T000000-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "s1",
                "prompt": "p",
                "plan": [],
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    resolved_run_dir, _checkpoint = await resolve_checkpoint_target("20260703")
    assert resolved_run_dir.run_id == "20260703T000000-abc123"


async def test_resolve_checkpoint_target_run_dir_without_checkpoint_falls_back_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    """A run dir that exists but never wrote a checkpoint (predates resume
    support, or never reached _build_dag) must not match — it falls through
    to the session-lookup path, and with nothing there either, refuses loudly."""
    import lionhub.orchestration.flow.checkpoint as ckmod

    runs_root = tmp_path / "runs"
    (runs_root / "no-checkpoint-run").mkdir(parents=True)
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    with pytest.raises(FlowResumeError):
        await resolve_checkpoint_target("no-checkpoint-run")


async def test_resolve_checkpoint_target_falls_back_to_session_run_id(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A session/invocation/play id with no matching run directory of its own
    resolves via its node_metadata run_id — the path a real `--resume <session-id>`
    invocation takes, as opposed to `--resume <run-id>`."""
    import lionhub.orchestration.flow.checkpoint as ckmod

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    env = _minimal_real_env()
    await start_live_persist(
        env, invocation_kind="flow", artifacts_path=str(tmp_path / "artifacts")
    )
    ctx = env._live_persist
    assert ctx is not None
    session_id = ctx["session_id"]

    run_dir_path = runs_root / "run-for-session"
    run_dir_path.mkdir(parents=True)
    (run_dir_path / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": session_id,
                "prompt": "p",
                "plan": [],
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )

    async with StateDB() as db:
        await db.update_session(session_id, node_metadata=json.dumps({"run_id": "run-for-session"}))

    resolved_run_dir, checkpoint = await resolve_checkpoint_target(session_id)
    assert resolved_run_dir.run_id == "run-for-session"
    assert checkpoint["session_id"] == session_id

    await stop_live_persist(env, status="completed")
