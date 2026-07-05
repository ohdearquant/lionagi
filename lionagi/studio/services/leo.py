# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Leo: the Studio operator agent — session registry, tool definitions, and routes.

Security boundary: mutating tools never execute. They return a proposed_action
dict; the frontend confirms and calls the real studio endpoint directly. The
Leo backend never writes studio state.

UI driving: ui_command tools return a declarative command dict the frontend
executes client-side (navigation, form prefill). Commands never mutate server
state.

Sessions are in-memory; a server restart clears history. Auth is the studio
bearer-token gate applied at the app-level middleware, same as every other
route.
"""

# NOTE: no `from __future__ import annotations` — the Leo tool callables are
# introspected by function_to_schema, which requires real (non-string)
# parameter annotations.

import asyncio
import json
import time
import uuid
from functools import partial
from typing import Any

import anyio
from fastapi import HTTPException
from pydantic import BaseModel

from lionagi._errors import NotFoundError
from lionagi.config import settings

from ..registry import studio_route
from ._sse import sse_response

# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

# Bounded so a long-running server doesn't grow this dict forever: capacity
# eviction drops the least-recently-used session, and idle eviction sweeps
# sessions nobody has touched in a while. Both run lazily on create/access —
# there is no background timer.
_MAX_SESSIONS = 50
_IDLE_EXPIRY_SECONDS = 2 * 60 * 60


class LeoSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.branch: Any = None  # lionagi.session.branch.Branch, built lazily
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.lock = asyncio.Lock()


_SESSIONS: dict[str, LeoSession] = {}


def _evict_idle(now: float) -> None:
    stale = [
        sid for sid, sess in _SESSIONS.items() if now - sess.last_used_at > _IDLE_EXPIRY_SECONDS
    ]
    for sid in stale:
        del _SESSIONS[sid]


def _evict_lru_if_full() -> None:
    if len(_SESSIONS) < _MAX_SESSIONS:
        return
    lru_id = min(_SESSIONS, key=lambda sid: _SESSIONS[sid].last_used_at)
    del _SESSIONS[lru_id]


def create_session() -> LeoSession:
    now = time.time()
    _evict_idle(now)
    _evict_lru_if_full()
    sid = str(uuid.uuid4())
    sess = LeoSession(sid)
    _SESSIONS[sid] = sess
    return sess


def get_session(session_id: str) -> LeoSession | None:
    _evict_idle(time.time())
    sess = _SESSIONS.get(session_id)
    if sess is not None:
        sess.last_used_at = time.time()
    return sess


# ---------------------------------------------------------------------------
# Branch factory
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are Leo — the resident operator of Lion Studio, working alongside the "
    "human operator. You inspect runs, invocations, sessions, playbooks, and "
    "schedules with read-only tools, and you drive the Studio UI itself.\n\n"
    "UI driving: when the operator asks to SEE something, do not just describe "
    "it — call show_in_ui to bring the right view up on their screen (failed "
    "runs or a specific run/session → space='fleet' with status='failed'; "
    "what's scheduled → space='schedules'). Pair it with a read-only tool call "
    "so your text answer carries the facts.\n\n"
    "Routines: when the operator asks to set up a recurring task, work out the "
    "name, the cadence as a 5-field cron expression, and the prompt the "
    "scheduled agent should run, then call prefill_schedule. That opens the "
    "create form filled in for the operator to review and confirm — you never "
    "create schedules yourself.\n\n"
    "Mutating actions (launch_playbook, create_playbook, run_maintenance) "
    "surface a proposed_action for the operator to confirm; you never execute "
    "them yourself.\n\n"
    "Keep responses terse and factual."
)


def build_branch() -> Any:
    """Construct a Branch with the studio default model and all Leo tools registered."""
    from lionagi.service.manager import iModel
    from lionagi.session.branch import Branch

    chat_model = iModel(
        provider=settings.LIONAGI_CHAT_PROVIDER,
        model=settings.LIONAGI_CHAT_MODEL,
    )
    return Branch(
        system=_SYSTEM_PROMPT,
        chat_model=chat_model,
        tools=_all_tools(),
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def _all_tools() -> list:
    """Return all Leo tool callables."""
    return [
        tool_list_runs,
        tool_list_invocations,
        tool_list_sessions,
        tool_list_playbooks,
        tool_get_playbook,
        tool_list_schedules,
        tool_studio_doctor,
        tool_show_in_ui,
        tool_prefill_schedule,
        tool_launch_playbook,
        tool_create_playbook,
        tool_run_maintenance,
    ]


# -- read-only tools ---------------------------------------------------------


async def tool_list_runs(page: int = 1, per_page: int = 10) -> dict[str, Any]:
    """List recent studio runs (read-only)."""
    from lionagi.studio.services import runs as runs_svc

    raw = await runs_svc.list_runs()
    return runs_svc.paginate_runs(raw, page=page, per_page=per_page)


async def tool_list_invocations(limit: int = 10, offset: int = 0) -> dict[str, Any]:
    """List recent invocations (read-only)."""
    from lionagi.studio.services import invocations as inv_svc

    rows = await inv_svc.list_invocations(limit=limit, offset=offset)
    return {"invocations": rows}


async def tool_list_sessions(limit: int = 10) -> dict[str, Any]:
    """List recent agent sessions (read-only)."""
    from lionagi.studio.services import sessions as sess_svc

    rows = await sess_svc.list_sessions()
    return {"sessions": rows[:limit]}


async def tool_list_playbooks() -> dict[str, Any]:
    """List available playbooks (read-only)."""
    from lionagi.studio.services import playbooks as pb_svc

    result = await anyio.to_thread.run_sync(pb_svc.list_playbooks)
    return {"playbooks": result}


async def tool_get_playbook(name: str) -> dict[str, Any]:
    """Get details for a specific playbook by name (read-only)."""
    from lionagi.studio.services import playbooks as pb_svc

    result = await anyio.to_thread.run_sync(partial(pb_svc.get_playbook, name))
    if result is None:
        return {"error": f"Playbook '{name}' not found"}
    return result


async def tool_list_schedules() -> dict[str, Any]:
    """List configured schedules (read-only)."""
    from lionagi.studio.services import schedules as sched_svc

    return {"schedules": await sched_svc.list_schedules()}


async def tool_studio_doctor() -> dict[str, Any]:
    """Run the studio doctor health check (read-only)."""
    from lionagi.studio.services import admin as admin_svc

    return await admin_svc.doctor(stale_hours=1.0)


# -- UI-drive tools (client-executed, never touch server state) --------------

_UI_SPACES = {
    "mission",
    "fleet",
    "designer",
    "library",
    "schedules",
    "system",
}

_UI_STATUSES = {"failed", "running", "completed", "cancelled", "pending"}


async def tool_show_in_ui(space: str, status: str = "", tab: str = "") -> dict[str, Any]:
    """Bring a Studio view up on the operator's screen. Spaces: mission, fleet, designer, library, schedules, system. For fleet, optional status filter (failed/running/completed/cancelled/pending) and tab (run/invocation/show)."""
    space = space.strip().lower()
    if space not in _UI_SPACES:
        return {"error": f"Unknown space '{space}'. Choose one of: {', '.join(sorted(_UI_SPACES))}"}
    params: dict[str, str] = {}
    if status:
        status = status.strip().lower()
        if status not in _UI_STATUSES:
            return {
                "error": f"Unknown status '{status}'. Choose one of: {', '.join(sorted(_UI_STATUSES))}"
            }
        params["status"] = status
    if tab:
        params["tab"] = tab.strip().lower()
    return {"ui_command": {"kind": "navigate", "space": space, "params": params}}


async def tool_prefill_schedule(
    name: str, cron_expr: str, action_prompt: str, description: str = ""
) -> dict[str, Any]:
    """Open the schedule-creation form pre-filled for the operator to review and confirm. cron_expr is a 5-field cron expression; action_prompt is the instruction the scheduled agent runs each firing. Does not create anything."""
    return {
        "ui_command": {
            "kind": "prefill_schedule",
            "space": "schedules",
            "params": {
                "name": name,
                "cron": cron_expr,
                "prompt": action_prompt,
                "desc": description,
            },
        }
    }


# -- mutating tools (proposal only — never execute) -------------------------


async def tool_launch_playbook(name: str, note: str = "") -> dict[str, Any]:
    """Propose launching a playbook. Returns a proposed_action — does not execute."""
    return {
        "proposed_action": {
            "kind": "launch_playbook",
            "params": {"name": name},
            "description": f"Launch playbook '{name}'" + (f" — {note}" if note else ""),
            "endpoint": "POST /api/launches/",
        }
    }


async def tool_create_playbook(
    name: str, description: str = "", prompt: str = ""
) -> dict[str, Any]:
    """Propose creating a new playbook. Returns a proposed_action — does not execute."""
    return {
        "proposed_action": {
            "kind": "create_playbook",
            "params": {"name": name, "description": description, "prompt": prompt},
            "description": f"Create playbook '{name}'",
            "endpoint": f"POST /api/playbooks/{name}",
        }
    }


async def tool_run_maintenance(action: str) -> dict[str, Any]:
    """Propose a DB maintenance action (vacuum/checkpoint/prune). Returns a proposed_action — does not execute."""
    if action not in ("vacuum", "checkpoint", "prune"):
        return {
            "error": f"Unknown maintenance action '{action}'. Choose vacuum, checkpoint, or prune."
        }
    return {
        "proposed_action": {
            "kind": "run_maintenance",
            "params": {"action": action},
            "description": f"Run DB maintenance: {action}",
            "endpoint": "POST /api/admin/maintenance",
        }
    }


# ---------------------------------------------------------------------------
# Route handlers — leo area
# ---------------------------------------------------------------------------


class _MessageBody(BaseModel):
    content: str


def _emit(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _run_turn(sess: LeoSession, user_content: str):
    """Run one Leo turn against the session's Branch, streaming SSE events.

    Scans only the messages the Branch.ReAct() call appends during this turn
    for tool outputs carrying proposed_action / ui_command, so a proposal
    surfaced on an earlier turn never resurfaces on a later one. Must only be
    called while holding sess.lock (see send_leo_message_route).
    """
    from lionagi.protocols.messages.action_response import ActionResponse

    if sess.branch is None:
        sess.branch = build_branch()

    before = len(sess.branch.messages)
    try:
        text = await sess.branch.ReAct(instruction=user_content)
    except Exception as exc:
        yield _emit({"type": "error", "detail": str(exc)})
        yield _emit({"type": "done", "ts": time.time()})
        return

    new_messages = list(sess.branch.messages)[before:]
    for msg in new_messages:
        if not isinstance(msg, ActionResponse):
            continue
        output = getattr(msg.content, "output", None)
        if not isinstance(output, dict):
            continue
        if "ui_command" in output:
            yield _emit({"type": "ui_command", "command": output["ui_command"]})
        if "proposed_action" in output:
            yield _emit({"type": "proposed_action", "action": output["proposed_action"]})

    yield _emit({"type": "text", "content": text if isinstance(text, str) else str(text)})
    yield _emit({"type": "done", "ts": time.time()})


async def _run_turn_locked(sess: LeoSession, user_content: str):
    """Wrap _run_turn, releasing sess.lock once the stream is fully consumed or aborted."""
    try:
        async for chunk in _run_turn(sess, user_content):
            yield chunk
    finally:
        sess.lock.release()


@studio_route("/leo/sessions", method="POST", area="leo", name="create_leo_session")
async def create_leo_session_route() -> dict[str, str]:
    sess = create_session()
    return {"id": sess.id}


@studio_route(
    "/leo/sessions/{session_id}/messages",
    method="POST",
    area="leo",
    name="send_leo_message",
    response_class=None,
)
async def send_leo_message_route(session_id: str, body: _MessageBody):
    sess = get_session(session_id)
    if sess is None:
        raise NotFoundError(f"Leo session '{session_id}' not found")

    if sess.lock.locked():
        raise HTTPException(
            status_code=409,
            detail=f"Leo session '{session_id}' is already processing a message",
        )
    # No await between the check above and this acquire, so no other request
    # can interleave: acquiring an unlocked asyncio.Lock never suspends.
    await sess.lock.acquire()

    return sse_response(_run_turn_locked(sess, body.content))
