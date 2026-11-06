# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

import uuid


async def _session(db, *, generation, metadata):
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression)
    await db.create_session(
        {
            "id": sid,
            "progression_id": progression,
            "status": "running",
            "cli_worker_generation": generation,
            "node_metadata": metadata,
        }
    )
    return sid
