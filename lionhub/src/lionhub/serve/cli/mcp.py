# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li mcp` — serve the lionagi MCP server over stdio.

The server submits ``li`` runs (agent/flow/fanout) as detached background jobs
and exposes tools to query, tail, and stop them. It needs the optional ``mcp``
extra (``pip install lionagi[mcp]``).
"""

from __future__ import annotations

import argparse

from lionagi._auto import CliDeclaration, auto_register
from lionagi._feature_errors import declared_missing_import_root
from lionhub.orchestration.console import log_error
from lionhub.orchestration.job import EXIT_CODE_ENVIRONMENT_ERROR

__all__ = ("add_mcp_subparser", "run_mcp")

# Import roots for the distributions declared in the mcp extra.
_MCP_IMPORT_ROOTS = ("fastmcp", "mcp", "starlette")


def add_mcp_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li mcp` with argparse."""
    p = subparsers.add_parser(
        "mcp",
        help="Serve the lionagi MCP server (background job submit/query) over stdio.",
        description=(
            "Serve the lionagi MCP server over stdio. It advertises one tool, "
            "request(ops=[{op, args}], help?), with every operation behind it as "
            "a namespaced verb: agent.submit, flow.submit, fanout.submit and "
            "play.submit start detached background runs, job.status, job.output, "
            "job.list, job.wait and job.kill follow them. Call request(help=true) "
            'for the verb catalog and request(help="<verb>") for one verb\'s '
            "parameters, generated from this CLI's own parsers. Requires the "
            "'mcp' extra: pip install 'lionagi[mcp]'."
        ),
    )
    p.add_argument(
        "action",
        nargs="?",
        default="serve",
        choices=["serve"],
        help=(
            "What to do: 'serve' (the default and only value) runs the server on stdin/stdout "
            "until the client disconnects."
        ),
    )


@auto_register(area="mcp", cli=CliDeclaration(seed="mcp", parser_factory=add_mcp_subparser))
def run_mcp(args: argparse.Namespace) -> int:
    if getattr(args, "action", "serve") != "serve":
        log_error(f"unknown mcp action: {args.action}")
        return 2
    try:
        # `lionhub.serve.mcp` is dependency-free; the server module needs the extra,
        # so it's imported here explicitly, before serving, so a failure here
        # (not mid-session) is what gets classified as "nothing was started".
        import lionhub.serve.mcp.server  # noqa: F401
        from lionhub.serve.mcp import serve
    except ImportError as exc:
        missing = declared_missing_import_root(exc, _MCP_IMPORT_ROOTS)
        if missing is not None:
            log_error(
                f"cannot serve: {missing} is not installed in this environment. "
                "The MCP server needs the 'mcp' extra: install lionagi[mcp], then "
                "re-run. Nothing was started."
            )
            return EXIT_CODE_ENVIRONMENT_ERROR
        detail = f"{exc.name}: {exc}" if exc.name else str(exc)
        log_error(f"cannot serve: {detail}")
        return 1
    serve()
    return 0
