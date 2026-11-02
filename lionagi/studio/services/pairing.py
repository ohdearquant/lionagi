# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pair a page on another origin with this daemon, through a one-time code.

``li studio`` mints a code in process, at launch or over the daemon's same-user
reconnect socket, and opens the hosted page with it in the URL fragment. The page
exchanges it for the bearer it then sends: the configured
``LIONAGI_STUDIO_AUTH_TOKEN`` when there is one, else this launch's in-memory
bearer (``lionagi.studio.launch_access``); and for a decision capability bound
to the page's origin, which the two Operator decision routes read.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..config import CORS_ORIGINS
from ..launch_access import (
    DECISION_TOKEN_HEADER,
    _canonical_origin,
    is_launch_origin,
    redeem_pairing_grant,
    revoke_decision_capability,
)
from ..registry import studio_route


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)


@studio_route(
    "/studio/pairing-codes",
    method="POST",
    area="pairing",
    tags=[],
    name="create_pairing_code",
    status_code=410,
)
async def create_pairing_code_route(request: Request) -> dict[str, object]:
    """Retired: a code is minted by the daemon, never by an HTTP request.

    ``li studio`` obtains a code in process when it starts the daemon, or over
    the daemon's same-user reconnect socket when one is already running, and
    opens the page with it. This route answered any admitted request before
    that, which made a code — and so a bearer — reachable by a program that can
    only speak HTTP. It still answers 410 rather than disappearing, so an older
    launcher learns why it cannot pair.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "Pairing codes are no longer issued over HTTP. Run `li studio` to open "
            "Studio with a fresh code."
        ),
    )


@studio_route("/studio/pair", method="POST", area="pairing", tags=[], name="redeem_pairing_code")
async def redeem_pairing_code_route(request: Request, body: PairRequest) -> dict[str, str]:
    """Spend a pairing code for the bearer a page on an allowed origin sends, and a
    decision capability bound to that origin.

    ``token_source`` says which bearer ``token`` is: ``"launch"`` for this daemon's
    launch bearer, which stops working when the daemon restarts, or ``"configured"``
    for the configured ``LIONAGI_STUDIO_AUTH_TOKEN``, a long-lived credential that a
    page keeps in memory only.
    """
    # Only a page on an origin CORS admits (by default the hosted app), or on an
    # origin a launcher in this process named for its page and asking through
    # that page's own server, may pair. CORS answers no other page, so it could
    # not read what this route returns; any such page is refused before the
    # code is spent.
    origin = request.headers.get("origin")
    if origin is None or (
        origin not in CORS_ORIGINS
        and not (is_launch_origin(origin) and _is_same_origin_request(origin, request))
    ):
        raise HTTPException(
            status_code=403,
            detail="pairing is open only to a page on an origin this daemon allows",
        )
    grant = redeem_pairing_grant(body.code, origin=origin)
    if grant is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "This pairing code is not valid: it was used, it expired, or this daemon "
                "did not issue it. Run `li studio` again to open Studio."
            ),
        )
    configured = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    return {
        "token": configured or grant.bearer,
        "token_source": "configured" if configured else "launch",
        "decision_token": grant.capability,
    }


def _is_same_origin_request(origin: str, request: Request) -> bool:
    """Whether *request* was sent to the address *origin* names.

    A dev server's proxy passes its page's own Host on, so a page that asks
    through it sends a same-origin request, which needs no CORS answer. A page
    that asks the daemon directly sends the daemon's Host, and only CORS lets it
    read the reply.
    """
    host = request.headers.get("host")
    if not host:
        return False
    scheme = origin.split("://", 1)[0]
    return _canonical_origin(origin) == _canonical_origin(f"{scheme}://{host}")


@studio_route(
    "/studio/disconnect",
    method="POST",
    area="pairing",
    tags=[],
    name="disconnect_decision_capability",
)
async def disconnect_decision_capability_route(request: Request) -> dict[str, bool]:
    """Revoke the decision capability a page explicitly disconnects with.

    The page sends it in the same header the decision requests use. An unknown
    or already revoked capability answers ``revoked: false``; the route admits
    exactly what it admitted before, so nothing here is a new gate.
    """
    revoked = revoke_decision_capability(request.headers.get(DECISION_TOKEN_HEADER) or "")
    return {"revoked": revoked}
