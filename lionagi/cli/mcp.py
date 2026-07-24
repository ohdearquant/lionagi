# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li mcp` — serve the lionagi MCP server over stdio.

The server submits ``li`` runs (agent/flow/fanout) as detached background jobs
and exposes tools to query, tail, and stop them. It needs the optional ``mcp``
extra (``pip install lionagi[mcp]``).
"""

from __future__ import annotations

import argparse

from ._logging import log_error

__all__ = ("add_mcp_subparser", "run_mcp")


def add_mcp_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li mcp` with argparse."""
    p = subparsers.add_parser(
        "mcp",
        help="Serve the lionagi MCP server (background job submit/query) over stdio.",
        description=(
            "Serve the lionagi MCP server over stdio. It submits li runs as "
            "detached background jobs (submit_agent/submit_flow/submit_fanout) "
            "and exposes job_status/job_output/job_kill/jobs_list. Requires the "
            "'mcp' extra: pip install 'lionagi[mcp]'."
        ),
    )
    p.add_argument(
        "action",
        nargs="?",
        default="serve",
        choices=["serve"],
        help="serve the server over stdio (default).",
    )


def run_mcp(args: argparse.Namespace) -> int:
    if getattr(args, "action", "serve") != "serve":
        log_error(f"unknown mcp action: {args.action}")
        return 2
    try:
        from lionagi.mcp import serve
    except ImportError as exc:
        log_error(str(exc))
        return 1
    serve()
    return 0
