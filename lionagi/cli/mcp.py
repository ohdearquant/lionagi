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
from ._util import EXIT_CODE_ENVIRONMENT_ERROR

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
        # `lionagi.mcp` is deliberately dependency-free, so importing it proves
        # nothing about whether the server can run. The server module is what
        # needs the extra, so it is imported here, explicitly and before
        # serving. That keeps the classification below on the import, where
        # "nothing was started" is true, rather than wrapping the serve loop,
        # where a lazy import failing mid-session would be misreported as an
        # installation that never started.
        import lionagi.mcp.server  # noqa: F401
        from lionagi.mcp import serve
    except ModuleNotFoundError as exc:
        # The extra is not installed. The server never started and nothing ran,
        # so this is the environment rather than a failed command, and returning
        # the ordinary failure code would leave a caller unable to tell an
        # uninstalled extra from a server that started and died.
        missing = exc.name or "a required module"
        log_error(
            f"cannot serve: {missing} is not installed in this environment. "
            "The MCP server needs the 'mcp' extra: install lionagi[mcp], then "
            "re-run. Nothing was started."
        )
        return EXIT_CODE_ENVIRONMENT_ERROR
    except ImportError as exc:
        # The module is present but something in it could not be imported. That
        # is a defect in what is installed, not a missing piece of it, so it
        # keeps the ordinary failure code.
        log_error(str(exc))
        return 1
    serve()
    return 0
