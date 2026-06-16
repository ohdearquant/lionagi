# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CLI-layer helpers for persisting a runtime Session and its branches/messages to StateDB."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.session.session import Session

    from .db import StateDB


async def persist_session(db: StateDB, session: Session) -> None:
    """Persist a runtime Session (fresh or resumed) with all branches and messages to StateDB."""
    session_dict = session.to_dict(mode="db")

    # Check if any branch already has a session in the DB (resume case)
    existing_session_id = None
    existing_session = None
    for branch in session.branches:
        existing_branch = await db.get_branch(str(branch.id))
        if existing_branch:
            existing_session_id = existing_branch["session_id"]
            existing_session = await db.get_session(existing_session_id)
            break

    if existing_session:
        session_id = existing_session_id
        session_prog_id = existing_session["progression_id"]
    else:
        session_id = session_dict["id"]
        session_prog_id = str(uuid.uuid4())
        await db.create_progression(session_prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": session_dict["created_at"],
                "node_metadata": session_dict.get("node_metadata"),
                "name": session_dict.get("name"),
                "user": session_dict.get("user"),
                "progression_id": session_prog_id,
                "first_msg_id": None,
                "last_msg_id": None,
            }
        )

    all_message_ids: list[str] = []
    existing_session_msgs = await db.get_progression(session_prog_id)

    for branch in session.branches:
        await _persist_branch(db, session_id, branch, all_message_ids)

    # Add only NEW messages to session progression
    new_session_msgs = [mid for mid in all_message_ids if mid not in existing_session_msgs]
    for mid in new_session_msgs:
        await db.append_to_progression(session_prog_id, mid)

    # Update session bookmarks
    full_session_msgs = existing_session_msgs + new_session_msgs
    if full_session_msgs:
        await db.update_session(
            session_id,
            first_msg_id=full_session_msgs[0],
            last_msg_id=full_session_msgs[-1],
        )


async def _persist_branch(
    db: StateDB,
    session_id: str,
    branch,
    all_message_ids: list[str],
) -> None:
    """Persist one branch's messages and progression; reuses existing progression on resume."""
    branch_dict = branch.to_dict(mode="db")
    branch_id = branch_dict["id"]

    existing_branch = await db.get_branch(branch_id)
    is_resume = existing_branch is not None
    if is_resume:
        branch_prog_id = existing_branch["progression_id"]
        existing_msg_ids = set(await db.get_progression(branch_prog_id))
    else:
        branch_prog_id = str(uuid.uuid4())
        await db.create_progression(branch_prog_id)
        existing_msg_ids = set()

    for msg in branch.messages:
        msg_dict = msg.to_dict(mode="db")
        msg_id = msg_dict["id"]

        await db.insert_message(msg_dict)
        all_message_ids.append(msg_id)

        if msg_id not in existing_msg_ids:
            await db.append_to_progression(branch_prog_id, msg_id)

    # Merge chat_model config into node_metadata for branch
    node_meta = branch_dict.get("node_metadata") or {}
    if isinstance(node_meta, str):
        import json

        node_meta = json.loads(node_meta)
    if "chat_model" in branch_dict:
        node_meta["chat_model"] = branch_dict["chat_model"]
    if "system" in branch_dict:
        node_meta["system"] = branch_dict["system"]

    # Persist system message as a regular message, store reference.
    # Only append to all_message_ids if the system message wasn't already
    # collected by the messages loop above (avoids double-counting on resume).
    system_msg_id = None
    if branch.system:
        sys_dict = branch.system.to_dict(mode="db")
        system_msg_id = sys_dict["id"]
        await db.insert_message(sys_dict)
        if system_msg_id not in existing_msg_ids and system_msg_id not in all_message_ids:
            all_message_ids.append(system_msg_id)

    # On resume the branch row already exists (INSERT OR IGNORE would be a no-op).
    # Skip create_branch to avoid unnecessary DB round-trip and keep semantics clear.
    if not is_resume:
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": branch_dict["created_at"],
                "node_metadata": node_meta,
                "user": branch_dict.get("user"),
                "name": branch_dict.get("name"),
                "session_id": session_id,
                "progression_id": branch_prog_id,
                "system_msg_id": system_msg_id,
            }
        )
