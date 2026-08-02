# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi MCP server — one advertised tool, every operation a verb behind it.

An advertised tool schema is sent to the model on every request in every
session, so this server advertises exactly one tool (``ops`` and ``help``) and
a verb's parameters are fetched by asking rather than carried in the tool
list. Verbs live in :mod:`lionagi.mcp.verbs`, dispatch in
:mod:`lionagi.mcp.dispatch`; a CLI command doesn't become a verb automatically.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from . import dispatch

# Renamed from "lionagi" to "lion" when this became a peer-driven machine
# contract; kept as a readable constant since older registrations/logs show it.
# fastmcp carries one name with no alias — a client addresses this server by
# its own config entry (the launch command), not by this string.
SERVER_NAME = "lion"
PREVIOUS_SERVER_NAME = "lionagi"

mcp = FastMCP(SERVER_NAME)

_OPS_DESCRIPTION = (
    "Operations to run, each {'op': '<verb>', 'args': {...}}. Verbs are namespaced: "
    "agent.submit, flow.submit, fanout.submit, play.submit spawn background runs; "
    "job.status, job.output, job.list, job.wait, job.kill observe and control them; "
    "profile.list, profile.show say which agent profiles exist here and what one runs. "
    "Call with help=true first for the full catalog with each verb's required "
    "parameters. Ops run in order; a failing op returns ok=false and does not stop "
    "the ops beside it."
)

_HELP_DESCRIPTION = (
    "true returns the verb catalog with one-line signatures; a verb name returns "
    "that verb's full parameter schema, generated from the CLI parser at call time; "
    "{'verb': '<verb>', 'playbook': '<name>'} additionally resolves that playbook's "
    "own declared arguments into the schema."
)


# The docstring below is not a comment. `@mcp.tool` publishes it as this tool's
# advertised description, so every MCP client and model sees it and calls the
# server based on it. Shortening it changes what callers are told, which is a
# contract change and not an editing decision. A test asserts the call-critical
# parts are still advertised.
@mcp.tool
async def request(
    ops: Annotated[list[dict[str, Any]] | None, Field(description=_OPS_DESCRIPTION)] = None,
    help: Annotated[  # noqa: A002 — the advertised parameter name
        bool | str | dict[str, str] | None, Field(description=_HELP_DESCRIPTION)
    ] = None,
) -> dict[str, Any]:
    """Dispatch lionagi operations, or ask what operations exist.

    Start with ``help=true``: it returns every verb with its required parameters,
    a one-line summary, and — for the verbs whose ops must carry one — the
    ``schema_fingerprint`` to send with the call, which is enough to write the
    common call without a second round-trip. ``help='<verb>'`` returns that
    verb's full schema. Ask for help in its own call: a catalog and a list of op
    results are different shapes, so one reply cannot carry both, and a request
    that asks for both is refused rather than answered in half.

    Results come back as ``{'status': 'success'|'partial', 'ops': [...]}``, one
    entry per op in the order given, each ``{'ok': true, 'op', 'result'}`` or
    ``{'ok': false, 'op', 'error'}``. A per-op failure never fails the call —
    check each ``ok``. A rejected op carries the schema it was judged against, so
    a wrong parameter tells you the right shape in the same reply.

    Argument validation is closed: an unknown or misspelled parameter is refused
    by name rather than ignored. Every value comes back as raw machine JSON — no
    relative timestamps, no formatted durations, no tables.
    """
    return await dispatch.request(ops=ops, help=help)


def main() -> None:
    """Console entrypoint: run the server over stdio."""
    from lionagi.cli._code_identity import snapshot_git_position

    # Taken now, before serving anything: the process keeps its imported
    # modules for its whole life, so a checkout that moves later is divergence
    # to report, not a new answer — only this snapshot can tell the two apart.
    snapshot_git_position()
    mcp.run()


if __name__ == "__main__":
    main()
