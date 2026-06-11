# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GET /api/casts — read-only casts catalog (roles, modes, emission contracts)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from lionagi.casts._catalog import build_catalog

router = APIRouter(prefix="/casts", tags=["casts"])


@router.get("/")
async def get_casts() -> dict[str, Any]:
    return build_catalog()
