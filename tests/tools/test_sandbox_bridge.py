# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Parity tests for the sandbox persistence bridge (ADR-0083).

The central claim of ADR-0083 is that a flow running inside a sandbox, whose
message events are replayed through ``SandboxBridge``, lands the *same*
``state.db`` rows a local run lands — so ``li monitor`` / Studio treat it
identically. ``test_bridge_matches_local_live_persist`` proves that by driving
the REAL local live-persist path (``start_live_persist`` + ``_register_branch_hook``
+ ``stop_live_persist``) and the bridge with the SAME message objects, then
asserting their progressions + message rows match. No Daytona, no LLM, no network.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lionagi import Branch, Session
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    _register_branch_hook,
    start_live_persist,
    stop_live_persist,
)
from lionagi.protocols.messages.manager import MessageManager
from lionagi.state.db import StateDB
from lionagi.tools.sandbox_bridge import SandboxBridge
from lionagi.tools.sandbox_protocol import (
    SENTINEL,
    branch_event,
    decode_line,
    encode_event,
    message_event,
    phase_event,
)

# ── fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture
def local_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the DEFAULT_DB_PATH (used by start_live_persist) at a temp file."""
    db_path = tmp_path / "local.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _minimal_env(orc_branch: Branch | None = None) -> OrchestrationEnv:
    """Stub OrchestrationEnv with just the fields live-persist reads.

    Mirrors tests/cli/orchestrate/test_live_persist.py — live-persist only
    touches ``env.session`` and ``env._live_persist``.
    """
    if orc_branch is None:
        orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    return OrchestrationEnv(
        run=MagicMock(),
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        default_model_spec="claude",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )


def _instruction(text: str, recipient: str):
    return MessageManager.create_instruction(instruction=text, sender="u", recipient=recipient)


# ── protocol ──────────────────────────────────────────────────────────────────


def test_protocol_roundtrip_and_noise():
    ev = message_event("b1", {"id": "m1", "role": "user", "content": "hi"})
    line = encode_event(ev)
    assert line.startswith(SENTINEL) and line.endswith("\n")
    assert decode_line(line) == ev

    # non-event lines and corrupt payloads degrade to None, never raise
    assert decode_line("ordinary stdout\n") is None
    assert decode_line('@@SIG@@ {"t": "RunEnd"}\n') is None  # the OTHER stream
    assert decode_line(SENTINEL + "{not json\n") is None
    assert decode_line(SENTINEL + "[1,2,3]\n") is None  # not a dict
    assert decode_line(SENTINEL + "\n") is None


# ── bridge core: rows land, session lifecycle ─────────────────────────────────


async def test_bridge_persists_session_branch_messages(tmp_path: Path):
    db_path = str(tmp_path / "bridge.db")
    worker = Branch(name="worker-1", system="you are a worker")
    m1 = _instruction("a", str(worker.id))
    m2 = _instruction("b", str(worker.id))

    bridge = SandboxBridge(
        db_path=db_path,
        invocation_kind="flow",
        model="openrouter/deepseek/deepseek-v4-flash",
        provider="pi",
        project="lionagi",
        node_metadata={"sandbox": {"backend": "daytona", "harness": "pi"}},
    )
    sid = await bridge.start()

    # session is visible (running) BEFORE any branch/message event
    async with StateDB(db_path) as db:
        s0 = await db.get_session(sid)
    assert s0["status"] == "running"
    assert s0["invocation_kind"] == "flow"
    assert s0["provider"] == "pi"
    assert s0["model"] == "openrouter/deepseek/deepseek-v4-flash"

    await bridge.feed_line(
        encode_event(
            branch_event(
                worker.to_dict(mode="db"),
                system_msg=worker.system.to_dict(mode="db"),
                model="openrouter/deepseek/deepseek-v4-flash",
                provider="pi",
            )
        )
    )
    await bridge.on_event(message_event(str(worker.id), m1.to_dict(mode="db")))
    await bridge.on_event(message_event(str(worker.id), m2.to_dict(mode="db")))
    await bridge.finish(status="completed")

    async with StateDB(db_path) as db:
        s = await db.get_session(sid)
        b = await db.get_branch(str(worker.id))
        sys_row = await db.get_message(str(worker.system.id))
        prog = await db.get_progression(bridge._branch_prog_ids[str(worker.id)])
        session_prog = await db.get_progression(bridge.session_prog_id)

    assert s["status"] == "completed"
    assert s["first_msg_id"] == str(m1.id)
    assert s["last_msg_id"] == str(m2.id)
    assert s["ended_at"] is not None
    # branch bound to THIS session, system message inserted + pointed at
    assert b["session_id"] == sid
    assert b["system_msg_id"] == str(worker.system.id)
    assert sys_row is not None
    # the conversation messages are in order; the system msg is NOT in the timeline
    assert prog == [str(m1.id), str(m2.id)]
    assert set(session_prog) == {str(m1.id), str(m2.id)}


# ── THE parity proof: bridge output == local live-persist output ──────────────


async def test_bridge_matches_local_live_persist(local_db_path: Path, tmp_path: Path):
    """Same messages → identical progressions + message rows, local vs bridge."""
    worker = Branch(name="worker-1", system="shared system")
    m1 = _instruction("from-worker-a", str(worker.id))
    m2 = _instruction("from-worker-b", str(worker.id))

    # ---- LOCAL reference: the real start_live_persist / hook / stop path ----
    env = _minimal_env()
    await start_live_persist(
        env, invocation_kind="flow", model="x", provider="pi", project="lionagi"
    )
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]
    await hook(m1)
    await hook(m2)
    ctx = env._live_persist
    local_branch_prog = ctx["branch_prog_ids"][str(worker.id)]
    local_session_prog = ctx["session_prog_id"]
    await stop_live_persist(env, status="completed")

    async with StateDB() as db:  # DEFAULT_DB_PATH == local.db
        local_branch_timeline = await db.get_progression(local_branch_prog)
        local_session_timeline = await db.get_progression(local_session_prog)
        local_m1 = await db.get_message(str(m1.id))
        local_branch_row = await db.get_branch(str(worker.id))

    # ---- BRIDGE: feed the SAME message dicts over the wire ----
    bridge = SandboxBridge(
        db_path=str(tmp_path / "bridge.db"),
        invocation_kind="flow",
        model="x",
        provider="pi",
        project="lionagi",
    )
    await bridge.start()
    await bridge.on_event(
        branch_event(worker.to_dict(mode="db"), system_msg=worker.system.to_dict(mode="db"))
    )
    await bridge.on_event(message_event(str(worker.id), m1.to_dict(mode="db")))
    await bridge.on_event(message_event(str(worker.id), m2.to_dict(mode="db")))
    await bridge.finish(status="completed")

    async with StateDB(str(tmp_path / "bridge.db")) as db:
        bridge_branch_timeline = await db.get_progression(bridge._branch_prog_ids[str(worker.id)])
        bridge_session_timeline = await db.get_progression(bridge.session_prog_id)
        bridge_m1 = await db.get_message(str(m1.id))
        bridge_branch_row = await db.get_branch(str(worker.id))

    # ---- PARITY: the rows that observability reads are identical ----
    assert bridge_branch_timeline == local_branch_timeline == [str(m1.id), str(m2.id)]
    assert set(bridge_session_timeline) == set(local_session_timeline) == {str(m1.id), str(m2.id)}
    # message rows: same content + role + lion_class (what Studio renders)
    for key in ("id", "role", "content", "lion_class"):
        assert bridge_m1[key] == local_m1[key], f"message field {key!r} diverged"
    # branch system pointer is reproduced
    assert (
        bridge_branch_row["system_msg_id"]
        == local_branch_row["system_msg_id"]
        == str(worker.system.id)
    )


# ── robustness: message before its branch event ───────────────────────────────


async def test_bridge_message_before_branch_is_not_lost(tmp_path: Path):
    """A message for an unseen branch lazily creates a minimal row (no data loss)."""
    db_path = str(tmp_path / "bridge.db")
    worker = Branch(name="late-branch")
    m1 = _instruction("orphan", str(worker.id))

    bridge = SandboxBridge(db_path=db_path, invocation_kind="flow")
    await bridge.start()
    await bridge.on_event(message_event(str(worker.id), m1.to_dict(mode="db")))
    await bridge.finish(status="completed")

    async with StateDB(db_path) as db:
        b = await db.get_branch(str(worker.id))
        prog = await db.get_progression(bridge._branch_prog_ids[str(worker.id)])
    assert b is not None
    assert prog == [str(m1.id)]


# ── phase + failure status ────────────────────────────────────────────────────


async def test_bridge_phase_sets_current_phase(tmp_path: Path):
    db_path = str(tmp_path / "bridge.db")
    bridge = SandboxBridge(db_path=db_path, invocation_kind="flow")
    sid = await bridge.start()
    await bridge.on_event(phase_event("executing"))
    async with StateDB(db_path) as db:
        s = await db.get_session(sid)
    assert s["current_phase"] == "executing"
    await bridge.finish(status="completed")


async def test_bridge_finish_failed_writes_reason(tmp_path: Path):
    db_path = str(tmp_path / "bridge.db")
    bridge = SandboxBridge(db_path=db_path, invocation_kind="flow")
    sid = await bridge.start()
    await bridge.finish(status="failed", exception=RuntimeError("boom"))
    async with StateDB(db_path) as db:
        s = await db.get_session(sid)
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.exception"
