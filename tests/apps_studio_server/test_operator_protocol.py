# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the durable ADR-0083 Studio Operator protocol."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from starlette.requests import Request

from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.permission_mcp import request_permission as mcp_permission
from lionagi.studio.operator.store import (
    OperatorAuditUnavailableError,
    OperatorConflictError,
    OperatorStore,
)
from lionagi.studio.operator.types import OperatorEngineEvent


class ScriptedEngine:
    async def _stream(self, _turn):
        yield OperatorEngineEvent(
            "text",
            {"content": "first ", "format": "plain", "role": "assistant"},
        )
        yield OperatorEngineEvent(
            "text",
            {"content": "second", "format": "plain", "role": "assistant"},
        )

    def stream(self, turn):
        return self._stream(turn)


class BlockingEngine:
    async def _stream(self, _turn):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    def stream(self, turn):
        return self._stream(turn)


class PermissionEngine:
    def __init__(self, command_type: str = "provider_permission") -> None:
        self.command_type = command_type

    async def _stream(self, turn):
        decision = await turn.request_permission(
            self.command_type,
            {"toolName": "Bash", "input": {"command": "git status"}, "toolUseId": "t1"},
            "execute",
            "Allow Bash for this turn",
        )
        yield OperatorEngineEvent(
            "text",
            {
                "content": "allowed" if decision.allowed else "denied",
                "format": "plain",
                "role": "assistant",
            },
        )

    def stream(self, turn):
        return self._stream(turn)


class NativePermissionEngine(PermissionEngine):
    async def _stream(self, turn):
        decision = await turn.request_permission(
            "provider_permission",
            {"toolName": "Bash", "input": {"command": "git status"}, "toolUseId": "t1"},
            "execute",
            "Allow Bash for this turn",
        )
        if decision.allowed:
            yield OperatorEngineEvent(
                "tool_result",
                {
                    "callId": "t1",
                    "ok": True,
                    "result": {"nativeToolCompleted": True},
                },
            )


class UiEffectEngine:
    async def _stream(self, _turn):
        yield OperatorEngineEvent(
            "ui_command",
            {
                "effect": {
                    "kind": "navigate",
                    "space": "history",
                    "params": {"status": "failed"},
                }
            },
        )

    def stream(self, turn):
        return self._stream(turn)


def test_real_operator_branch_exposes_only_strict_request_scoped_mcp_tools(tmp_path):
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    branch = build_operator_branch(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="inspect recent failures",
            context={},
            history=(),
            request_permission=request_permission,
            store_path=str(tmp_path / "state.db"),
        )
    )
    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs["permission_mode"] == "default"
    assert kwargs["strict_mcp_config"] is True
    assert kwargs.get("allow_dangerously_skip_permissions") is not True
    assert set(kwargs["mcp_servers"]) == {"studio_permission", "studio_operator"}
    assert kwargs["permission_prompt_tool_name"] == ("mcp__studio_permission__request_permission")
    assert set(kwargs["allowed_tools"]) == {
        "mcp__studio_operator__list_recent_runs",
        "mcp__studio_operator__navigate",
        "mcp__studio_operator__prefill_schedule",
        "mcp__studio_operator__launch_playbook",
    }
    for server in kwargs["mcp_servers"].values():
        assert "LIONAGI_STUDIO_AUTH_TOKEN" not in server["env"]
        assert "LIONAGI_STUDIO_HUMAN_TOKEN" not in server["env"]


@pytest.mark.asyncio
async def test_application_mcp_read_query_is_bounded_and_redacted(monkeypatch):
    from lionagi.studio.operator.application_mcp import list_recent_runs
    from lionagi.studio.services import runs as runs_service

    observed = {}

    async def fake_list_runs(*, status, limit, offset):
        observed.update(status=status, limit=limit, offset=offset)
        return [
            {
                "id": "run-1",
                "agent_name": "Operator",
                "status": "failed",
                "project": "/Users/example/private",
                "started_at": 1.0,
                "ended_at": 2.0,
                "prompt": "must not leave the service",
                "artifacts_path": "/secret/path",
            },
            {
                "id": "run-2",
                "agent_name": "Researcher",
                "status": "failed",
                "project": "acme/research",
                "started_at": 3.0,
                "ended_at": 4.0,
            },
        ]

    monkeypatch.setattr(runs_service, "list_runs", fake_list_runs)
    result = await list_recent_runs({"limit": 2, "status": "failed"})
    assert observed == {"status": "failed", "limit": 2, "offset": 0}
    assert result == {
        "runs": [
            {
                "id": "run-1",
                "agentName": "Operator",
                "status": "failed",
                "project": "private",
                "startedAt": 1.0,
                "endedAt": 2.0,
                "href": "/runs/run-1",
            },
            {
                "id": "run-2",
                "agentName": "Researcher",
                "status": "failed",
                "project": "acme/research",
                "startedAt": 3.0,
                "endedAt": 4.0,
                "href": "/runs/run-2",
            },
        ],
        "count": 2,
        "bounded": True,
    }


@pytest.mark.asyncio
async def test_application_mcp_effects_are_typed_durable_and_client_acknowledged(
    tmp_path, monkeypatch
):
    from lionagi.studio.operator.application_mcp import (
        _dispatch,
        navigate,
        prefill_schedule,
    )

    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="drive the UI",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    navigation = await navigate({"space": "history", "status": "failed"})
    prefill = await prefill_schedule(
        {
            "name": "Daily triage",
            "cron": "0 9 * * *",
            "prompt": "Review recent failures",
        }
    )
    assert navigation["status"] == prefill["status"] == "pending"
    frames = await store.list_frames(cid)
    effects = [frame["payload"]["effect"] for frame in frames if frame["type"] == "ui_command"]
    assert effects == [
        {
            "id": navigation["effectId"],
            "kind": "navigate",
            "space": "history",
            "params": {"status": "failed"},
        },
        {
            "id": prefill["effectId"],
            "kind": "prefill",
            "form": "schedule",
            "values": {
                "name": "Daily triage",
                "cron": "0 9 * * *",
                "prompt": "Review recent failures",
                "description": "",
            },
        },
    ]
    invalid = await _dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "navigate",
                "arguments": {
                    "space": "history",
                    "raw_url": "https://attacker.invalid",
                },
            },
        }
    )
    assert invalid["result"]["isError"] is True
    assert navigation["effectId"] != prefill["effectId"]
    assert (
        await store.acknowledge_effect(
            cid,
            navigation["effectId"],
            status="applied",
            rejection_code=None,
        )
    ) == {"effectId": navigation["effectId"], "status": "applied"}
    await store.finish_turn(accepted["requestId"], outcome="completed")


@pytest.mark.asyncio
async def test_application_mcp_launch_blocks_on_real_durable_human_proposal(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.application_mcp import launch_playbook
    from lionagi.studio.services import playbooks as playbooks_service

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    playbooks_root = tmp_path / "playbooks"
    playbooks_root.mkdir()
    monkeypatch.setattr(playbooks_service, "_PLAYBOOKS_ROOT", playbooks_root)
    (playbooks_root / "daily-triage.playbook.yaml").write_text(
        "description: Daily triage\nsteps: []\n"
    )
    async with StateDB():
        pass
    calls = []

    async def execute(command_type, command):
        calls.append((command_type, command))
        return {
            "invocation_id": "inv-1",
            "action_kind": "play",
            "href": "/invocations/inv-1",
        }

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(
        store=store,
        engine_factory=ScriptedEngine,
        command_executor=execute,
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="launch the safe playbook",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    task = asyncio.create_task(
        launch_playbook({"playbook": "daily-triage", "note": "review first"})
    )
    proposal = await _wait_proposal(store, accepted["requestId"])
    assert not task.done()
    assert proposal["commandType"] == "launch"
    assert proposal["command"] == {
        "action_kind": "play",
        "action_playbook": "daily-triage",
    }
    assert proposal["targetVersion"].startswith("sha256:")
    proposal_frame = await _wait_frame(store, cid, frame_type="proposal")
    assert proposal_frame["payload"]["proposal"]["target"] == {
        "kind": "playbook",
        "id": "daily-triage",
        "version": proposal["targetVersion"],
    }
    assert "endpoint" not in proposal["command"]
    assert "command" not in proposal["command"]

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)
    assert decision["status"] == "succeeded"
    assert calls == [
        (
            "launch",
            {
                "action_kind": "play",
                "action_playbook": "daily-triage",
            },
        )
    ]
    assert result == {
        "status": "succeeded",
        "proposalId": proposal["id"],
        "result": {
            "invocation_id": "inv-1",
            "action_kind": "play",
            "href": "/invocations/inv-1",
        },
        "errorCode": None,
    }
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_application_mcp_playbook_mutation_after_proposal_conflicts_before_execution(
    tmp_path, monkeypatch
):
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.application_mcp import launch_playbook
    from lionagi.studio.services import playbooks as playbooks_service

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    playbooks_root = tmp_path / "playbooks"
    playbooks_root.mkdir()
    monkeypatch.setattr(playbooks_service, "_PLAYBOOKS_ROOT", playbooks_root)
    playbook_path = playbooks_root / "daily-triage.playbook.yaml"
    playbook_path.write_text("description: First approved version\nsteps: []\n")
    async with StateDB():
        pass
    calls = []

    async def execute(command_type, command):
        calls.append((command_type, command))
        return {"invocation_id": "must-not-run"}

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(
        store=store,
        engine_factory=ScriptedEngine,
        command_executor=execute,
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="launch exactly the version I approve",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    task = asyncio.create_task(launch_playbook({"playbook": "daily-triage"}))
    proposal = await _wait_proposal(store, accepted["requestId"])
    approved_version = proposal["targetVersion"]
    playbook_path.write_text("description: Mutated after rendering\nsteps: []\n")

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=approved_version,
    )
    result = await asyncio.wait_for(task, timeout=2)
    assert calls == []
    assert decision["status"] == "conflict"
    assert decision["error"]["code"] == "stale_context"
    assert result["status"] == "conflict"
    assert result["errorCode"] == "stale_context"
    frames = await store.list_frames(cid)
    failed_result = next(
        frame
        for frame in frames
        if frame["type"] == "tool_result" and frame["payload"].get("callId") == proposal["id"]
    )
    assert failed_result["payload"]["ok"] is False
    assert failed_result["payload"]["error"]["code"] == "stale_context"
    assert await _audit_decisions(proposal["id"]) == ["confirmed", "failed"]
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


async def _wait_done(
    store: OperatorStore, conversation_id: str, *, timeout: float = 5
) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = await store.list_frames(conversation_id)
        if any(frame["type"] == "done" for frame in frames):
            return frames
        await asyncio.sleep(0.01)
    raise TimeoutError("Operator turn did not finish")


async def _wait_proposal(store: OperatorStore, request_id: str, *, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await store.list_proposals_for_request(request_id)
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise TimeoutError("Operator proposal did not appear")


async def _wait_frame(
    store: OperatorStore,
    conversation_id: str,
    *,
    frame_type: str,
    timeout: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = await store.list_frames(conversation_id)
        match = next((frame for frame in frames if frame["type"] == frame_type), None)
        if match is not None:
            return match
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Operator {frame_type!r} frame did not appear")


async def _audit_decisions(proposal_id: str) -> list[str]:
    from lionagi.state.db import StateDB

    async with StateDB(readonly=True) as db:
        events = await db.list_admin_events(target_id=proposal_id)
    details = [
        json.loads(event["details"]) if isinstance(event["details"], str) else event["details"]
        for event in events
    ]
    return [detail["decision"] for detail in reversed(details)]


def _patch_state_db(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import lionagi.cli._runs as runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(sessions_mod, "_DB", str(path))
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", path.parent / "runs")


@pytest.mark.asyncio
async def test_store_is_restart_durable_monotonic_and_single_active(tmp_path):
    path = tmp_path / "state.db"
    store = OperatorStore(path)
    conversation = await store.create_conversation(title="Persistent")
    cid = conversation["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="hello",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    with pytest.raises(OperatorConflictError):
        await store.submit_turn(
            cid,
            instruction="racing",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=1,
        )
    assert await store.mark_running(accepted["requestId"])
    for index in range(5):
        await store.append_frame(
            cid,
            accepted["requestId"],
            "text",
            {"content": str(index), "format": "plain", "role": "assistant"},
        )
    await store.finish_turn(accepted["requestId"], outcome="completed")

    reopened = OperatorStore(path)
    frames = await reopened.list_frames(cid)
    assert [frame["sequence"] for frame in frames] == list(range(1, 8))
    assert frames[0]["payload"]["role"] == "user"
    assert frames[-1]["payload"] == {"outcome": "completed", "lastSequence": 7}
    page_one = await reopened.list_frames(cid, after_sequence=0, limit=3)
    page_two = await reopened.list_frames(cid, after_sequence=page_one[-1]["sequence"], limit=3)
    assert [f["sequence"] for f in page_one + page_two] == [1, 2, 3, 4, 5, 6]
    coordinator = OperatorCoordinator(store=reopened, engine_factory=ScriptedEngine)
    await coordinator.startup()
    snapshot = await coordinator.snapshot(cid, after_sequence=0, limit=3)
    assert snapshot["hasMore"] is True
    assert snapshot["nextAfterSequence"] == 3
    assert snapshot["latestSequence"] == 7


@pytest.mark.asyncio
async def test_default_store_reinitializes_schema_when_database_file_changes(
    tmp_path,
    monkeypatch,
):
    """A process-global store must not carry schema readiness across test/DB files."""
    import lionagi.state.db as state_db_mod

    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    store = OperatorStore()

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", first_path)
    first = await store.create_conversation(title="First")
    assert (await store.get_conversation(first["id"]))["title"] == "First"

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", second_path)
    second = await store.create_conversation(title="Second")
    assert (await store.get_conversation(second["id"]))["title"] == "Second"
    assert first_path.is_file()
    assert second_path.is_file()


@pytest.mark.asyncio
async def test_proposal_idempotency_key_rejects_changed_command(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="write the approved content",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    kwargs = {
        "conversation_id": cid,
        "request_id": accepted["requestId"],
        "command_type": "provider_permission",
        "risk": "mutate",
        "summary": "Allow Write for this Operator turn",
        "idempotency_key": "provider:fixed",
    }
    original = await store.create_proposal(
        **kwargs,
        command={
            "toolName": "Write",
            "input": {"file_path": "notes.txt", "content": "approved"},
            "toolUseId": "native-1",
        },
    )
    replay = await store.create_proposal(
        **kwargs,
        command={
            "toolName": "Write",
            "input": {"file_path": "notes.txt", "content": "approved"},
            "toolUseId": "native-1",
        },
    )
    assert replay["id"] == original["id"]

    with pytest.raises(OperatorConflictError, match="different Operator proposal"):
        await store.create_proposal(
            **kwargs,
            command={
                "toolName": "Write",
                "input": {"file_path": "notes.txt", "content": "unreviewed"},
                "toolUseId": "native-1",
            },
        )


@pytest.mark.asyncio
async def test_scripted_turn_streams_and_is_visible_as_canonical_run(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await coordinator.startup()
    snapshot = await coordinator.create_conversation(title="Canonical")
    cid = snapshot["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="run it",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames = await _wait_done(coordinator.store, cid)
    assert [f["sequence"] for f in frames] == list(range(1, len(frames) + 1))
    assert sum(frame["type"] == "done" for frame in frames) == 1
    link = next(
        frame
        for frame in frames
        if frame["type"] == "tool_result"
        and isinstance(frame["payload"].get("result"), dict)
        and frame["payload"]["result"].get("runId")
    )
    run_id = link["payload"]["result"]["runId"]
    branch_id = link["payload"]["result"]["branchId"]

    from lionagi.cli._runs import find_branch
    from lionagi.session.branch import Branch
    from lionagi.state.db import StateDB
    from lionagi.studio.services.run_resume import (
        _ensure_branch_snapshot_available,
        _resolve_branch,
    )

    # The Operator's canonical Run is not display-only: the exact branch
    # and DB run mapping consumed by `li agent -r` already exist.
    async with StateDB(readonly=True) as db:
        session = await db.get_session(run_id)
        db_branches = await db.list_branches(run_id)
    assert session is not None
    assert [row["id"] for row in db_branches] == [branch_id]
    assert await _resolve_branch(run_id, None) == branch_id
    await _ensure_branch_snapshot_available(branch_id)
    snapshot_run_id, snapshot_path = find_branch(branch_id)
    assert snapshot_run_id == session["run_id"]
    serialized = json.loads(snapshot_path.read_text())
    assert str(Branch.from_dict(serialized).id) == branch_id
    request_kwargs = serialized["chat_model"]["endpoint"]["config"]["kwargs"]
    assert "permission_prompt_tool_name" not in request_kwargs
    assert "strict_mcp_config" not in request_kwargs
    assert "setting_sources" not in request_kwargs
    assert "allowed_tools" not in request_kwargs
    assert "studio_permission" not in request_kwargs.get("mcp_servers", {})
    assert "studio_operator" not in request_kwargs.get("mcp_servers", {})

    from lionagi.studio.services.runs import list_runs

    runs = await list_runs(limit=50, offset=0)
    run = next(item for item in runs if item["id"] == run_id)
    assert run["agent_name"] == "Operator"
    assert (await coordinator.store.get_turn(accepted["requestId"]))["status"] == "completed"
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_missing_claude_cli_finishes_with_public_provider_fix(tmp_path, monkeypatch):
    from lionagi.providers.anthropic import claude_code
    from lionagi.studio.operator.engine import BranchOperatorEngine

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.setattr(claude_code, "CLAUDE_CLI", None)
    coordinator = OperatorCoordinator(
        store=OperatorStore(path), engine_factory=BranchOperatorEngine
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="inspect this project",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames = await _wait_done(coordinator.store, cid)
    error = next(frame for frame in frames if frame["type"] == "error")
    assert error["payload"]["error"] == {
        "code": "provider_unavailable",
        "message": (
            "Claude Code CLI is unavailable. Install it with "
            "`npm install -g @anthropic-ai/claude-code`, then run "
            "`claude auth login`."
        ),
        "retryable": False,
    }
    assert frames[-1]["payload"]["outcome"] == "failed"
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_concurrent_allow_claims_and_executes_application_command_once(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    calls = 0
    executing = asyncio.Event()
    release = asyncio.Event()

    async def execute(_command_type, _command):
        nonlocal calls
        calls += 1
        executing.set()
        await release.wait()
        return {"href": "/runs/child", "run_id": "child"}

    coordinator = OperatorCoordinator(
        store=OperatorStore(path),
        engine_factory=lambda: PermissionEngine("launch"),
        command_executor=execute,
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="launch once",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    proposal = await _wait_proposal(coordinator.store, accepted["requestId"])

    original_decide = coordinator.store.decide_proposal

    async def audit_down(*_args, **_kwargs):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(coordinator.store, "decide_proposal", audit_down)
    with pytest.raises(OperatorAuditUnavailableError):
        await coordinator.decide(
            cid,
            proposal["id"],
            allow=True,
            expected_command_hash=proposal["commandHash"],
            expected_target_version=None,
        )
    assert calls == 0
    assert (await coordinator.store.get_proposal(proposal["id"]))["status"] == "pending"
    monkeypatch.setattr(coordinator.store, "decide_proposal", original_decide)

    # Validation precedes the audit/claim transaction.
    with pytest.raises(OperatorConflictError):
        await coordinator.decide(
            cid,
            proposal["id"],
            allow=True,
            expected_command_hash="0" * 64,
            expected_target_version=None,
        )
    from lionagi.state.db import StateDB

    async with StateDB(readonly=True) as db:
        assert await db.list_admin_events(target_id=proposal["id"]) == []

    first = asyncio.create_task(
        coordinator.decide(
            cid,
            proposal["id"],
            allow=True,
            expected_command_hash=proposal["commandHash"],
            expected_target_version=None,
        )
    )
    await asyncio.wait_for(executing.wait(), timeout=2)
    second = await asyncio.wait_for(
        coordinator.decide(
            cid,
            proposal["id"],
            allow=True,
            expected_command_hash=proposal["commandHash"],
            expected_target_version=None,
        ),
        timeout=2,
    )
    assert calls == 1
    assert second["status"] == "executing"
    release.set()
    first_result = await asyncio.wait_for(first, timeout=2)
    assert first_result["status"] == "succeeded"
    assert calls == 1

    async with StateDB(readonly=True) as db:
        audits = await db.list_admin_events(target_id=proposal["id"])
    for event in audits:
        if isinstance(event["details"], str):
            event["details"] = json.loads(event["details"])
    assert {event["actor"] for event in audits} == {"studio_operator"}
    assert {event["details"]["decision"] for event in audits} == {
        "confirmed",
        "executed",
    }
    required_keys = {
        "conversation_id",
        "request_id",
        "proposal_id",
        "command_type",
        "command_hash",
        "target",
        "risk",
        "idempotency_key",
        "decision",
        "result",
        "error_code",
        "confirmed_at",
        "completed_at",
    }
    assert all(set(event["details"]) == required_keys for event in audits)
    await _wait_done(coordinator.store, cid)
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_ui_effect_is_persisted_before_frame_and_ack_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=UiEffectEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="show failed runs",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames = await _wait_done(coordinator.store, cid)
    effect_frame = next(frame for frame in frames if frame["type"] == "ui_command")
    effect = effect_frame["payload"]["effect"]
    assert effect["kind"] == "navigate"
    assert effect["id"]

    first = await coordinator.store.acknowledge_effect(
        cid, effect["id"], status="applied", rejection_code=None
    )
    repeated = await coordinator.store.acknowledge_effect(
        cid, effect["id"], status="applied", rejection_code=None
    )
    assert first == repeated == {"effectId": effect["id"], "status": "applied"}
    with pytest.raises(OperatorConflictError):
        await coordinator.store.acknowledge_effect(
            cid, effect["id"], status="rejected", rejection_code="client_error"
        )
    await coordinator.shutdown()


def test_allow_decision_requires_the_rendered_command_hash():
    from pydantic import ValidationError

    from lionagi.studio.operator.types import DecideProposalRequest

    with pytest.raises(ValidationError):
        DecideProposalRequest(decision="allow")
    assert DecideProposalRequest(decision="deny").expected_command_hash is None


@pytest.mark.asyncio
async def test_confirm_route_threads_the_rendered_target_version(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from lionagi.studio.operator.types import ConfirmProposalRequest
    from lionagi.studio.services import operator as operator_svc

    coordinator = MagicMock()
    coordinator.decide = AsyncMock(return_value={"status": "succeeded"})
    require_human = AsyncMock()
    monkeypatch.setattr(operator_svc, "get_operator_coordinator", lambda: coordinator)
    monkeypatch.setattr(operator_svc, "_require_human", require_human)
    command_hash = "a" * 64
    target_version = "sha256:rendered-playbook"
    body = ConfirmProposalRequest.model_validate(
        {
            "expectedCommandHash": command_hash,
            "expectedTargetVersion": target_version,
        }
    )

    result = await operator_svc.confirm_operator_proposal(
        "conversation",
        "proposal",
        body,
        MagicMock(),
    )

    assert result == {"status": "succeeded"}
    require_human.assert_awaited_once()
    coordinator.decide.assert_awaited_once_with(
        "conversation",
        "proposal",
        allow=True,
        expected_command_hash=command_hash,
        expected_target_version=target_version,
    )


def test_context_compiler_keeps_newest_complete_turn_when_older_turn_exceeds_budget():
    from lionagi.studio.operator.engine import (
        _compile_operator_prompt,
        compile_operator_history,
    )
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("context compilation cannot request permission")

    newer = [
        {
            "conversationId": "conversation",
            "requestId": "newer",
            "sequence": 3,
            "type": "text",
            "payload": {
                "role": "user",
                "format": "plain",
                "content": "recent context survives",
            },
        },
        {
            "conversationId": "conversation",
            "requestId": "newer",
            "sequence": 4,
            "type": "done",
            "payload": {"outcome": "completed", "lastSequence": 4},
        },
    ]
    older = [
        {
            "conversationId": "conversation",
            "requestId": "older",
            "sequence": 1,
            "type": "text",
            "payload": {
                "role": "assistant",
                "format": "plain",
                "content": "x" * (129 * 1024),
            },
        },
        {
            "conversationId": "conversation",
            "requestId": "older",
            "sequence": 2,
            "type": "done",
            "payload": {"outcome": "completed", "lastSequence": 2},
        },
    ]
    compiled = compile_operator_history([newer, older])
    prompt = _compile_operator_prompt(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="current instruction",
            context={},
            history=compiled.frames,
            request_permission=request_permission,
        )
    )
    assert compiled.metadata["turnCount"] == 1
    assert compiled.metadata["firstSequence"] == 3
    assert compiled.metadata["lastSequence"] == 4
    assert "recent context survives" in prompt
    assert "x" * 1024 not in prompt
    assert prompt.endswith("current instruction")


@pytest.mark.asyncio
async def test_context_compilation_groups_complete_turns_merges_deltas_and_persists_receipt(
    tmp_path,
):
    from lionagi.studio.operator.engine import (
        _compile_operator_prompt,
        compile_operator_history,
    )
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("context compilation cannot request permission")

    store = OperatorStore(tmp_path / "state.db")
    cid = (await store.create_conversation())["id"]
    first = await store.submit_turn(
        cid,
        instruction="remember the original requirement",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(first["requestId"])
    for index in range(70):
        await store.append_frame(
            cid,
            first["requestId"],
            "text",
            {
                "content": f"delta-{index}|",
                "format": "plain",
                "role": "assistant",
            },
        )
    await store.append_frame(
        cid,
        first["requestId"],
        "tool_call",
        {
            "callId": "paired-1",
            "tool": "Inspect",
            "arguments": {"path": "README.md"},
            "mode": "read",
        },
    )
    await store.append_frame(
        cid,
        first["requestId"],
        "tool_result",
        {
            "callId": "paired-1",
            "ok": True,
            "result": {"summary": "found"},
        },
    )
    await store.finish_turn(first["requestId"], outcome="completed")

    latest = (await store.get_conversation(cid))["nextSequence"] - 1
    second = await store.submit_turn(
        cid,
        instruction="newer complete request",
        context={"space": "history", "route": "/fleet", "filters": {}},
        expected_last_sequence=latest,
    )
    assert await store.mark_running(second["requestId"])
    await store.append_frame(
        cid,
        second["requestId"],
        "text",
        {
            "content": "newer complete answer",
            "format": "plain",
            "role": "assistant",
        },
    )
    await store.finish_turn(second["requestId"], outcome="completed")

    latest = (await store.get_conversation(cid))["nextSequence"] - 1
    current = await store.submit_turn(
        cid,
        instruction="use both prior turns",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=latest,
    )
    assert await store.mark_running(current["requestId"])
    groups = await store.list_complete_turn_frame_groups(
        cid, exclude_request_id=current["requestId"]
    )
    compiled = compile_operator_history(groups)
    repeated = compile_operator_history(groups)

    text = [frame["payload"]["content"] for frame in compiled.frames if frame["type"] == "text"]
    assert text[0] == "remember the original requirement"
    assert "delta-0|" in text[1]
    assert "delta-69|" in text[1]
    assert text[-2:] == ["newer complete request", "newer complete answer"]
    tool_frames = [frame for frame in compiled.frames if frame["type"].startswith("tool_")]
    assert [frame["type"] for frame in tool_frames] == ["tool_call", "tool_result"]
    assert {frame["payload"]["callId"] for frame in tool_frames} == {"paired-1"}
    assert compiled.metadata == repeated.metadata
    assert compiled.metadata["frameCount"] == 6
    assert compiled.metadata["turnCount"] == 2
    assert compiled.metadata["firstSequence"] == 1
    assert compiled.metadata["lastSequence"] == current["acceptedSequence"] - 1
    assert len(compiled.metadata["hash"]) == 64

    context = await store.record_context_compilation(current["requestId"], compiled.metadata)
    turn = await store.get_turn(current["requestId"])
    assert context["operatorCompilation"] == compiled.metadata
    assert turn["context"] == context
    assert turn["contextHash"] == store.canonical_hash(context)
    prompt = _compile_operator_prompt(
        OperatorEngineTurn(
            conversation_id=cid,
            request_id=current["requestId"],
            instruction="use both prior turns",
            context=context,
            history=compiled.frames,
            request_permission=request_permission,
        )
    )
    assert "remember the original requirement" in prompt
    assert "delta-69|" in prompt
    assert "assistant tool call Inspect [paired-1]" in prompt
    assert "tool result [paired-1] (ok)" in prompt
    assert prompt.endswith("use both prior turns")
    await store.finish_turn(current["requestId"], outcome="cancelled")


@pytest.mark.asyncio
async def test_agent_and_scheduler_children_do_not_inherit_studio_credentials(
    monkeypatch,
):
    from lionagi.providers._cli_subprocess import ndjson_from_cli
    from lionagi.studio.scheduler.subprocess import spawn_and_wait

    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "must-not-reach-child")
    monkeypatch.setenv("LIONAGI_STUDIO_HUMAN_TOKEN", "human-must-not-reach-child")
    probe = (
        "import json,os;"
        "print(json.dumps({'auth':os.getenv('LIONAGI_STUDIO_AUTH_TOKEN'),"
        "'human':os.getenv('LIONAGI_STUDIO_HUMAN_TOKEN')}))"
    )
    rows = [row async for row in ndjson_from_cli([sys.executable, "-c", probe])]
    assert rows == [{"auth": None, "human": None}]

    scheduler_probe = (
        "import os,sys;"
        "sys.stderr.write(repr((os.getenv('LIONAGI_STUDIO_AUTH_TOKEN'),"
        "os.getenv('LIONAGI_STUDIO_HUMAN_TOKEN'))))"
    )
    exit_code, stderr = await spawn_and_wait(
        [sys.executable, "-c", scheduler_probe], "operator-env-probe"
    )
    assert exit_code == 0
    assert stderr == "(None, None)"


@pytest.mark.asyncio
async def test_immediate_and_repeated_cancel_always_terminalize(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=BlockingEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="wait",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    first = await coordinator.cancel(cid, accepted["requestId"])
    second = await coordinator.cancel(cid, accepted["requestId"])
    frames = await _wait_done(coordinator.store, cid)
    assert first["cancelRequested"] is True
    assert second["cancelRequested"] is False
    assert sum(frame["type"] == "done" for frame in frames) == 1
    assert frames[-1]["payload"]["outcome"] == "cancelled"
    assert (await coordinator.store.get_conversation(cid))["activeRequestId"] is None
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_startup_recovers_interrupted_turn_with_error_and_done(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="interrupted",
        context={"space": "system", "route": "/system", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    recovered = await OperatorCoordinator(store=store, engine_factory=ScriptedEngine).startup()
    assert recovered == [accepted["requestId"]]
    frames = await store.list_frames(cid)
    assert [frame["type"] for frame in frames[-2:]] == ["error", "done"]
    assert frames[-2]["payload"]["error"]["code"] == "service_restarted"
    assert frames[-1]["payload"]["outcome"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "word"), [(True, "allowed"), (False, "denied")])
async def test_engine_permission_really_blocks_until_allow_or_deny(
    tmp_path, monkeypatch, allow, word
):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=PermissionEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="gated",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    proposal = await _wait_proposal(coordinator.store, accepted["requestId"])
    before = await coordinator.store.list_frames(cid)
    assert not any(frame["type"] == "done" for frame in before)
    assert proposal["command"]["toolName"] == "Bash"
    assert proposal["command"]["input"] == {"command": "git status"}

    result = await coordinator.decide(
        cid,
        proposal["id"],
        allow=allow,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=None,
    )
    frames = await _wait_done(coordinator.store, cid)
    text = "".join(
        frame["payload"].get("content", "") for frame in frames if frame["type"] == "text"
    )
    assert word in text
    assert result["status"] == ("executing" if allow else "failed")
    if not allow:
        assert any(
            frame["type"] == "confirmation"
            and frame["payload"] == {"proposalId": proposal["id"], "state": "cancelled"}
            for frame in frames
        )
        assert (await coordinator.store.get_proposal(proposal["id"]))["status"] == "cancelled"
        assert await _audit_decisions(proposal["id"]) == ["denied"]
    else:
        unfinished = await coordinator.store.get_proposal(proposal["id"])
        assert unfinished["status"] == "failed"
        assert unfinished["errorCode"] == "provider_result_missing"
        missing_result = next(
            frame
            for frame in frames
            if frame["type"] == "tool_result" and frame["payload"].get("callId") == "t1"
        )
        assert missing_result["payload"] == {
            "callId": "t1",
            "ok": False,
            "error": {
                "code": "service_failure",
                "message": (
                    "The provider ended without returning a terminal result for this approved tool"
                ),
                "retryable": False,
            },
        }
        assert frames[-1]["payload"]["outcome"] == "failed"
        assert (await coordinator.store.get_turn(accepted["requestId"]))["status"] == "failed"
        assert await _audit_decisions(proposal["id"]) == [
            "confirmed",
            "indeterminate",
        ]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_native_tool_result_terminalizes_and_audits_provider_permission(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(
        store=OperatorStore(path), engine_factory=NativePermissionEngine
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="gated native tool",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    proposal = await _wait_proposal(coordinator.store, accepted["requestId"])
    await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=None,
    )
    frames = await _wait_done(coordinator.store, cid)
    terminal = await coordinator.store.get_proposal(proposal["id"])
    assert terminal["status"] == "succeeded"
    assert terminal["result"] == {"nativeToolCompleted": True}
    assert await _audit_decisions(proposal["id"]) == ["confirmed", "executed"]
    assert any(
        frame["type"] == "confirmation"
        and frame["payload"] == {"proposalId": proposal["id"], "state": "executed"}
        for frame in frames
    )
    await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminalizer", "proposal_status", "audit_decision"),
    [
        ("cancel", "cancelled", "denied"),
        ("expire", "expired", "expired"),
    ],
)
async def test_pending_provider_permission_cancel_and_expiry_are_audited(
    tmp_path, monkeypatch, terminalizer, proposal_status, audit_decision
):
    from lionagi.studio.services._db import open_db

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=PermissionEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="pending permission",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    proposal = await _wait_proposal(coordinator.store, accepted["requestId"])
    if terminalizer == "cancel":
        await coordinator.cancel(cid, accepted["requestId"])
    else:
        async with open_db(str(path)) as db:
            await db.execute(
                "UPDATE studio_operator_proposals SET expires_at=0 WHERE id=?",
                (proposal["id"],),
            )
            await db.commit()
    await _wait_done(coordinator.store, cid)
    terminal = await coordinator.store.get_proposal(proposal["id"])
    assert terminal["status"] == proposal_status
    assert await _audit_decisions(proposal["id"]) == [audit_decision]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_stdio_permission_bridge_polls_durable_decision(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="native tool",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])
    task = asyncio.create_task(
        mcp_permission(
            {
                "tool_name": "Write",
                "input": {"file_path": "notes.txt", "content": "hello"},
                "tool_use_id": "native-1",
            }
        )
    )
    proposal = await _wait_proposal(store, accepted["requestId"])
    await asyncio.sleep(0)
    assert not task.done()
    await store.decide_proposal(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=None,
    )
    decision = await asyncio.wait_for(task, timeout=2)
    assert decision == {
        "behavior": "allow",
        "updatedInput": {"file_path": "notes.txt", "content": "hello"},
    }


@pytest.mark.asyncio
async def test_stdio_permission_bridge_never_reuses_approval_for_changed_input(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="native tool",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    first_input = {"file_path": "notes.txt", "content": "approved"}
    first_task = asyncio.create_task(
        mcp_permission(
            {
                "tool_name": "Write",
                "input": first_input,
                "tool_use_id": "native-1",
            }
        )
    )
    first = await _wait_proposal(store, accepted["requestId"])
    await store.decide_proposal(
        cid,
        first["id"],
        allow=True,
        expected_command_hash=first["commandHash"],
        expected_target_version=None,
    )
    assert await asyncio.wait_for(first_task, timeout=2) == {
        "behavior": "allow",
        "updatedInput": first_input,
    }

    changed_input = {"file_path": "notes.txt", "content": "not yet approved"}
    changed_task = asyncio.create_task(
        mcp_permission(
            {
                "tool_name": "Write",
                "input": changed_input,
                "tool_use_id": "native-1",
            }
        )
    )
    deadline = time.monotonic() + 2
    proposals = []
    while time.monotonic() < deadline:
        proposals = await store.list_proposals_for_request(accepted["requestId"])
        if len(proposals) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(proposals) == 2
    changed = proposals[1]
    assert changed["id"] != first["id"]
    assert changed["command"]["input"] == changed_input
    assert changed["status"] == "pending"
    assert not changed_task.done()

    await store.decide_proposal(
        cid,
        changed["id"],
        allow=False,
        expected_command_hash=changed["commandHash"],
        expected_target_version=None,
    )
    assert await asyncio.wait_for(changed_task, timeout=2) == {
        "behavior": "deny",
        "message": "The Lion Studio operator denied this tool request",
    }


def _request(
    *,
    client: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    if not any(key == b"host" for key, _ in raw_headers):
        raw_headers.append((b"host", b"127.0.0.1:8765"))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": raw_headers,
            "client": (client, 1234),
            "server": ("127.0.0.1", 8765),
        }
    )


@pytest.mark.asyncio
async def test_operator_human_gate_requires_exact_browser_credential(monkeypatch):
    from fastapi import HTTPException

    from lionagi.studio.security import (
        capture_studio_credentials,
        clear_captured_studio_credentials,
    )
    from lionagi.studio.services.operator import (
        _require_human,
        _require_safe_operator_credential_origin,
    )

    clear_captured_studio_credentials()
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)
    with pytest.raises(HTTPException) as missing:
        _require_safe_operator_credential_origin(_request())
    assert missing.value.status_code == 403
    assert "generated browser credential" in missing.value.detail
    with pytest.raises(HTTPException):
        await _require_human(_request())

    monkeypatch.setenv("LIONAGI_STUDIO_HUMAN_TOKEN", "human-secret")
    with pytest.raises(HTTPException) as unsafe:
        await _require_human(_request(headers={"authorization": "Bearer human-secret"}))
    assert unsafe.value.status_code == 403
    assert "environment-derived" in unsafe.value.detail

    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)
    generated = capture_studio_credentials(generate_human=True)
    assert generated
    with pytest.raises(HTTPException):
        await _require_human(_request(headers={"authorization": "Bearer wrong"}))
    with pytest.raises(HTTPException):
        await _require_human(
            _request(
                headers={
                    "authorization": f"Bearer {generated}",
                    "x-lionagi-operator-principal": "service",
                }
            )
        )
    await _require_human(_request(headers={"authorization": f"Bearer {generated}"}))
    clear_captured_studio_credentials()


@pytest.mark.asyncio
async def test_direct_app_factory_captures_and_erases_environment_credential(
    monkeypatch,
):
    from fastapi import HTTPException

    from lionagi.studio.app import create_app
    from lionagi.studio.security import clear_captured_studio_credentials
    from lionagi.studio.services.operator import (
        _require_human,
        _require_safe_operator_credential_origin,
    )

    clear_captured_studio_credentials()
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "direct-uvicorn-secret")
    monkeypatch.setenv("LIONAGI_STUDIO_HUMAN_TOKEN", "lower-priority-secret")
    app = create_app()
    assert "LIONAGI_STUDIO_AUTH_TOKEN" not in os.environ
    assert "LIONAGI_STUDIO_HUMAN_TOKEN" not in os.environ
    assert app.state.studio_auth_token == "direct-uvicorn-secret"
    assert app.state.studio_human_token == "direct-uvicorn-secret"
    assert app.state.studio_operator_credential_origin == "environment"

    request = _request(headers={"authorization": "Bearer direct-uvicorn-secret"})
    request.scope["app"] = app
    with pytest.raises(HTTPException) as origin_error:
        _require_safe_operator_credential_origin(request)
    assert origin_error.value.status_code == 403
    assert "environment-derived" in origin_error.value.detail
    with pytest.raises(HTTPException):
        await _require_human(request)
    clear_captured_studio_credentials()


@pytest.mark.asyncio
async def test_direct_app_without_browser_credential_rejects_operator_submit(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.security import clear_captured_studio_credentials

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    clear_captured_studio_credentials()
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)
    app = create_app()
    coordinator = OperatorCoordinator(
        store=OperatorStore(path),
        engine_factory=ScriptedEngine,
    )
    await reset_operator_coordinator_for_testing(coordinator)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
    ) as client:
        assert (await client.get("/openapi.json")).status_code == 200
        # No configured credential means no Operator access at all — the
        # conversation snapshot is state, and state is gated.
        assert (await client.get(f"/api/operator/conversations/{cid}")).status_code == 403
        blocked = await client.post(
            f"/api/operator/conversations/{cid}/turns",
            json={
                "instruction": "This must not start.",
                "context": {"space": "mission", "route": "/", "filters": {}},
                "expectedLastSequence": 0,
            },
        )
    assert blocked.status_code == 403
    assert "generated browser credential" in blocked.json()["detail"]
    assert await coordinator.store.list_frames(cid) == []
    await coordinator.shutdown()
    clear_captured_studio_credentials()


@pytest.mark.asyncio
async def test_generated_browser_bearer_guards_operator_get_sse_and_submit(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.security import (
        capture_studio_credentials,
        clear_captured_studio_credentials,
    )

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    clear_captured_studio_credentials()
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)
    token = capture_studio_credentials(generate_human=True)
    assert token
    app = create_app()
    assert app.state.studio_auth_token == token
    assert app.state.studio_human_token == token
    assert app.state.studio_operator_credential_origin == "generated"

    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        assert (await client.get("/openapi.json")).status_code == 401
        assert (await client.get(f"/api/operator/conversations/{cid}")).status_code == 401
        assert (await client.get(f"/api/operator/conversations/{cid}/stream")).status_code == 401
        unauthenticated_submit = await client.post(
            f"/api/operator/conversations/{cid}/turns",
            json={
                "instruction": "must be rejected",
                "context": {"space": "mission", "route": "/", "filters": {}},
                "expectedLastSequence": 0,
            },
        )
        assert unauthenticated_submit.status_code == 401

        headers = {"Authorization": f"Bearer {token}"}
        assert (
            await client.get(f"/api/operator/conversations/{cid}", headers=headers)
        ).status_code == 200
        submitted = await client.post(
            f"/api/operator/conversations/{cid}/turns",
            headers=headers,
            json={
                "instruction": "authenticated turn",
                "context": {"space": "mission", "route": "/", "filters": {}},
                "expectedLastSequence": 0,
            },
        )
        assert submitted.status_code == 202
    await _wait_done(coordinator.store, cid)

    messages: list[dict] = []
    request_sent = False
    body_sent = asyncio.Event()

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await body_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            body_sent.set()

    await asyncio.wait_for(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": f"/api/operator/conversations/{cid}/stream",
                "raw_path": f"/api/operator/conversations/{cid}/stream".encode(),
                "query_string": b"after_sequence=0",
                "headers": [
                    (b"host", b"127.0.0.1:8765"),
                    (b"authorization", f"Bearer {token}".encode()),
                ],
                "client": ("127.0.0.1", 54321),
                "server": ("127.0.0.1", 8765),
            },
            receive,
            send,
        ),
        timeout=2,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    streamed = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    assert start["status"] == 200
    assert b"data:" in streamed
    await coordinator.shutdown()
    clear_captured_studio_credentials()


@pytest.mark.asyncio
async def test_sse_replays_committed_frames_in_sequence(tmp_path, monkeypatch):
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.security import (
        capture_studio_credentials,
        clear_captured_studio_credentials,
    )
    from lionagi.studio.services.operator import stream_operator_conversation

    clear_captured_studio_credentials()
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)
    token = capture_studio_credentials(generate_human=True)
    store = OperatorStore(tmp_path / "state.db")
    coordinator = OperatorCoordinator(store=store, engine_factory=BlockingEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="replay me",
        context={"space": "history", "route": "/fleet", "filters": {}},
        expected_last_sequence=0,
    )

    class Connected:
        # The stream route authorizes before it replays, so the stand-in has to
        # carry a credential like a real request does.
        scope: dict = {}
        headers = {"authorization": f"Bearer {token}"}

        async def is_disconnected(self):
            return False

    response = await stream_operator_conversation(cid, Connected(), after_sequence=0)
    iterator = response.body_iterator
    first = await anext(iterator)
    payload = json.loads(first.removeprefix("data:").strip())
    assert payload["requestId"] == accepted["requestId"]
    assert payload["sequence"] == 1
    assert payload["payload"]["role"] == "user"
    await iterator.aclose()
    await coordinator.shutdown()
    clear_captured_studio_credentials()


@pytest.mark.asyncio
async def test_http_create_submit_and_paged_replay_contract(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.security import (
        capture_studio_credentials,
        clear_captured_studio_credentials,
    )

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    clear_captured_studio_credentials()
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)
    token = capture_studio_credentials(generate_human=True)
    assert token
    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
        headers={"authorization": f"Bearer {token}"},
    ) as client:
        created = await client.post(
            "/api/operator/conversations",
            json={"title": "HTTP contract"},
        )
        assert created.status_code == 200
        cid = created.json()["conversation"]["id"]
        submitted = await client.post(
            f"/api/operator/conversations/{cid}/turns",
            json={
                "instruction": "hello over HTTP",
                "context": {"space": "mission", "route": "/", "filters": {}},
                "expectedLastSequence": 0,
            },
        )
        assert submitted.status_code == 202
        accepted = submitted.json()
        assert accepted["acceptedSequence"] == 1

        await _wait_done(coordinator.store, cid)
        replay = await client.get(
            f"/api/operator/conversations/{cid}",
            params={"after_sequence": 0, "limit": 2},
        )
        assert replay.status_code == 200
        body = replay.json()
        assert body["hasMore"] is True
        assert body["nextAfterSequence"] == 2
        assert body["latestSequence"] >= 5
        assert body["frames"][0]["requestId"] == accepted["requestId"]
    await coordinator.shutdown()
    clear_captured_studio_credentials()
