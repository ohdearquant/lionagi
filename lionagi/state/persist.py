# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CLI-layer helper for persisting runtime Session → StateDB.

This module bridges the runtime objects and the database. It is NOT
part of the runtime — the runtime stays DB-unaware. Call from CLI
entry points (li agent, li play, etc.) after the session completes.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.session.session import Session

    from .db import StateDB


async def persist_session(db: StateDB, session: Session) -> None:
    """Persist a runtime Session and all its branches/messages to StateDB.

    1. Creates a progression for the session.
    2. Inserts the session row.
    3. For each branch: creates a progression, inserts messages, creates the branch row.
    4. Updates the session's first/last message bookmarks.
    """
    now = time.time()

    # Session progression
    session_prog_id = str(uuid.uuid4())
    await db.create_progression(session_prog_id)

    session_dict = session.to_dict(mode="db")
    await db.create_session({
        "id": session_dict["id"],
        "created_at": session_dict["created_at"],
        "node_metadata": session_dict.get("node_metadata"),
        "name": session_dict.get("name"),
        "user": session_dict.get("user"),
        "progression_id": session_prog_id,
        "first_msg_id": None,
        "last_msg_id": None,
    })

    all_message_ids: list[str] = []

    for branch in session.branches:
        await _persist_branch(db, session_dict["id"], branch, all_message_ids)

    # Update session bookmarks + progression
    if all_message_ids:
        await db.update_session(
            session_dict["id"],
            first_msg_id=all_message_ids[0],
            last_msg_id=all_message_ids[-1],
        )
        for mid in all_message_ids:
            await db.append_to_progression(session_prog_id, mid)


async def _persist_branch(
    db: StateDB,
    session_id: str,
    branch,
    all_message_ids: list[str],
) -> None:
    """Persist a single branch: its messages, progression, and branch row."""

    branch_prog_id = str(uuid.uuid4())
    await db.create_progression(branch_prog_id)

    branch_dict = branch.to_dict(mode="db")

    # Extract and persist messages from branch's Pile
    branch_message_ids: list[str] = []
    for msg in branch.messages:
        msg_dict = msg.to_dict(mode="db")
        msg_id = msg_dict["id"]

        # Skip if already inserted (shared across branches via fork)
        existing = await db.get_message(msg_id)
        if not existing:
            await db.insert_message(msg_dict)
            all_message_ids.append(msg_id)

        branch_message_ids.append(msg_id)
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

    # Persist system message as a regular message, store reference
    system_msg_id = None
    if branch.system:
        sys_dict = branch.system.to_dict(mode="db")
        system_msg_id = sys_dict["id"]
        existing = await db.get_message(system_msg_id)
        if not existing:
            await db.insert_message(sys_dict)
            all_message_ids.append(system_msg_id)

    await db.create_branch({
        "id": branch_dict["id"],
        "created_at": branch_dict["created_at"],
        "node_metadata": node_meta,
        "user": branch_dict.get("user"),
        "name": branch_dict.get("name"),
        "session_id": session_id,
        "progression_id": branch_prog_id,
        "system_msg_id": system_msg_id,
    })
