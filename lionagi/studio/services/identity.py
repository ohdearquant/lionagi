# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cheap authenticated identity probe for the desktop launch handshake."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

from lionagi.version import __version__

from ..registry import studio_route


def _presented_bearer_matches(presented: str, token: str) -> bool:
    """Constant-time compare; a non-ASCII header is a mismatch, not a 500."""
    try:
        return hmac.compare_digest(presented, f"Bearer {token}")
    except TypeError:
        return False


@studio_route("/identity", method="GET", area="identity", tags=[], name="get_identity")
async def get_identity_route(request: Request) -> dict[str, str]:
    """Identify this daemon only to a caller that proved it holds the token."""
    # Repeated here rather than left to the bearer middleware, which skips
    # entirely when no token is configured: answering then would prove only
    # that something on the port calls itself lionagi-studio.
    token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "identity requires a configured auth token "
                "(set LIONAGI_STUDIO_AUTH_TOKEN); refusing on an open daemon"
            ),
        )
    if not _presented_bearer_matches(request.headers.get("authorization") or "", token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"identity": "lionagi-studio", "version": __version__}
