# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi MCP server — submit ``li`` runs as background jobs and query them.

The server itself lives in :mod:`lionagi.mcp.server` and needs the optional
``mcp`` extra (``pip install lionagi[mcp]``). Importing this package never pulls
that dependency: the job engine (:mod:`lionagi.mcp.jobs`) and paths
(:mod:`lionagi.mcp.config`) are stdlib-only, so the terminal hook the CLI runs
imports cleanly even where the server dependency is absent.

Serve it with ``li mcp`` (or ``python -m lionagi.mcp``).
"""

from __future__ import annotations

__all__ = ("serve",)


def serve() -> None:
    """Run the MCP server over stdio. Requires the ``mcp`` extra."""
    try:
        from .server import main
    except ImportError as exc:  # pragma: no cover - exercised via the CLI path
        raise ImportError(
            "the lionagi MCP server requires the 'mcp' extra; "
            "install it with: pip install 'lionagi[mcp]'"
        ) from exc
    main()
