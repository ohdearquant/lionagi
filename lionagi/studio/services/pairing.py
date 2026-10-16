# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pair a page on another origin with this daemon, through a one-time code.

``li studio`` mints a code (in process at launch, or over the first route when a
daemon already runs) and opens the hosted page with it in the URL fragment. The
page exchanges it over the second route for the bearer it then sends: the
configured ``LIONAGI_STUDIO_AUTH_TOKEN`` when there is one, else this launch's
in-memory bearer (``lionagi.studio.launch_access``).
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..config import CORS_ORIGINS
from ..launch_access import PAIRING_CODE_TTL_S, mint_pairing_code, redeem_pairing_code
from ..registry import studio_route


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)


@studio_route(
    "/studio/pairing-codes",
    method="POST",
    area="pairing",
    tags=[],
    name="create_pairing_code",
    status_code=201,
)
async def create_pairing_code_route(request: Request) -> dict[str, object]:
    """Mint a one-time code for the command that opens a page; never for a page."""
    # A page that could mint codes could pair itself. The command line sends no
    # Origin; every browser request that could reach here does.
    if request.headers.get("origin") is not None:
        raise HTTPException(
            status_code=403,
            detail="pairing codes are issued to the command line, not to a page",
        )
    return {"code": mint_pairing_code(), "expires_in": PAIRING_CODE_TTL_S}


@studio_route("/studio/pair", method="POST", area="pairing", tags=[], name="redeem_pairing_code")
async def redeem_pairing_code_route(request: Request, body: PairRequest) -> dict[str, str]:
    """Spend a pairing code for the bearer a page on an allowed origin sends."""
    # Only a page on an origin CORS admits (by default the hosted app) may pair;
    # any other page that came by a code is refused before the code is spent.
    origin = request.headers.get("origin")
    if origin is None or origin not in CORS_ORIGINS:
        raise HTTPException(
            status_code=403,
            detail="pairing is open only to a page on an origin this daemon allows",
        )
    bearer = redeem_pairing_code(body.code)
    if bearer is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "This pairing code is not valid: it was used, it expired, or this daemon "
                "did not issue it. Run `li studio` again to open Studio."
            ),
        )
    return {"token": os.getenv("LIONAGI_STUDIO_AUTH_TOKEN") or bearer}
