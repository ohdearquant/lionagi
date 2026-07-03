# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `_apply_session_control` (ADR-0085 part 1 control poller).

Drives apply/stamp ordering per verb class against a real StateDB (for the
finalize/mark-applying side effects) and a minimal fake executor (pause/
resume calls recorded, a real Note context, a real Pile of Operation nodes
for the message-injection "pending op" check).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.orchestrate.flow import _apply_session_control
from lionagi.models.note import Note
from lionagi.operations.node import Operation
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.types import EventStatus
from lionagi.state.db import StateDB


class _FakeGraph:
    def __init__(self, nodes: Pile[Operation]):
        self.internal_nodes = nodes


class _FakeExecutor:
    def __init__(self, *, nodes: Pile[Operation] | None = None, context: dict | None = None):
        self.paused = 0
        self.resumed = 0
        self.context = Note(**(context or {}))
        self.graph = _FakeGraph(nodes if nodes is not None else Pile(item_type={Operation}))

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1


async def _make_session(db: StateDB) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": "running",
            "invocation_kind": "flow",
            "started_at": time.time(),
        }
    )
    return sid


async def _queue_control(db: StateDB, session_id: str, verb: str, payload=None) -> dict:
    control_id = await db.insert_session_control(session_id=session_id, verb=verb, payload=payload)
    return await db.get_session_control(control_id)


def _pending_operation() -> Operation:
    return Operation(operation="chat")


# ── pause / resume: idempotent apply-then-stamp ─────────────────────────────


async def test_pause_applies_and_stamps(tmp_path: Path) -> None:
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "pause")
        executor = _FakeExecutor()

        result = await _apply_session_control(db, executor, row)

        assert result == "applied"
        assert executor.paused == 1
        finalized = await db.get_session_control(row["id"])
        assert finalized["applied_at"] is not None
        assert finalized["result"] == "applied"


async def test_resume_applies_and_stamps(tmp_path: Path) -> None:
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "resume")
        executor = _FakeExecutor()

        result = await _apply_session_control(db, executor, row)

        assert result == "applied"
        assert executor.resumed == 1
        finalized = await db.get_session_control(row["id"])
        assert finalized["result"] == "applied"


async def test_pause_already_applied_not_stamped_is_safe_to_reapply(tmp_path: Path) -> None:
    """Crash-window: apply succeeded but finalize() never landed (row still
    pending). The poller's next pass re-applies — safe, since pause() is
    idempotent on the executor and finalize() this time actually stamps it."""
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "pause")
        executor = _FakeExecutor()
        executor.pause()  # simulate: apply already landed before the crash
        assert executor.paused == 1

        # Row is still pending (applied_at IS NULL) — poller re-applies it.
        result = await _apply_session_control(db, executor, row)

        assert result == "applied"
        assert executor.paused == 2  # re-applying pause is harmless (idempotent gate)
        finalized = await db.get_session_control(row["id"])
        assert finalized["applied_at"] is not None


async def test_pause_and_resume_are_excluded_from_pending_after_finalize(tmp_path: Path) -> None:
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "pause")
        executor = _FakeExecutor()

        pending_before = await db.list_pending_session_controls(sid)
        assert len(pending_before) == 1

        await _apply_session_control(db, executor, row)

        pending_after = await db.list_pending_session_controls(sid)
        assert pending_after == []


# ── message: non-idempotent stamp-then-apply ────────────────────────────────


async def test_message_deep_merges_into_context_when_pending_op_exists(tmp_path: Path) -> None:
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "message", payload={"text": "please slow down"})
        nodes = Pile(item_type={Operation})
        nodes.include(_pending_operation())
        executor = _FakeExecutor(nodes=nodes)

        result = await _apply_session_control(db, executor, row)

        assert result == "applied"
        messages = executor.context.content.get("operator_messages")
        assert messages is not None
        assert len(messages) == 1
        assert messages[0]["text"] == "please slow down"
        finalized = await db.get_session_control(row["id"])
        assert finalized["result"] == "applied"
        assert finalized["applied_at"] is not None


async def test_message_appends_to_existing_operator_messages(tmp_path: Path) -> None:
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "message", payload={"text": "second message"})
        nodes = Pile(item_type={Operation})
        nodes.include(_pending_operation())
        executor = _FakeExecutor(
            nodes=nodes, context={"operator_messages": [{"ts": 1.0, "text": "first message"}]}
        )

        await _apply_session_control(db, executor, row)

        messages = executor.context.content["operator_messages"]
        assert [m["text"] for m in messages] == ["first message", "second message"]


async def test_message_rejected_when_no_pending_ops(tmp_path: Path) -> None:
    """Checked, not assumed: an executor with no PENDING op must reject the
    message rather than silently drop it into a context nothing will read."""
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "message", payload={"text": "too late"})
        completed_op = _pending_operation()
        completed_op.execution.status = EventStatus.COMPLETED
        nodes = Pile(item_type={Operation})
        nodes.include(completed_op)
        executor = _FakeExecutor(nodes=nodes)

        result = await _apply_session_control(db, executor, row)

        assert result == "rejected:no-pending-ops"
        assert executor.context.content.get("operator_messages") is None
        finalized = await db.get_session_control(row["id"])
        assert finalized["applied_at"] is not None
        assert finalized["result"] == "rejected:no-pending-ops"


async def test_message_rejected_when_graph_empty(tmp_path: Path) -> None:
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "message", payload={"text": "anyone there?"})
        executor = _FakeExecutor()  # empty graph

        result = await _apply_session_control(db, executor, row)

        assert result == "rejected:no-pending-ops"


async def test_message_stamped_applying_but_unapplied_is_not_reapplied(tmp_path: Path) -> None:
    """Crash-window: mark_session_control_applying() landed but the poller
    crashed before finalize_session_control(). At-most-once: the row must
    stay result='applying' — a re-poll must skip it, not risk a double
    injection if the earlier apply actually landed before the crash."""
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        control_id = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "in flight"}
        )
        await db.mark_session_control_applying(control_id)
        row = await db.get_session_control(control_id)
        assert row["result"] == "applying"
        nodes = Pile(item_type={Operation})
        nodes.include(_pending_operation())
        executor = _FakeExecutor(nodes=nodes)

        result = await _apply_session_control(db, executor, row)

        assert result is None  # left untouched
        assert executor.context.content.get("operator_messages") is None
        still_row = await db.get_session_control(control_id)
        assert still_row["applied_at"] is None
        assert still_row["result"] == "applying"


# ── unsupported verb / stop (schema-reserved, no CLI verb emits it) ────────


async def test_stop_verb_is_rejected_as_unsupported(tmp_path: Path) -> None:
    """'stop' is schema-reserved for a later slice (the checkpoint writer);
    the poller must reject it loudly rather than silently no-op forever."""
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "stop")
        executor = _FakeExecutor()

        result = await _apply_session_control(db, executor, row)

        assert result == "rejected:unsupported-verb:stop"
        finalized = await db.get_session_control(row["id"])
        assert finalized["applied_at"] is not None


# ── crash safety: apply must never raise into the caller ──────────────────


async def test_exception_during_apply_is_caught_and_recorded(tmp_path: Path) -> None:
    """The poller must never crash the run it rides alongside: an exploding
    executor.pause() is caught, recorded as a rejected result, and does not
    propagate."""

    class _ExplodingExecutor(_FakeExecutor):
        def pause(self) -> None:
            raise RuntimeError("boom")

    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "pause")
        executor = _ExplodingExecutor()

        result = await _apply_session_control(db, executor, row)

        assert result is not None
        assert result.startswith("rejected:error:")
        assert "boom" in result
        finalized = await db.get_session_control(row["id"])
        assert finalized["applied_at"] is not None
        assert finalized["result"] == result


async def test_exception_during_finalize_does_not_raise(tmp_path: Path) -> None:
    """If even the finalize-on-error write fails, _apply_session_control must
    still return a result string rather than propagating."""

    class _BrokenDB:
        def __init__(self, real_db: StateDB):
            self._real = real_db

        async def finalize_session_control(self, *_a, **_kw):
            raise RuntimeError("db unreachable")

        async def mark_session_control_applying(self, *a, **kw):
            return await self._real.mark_session_control_applying(*a, **kw)

    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        row = await _queue_control(db, sid, "pause")
        executor = _FakeExecutor()

        result = await _apply_session_control(_BrokenDB(db), executor, row)

        assert result is not None
        assert result.startswith("rejected:error:")
