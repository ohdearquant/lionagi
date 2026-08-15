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


class MessageWritingEngine:
    """Adds real messages to the turn's branch through the same hooked
    async add-path `Branch.chat_and_record` uses, so persistence-layer
    append/dedup behavior is actually exercised. `ScriptedEngine` never
    touches the branch's messages at all, so it cannot stand in for a real
    engine when a test cares about what got persisted."""

    async def _stream(self, turn):
        branch = turn.runtime_branch
        await branch.msgs.a_add_message(
            instruction=turn.instruction, sender=branch.user, recipient=branch.id
        )
        await branch.msgs.a_add_message(
            assistant_response=f"ack: {turn.instruction}",
            sender=branch.id,
            recipient=branch.user,
        )
        yield OperatorEngineEvent("text", {"content": "ok", "format": "plain", "role": "assistant"})

    def stream(self, turn):
        return self._stream(turn)


class FailingEngine:
    async def _stream(self, _turn):
        raise RuntimeError("engine exploded mid-turn")
        yield  # pragma: no cover

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


def test_real_operator_branch_exposes_only_strict_request_scoped_mcp_tools(tmp_path, monkeypatch):
    import lionagi._paths as paths_mod
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    # Hermetic: no operator_mcp.json / house-rules file from the developer's
    # real LIONAGI_HOME may leak extra servers or tools into this pin.
    monkeypatch.setattr(paths_mod, "LIONAGI_HOME", tmp_path / "lionagi-home")

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
    # Widening this set is a deliberate act. Everything added since the
    # original four is read-only; the three gated tools are unchanged.
    assert set(kwargs["allowed_tools"]) == {
        "mcp__studio_operator__list_recent_runs",
        "mcp__studio_operator__run_stats",
        "mcp__studio_operator__get_current_view",
        "mcp__studio_operator__list_schedules",
        "mcp__studio_operator__list_agents",
        "mcp__studio_operator__list_playbooks",
        "mcp__studio_operator__navigate",
        "mcp__studio_operator__prefill_schedule",
        "mcp__studio_operator__launch_playbook",
        "mcp__studio_operator__run_progress",
        "mcp__studio_operator__run_findings",
        "mcp__studio_operator__run_detail",
        "mcp__studio_operator__cancel_run",
        "mcp__studio_operator__resume_run",
        "mcp__studio_operator__rename_run",
        "mcp__studio_operator__list_sessions",
        "mcp__studio_operator__session_detail",
        "mcp__studio_operator__session_signals",
        "mcp__studio_operator__get_invocation",
        "mcp__studio_operator__list_artifacts",
        "mcp__studio_operator__get_artifact",
    }
    # The first turn of a conversation has nothing to resume.
    assert "resume" not in kwargs


def test_operator_extra_mcp_config_grants_servers_tools_and_house_rules(tmp_path, monkeypatch):
    """operator_mcp.json attaches extra servers and allows their tools; the
    request-scoped servers cannot be overridden, an allowed tool must name a
    server the config itself attaches, and operator_house_rules.md reaches
    the system prompt."""
    import lionagi._paths as paths_mod
    from lionagi.studio.operator.engine import (
        _operator_system_prompt,
        build_operator_branch,
    )
    from lionagi.studio.operator.types import OperatorEngineTurn

    home = tmp_path / "lionagi-home"
    home.mkdir()
    (home / "operator_mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "khive": {"command": "/opt/knowledge/bin/server", "args": ["mcp"]},
                    "studio_operator": {"command": "/bin/evil"},
                    "broken": {"args": ["no-command-string"]},
                },
                "allowed_tools": [
                    "mcp__khive__request",
                    "mcp__elsewhere__request",
                    "Bash",
                ],
            }
        )
    )
    (home / "operator_house_rules.md").write_text("Answer as the house Operator.")
    monkeypatch.setattr(paths_mod, "LIONAGI_HOME", home)

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
    assert set(kwargs["mcp_servers"]) == {"studio_permission", "studio_operator", "khive"}
    assert kwargs["mcp_servers"]["khive"]["command"] == "/opt/knowledge/bin/server"
    # The reserved name keeps Studio's own server, never the config's.
    assert kwargs["mcp_servers"]["studio_operator"]["command"] != "/bin/evil"
    allowed = kwargs["allowed_tools"]
    assert "mcp__khive__request" in allowed
    assert "mcp__elsewhere__request" not in allowed, "tool without an attached server admitted"
    assert "Bash" not in allowed, "non-MCP tool admitted through the extra allowlist"
    # Every original application tool survives the widening.
    assert "mcp__studio_operator__list_recent_runs" in allowed

    prompt = _operator_system_prompt()
    assert prompt.endswith("Answer as the house Operator.")
    assert "You are the resident Operator" in prompt


def test_operator_extra_mcp_absent_config_changes_nothing(tmp_path, monkeypatch):
    import lionagi._paths as paths_mod
    from lionagi.studio.operator.engine import (
        _SYSTEM_PROMPT,
        _operator_extra_mcp,
        _operator_system_prompt,
    )

    monkeypatch.setattr(paths_mod, "LIONAGI_HOME", tmp_path / "empty-home")
    assert _operator_extra_mcp() == ({}, [])
    assert _operator_system_prompt() == _SYSTEM_PROMPT


# The tool set the Operator is actually supposed to expose. Asserted
# against both registries below -- registry *parity* alone (each registry
# matching the other) would stay green even if a tool were missing from
# both at once, which is exactly how `resume_run` shipped fully built and
# unreachable: both registries agreed with each other while agreeing to
# omit it.
_REQUIRED_OPERATOR_TOOLS = frozenset(
    {
        "list_recent_runs",
        "run_stats",
        "get_current_view",
        "list_schedules",
        "list_agents",
        "list_playbooks",
        "navigate",
        "prefill_schedule",
        "launch_playbook",
        "run_progress",
        "run_findings",
        "run_detail",
        "cancel_run",
        "resume_run",
        "rename_run",
        "list_sessions",
        "session_detail",
        "session_signals",
        "get_invocation",
        "list_artifacts",
        "get_artifact",
    }
)


def test_operator_mcp_tool_registries_agree_exactly_in_both_directions():
    """`application_mcp.py`'s tool registry and `engine.py`'s allowlist must
    name the exact same tools, and that set must be the required set -- a
    tool added to one but not the other is either invisible to the Operator
    (allowlist missing it) or silently unreachable despite being allowed
    (application registry missing it), both look exactly like a broken model
    from the outside, and a tool omitted from both registries agreeing with
    each other is a regression neither half alone can catch."""
    from lionagi.studio.operator.application_mcp import (
        _TOOL_DESCRIPTIONS,
        _TOOL_HANDLERS,
        _TOOL_MODELS,
    )
    from lionagi.studio.operator.engine import _OPERATOR_MCP_TOOLS

    application_names = set(_TOOL_MODELS)
    assert application_names == set(_TOOL_HANDLERS)
    assert application_names == set(_TOOL_DESCRIPTIONS)
    assert application_names == _REQUIRED_OPERATOR_TOOLS

    prefix = "mcp__studio_operator__"
    assert all(name.startswith(prefix) for name in _OPERATOR_MCP_TOOLS)
    assert len(_OPERATOR_MCP_TOOLS) == len(set(_OPERATOR_MCP_TOOLS))
    allowlist_names = {name.removeprefix(prefix) for name in _OPERATOR_MCP_TOOLS}

    assert allowlist_names == application_names


def test_operator_branch_resumes_the_conversations_provider_session(tmp_path):
    """A second turn continues the same provider session instead of a new one."""
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    branch = build_operator_branch(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request-2",
            instruction="and what about yesterday?",
            context={},
            history=(),
            request_permission=request_permission,
            store_path=str(tmp_path / "state.db"),
            provider_session_id="session-abc",
        )
    )
    assert branch.chat_model.endpoint.config.kwargs["resume"] == "session-abc"


@pytest.mark.asyncio
async def test_application_mcp_read_query_is_bounded_and_redacted(monkeypatch):
    from lionagi.studio.operator.application_mcp import list_recent_runs
    from lionagi.studio.services import runs as runs_service

    observed = {}

    async def fake_list_runs(*, status, kind=None, limit, offset):
        observed.update(status=status, kind=kind, limit=limit, offset=offset)
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
                "invocation_kind": "play",
                "playbook_name": "daily-triage",
            },
            {
                "id": "run-2",
                "agent_name": "Researcher",
                "status": "failed",
                "project": "acme/research",
                "started_at": 3.0,
                "ended_at": 4.0,
                "invocation_kind": "agent",
                "playbook_name": None,
            },
        ]

    monkeypatch.setattr(runs_service, "list_runs", fake_list_runs)
    result = await list_recent_runs({"limit": 2, "status": "failed"})
    assert observed == {"status": "failed", "kind": None, "limit": 2, "offset": 0}
    assert result == {
        "runs": [
            {
                "id": "run-1",
                "agentName": "Operator",
                "status": "failed",
                "project": "private",
                "startedAt": 1.0,
                "endedAt": 2.0,
                "endedAtApproximate": False,
                "href": "/runs/run-1",
                "kind": "play",
                "playbookName": "daily-triage",
            },
            {
                "id": "run-2",
                "agentName": "Researcher",
                "status": "failed",
                "project": "acme/research",
                "startedAt": 3.0,
                "endedAt": 4.0,
                "endedAtApproximate": False,
                "href": "/runs/run-2",
                "kind": "agent",
                "playbookName": None,
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
async def test_navigate_targets_a_specific_run_and_library_entry(tmp_path, monkeypatch):
    """navigate can put the human on one run (run_id -> s param) or one
    library entry (sel), instead of only a space whose view default-selects
    its first row."""
    from lionagi.studio.operator.application_mcp import navigate

    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="show me the run",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    run = await navigate({"space": "history", "run_id": "fb0a809a-28a8-4778-a49d-495b0cf14bec"})
    entry = await navigate({"space": "library", "sel": "playbook:builtin:audit"})
    both = await navigate({"space": "history", "status": "failed", "run_id": "abcd1234"})
    faceted = await navigate({"space": "history", "status": "running", "run_kind": "play"})

    frames = await store.list_frames(cid)
    effects = [frame["payload"]["effect"] for frame in frames if frame["type"] == "ui_command"]
    assert [e["params"] for e in effects] == [
        {"s": "fb0a809a-28a8-4778-a49d-495b0cf14bec"},
        {"sel": "playbook:builtin:audit"},
        {"status": "failed", "s": "abcd1234"},
        {"status": "running", "kind": "play"},
    ]
    assert [e["space"] for e in effects] == ["history", "library", "history", "history"]
    assert run["status"] == entry["status"] == both["status"] == faceted["status"] == "pending"

    # Targets are space-scoped: a run belongs to the fleet view, a sel to the
    # library — the wrong pairing is refused before any effect is persisted.
    with pytest.raises(ValueError, match="run_id"):
        await navigate({"space": "library", "run_id": "abcd1234"})
    with pytest.raises(ValueError, match="sel"):
        await navigate({"space": "history", "sel": "playbook:builtin:audit"})
    with pytest.raises(ValueError, match="run_kind"):
        await navigate({"space": "library", "run_kind": "play"})
    assert len(await store.list_frames(cid)) == len(frames)
    await store.finish_turn(accepted["requestId"], outcome="completed")


@pytest.mark.asyncio
async def test_application_mcp_generic_value_error_scrubs_known_env_secret_values(monkeypatch):
    """The generic `ValueError` arm in `_dispatch` returns `str(exc)`
    unmodified today -- any tool handler that raises a ValueError whose
    message happens to include a run's own secret value would leak it
    straight through this arm."""
    import lionagi.studio.operator.application_mcp as app_mcp

    monkeypatch.setenv("ACME_APP_TOKEN", "greenelephant")

    async def boom(_arguments):
        raise ValueError("could not resolve using token greenelephant")

    monkeypatch.setitem(app_mcp._TOOL_HANDLERS, "list_recent_runs", boom)

    response = await app_mcp._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_recent_runs", "arguments": {}},
        }
    )

    text = response["result"]["content"][0]["text"]
    assert "greenelephant" not in text


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
    # The frame is the only view of a proposal a stream client ever gets, so
    # the row carrying commandType (asserted above) says nothing about whether
    # the frame does. A client checking a returned proposal against the request
    # it made needs the type here, and the two must agree.
    assert proposal_frame["payload"]["proposal"]["commandType"] == "launch"
    assert proposal_frame["payload"]["proposal"]["commandType"] == proposal["commandType"]
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


async def _seed_running_session(db, *, project: str = "/Users/admin/test-project") -> str:
    import uuid

    run_id = str(uuid.uuid4())
    progression_id = str(uuid.uuid4())
    await db.create_progression(progression_id)
    await db.create_session(
        {
            "id": run_id,
            "progression_id": progression_id,
            "status": "running",
            "started_at": time.time(),
            "project": project,
        }
    )
    return run_id


@pytest.mark.asyncio
async def test_application_mcp_cancel_run_allow_executes_via_the_real_default_coordinator(
    tmp_path, monkeypatch
):
    """Unlike the `cancel_run` unit tests (which simulate the coordinator
    wiring this integration step owns), this exercises the actual default
    `OperatorCoordinator` -- no custom `command_executor` override -- proving
    `coordinator.py::_execute_application_command`'s `cancel` branch really
    dispatches to `cancel_run.execute_cancel_command` end to end. Mirrors
    `test_application_mcp_launch_blocks_on_real_durable_human_proposal`
    above: same real coordinator, same allow path, same durable proposal
    gate, but for the lifecycle tool instead of the launch tool."""
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.cancel_run import cancel_run

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_running_session(db)

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="stop that run",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/test-project",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    task = asyncio.create_task(cancel_run({"run": run_id, "reason": "hung"}))
    proposal = await _wait_proposal(store, accepted["requestId"])
    assert not task.done()
    assert proposal["commandType"] == "cancel"
    assert proposal["command"] == {
        "session_id": run_id,
        "reason": "hung",
        "project": "/Users/admin/test-project",
    }
    assert proposal["risk"] == "execute"

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "succeeded"
    assert result == {
        "cancelled": True,
        "status": "terminal",
        "id": run_id,
        "signal": "no_pid",
        # False here because the run was in fact cancelled; the deny and
        # not-found paths return True. Every other assertion on a cancel
        # result carries this field, and it is what tells the operator
        # whether anything actually changed.
        "run_untouched": False,
    }

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "cancelled"
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_application_mcp_cancel_run_deny_leaves_run_untouched_via_real_coordinator(
    tmp_path, monkeypatch
):
    """Same real default wiring as the allow-path test above, but denied:
    proves the run is left exactly as it was and the coordinator's `cancel`
    branch (and therefore `execute_cancel_command`) is never invoked -- a
    denied proposal cannot reach the mutation regardless of which command
    type it names."""
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.cancel_run import cancel_run

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_running_session(db)

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="stop that run",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/test-project",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    task = asyncio.create_task(cancel_run({"run": run_id}))
    proposal = await _wait_proposal(store, accepted["requestId"])

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=False,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "failed"
    assert decision["error"]["code"] == "denied"
    assert result == {
        "cancelled": False,
        "reason": "denied",
        "run_untouched": True,
        "id": run_id,
    }

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "running"
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_application_mcp_resume_run_is_reachable_end_to_end(tmp_path, monkeypatch):
    """`resume_run` must be listed by `tools/list`, callable through the same
    durable proposal gate `cancel_run`/`launch_playbook` use, and actually
    dispatched by the real default `OperatorCoordinator` -- not merely
    present in source with no registry entry and no coordinator branch."""
    import uuid

    import lionagi.studio.services.run_resume as resume_svc
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.application_mcp import _dispatch
    from lionagi.studio.operator.resume_run import resume_run

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    run_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    async with StateDB() as db:
        progression_id = str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": run_id,
                "progression_id": progression_id,
                "status": "completed",
                "started_at": time.time(),
                "invocation_kind": "agent",
                "project": "/Users/admin/test-project",
            }
        )
        branch_progression_id = str(uuid.uuid4())
        await db.create_progression(branch_progression_id)
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": 1.0,
                "name": "worker",
                "session_id": run_id,
                "progression_id": branch_progression_id,
                "model": "claude_code/sonnet",
                "provider": "claude_code",
            }
        )

    launched: list[tuple[list[str], dict]] = []

    async def _fake_launch(argv, **kwargs):
        launched.append((argv, kwargs))
        return "resumeinv123"

    monkeypatch.setattr(resume_svc._launches, "launch_detached_argv", _fake_launch)
    monkeypatch.setattr(
        resume_svc._subprocess, "resolve_li_executable", lambda: (["/opt/lionagi/bin/li"], None)
    )
    monkeypatch.setattr(resume_svc, "_ensure_branch_snapshot_available", lambda _bid: _noop())

    tools_list = await _dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool_names = {tool["name"] for tool in tools_list["result"]["tools"]}
    assert "resume_run" in tool_names

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="continue that run",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/test-project",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    task = asyncio.create_task(
        resume_run({"run": run_id, "instruction": "keep going with step two"})
    )
    proposal = await _wait_proposal(store, accepted["requestId"])
    assert not task.done()
    assert proposal["commandType"] == "resume"

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "succeeded"
    assert result == {
        "resumed": True,
        "id": run_id,
        "branchId": branch_id,
        "invocationId": "resumeinv123",
    }
    assert launched == [
        (
            [
                "/opt/lionagi/bin/li",
                "agent",
                "-r",
                branch_id,
                "--prompt",
                "keep going with step two",
            ],
            {
                "skill": "resume:agent",
                "plugin": "studio_run_resume",
                "prompt": "keep going with step two",
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
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


async def test_application_mcp_resume_run_delegates_flow_kind_to_checkpoint_replay(
    tmp_path, monkeypatch
):
    """resume_run is a thin pass-through onto the service's per-kind dispatch,
    not a second kind classifier: a 'flow' run has no branch to reopen, so the
    Operator tool must forward straight to `li o flow --resume` with no
    instruction, and report back invocationKind/checkpointRunId instead of
    the agent path's branchId -- exercised through the real coordinator, the
    same as the agent-kind end-to-end test above."""
    import uuid

    import lionagi.cli.orchestrate._checkpoint as ckmod
    import lionagi.studio.services.run_resume as resume_svc
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.application_mcp import _dispatch
    from lionagi.studio.operator.resume_run import resume_run

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    run_id = str(uuid.uuid4())
    cli_run_id = f"cli-run-{uuid.uuid4()}"
    async with StateDB() as db:
        progression_id = str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": run_id,
                "progression_id": progression_id,
                "status": "completed",
                "started_at": time.time(),
                "invocation_kind": "flow",
                "project": "/Users/admin/test-project",
                "node_metadata": {"run_id": cli_run_id},
            }
        )
    run_dir = runs_root / cli_run_id
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 2,
                "session_id": "irrelevant-to-studio",
                "prompt": "original prompt",
                "plan": [{"agent_id": "worker-1", "assignee": "worker", "dep_indices": []}],
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )

    launched: list[tuple[list[str], dict]] = []

    async def _fake_launch(argv, **kwargs):
        launched.append((argv, kwargs))
        return "resumeinv456"

    monkeypatch.setattr(resume_svc._launches, "launch_detached_argv", _fake_launch)
    monkeypatch.setattr(
        resume_svc._subprocess, "resolve_li_executable", lambda: (["/opt/lionagi/bin/li"], None)
    )

    tools_list = await _dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool_names = {tool["name"] for tool in tools_list["result"]["tools"]}
    assert "resume_run" in tool_names

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="continue that run",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/test-project",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    # No instruction: the checkpoint owns the plan for a flow-kind resume.
    task = asyncio.create_task(resume_run({"run": run_id}))
    proposal = await _wait_proposal(store, accepted["requestId"])
    assert not task.done()
    assert proposal["commandType"] == "resume"

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "succeeded"
    assert result == {
        "resumed": True,
        "id": run_id,
        "invocationId": "resumeinv456",
        "invocationKind": "flow",
        "checkpointRunId": cli_run_id,
    }
    assert launched == [
        (
            ["/opt/lionagi/bin/li", "orchestrate", "flow", "--resume", run_id],
            {
                "skill": "resume:flow",
                "plugin": "studio_run_resume",
                "prompt": None,
                "tmp_path": None,
                "action_kind": "flow",
                "node_metadata": {
                    "run_id": run_id,
                    "invocation_kind": "flow",
                    "resume": True,
                    "allow_degraded_context": False,
                    "checkpoint_run_id": cli_run_id,
                },
            },
        )
    ]
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


async def test_application_mcp_resume_run_rejects_instruction_for_flow_kind_before_proposal(
    tmp_path, monkeypatch
):
    """A flow-kind run supplied an instruction must never produce an
    approvable proposal describing a replay-with-instruction: the real
    dispatcher (`run_resume.py::_dispatch_resume_by_kind`) rejects an
    instruction for a checkpoint-replay kind, so offering that as an
    approvable action would let a human approve something the executor was
    always going to refuse. No proposal may be created at all -- the
    mismatch must be caught before `store.create_proposal`, not surfaced as
    a later execution failure."""
    import uuid

    from lionagi.state.db import StateDB
    from lionagi.studio.operator.resume_run import resume_run

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    run_id = str(uuid.uuid4())
    async with StateDB() as db:
        progression_id = str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": run_id,
                "progression_id": progression_id,
                "status": "completed",
                "started_at": time.time(),
                "invocation_kind": "flow",
                "project": "studio-test-project",
            }
        )

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="continue that run",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "studio-test-project",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    result = await asyncio.wait_for(
        resume_run({"run": run_id, "instruction": "keep going with step two"}), timeout=2
    )

    assert result["resumed"] is False
    assert result["reason"] == "invalid_input"
    assert result["id"] == run_id
    assert "instruction" in result["message"]

    proposals = await store.list_proposals_for_request(accepted["requestId"])
    assert proposals == []

    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


async def _noop() -> None:
    return None


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


async def _wait_done_since(
    store: OperatorStore, conversation_id: str, after_sequence: int, *, timeout: float = 5
) -> list[dict]:
    """Like `_wait_done`, scoped to frames after *after_sequence* -- needed
    once a conversation has more than one turn, since `_wait_done` would
    otherwise match the earlier turn's own "done" frame."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = await store.list_frames(conversation_id, after_sequence=after_sequence)
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

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
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
async def test_model_failure_points_at_the_run_it_created(tmp_path, monkeypatch):
    """An engine failure that happens after the canonical run exists must name it."""
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=FailingEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="do something that will fail",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames = await _wait_done(coordinator.store, cid)
    error = next(frame for frame in frames if frame["type"] == "error")["payload"]["error"]
    assert error["code"] == "model_failure"
    run_id = error["details"]["runId"]
    assert run_id
    assert f"/runs/{run_id}" in error["message"]
    assert error["details"]["href"] == f"/runs/{run_id}"
    assert "daemon logs" not in error["message"]

    # The pointer resolves: a real, durable run exists under that id.
    from lionagi.state.db import StateDB

    async with StateDB(readonly=True) as db:
        session = await db.get_session(run_id)
    assert session is not None
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_model_failure_states_absence_when_no_run_was_created(tmp_path, monkeypatch):
    """A failure before the canonical run exists must say so, not fabricate a pointer."""
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    from lionagi.studio.operator import coordinator as coordinator_mod

    def broken_history(*_args, **_kwargs):
        raise RuntimeError("history compilation exploded")

    monkeypatch.setattr(coordinator_mod, "compile_operator_history", broken_history)

    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="this never reaches a run",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames = await _wait_done(coordinator.store, cid)
    error = next(frame for frame in frames if frame["type"] == "error")["payload"]["error"]
    assert error["code"] == "model_failure"
    assert error["details"]["runId"] is None
    assert "no run was recorded" in error["message"]
    assert "/runs/" not in error["message"]
    assert "daemon logs" not in error["message"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_model_failure_after_session_commit_points_at_the_orphaned_run(tmp_path, monkeypatch):
    """Setup can commit the session row and then fail on a later step (here,
    the branch insert): the failure message must not claim nothing was
    recorded, and the row must not be left "running" forever."""
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB

    async def broken_create_branch(self, *_args, **_kwargs):
        raise RuntimeError("branch insert exploded")

    monkeypatch.setattr(StateDB, "create_branch", broken_create_branch)

    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="setup will fail after the session row commits",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames = await _wait_done(coordinator.store, cid)
    error = next(frame for frame in frames if frame["type"] == "error")["payload"]["error"]
    assert error["code"] == "model_failure"
    run_id = error["details"]["runId"]
    assert run_id
    assert f"/runs/{run_id}" in error["message"]
    assert error["details"]["href"] == f"/runs/{run_id}"
    assert "no run was recorded" not in error["message"]

    # The pointer resolves to a real, durable, no-longer-orphaned row: the
    # commit survived setup's failure, but it is not left "running" forever.
    async with StateDB(readonly=True) as db:
        session = await db.get_session(run_id)
    assert session is not None
    assert session["status"] in SESSION_TERMINAL_STATUSES
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_application_command_failure_points_at_its_invocation(tmp_path, monkeypatch):
    """A command failure after its invocation was recorded must name that invocation."""
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.coordinator import ApplicationCommandError

    async with StateDB() as db:
        await db.create_invocation(
            {
                "id": "inv-real",
                "skill": "launch:play",
                "plugin": "studio_launch",
                "prompt": None,
                "started_at": time.time(),
                "status": "running",
            }
        )

    async def execute(_command_type, _command):
        raise ApplicationCommandError("spawn died mid-flight", invocation_id="inv-real")

    coordinator = OperatorCoordinator(
        store=OperatorStore(path),
        engine_factory=lambda: PermissionEngine("launch"),
        command_executor=execute,
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="launch it",
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
    frames = await coordinator.store.list_frames(cid)
    tool_result = next(
        frame
        for frame in frames
        if frame["type"] == "tool_result" and frame["payload"].get("callId") == proposal["id"]
    )
    error = tool_result["payload"]["error"]
    assert error["code"] == "service_failure"
    assert "/invocations/inv-real" in error["message"]
    assert error["details"]["invocationId"] == "inv-real"
    assert "daemon logs" not in error["message"]

    async with StateDB(readonly=True) as db:
        invocation = await db.get_invocation("inv-real")
    assert invocation is not None
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_application_command_failure_states_absence_when_no_invocation_was_recorded(
    tmp_path, monkeypatch
):
    """A command failure before any invocation exists must say so, not fabricate a pointer."""
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)

    async def execute(_command_type, _command):
        raise ValueError("li executable could not be resolved")

    coordinator = OperatorCoordinator(
        store=OperatorStore(path),
        engine_factory=lambda: PermissionEngine("launch"),
        command_executor=execute,
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="launch it",
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
    frames = await coordinator.store.list_frames(cid)
    tool_result = next(
        frame
        for frame in frames
        if frame["type"] == "tool_result" and frame["payload"].get("callId") == proposal["id"]
    )
    error = tool_result["payload"]["error"]
    assert error["code"] == "service_failure"
    assert error["details"]["invocationId"] is None
    assert "no invocation was recorded" in error["message"]
    assert "/invocations/" not in error["message"]
    assert "daemon logs" not in error["message"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_execute_application_command_failure_has_no_invocation_before_launch_returns(
    monkeypatch,
):
    """launch() raising before it returns leaves no invocation id to carry forward."""
    import lionagi.studio.services.launches as launches_mod
    from lionagi.studio.operator.coordinator import (
        ApplicationCommandError,
        _execute_application_command,
    )

    async def broken_launch(_data):
        raise ValueError("validation failed")

    monkeypatch.setattr(launches_mod, "launch", broken_launch)

    with pytest.raises(ApplicationCommandError) as excinfo:
        await _execute_application_command("launch", {"action_kind": "play"})
    assert excinfo.value.invocation_id is None


@pytest.mark.asyncio
async def test_execute_application_command_failure_carries_invocation_id_once_recorded(
    monkeypatch,
):
    """A poll failure after launch() succeeds must still carry the recorded invocation id."""
    import lionagi.studio.services.invocations as invocations_mod
    import lionagi.studio.services.launches as launches_mod
    from lionagi.studio.operator.coordinator import (
        ApplicationCommandError,
        _execute_application_command,
    )

    async def fake_launch(_data):
        return {"invocation_id": "inv-42", "action_kind": "play"}

    async def broken_get_invocation(_invocation_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(launches_mod, "launch", fake_launch)
    monkeypatch.setattr(invocations_mod, "get_invocation", broken_get_invocation)

    with pytest.raises(ApplicationCommandError) as excinfo:
        await _execute_application_command("launch", {"action_kind": "play"})
    assert excinfo.value.invocation_id == "inv-42"


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
    monkeypatch.setattr(operator_svc, "get_operator_coordinator", lambda: coordinator)
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
    )

    assert result == {"status": "succeeded"}
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
async def test_provider_session_column_is_added_to_a_preexisting_conversation_store(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op on an existing database.

    The demo store predates this column, so without the additive migration the
    round-trip below raises and every turn silently starts a new session.
    """
    import aiosqlite

    db_path = tmp_path / "state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE studio_operator_conversations ("
            "id TEXT PRIMARY KEY, project TEXT, title TEXT, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "next_sequence INTEGER NOT NULL DEFAULT 1, active_request_id TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "archived_at REAL, deleted_at REAL)"
        )
        await db.commit()

    store = OperatorStore(db_path)
    conversation_id = (await store.create_conversation())["id"]
    assert (await store.get_conversation(conversation_id))["providerSessionId"] is None

    await store.set_provider_session_id(conversation_id, "session-xyz")
    assert (await store.get_conversation(conversation_id))["providerSessionId"] == "session-xyz"


def test_compiled_prompt_carries_the_view_the_human_is_looking_at():
    """The browser sends a view snapshot every turn; the prompt must show it.

    Without this the Operator answers "I cannot tell which page you are on"
    while the turn it is answering carries the route verbatim.
    """
    from lionagi.studio.operator.engine import _compile_operator_prompt
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("prompt compilation cannot request permission")

    prompt = _compile_operator_prompt(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="which page am I on?",
            context={
                "space": "library",
                "route": "/library?sel=agent%3Aadvisor",
                "project": "lionagi",
                "selection": {"agent": "advisor"},
                "filters": {"kind": "agent"},
            },
            history=(),
            request_permission=request_permission,
        )
    )
    assert "library" in prompt
    assert "/library?sel=agent%3Aadvisor" in prompt
    assert "advisor" in prompt
    # The instruction stays last so the model reads the view as background.
    assert prompt.endswith("which page am I on?")


def test_compiled_prompt_bounds_an_oversized_filter_payload():
    from lionagi.studio.operator.engine import (
        _CONTEXT_VALUE_BYTE_LIMIT,
        _compile_operator_prompt,
    )
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("prompt compilation cannot request permission")

    prompt = _compile_operator_prompt(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="current instruction",
            context={"space": "history", "route": "/fleet", "filters": {"q": "y" * 16_384}},
            history=(),
            request_permission=request_permission,
        )
    )
    assert "truncated" in prompt
    assert "y" * (_CONTEXT_VALUE_BYTE_LIMIT + 1) not in prompt
    assert prompt.endswith("current instruction")


def test_compiled_prompt_is_the_bare_instruction_without_view_or_history():
    from lionagi.studio.operator.engine import _compile_operator_prompt
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("prompt compilation cannot request permission")

    assert (
        _compile_operator_prompt(
            OperatorEngineTurn(
                conversation_id="conversation",
                request_id="request",
                instruction="current instruction",
                context={},
                history=(),
                request_permission=request_permission,
            )
        )
        == "current instruction"
    )


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
        "message": "The human at the Studio permission prompt declined this tool request",
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
async def test_sse_replays_committed_frames_in_sequence(tmp_path, monkeypatch):
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.services.operator import stream_operator_conversation

    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
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
        scope: dict = {}
        headers: dict = {}

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


@pytest.mark.asyncio
async def test_http_create_submit_and_paged_replay_contract(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
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


@pytest.mark.asyncio
async def test_current_view_prefers_a_navigation_reported_after_the_instruction(
    tmp_path, monkeypatch
):
    """A turn's context is frozen at submit, so it goes stale the moment the human moves.

    Without preferring a later-reported view, the Operator answers "where am I"
    with wherever they were when they hit send. That is wrong precisely in the
    case the question gets asked, and it is wrong in the confident direction:
    the answer looks like a live read.
    """
    from lionagi.studio.operator.application_mcp import get_current_view

    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="where am I?",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "observationSeq": 1,
            "observerId": "page-a",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    # Nothing reported yet: the turn's own snapshot is the freshest thing there
    # is, and the answer says so rather than implying it is live.
    before = await get_current_view({})
    assert before["known"] is True
    assert before["space"] == "mission"
    assert before["source"] == "turn"

    # The human navigates mid-turn and the browser reports it.
    await store.record_view(
        cid, {"space": "library", "route": "/library?tab=playbook", "filters": {}}, 2, "page-a"
    )

    after = await get_current_view({})
    assert after["space"] == "library"
    assert after["route"] == "/library?tab=playbook"
    assert after["source"] == "live"


@pytest.mark.asyncio
async def test_live_view_columns_are_added_to_a_preexisting_conversation_store(tmp_path):
    """The demo store predates these columns, so the additive migration carries them.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing database, so without
    the migration record_view raises on every navigation against a store that
    already exists, which is every store that matters.
    """
    import aiosqlite

    db_path = tmp_path / "state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE studio_operator_conversations ("
            "id TEXT PRIMARY KEY, project TEXT, title TEXT, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "next_sequence INTEGER NOT NULL DEFAULT 1, active_request_id TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "archived_at REAL, deleted_at REAL)"
        )
        await db.commit()

    store = OperatorStore(db_path)
    cid = (await store.create_conversation())["id"]
    assert await store.get_view(cid, "page-a") == (None, None)

    assert await store.record_view(
        cid, {"space": "system", "route": "/system", "filters": {}}, 7, "page-a"
    )
    view, seq = await store.get_view(cid, "page-a")
    assert view["space"] == "system"
    assert seq == 7
    assert await store.get_view(cid, "page-b") == (None, None), (
        "one page's report says nothing about where another page is"
    )


@pytest.mark.asyncio
async def test_a_late_arriving_older_navigation_does_not_overwrite_the_current_view(
    tmp_path, monkeypatch
):
    """Reports race, and the loser of that race is the stale view.

    Each navigation report is its own request, so arrival order is not
    observation order. Ordering by arrival lets a delayed report for the page
    the human already left overwrite the page they are actually on, and the
    read still labels it "live" — a stale answer wearing the fresh label, which
    is worse than the frozen snapshot this mechanism replaced.
    """
    from lionagi.studio.operator.application_mcp import get_current_view

    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="where am I?",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "observationSeq": 2,
            "observerId": "page-a",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    # The browser saw /library first and /schedules second, but the reports
    # reach the server in the opposite order.
    newer_applied = await store.record_view(
        cid, {"space": "schedules", "route": "/schedules", "filters": {}}, 3, "page-a"
    )
    older_applied = await store.record_view(
        cid, {"space": "library", "route": "/library", "filters": {}}, 1, "page-a"
    )
    assert newer_applied is True
    assert older_applied is False, "an older observation must not overwrite a newer one"

    view = await get_current_view({})
    assert view["space"] == "schedules", "the human is on the page they navigated to last"
    assert view["source"] == "live"


@pytest.mark.asyncio
async def test_a_report_observed_before_the_turn_is_not_live_when_it_arrives_after(
    tmp_path, monkeypatch
):
    """Arriving after the instruction is not the same as being seen after it.

    A report the browser sent while on the previous page can be delayed past
    the submission of a turn sent from the next one. If arrival decided
    freshness, that pre-question observation would come back as the answer to
    the question, labelled live, and the human would be told they are on a page
    they had already left before they asked. Driven over HTTP because the
    ordering that matters is the one the wire produces.
    """
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.application_mcp import get_current_view
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        created = await client.post("/api/operator/conversations", json={"title": "ordering"})
        cid = created.json()["conversation"]["id"]

        # Seen on /library, but the report is held back by the network.
        stale_report = {
            "space": "library",
            "route": "/library",
            "filters": {},
            "observationSeq": 1,
            "observerId": "page-a",
        }

        # The human moves to /mission and asks from there.
        submitted = await client.post(
            f"/api/operator/conversations/{cid}/turns",
            json={
                "instruction": "where am I?",
                "context": {
                    "space": "mission",
                    "route": "/",
                    "filters": {},
                    "observationSeq": 2,
                    "observerId": "page-a",
                },
                "expectedLastSequence": 0,
            },
        )
        assert submitted.status_code == 202
        request_id = submitted.json()["requestId"]

        # Only now does the /library report land.
        delayed = await client.post(
            f"/api/operator/conversations/{cid}/view",
            json=stale_report,
        )
        assert delayed.status_code == 200
        assert delayed.json()["applied"] is True, (
            "the first report on a conversation is stored; being stored is not being current"
        )

        monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
        monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
        monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)

        view = await get_current_view({})
        assert view["space"] == "mission", "a view seen before the question cannot answer it"
        assert view["source"] == "turn"

        # And a report genuinely observed after the turn does flip it, so the
        # assertion above is about ordering rather than about live never firing.
        after = await client.post(
            f"/api/operator/conversations/{cid}/view",
            json={
                "space": "schedules",
                "route": "/schedules",
                "filters": {},
                "observationSeq": 3,
                "observerId": "page-a",
            },
        )
        assert after.status_code == 200
        moved = await get_current_view({})
        assert moved["space"] == "schedules"
        assert moved["source"] == "live"
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_another_pages_later_count_cannot_answer_for_the_page_that_asked(
    tmp_path, monkeypatch
):
    """A count means nothing outside the page that did the counting.

    Two tabs open on one conversation are looking at two different pages and
    count independently, so the busier tab reaches a higher number without
    having seen anything more recent. Comparing across them lets a tab the human
    is not looking at answer for the tab they asked from, and the answer wears
    the live label. Only the page the instruction came from can say where they
    are; every other page can cost freshness and never correctness.

    This is also what makes a reload safe, since a reloaded page is a new
    observer whose restarted count is never measured against the page it
    replaced.
    """
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.application_mcp import get_current_view
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        created = await client.post("/api/operator/conversations", json={"title": "two tabs"})
        cid = created.json()["conversation"]["id"]

        # Tab A has been busy and is deep into its own count.
        busy = await client.post(
            f"/api/operator/conversations/{cid}/view",
            json={
                "space": "schedules",
                "route": "/schedules",
                "filters": {},
                "observationSeq": 40,
                "observerId": "page-a",
            },
        )
        assert busy.status_code == 200

        # The human asks from tab B, which has seen far fewer views.
        submitted = await client.post(
            f"/api/operator/conversations/{cid}/turns",
            json={
                "instruction": "where am I?",
                "context": {
                    "space": "system",
                    "route": "/system",
                    "filters": {},
                    "observationSeq": 2,
                    "observerId": "page-b",
                },
                "expectedLastSequence": 0,
            },
        )
        assert submitted.status_code == 202

        monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
        monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
        monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", submitted.json()["requestId"])

        view = await get_current_view({})
        assert view["space"] == "system", "the tab they asked from is the one that answers"
        assert view["source"] == "turn"

        # Tab B's own low-numbered report is stored even though tab A counted
        # higher, because refusing it would silence whichever tab started later.
        mine = await client.post(
            f"/api/operator/conversations/{cid}/view",
            json={
                "space": "library",
                "route": "/library",
                "filters": {},
                "observationSeq": 3,
                "observerId": "page-b",
            },
        )
        assert mine.status_code == 200
        assert mine.json()["applied"] is True

        moved = await get_current_view({})
        assert moved["space"] == "library", "a later view from the asking tab does answer"
        assert moved["source"] == "live"
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_another_pages_report_does_not_readmit_a_stale_one_from_the_asking_page(
    tmp_path, monkeypatch
):
    """A second tab must not erase what the asking tab has already reported.

    Keeping one view per conversation makes every page's report overwrite the
    page before it, which throws away the asking page's high-water mark. A
    delayed older report from that page then has nothing to lose to and is
    stored as its latest, so the read returns a page the human left two
    navigations ago and calls it live. The other tab is not even the one being
    answered about: it is only the eraser.
    """
    from lionagi.studio.operator.application_mcp import get_current_view

    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="where am I?",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "observationSeq": 1,
            "observerId": "page-a",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    # The asking page moves twice, and its reports leave in order.
    assert await store.record_view(
        cid, {"space": "schedules", "route": "/schedules", "filters": {}}, 3, "page-a"
    )
    # The other tab reports in between.
    assert await store.record_view(
        cid, {"space": "designer", "route": "/designer", "filters": {}}, 9, "page-b"
    )
    # And now the asking page's EARLIER report finally arrives.
    assert not await store.record_view(
        cid, {"space": "library", "route": "/library", "filters": {}}, 2, "page-a"
    ), "a page's own older report stays older, whoever reported in between"

    view = await get_current_view({})
    assert view["space"] == "schedules", "the asking page is where its newest report put it"
    assert view["source"] == "live"


@pytest.mark.asyncio
async def test_a_repeated_observation_timestamp_is_not_applied_twice(tmp_path):
    """Equal observation times are the same observation, not a newer one.

    Guarded explicitly because ">=" and ">" differ here only in the case a
    retry produces, and a retried report re-applying is indistinguishable from
    a real navigation until it is the stale one that wins.
    """
    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation())["id"]

    first = await store.record_view(
        cid, {"space": "library", "route": "/library", "filters": {}}, 5, "page-a"
    )
    replay = await store.record_view(
        cid, {"space": "mission", "route": "/", "filters": {}}, 5, "page-a"
    )
    assert first is True
    assert replay is False

    view, _ = await store.get_view(cid, "page-a")
    assert view["space"] == "library"


# -- Part 1: one conversation is one branch -----------------------------


def _run_link(frames: list[dict]) -> dict:
    frame = next(
        f
        for f in frames
        if f["type"] == "tool_result"
        and isinstance(f["payload"].get("result"), dict)
        and f["payload"]["result"].get("runId")
    )
    return frame["payload"]["result"]


@pytest.mark.asyncio
async def test_second_turn_on_a_conversation_reuses_the_same_branch_and_appends(
    tmp_path, monkeypatch
):
    """A conversation of N turns must be ONE branch/session in the log
    (asserted end to end, from the store through the actual sessions/
    branches reader), and the second turn's messages must be appended to
    the first turn's progression, never overwrite it."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.runs import list_runs

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="Multi-turn"))["conversation"]["id"]

    await coordinator.submit(
        cid,
        instruction="first turn",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames1 = await _wait_done(store, cid)
    link1 = _run_link(frames1)
    run_id1, branch_id1 = link1["runId"], link1["branchId"]

    conv_after_turn1 = await store.get_conversation(cid)
    assert conv_after_turn1["branchId"] == branch_id1

    async with StateDB() as db:
        branch_row = await db.get_branch(branch_id1)
        msg_ids_after_turn1 = set(await db.get_progression(branch_row["progression_id"]))
    assert msg_ids_after_turn1, "turn 1 must have written at least one message"

    last_seq = frames1[-1]["sequence"]
    await coordinator.submit(
        cid,
        instruction="second turn",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=last_seq,
    )
    frames2 = await _wait_done_since(store, cid, last_seq)
    link2 = _run_link(frames2)
    run_id2, branch_id2 = link2["runId"], link2["branchId"]

    # The identity itself: both turns constructed their Branch against the
    # same conversation-level id, and the CLI persistence layer's existing
    # resume path folded the second turn into the same DB session row.
    assert branch_id2 == branch_id1
    assert run_id2 == run_id1

    # From the store through to what the sessions/messages reader serves --
    # not just that the id was written, but that the log actually shows one
    # branch and one run for this conversation.
    async with StateDB() as db:
        db_branches = await db.list_branches(run_id1)
        branch_row_after = await db.get_branch(branch_id1)
        msg_ids_after_turn2 = set(await db.get_progression(branch_row_after["progression_id"]))
    assert [row["id"] for row in db_branches] == [branch_id1]

    runs = await list_runs(limit=50, offset=0)
    matching_runs = [item for item in runs if item["id"] == run_id1]
    assert len(matching_runs) == 1

    # Append, not clobber: every message turn 1 wrote is still there, and
    # turn 2 added strictly more.
    assert msg_ids_after_turn1 <= msg_ids_after_turn2
    assert len(msg_ids_after_turn2) > len(msg_ids_after_turn1)

    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_conversation_with_no_stored_branch_id_adopts_one_lazily_on_first_turn(
    tmp_path, monkeypatch
):
    """A conversation created before `branch_id` existed (or simply before
    its first turn) has no stored identity. It must keep working: the rule
    is adopt-on-next-turn, never a migration that rewrites history."""
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="Legacy"))["conversation"]["id"]

    assert (await store.get_conversation(cid))["branchId"] is None

    await coordinator.submit(
        cid,
        instruction="first turn ever",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    frames = await _wait_done(store, cid)
    link = _run_link(frames)

    conv_after = await store.get_conversation(cid)
    assert conv_after["branchId"] == link["branchId"]
    assert conv_after["branchId"] is not None

    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_concurrent_claim_branch_id_converges_on_one_id(tmp_path):
    """Two turns racing to claim the first branch id for one conversation
    must never leave it with two candidate ids to disagree about."""
    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation(title="Race"))["id"]
    assert (await store.get_conversation(cid))["branchId"] is None

    claimed = await asyncio.gather(*(store.claim_branch_id(cid) for _ in range(8)))

    assert len(set(claimed)) == 1
    assert (await store.get_conversation(cid))["branchId"] == claimed[0]


# -- Part 2: the rename_session Operator tool -----------------------------

# Neutral, non-host-shaped project label for the rename_session tests below
# -- unlike `_seed_running_session`'s own default, this deliberately avoids
# looking like a machine path.
_RENAME_TEST_PROJECT = "studio-test-project"


@pytest.mark.asyncio
async def test_application_mcp_rename_session_allow_executes_via_the_real_default_coordinator(
    tmp_path, monkeypatch
):
    """Same real default `OperatorCoordinator` wiring as the cancel_run/
    resume_run integration tests above: proves
    `coordinator.py::_execute_application_command`'s `rename_session` branch
    really dispatches to `rename_session.execute_rename_session_command`
    end to end, and that the run's name is actually changed once a human
    allows the proposal."""
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.rename_session import rename_session

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_running_session(db, project=_RENAME_TEST_PROJECT)

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="call that run 'nightly backfill'",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": _RENAME_TEST_PROJECT,
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    task = asyncio.create_task(rename_session({"run": run_id, "name": "nightly backfill"}))
    proposal = await _wait_proposal(store, accepted["requestId"])
    assert not task.done()
    assert proposal["commandType"] == "rename_session"
    assert proposal["command"] == {
        "session_id": run_id,
        "name": "nightly backfill",
        "project": _RENAME_TEST_PROJECT,
    }
    assert proposal["risk"] == "mutate"

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "succeeded"
    assert result == {
        "renamed": True,
        "status": "renamed",
        "id": run_id,
        "name": "nightly backfill",
    }

    async with StateDB() as db:
        row = await db.fetch_one("SELECT name FROM sessions WHERE id = ?", (run_id,))
        assert row["name"] == "nightly backfill"
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_application_mcp_rename_session_deny_leaves_run_untouched_via_real_coordinator(
    tmp_path, monkeypatch
):
    """Same real default wiring as the allow-path test above, but denied:
    the run's name must be left exactly as it was and
    `execute_rename_session_command` must never run, regardless of which
    command type a proposal names."""
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.rename_session import rename_session

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_running_session(db, project=_RENAME_TEST_PROJECT)

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="rename that run",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": _RENAME_TEST_PROJECT,
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    task = asyncio.create_task(rename_session({"run": run_id, "name": "should not land"}))
    proposal = await _wait_proposal(store, accepted["requestId"])

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=False,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "failed"
    assert decision["error"]["code"] == "denied"
    assert result == {"renamed": False, "reason": "denied", "id": run_id}

    async with StateDB() as db:
        row = await db.fetch_one("SELECT name FROM sessions WHERE id = ?", (run_id,))
        assert row["name"] is None
    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_rename_session_not_found_reports_reason_without_creating_a_proposal(
    tmp_path, monkeypatch
):
    """An unresolvable run reference is a distinct reported outcome, not a
    generic failure -- and critically, never reaches the proposal flow at
    all, so nothing is ever offered to a human for approval."""
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.rename_session import rename_session

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        # Only to force the schema into existence; this run is never named.
        await _seed_running_session(db)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="rename a run that doesn't exist",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": _RENAME_TEST_PROJECT,
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    result = await rename_session({"run": "not-a-real-run-id", "name": "ghost"})
    assert result["renamed"] is False
    assert result["reason"] == "not_found"
    assert isinstance(result.get("detail"), str) and result["detail"]
    assert await store.list_proposals_for_request(accepted["requestId"]) == []

    await store.finish_turn(accepted["requestId"], outcome="completed")
    await coordinator.shutdown()


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("bad\nname", id="embedded-newline"),
        pytest.param("bad\x00name", id="embedded-nul"),
        pytest.param("bad‮name", id="bidi-control-rlo"),
        pytest.param("bad name", id="line-separator"),
        pytest.param("bad name", id="paragraph-separator"),
        pytest.param("bad\x7fname", id="delete-control"),
    ],
)
def test_rename_session_input_rejects_unicode_control_characters(name):
    """`RenameSessionInput.name` must refuse Unicode `Cc`/`Cf`/`Zl`/`Zp`
    characters before a proposal is ever created -- the value is copied
    verbatim into the proposal command and reaches `db.update_session`,
    which validates column names, not values. Category-based, not an
    enumerated blocklist, so this covers control characters the author
    never explicitly thought of."""
    from pydantic import ValidationError

    from lionagi.studio.operator.rename_session import RenameSessionInput

    with pytest.raises(ValidationError):
        RenameSessionInput.model_validate({"run": "some-run", "name": name})


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("café", id="accented-latin"),
        pytest.param("日本語のセッション", id="cjk"),
        pytest.param("nightly backfill 🎉", id="emoji"),
        pytest.param("nightly backfill", id="internal-space"),
    ],
)
def test_rename_session_input_keeps_non_ascii_names(name):
    """The control-character check must not overshoot into rejecting
    ordinary non-ASCII text: accented Latin, CJK, emoji, and an internal
    space all name a run just as validly as plain ASCII does."""
    from lionagi.studio.operator.rename_session import RenameSessionInput

    args = RenameSessionInput.model_validate({"run": "some-run", "name": name})
    assert args.name == name


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("x" * 161, id="over-length"),
    ],
)
def test_rename_session_input_still_rejects_empty_whitespace_and_over_length(name):
    """The pre-existing length/whitespace rejections must keep working
    alongside the new control-character check, not be silently dropped by
    it."""
    from pydantic import ValidationError

    from lionagi.studio.operator.rename_session import RenameSessionInput

    with pytest.raises(ValidationError):
        RenameSessionInput.model_validate({"run": "some-run", "name": name})
