# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the run_findings Operator read tool."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402

pytestmark = pytest.mark.asyncio


async def seed_session(
    db_path: Path,
    *,
    session_id: str,
    status: str = "completed",
    name: str | None = None,
    project: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract_json: dict | None = None,
    artifact_verification_json: dict | None = None,
) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": prog_id,
                "name": name or f"run-{session_id}",
                "status": status,
                "project": project,
                "artifacts_path": artifacts_path,
                "artifact_contract_json": artifact_contract_json,
                "artifact_verification_json": artifact_verification_json,
                "updated_at": time.time(),
                "invocation_kind": "agent",
                "source_kind": "live",
            }
        )
    if status_needs_reason(status):
        await _set_status_reason(db_path, session_id, "run_failed", "the critic rejected the diff")


def status_needs_reason(status: str) -> bool:
    return status == "failed"


async def _set_status_reason(db_path: Path, session_id: str, code: str, summary: str) -> None:
    """Seed status_reason_code/summary directly; update_status()'s lifecycle
    policy machinery requires a real prior-status transition to be seeded
    through, which is unnecessary ceremony for a read-path test fixture."""
    import aiosqlite as _aiosqlite

    async with _aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE sessions SET status_reason_code = ?, status_reason_summary = ? WHERE id = ?",
            (code, summary, session_id),
        )
        await conn.commit()


async def seed_branch(
    db_path: Path,
    *,
    branch_id: str,
    session_id: str,
    name: str = "worker",
    agent_name: str | None = None,
    status: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> None:
    prog_id = f"{branch_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": 200.0,
                "name": name,
                "session_id": session_id,
                "progression_id": prog_id,
                "model": "gpt-5",
                "provider": "openai",
                "agent_name": agent_name or name,
            }
        )
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if started_at is not None:
            fields["started_at"] = started_at
        if ended_at is not None:
            fields["ended_at"] = ended_at
        if fields:
            await db.update_branch(branch_id, **fields)


async def seed_text_message(
    db_path: Path,
    *,
    branch_id: str,
    message_id: str,
    role: str,
    content: dict[str, Any],
    timestamp: float = 100.0,
) -> None:
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": message_id,
                "created_at": timestamp,
                "role": role,
                "sender": "system",
                "content": content,
            }
        )
    await _append_to_progression(db_path, branch_id, [message_id])


async def seed_action_pair(
    db_path: Path,
    *,
    branch_id: str,
    request_id: str,
    response_id: str,
    function: str,
    arguments: dict[str, Any],
    output: str,
    timestamp: float = 100.0,
) -> None:
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": request_id,
                "created_at": timestamp,
                "role": "action",
                "sender": "system",
                "content": {
                    "function": function,
                    "arguments": arguments,
                    "action_response_id": response_id,
                },
                "node_metadata": {"lion_class": "ActionRequest"},
            }
        )
        await db.insert_message(
            {
                "id": response_id,
                "created_at": timestamp + 0.1,
                "role": "action",
                "sender": "system",
                "content": {"output": output},
                "node_metadata": {"lion_class": "ActionResponse"},
            }
        )
    await _append_to_progression(db_path, branch_id, [request_id, response_id])


async def _append_to_progression(db_path: Path, branch_id: str, message_ids: list[str]) -> None:
    async with StateDB(db_path) as db:
        branch = await db.get_branch(branch_id)
        prog_id = branch["progression_id"]
        existing = await db.get_progression(prog_id)
        await db.set_progression(prog_id, [*existing, *message_ids])


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: Any) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    return path


async def test_run_findings_not_found(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    async with StateDB(db_path):
        pass

    result = await run_findings({"run": str(uuid.uuid4())})
    assert result == {"found": False}


async def test_run_findings_ambiguous_reference(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid_a, name="triage-run-alpha")
    await seed_session(db_path, session_id=sid_b, name="triage-run-beta")

    result = await run_findings({"run": "triage-run"})

    assert result["found"] is True
    assert result["ambiguous"] is True
    assert {c["id"] for c in result["candidates"]} == {sid_a, sid_b}


async def test_run_findings_zero_operations(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running")

    result = await run_findings({"run": sid})

    assert result["found"] is True
    assert result["messages"] == {"items": [], "truncated": False, "returned": 0, "total": 0}
    assert result["toolCalls"] == {"items": [], "truncated": False}
    assert result["errors"] == {"items": [], "truncated": False}
    assert result["artifacts"] == {
        "contract": None,
        "contractTruncated": False,
        "verification": None,
        "verificationTruncated": False,
        "artifactsPath": None,
    }


async def test_run_findings_messages_happy_path(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    await seed_branch(
        db_path, branch_id=f"{sid}-br1", session_id=sid, name="critic", agent_name="critic"
    )
    await seed_text_message(
        db_path,
        branch_id=f"{sid}-br1",
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "the diff looks correct"},
        timestamp=10.0,
    )

    result = await run_findings({"run": sid, "kind": "messages"})

    assert result["found"] is True
    assert "toolCalls" not in result
    assert "errors" not in result
    assert "artifacts" not in result
    messages = result["messages"]["items"]
    assert len(messages) == 1
    assert messages[0]["agentName"] == "critic"
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "the diff looks correct"


async def test_run_findings_tool_calls_success_and_error(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(
        db_path, branch_id=branch_id, session_id=sid, name="implementer", agent_name="implementer"
    )
    await seed_action_pair(
        db_path,
        branch_id=branch_id,
        request_id=f"{sid}-req1",
        response_id=f"{sid}-res1",
        function="Bash",
        arguments={"command": "pytest -q"},
        output="12 passed",
        timestamp=10.0,
    )
    await seed_action_pair(
        db_path,
        branch_id=branch_id,
        request_id=f"{sid}-req2",
        response_id=f"{sid}-res2",
        function="Bash",
        arguments={"command": "pytest -q"},
        output="process exited with code 1.\nerror: 1 failed",
        timestamp=11.0,
    )

    result = await run_findings({"run": sid, "kind": "tool_calls"})

    calls = result["toolCalls"]["items"]
    assert len(calls) == 2
    outcomes = {call["function"]: call["outcome"] for call in calls}
    # Both requests share function name "Bash"; assert by position instead.
    assert calls[0]["outcome"] == "success"
    assert calls[1]["outcome"] == "error"
    assert calls[1]["exitCode"] == 1
    assert outcomes  # sanity: dict built without raising


async def test_run_findings_errors_kind_includes_tool_and_branch_and_session(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="failed")
    branch_id = f"{sid}-br1"
    await seed_branch(
        db_path,
        branch_id=branch_id,
        session_id=sid,
        name="implementer",
        agent_name="implementer",
        status="failed",
        started_at=10.0,
        ended_at=20.0,
    )
    await seed_action_pair(
        db_path,
        branch_id=branch_id,
        request_id=f"{sid}-req1",
        response_id=f"{sid}-res1",
        function="Bash",
        arguments={"command": "make test"},
        output="error: make: No such file or directory",
        timestamp=10.0,
    )

    result = await run_findings({"run": sid, "kind": "errors"})

    items = result["errors"]["items"]
    kinds = {item["kind"] for item in items}
    assert kinds == {"tool_call", "branch_status", "session_status"}
    session_error = next(item for item in items if item["kind"] == "session_status")
    assert session_error["statusReasonCode"] == "run_failed"
    assert session_error["message"] == "the critic rejected the diff"


async def test_run_findings_artifacts_kind(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    contract = {"files": ["report.md"], "required": True}
    verification = {"status": "not_recorded"}
    await seed_session(
        db_path,
        session_id=sid,
        status="completed",
        artifacts_path="/Users/admin/private/artifacts",
        artifact_contract_json=contract,
        artifact_verification_json=verification,
    )

    result = await run_findings({"run": sid, "kind": "artifacts"})

    artifacts = result["artifacts"]
    assert artifacts["contract"] == contract
    assert artifacts["contractTruncated"] is False
    assert artifacts["verification"] == verification
    assert artifacts["verificationTruncated"] is False
    assert artifacts["artifactsPath"] == "artifacts"


async def test_a_secret_nested_under_a_credential_name_is_withheld_on_both_read_paths(
    db_path, monkeypatch
):
    """A credential field name has to cover what is stored underneath it.

    The two read layers share one rule about which field names a secret, so
    a caller must not be served on one path what it is denied on the other.
    Testing that rule directly can't see this gap: both layers agree `auth`
    names a credential, but one of them served the object stored under it
    anyway because only one consulted the name before descending into a
    container. So this asks both public tools for the same payload and
    requires the same answer.

    The planted value is deliberately shapeless -- spaces, no known prefix,
    no header or assignment form -- so nothing except the field name can
    withhold it; a secret that looked like one would pass here regardless
    of whether the name was consulted.
    """
    import json

    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.operator.run_findings import run_findings
    from lionagi.studio.services import invocations

    nested = "correct horse battery staple"
    payload = {
        "auth": {"value": nested},
        "credentials": [nested],
        "steps": [{"api_key": {"header": nested}}],
        "files": ["report.md"],
    }

    sid = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid,
        status="completed",
        artifact_contract_json=payload,
        artifact_verification_json={"status": "ok"},
    )

    async def fake_get_artifact(_artifact_id, **_kwargs):
        return {"id": "a-nested", "kind": "result", "name": "result", "content": payload}

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)

    findings = await run_findings({"run": sid, "kind": "artifacts"})
    artifact = await get_artifact({"artifact_id": "a-nested"})

    # Positive control: the value really is in the payload both tools read, so
    # the two absence checks below are not vacuously true.
    assert nested in json.dumps(payload)

    assert nested not in json.dumps(findings)
    assert nested not in json.dumps(artifact)
    # And the ordinary field beside it is still served. Withholding too much is
    # a defect too, and a quieter one.
    assert findings["artifacts"]["contract"]["files"] == ["report.md"]


async def test_run_findings_oversized_artifact_contract_is_capped(db_path):
    """A multi-megabyte artifact contract must not make the serialized
    response exceed the same aggregate bound every other findings section
    honors -- redaction alone is not a size bound."""
    from lionagi.studio.operator.redact import ARTIFACT_BYTE_CAP
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    # Many small strings rather than one giant string: redact_arguments()
    # caps any single string field at PER_ITEM_TEXT_CAP, so the payload must
    # stay large in aggregate *after* redaction, not just before it.
    huge_contract = {"files": [f"artifact-file-number-{i:08d}.txt" for i in range(160_000)]}
    await seed_session(
        db_path,
        session_id=sid,
        status="completed",
        artifact_contract_json=huge_contract,
        artifact_verification_json={"status": "ok"},
    )

    result = await run_findings({"run": sid, "kind": "artifacts"})

    artifacts = result["artifacts"]
    assert artifacts["contractTruncated"] is True
    assert artifacts["contract"] != huge_contract
    assert artifacts["verificationTruncated"] is False
    # resolve_artifact_verification labels a stored verdict it cannot
    # re-check against current disk state (no real artifacts_path here)
    # rather than silently reporting it as fresh.
    assert artifacts["verification"] == {"status": "ok", "staleness_check": "unknown"}

    import json

    assert len(json.dumps(artifacts["contract"])) < ARTIFACT_BYTE_CAP


async def test_run_findings_agent_filter(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    await seed_branch(
        db_path, branch_id=f"{sid}-br1", session_id=sid, name="critic", agent_name="critic"
    )
    await seed_branch(
        db_path,
        branch_id=f"{sid}-br2",
        session_id=sid,
        name="implementer",
        agent_name="implementer",
    )
    await seed_text_message(
        db_path,
        branch_id=f"{sid}-br1",
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "critic says: looks good"},
    )
    await seed_text_message(
        db_path,
        branch_id=f"{sid}-br2",
        message_id=f"{sid}-m2",
        role="assistant",
        content={"assistant_response": "implementer says: done"},
    )

    result = await run_findings({"run": sid, "kind": "messages", "agent_filter": "critic"})

    messages = result["messages"]["items"]
    assert len(messages) == 1
    assert messages[0]["agentName"] == "critic"


async def test_run_findings_agent_filter_matches_branch_id(db_path):
    """The filter must accept a known operation/branch id, not only a
    display name substring -- a caller who only knows the id (e.g. from a
    prior run_progress `currentOps` entry) still has to be able to narrow."""
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    target_branch_id = f"{sid}-br-target-op"
    await seed_branch(
        db_path,
        branch_id=target_branch_id,
        session_id=sid,
        name="worker-1",
        agent_name="worker",
    )
    await seed_branch(
        db_path,
        branch_id=f"{sid}-br-other-op",
        session_id=sid,
        name="worker-2",
        agent_name="worker",
    )
    await seed_text_message(
        db_path,
        branch_id=target_branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "from the target op"},
    )
    await seed_text_message(
        db_path,
        branch_id=f"{sid}-br-other-op",
        message_id=f"{sid}-m2",
        role="assistant",
        content={"assistant_response": "from the other op"},
    )

    result = await run_findings({"run": sid, "kind": "messages", "agent_filter": "target-op"})

    messages = result["messages"]["items"]
    assert len(messages) == 1
    assert messages[0]["content"] == "from the target op"


async def test_run_findings_secret_shaped_argument_is_redacted(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(
        db_path, branch_id=branch_id, session_id=sid, name="deployer", agent_name="deployer"
    )
    await seed_action_pair(
        db_path,
        branch_id=branch_id,
        request_id=f"{sid}-req1",
        response_id=f"{sid}-res1",
        function="deploy",
        arguments={"api_key": "ghp_abcdefghijklmnopqrstuvwxyz1234", "region": "us-east-1"},
        output="deployed",
    )

    result = await run_findings({"run": sid, "kind": "tool_calls"})

    call = result["toolCalls"]["items"][0]
    assert call["arguments"]["api_key"] == "[redacted]"
    assert call["arguments"]["region"] == "us-east-1"
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in str(result)


async def test_run_findings_absolute_path_shaped_data_is_redacted(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(
        db_path, branch_id=branch_id, session_id=sid, name="writer", agent_name="writer"
    )
    await seed_text_message(
        db_path,
        branch_id=branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "wrote output to /Users/admin/secret-project/notes.md"},
    )
    await seed_action_pair(
        db_path,
        branch_id=branch_id,
        request_id=f"{sid}-req1",
        response_id=f"{sid}-res1",
        function="Write",
        arguments={"file_path": "/Users/admin/secret-project/notes.md", "content": "hello"},
        output="ok",
    )

    result = await run_findings({"run": sid})

    assert "/Users/admin/secret-project" not in str(result)
    assert result["messages"]["items"][0]["content"].endswith("notes.md")
    assert result["toolCalls"]["items"][0]["arguments"]["file_path"] == "notes.md"


async def test_run_findings_path_with_spaces_is_fully_redacted(db_path):
    """A directory segment containing spaces (e.g. 'My Project') must not
    survive redaction just because the naive path regex stops at the first
    space -- only the leaf filename may remain."""
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(
        db_path, branch_id=branch_id, session_id=sid, name="writer", agent_name="writer"
    )
    await seed_text_message(
        db_path,
        branch_id=branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={
            "assistant_response": (
                "wrote to /Users/lion/My Project/private notes/secret.txt and finished"
            )
        },
    )

    result = await run_findings({"run": sid, "kind": "messages"})

    text = result["messages"]["items"][0]["content"]
    assert "My Project" not in text
    assert "private notes" not in text
    assert "/Users/lion" not in text
    assert text.endswith("secret.txt and finished")


async def test_run_findings_bearer_and_assignment_secrets_in_free_text_are_redacted(db_path):
    """Generic 'Authorization: Bearer <token>' and 'KEY=<value>' shell-style
    assignments embedded in free text must be redacted even though they carry
    no recognized fixed token prefix and no dedicated dict key marker."""
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(
        db_path, branch_id=branch_id, session_id=sid, name="deployer", agent_name="deployer"
    )
    await seed_text_message(
        db_path,
        branch_id=branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "Authorization: Bearer super-secret-value-123456789"},
    )
    await seed_action_pair(
        db_path,
        branch_id=branch_id,
        request_id=f"{sid}-req1",
        response_id=f"{sid}-res1",
        function="Bash",
        arguments={"command": "export API_KEY=super-secret-value-123456789"},
        output="ok",
    )

    result = await run_findings({"run": sid})

    assert "super-secret-value-123456789" not in str(result)
    assert "[redacted]" in result["messages"]["items"][0]["content"]
    assert "[redacted]" in result["toolCalls"]["items"][0]["arguments"]["command"]


async def test_run_findings_per_branch_item_cap(db_path):
    from lionagi.studio.operator.redact import PER_KIND_ITEM_CAP
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(
        db_path, branch_id=branch_id, session_id=sid, name="chatty", agent_name="chatty"
    )
    for index in range(PER_KIND_ITEM_CAP + 20):
        await seed_text_message(
            db_path,
            branch_id=branch_id,
            message_id=f"{sid}-m{index}",
            role="assistant",
            content={"assistant_response": f"update {index}"},
            timestamp=float(index),
        )

    result = await run_findings({"run": sid, "kind": "messages"})

    items = result["messages"]["items"]
    assert len(items) <= PER_KIND_ITEM_CAP
    # The window is the tail: the newest update must survive the cap.
    assert items[-1]["content"] == f"update {PER_KIND_ITEM_CAP + 19}"


async def test_run_findings_cross_project_isolation(db_path):
    from lionagi.studio.operator.run_findings import run_findings

    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    await seed_session(
        db_path, session_id=sid_a, name="shared-name", project="acme/research", status="completed"
    )
    await seed_session(
        db_path, session_id=sid_b, name="shared-name", project="acme/ops", status="completed"
    )
    await seed_branch(db_path, branch_id=f"{sid_a}-br1", session_id=sid_a, name="alpha-worker")
    await seed_branch(db_path, branch_id=f"{sid_b}-br1", session_id=sid_b, name="beta-worker")
    await seed_text_message(
        db_path,
        branch_id=f"{sid_a}-br1",
        message_id=f"{sid_a}-m1",
        role="assistant",
        content={"assistant_response": "alpha project secret finding"},
    )
    await seed_text_message(
        db_path,
        branch_id=f"{sid_b}-br1",
        message_id=f"{sid_b}-m1",
        role="assistant",
        content={"assistant_response": "beta project unrelated finding"},
    )

    result_a = await run_findings({"run": sid_a, "kind": "messages"})
    result_b = await run_findings({"run": sid_b, "kind": "messages"})

    assert result_a["id"] == sid_a
    assert result_b["id"] == sid_b
    a_text = [m["content"] for m in result_a["messages"]["items"]]
    b_text = [m["content"] for m in result_b["messages"]["items"]]
    assert a_text == ["alpha project secret finding"]
    assert b_text == ["beta project unrelated finding"]
    assert "beta" not in str(result_a)
    assert "alpha" not in str(result_b)


async def test_run_findings_exact_id_of_a_foreign_project_run_is_not_found(db_path, monkeypatch):
    """run_findings inherits resolve_run() from run_progress.py -- an exact-id
    reference to a run outside the calling turn's project must not resolve,
    the same way the text-search arm is already scoped."""
    from lionagi.studio.operator.run_findings import run_findings
    from lionagi.studio.operator.store import OperatorStore

    foreign = str(uuid.uuid4())
    await seed_session(
        db_path, session_id=foreign, name="foreign-run", project="acme/ops", status="completed"
    )

    store = OperatorStore(db_path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="what did that run find?",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "acme/research",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    result = await run_findings({"run": foreign})
    assert result == {"found": False}


async def test_run_findings_turn_with_no_project_context_fails_closed(db_path, monkeypatch):
    """A turn whose identity is present but whose own context names no
    project must never fall back to matching every project's runs --
    run_findings inherits this from resolve_run() (run_progress.py) the same
    way it already inherits project scoping."""
    from lionagi.studio.operator.run_findings import run_findings
    from lionagi.studio.operator.run_progress import MissingOwnerContextError
    from lionagi.studio.operator.store import OperatorStore

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, name="nightly-triage", status="completed")

    store = OperatorStore(db_path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="what did that run find?",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    with pytest.raises(MissingOwnerContextError):
        await run_findings({"run": sid})


async def test_run_findings_env_secret_value_is_redacted_even_without_a_known_shape(
    db_path, monkeypatch
):
    """scrub_text's patterns only catch a secret that is *shaped* like one
    (a known prefix, an assignment, a header). A run's own config can carry
    an arbitrary secret value with none of those shapes -- this must still
    be redacted if it is echoed back verbatim in a message."""
    from lionagi.studio.operator.run_findings import run_findings

    monkeypatch.setenv("ACME_APP_API_KEY", "greenelephant")
    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(db_path, branch_id=branch_id, session_id=sid, name="worker")
    await seed_text_message(
        db_path,
        branch_id=branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "the configured secret is greenelephant, reused it"},
    )

    result = await run_findings({"run": sid, "kind": "messages"})

    assert "greenelephant" not in str(result)
    assert result["messages"]["items"][0]["content"].endswith("reused it")


async def test_run_findings_rejects_unknown_fields(db_path):
    from pydantic import ValidationError

    from lionagi.studio.operator.run_findings import RunFindingsInput

    with pytest.raises(ValidationError):
        RunFindingsInput.model_validate({"run": "x", "unexpected": True})


async def test_run_findings_reports_the_message_window_it_did_not_load(db_path):
    """A run bigger than the message window must say so.

    The window is fifty messages; the byte cap the flag used to report is two
    megabytes. Fifty messages never approach two megabytes, so the flag read
    False on every response the window had trimmed -- and on this surface that
    reads as "here is the run", not "here is the tail of it". Measured against
    live data at the time this was written, 39% of branches were over the
    window and the largest held 48,123 messages, of which the tool returned 50
    and called the result complete.
    """
    from lionagi.studio.operator.redact import PER_KIND_ITEM_CAP
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    bid = f"{sid}-b1"
    await seed_session(db_path, session_id=sid, status="completed")
    await seed_branch(db_path, branch_id=bid, session_id=sid, name="worker-1")

    total_seeded = PER_KIND_ITEM_CAP + 10
    for i in range(total_seeded):
        await seed_text_message(
            db_path,
            branch_id=bid,
            message_id=f"{bid}-m{i:03d}",
            role="assistant",
            content={"assistant_response": f"step {i}"},
            timestamp=100.0 + i,
        )

    result = await run_findings({"run": sid})

    messages = result["messages"]
    assert messages["total"] == total_seeded
    assert messages["returned"] == PER_KIND_ITEM_CAP
    assert messages["truncated"] is True
    # The counts are the point: "truncated" alone cannot tell a reader whether
    # it is missing three messages or forty-eight thousand.
    assert messages["returned"] < messages["total"]

    # Everything derived from the same window inherits the incompleteness. An
    # errors list is the dangerous one -- it reads as authoritative, and its
    # tool-call half came from a fraction of the messages.
    assert result["toolCalls"]["truncated"] is True
    assert result["errors"]["truncated"] is True


async def test_run_findings_does_not_claim_truncation_when_it_loaded_everything(db_path):
    """Companion: the flag must stay False on a run that fits.

    Without this the row above is satisfiable by hardcoding True, which is the
    same defect in the other direction -- a surface that always warns tells a
    reader nothing and gets ignored exactly when it matters.
    """
    from lionagi.studio.operator.run_findings import run_findings

    sid = str(uuid.uuid4())
    bid = f"{sid}-b1"
    await seed_session(db_path, session_id=sid, status="completed")
    await seed_branch(db_path, branch_id=bid, session_id=sid, name="worker-1")
    for i in range(3):
        await seed_text_message(
            db_path,
            branch_id=bid,
            message_id=f"{bid}-m{i}",
            role="assistant",
            content={"assistant_response": f"step {i}"},
            timestamp=100.0 + i,
        )

    result = await run_findings({"run": sid})

    assert result["messages"] == {
        "items": result["messages"]["items"],
        "truncated": False,
        "returned": 3,
        "total": 3,
    }
    assert len(result["messages"]["items"]) == 3
    assert result["toolCalls"]["truncated"] is False
    assert result["errors"]["truncated"] is False
