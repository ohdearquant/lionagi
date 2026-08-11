# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cheap authenticated identity probe for the desktop launch handshake."""

from __future__ import annotations

from lionagi.version import __version__

from ..registry import studio_route


@studio_route("/identity", method="GET", area="identity", tags=[], name="get_identity")
async def get_identity_route() -> dict[str, str]:
    """Identify this daemon without touching its state store."""
    return {"identity": "lionagi-studio", "version": __version__}
