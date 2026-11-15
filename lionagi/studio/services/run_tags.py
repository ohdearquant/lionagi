# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from lionagi._errors import NotFoundError

# Tag storage and reads live in lionhub; these names stay importable from here.
from lionhub.serve.daemon.services.run_tags_store import (  # noqa: F401 -- preserve the compatibility import
    _ENSURE_TABLE_SQL,
    _MAX_SQL_VARS,
    _ensure_table,
    add_tag,
    remove_tag,
    session_ids_with_tags,
    tags_for_sessions,
)

from ..registry import studio_route


class TagBody(BaseModel):
    tag: str


@studio_route("/sessions/{session_id}/tags", method="POST", area="sessions", name="add_run_tag")
async def add_run_tag(session_id: str, body: TagBody) -> dict[str, Any]:
    # Reject tagging a nonexistent session: list_runs() only surfaces rows
    # joined from `sessions`, so a tag on a stale id would be a silent orphan.
    from .sessions import session_exists

    if not await session_exists(session_id):
        raise NotFoundError(f"Session '{session_id}' not found")
    await add_tag(session_id, body.tag)
    current = await tags_for_sessions([session_id])
    return {"session_id": session_id, "tags": current.get(session_id, [])}


@studio_route(
    "/sessions/{session_id}/tags/{tag:path}",
    method="DELETE",
    area="sessions",
    name="remove_run_tag",
)
async def remove_run_tag(session_id: str, tag: str) -> dict[str, Any]:
    await remove_tag(session_id, tag)
    current = await tags_for_sessions([session_id])
    return {"session_id": session_id, "tags": current.get(session_id, [])}
