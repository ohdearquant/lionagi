# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression test for the flow-created clone-branch persistence gap (codex P1, #1833).

`workflow_run._setup_run_persist` registers per-branch persistence
(`register_branch_hook`) only for the branches present on `session.branches`
at setup time. `session.flow()` -> `FlowExecutor._preallocate_all_branches`
then clones `session.default_branch` for every operation that has a
predecessor and no explicit `branch_id` (exactly what the Studio compiler
produces for any multi-step workflow). Those clones are born *after* setup,
so without the `on_branch_created` seam their transcripts never persist —
even though the run-DAG *signals* still render, because those persist via a
separate session-level observer, not the per-branch message hook.

This test proves the fix by exercising the exact code path
`workflow_run.run_workflow_def` uses (`Session.flow(..., on_branch_created=...)`
wired to `register_branch_hook`), forcing a real branch clone, producing a
real persisted message on it (via `branch.communicate()`, which -- unlike
the Studio "chat" node kind -- actually calls `branch.msgs.a_add_message()`
and so fires the `on_message_added` -> `MESSAGE_ADD` hook chain that
`register_branch_hook` wires into StateDB), and then reading the message
back out of a *fresh* StateDB connection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
pytest.importorskip("fastapi", reason="studio extra not installed")


def _mock_chat_branch(name: str = "workflow-default"):
    """Same convention as test_workflow_run.py / test_edge_conditions_tdd.py."""
    from lionagi.protocols.generic.event import EventStatus
    from lionagi.session.branch import Branch
    from lionagi.testing import LionAGIMockFactory

    branch = Branch(user="test_user", name=name)

    async def _fake_invoke(**kwargs):
        return LionAGIMockFactory.create_api_calling_mock(
            response_data="go",
            status=EventStatus.COMPLETED,
            model="gpt-4-mini",
        )

    mock_chat_model = LionAGIMockFactory.create_mocked_imodel(
        provider="openai", model="gpt-4-mini", response="overridden-below"
    )
    mock_chat_model.invoke = AsyncMock(side_effect=_fake_invoke)
    branch.chat_model = mock_chat_model
    return branch


@pytest.fixture
def patched_env(tmp_path: Path, monkeypatch):
    import lionagi.state.db as db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    return db_path


async def test_flow_clone_branch_transcript_persists(patched_env):
    """A downstream op with a predecessor and no branch_id gets a CLONED
    branch (verified: its id differs from the default branch); this test
    proves that clone's transcript (not just the default branch's, not just
    run-completion) lands in StateDB -- reproducing exactly the seam
    `run_workflow_def` wires via `on_branch_created=lambda b:
    register_branch_hook(ctx, b)`.
    """
    db_path = patched_env

    from lionagi.cli.orchestrate._orchestration import register_branch_hook
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import (
        _setup_run_persist,
        _teardown_run_persist,
    )

    mock_branch = _mock_chat_branch()
    session = Session(default_branch=mock_branch)

    # Two "communicate" ops (communicate() -- unlike the Studio "chat" node
    # kind -- calls branch.msgs.a_add_message(), the thing that actually
    # exercises the persistence hook chain): op2 depends on op1 with no
    # explicit branch, so _preallocate_all_branches must clone a branch for it.
    builder = OperationGraphBuilder("clone-persist-test")
    op1_id = builder.add_operation("communicate", instruction="draft a message")
    op2_id = builder.add_operation("communicate", instruction="continue the thread")
    graph = builder.graph

    ctx = await _setup_run_persist(session, invocation_kind="flow")

    created_branches: list[Any] = []

    def _on_branch_created(branch: Any) -> None:
        created_branches.append(branch)
        register_branch_hook(ctx, branch)

    status = "completed"
    exc: BaseException | None = None
    try:
        result = await session.flow(
            graph,
            context={},
            on_branch_created=_on_branch_created,
        )
    except Exception as e:  # noqa: BLE001
        status = "failed"
        exc = e
        raise
    finally:
        await _teardown_run_persist(ctx, status=status, exception=exc)

    assert not isinstance(result.get("operation_results", {}).get(op1_id), dict) or (
        "error" not in result["operation_results"].get(op1_id, {})
    )
    assert "error" not in result["operation_results"].get(op2_id, {})

    # --- Prove op2 actually ran on a CLONE branch (predecessor, no branch_id) ---
    assert len(created_branches) == 1, (
        "expected exactly one clone: op1 has no predecessor (stays on default "
        "branch); op2 depends on op1 with no explicit branch_id, so "
        "_preallocate_all_branches must clone default_branch for it"
    )
    clone_branch = created_branches[0]
    clone_branch_id = str(clone_branch.id)
    assert clone_branch_id != str(mock_branch.id), (
        "the persisted branch must be the CLONE flow allocated for op2, not "
        "the session's original default branch"
    )

    # --- Query a FRESH StateDB connection (ctx["db"] is closed by teardown) ---
    from lionagi.state.db import StateDB

    db = StateDB(db_path)
    await db.open()
    try:
        branch_row = await db.get_branch(clone_branch_id)
        assert branch_row is not None, (
            "clone branch row missing from StateDB -- the clone was never "
            "registered via register_branch_hook (the P1 bug)"
        )
        assert branch_row["session_id"] == str(session.id)

        messages = await db.get_branch_messages(clone_branch_id)
        assert len(messages) >= 2, (
            "the clone branch's communicate() call must have persisted its "
            "instruction + assistant-response messages under ITS OWN "
            f"progression; got {messages!r}"
        )
        senders_recipients = {m.get("sender") for m in messages} | {
            m.get("recipient") for m in messages
        }
        assert clone_branch_id in senders_recipients, (
            "persisted messages must be attributed to the clone branch, not the default branch"
        )
    finally:
        await db.close()


async def test_default_branch_op_with_no_predecessor_is_not_cloned(patched_env):
    """Guard: an op with no predecessor and no branch_id stays on the
    default branch (no clone) -- confirms the setup in the test above forces
    a clone specifically for the DOWNSTREAM op, not both.
    """
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.session import Session

    mock_branch = _mock_chat_branch()
    session = Session(default_branch=mock_branch)

    builder = OperationGraphBuilder("no-clone-test")
    builder.add_operation("communicate", instruction="draft a message")
    graph = builder.graph

    created_branches: list[Any] = []
    await session.flow(
        graph,
        context={},
        on_branch_created=lambda b: created_branches.append(b),
    )

    assert created_branches == []
