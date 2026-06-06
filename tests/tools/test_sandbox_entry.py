# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Full-loop tests for the in-sandbox emit side (ADR-0083 Phase 2).

These drive the REAL emit path: ``attach_persistence_emitter`` wires the branch's
``_persist_via_bus`` emit hook (same seam persistence uses), so firing it sends a
``MESSAGE_ADD`` through the session bus → the emitter serializes ``@@LIONDB@@``
wire lines → we feed those lines to a ``SandboxBridge`` → assert the host
``state.db`` rows. This closes the loop emit-side(sandbox) → wire → bridge(host) →
StateDB entirely in-process — no Daytona, no pi, no network. Phase 1's
``test_bridge_matches_local_live_persist`` already pins bridge output == local
live-persist, so emitter→bridge correct ⇒ emitter→bridge == local.
"""

from __future__ import annotations

from pathlib import Path

from lionagi import Branch, Session
from lionagi.protocols.messages.manager import MessageManager
from lionagi.state.db import StateDB
from lionagi.tools.sandbox_bridge import SandboxBridge
from lionagi.tools.sandbox_entry import attach_persistence_emitter
from lionagi.tools.sandbox_protocol import encode_event


def _instruction(text: str, recipient: str):
    return MessageManager.create_instruction(instruction=text, sender="u", recipient=recipient)


async def _replay(lines: list[str], db_path: str, **bridge_kw) -> SandboxBridge:
    bridge = SandboxBridge(db_path=db_path, **bridge_kw)
    await bridge.start()
    for line in lines:
        ok = await bridge.feed_line(line)
        assert ok, f"line was not recognized as a @@LIONDB@@ event: {line!r}"
    await bridge.finish(status="completed")
    return bridge


async def test_emitter_streams_branch_and_messages_to_bridge(tmp_path: Path):
    session = Session(default_branch=Branch(name="orc"))
    worker = Branch(name="worker-1", system="you are a worker")
    session.include_branches(worker)

    captured: list[str] = []
    attach_persistence_emitter(
        session, worker, lambda ev: captured.append(encode_event(ev)), model="m", provider="pi"
    )

    m1 = _instruction("a", str(worker.id))
    m2 = _instruction("b", str(worker.id))
    # Fire the REAL emit hook (MESSAGE_ADD on the session bus).
    await worker._persist_via_bus(m1)
    await worker._persist_via_bus(m2)

    # branch event emitted once (on first message), then one event per message.
    assert len(captured) == 3

    bridge = await _replay(
        captured, str(tmp_path / "bridge.db"), invocation_kind="flow", model="m", provider="pi"
    )

    async with StateDB(str(tmp_path / "bridge.db")) as db:
        s = await db.get_session(bridge.session_id)
        b = await db.get_branch(str(worker.id))
        sys_row = await db.get_message(str(worker.system.id))
        prog = await db.get_progression(bridge._branch_prog_ids[str(worker.id)])
        m1_row = await db.get_message(str(m1.id))

    assert s["status"] == "completed"
    assert b is not None and b["session_id"] == bridge.session_id
    assert b["system_msg_id"] == str(worker.system.id)  # construction-time system carried
    assert sys_row is not None
    assert prog == [str(m1.id), str(m2.id)]  # ordered conversation timeline
    assert m1_row["content"] is not None and m1_row["role"] == "user"


async def test_emitter_multi_branch_lands_all_in_session_timeline(tmp_path: Path):
    """Two branches, one bridge — every branch's messages reach the session timeline."""
    session = Session(default_branch=Branch(name="orc"))
    w1 = Branch(name="worker-1")
    w2 = Branch(name="worker-2")
    session.include_branches(w1)
    session.include_branches(w2)

    captured: list[str] = []
    sink = lambda ev: captured.append(encode_event(ev))  # noqa: E731
    attach_persistence_emitter(session, w1, sink)
    attach_persistence_emitter(session, w2, sink)

    m1 = _instruction("from-w1", str(w1.id))
    m2 = _instruction("from-w2", str(w2.id))
    await w1._persist_via_bus(m1)
    await w2._persist_via_bus(m2)

    bridge = await _replay(captured, str(tmp_path / "bridge.db"), invocation_kind="flow")

    async with StateDB(str(tmp_path / "bridge.db")) as db:
        b1 = await db.get_branch(str(w1.id))
        b2 = await db.get_branch(str(w2.id))
        session_prog = await db.get_progression(bridge.session_prog_id)
        w1_prog = await db.get_progression(bridge._branch_prog_ids[str(w1.id)])
        w2_prog = await db.get_progression(bridge._branch_prog_ids[str(w2.id)])

    assert b1 is not None and b2 is not None
    assert w1_prog == [str(m1.id)]
    assert w2_prog == [str(m2.id)]
    # Both workers' messages land in the single session timeline (Studio renders one).
    assert set(session_prog) == {str(m1.id), str(m2.id)}
