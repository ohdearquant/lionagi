# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine-definition lookups for the daemon's launch path.

A launch names a saved engine definition by id or by name. These reads are the only part of
the engine-definition store the launch path needs; creating, editing and listing definitions
stay with the application that serves those routes.
"""

from __future__ import annotations

from typing import Any

import lionhub.orchestration.engine_definitions as _engine_registry
from lionagi.state.db import StateDB, state_db_known_absent

__all__ = (
    "get_engine_def",
    "get_engine_def_by_name",
    "is_registered_engine_kind",
)

is_registered_engine_kind = _engine_registry._is_registered_engine_kind


async def get_engine_def(def_id: str) -> dict[str, Any] | None:
    """Return the engine definition with id *def_id*, or None when there is none."""
    if state_db_known_absent():
        return None
    async with StateDB() as db:
        return await db.get_engine_def(def_id)


async def get_engine_def_by_name(name: str) -> dict[str, Any] | None:
    """Return the engine definition named *name*, or None when there is none."""
    if state_db_known_absent():
        return None
    async with StateDB() as db:
        return await db.get_engine_def_by_name(name)
