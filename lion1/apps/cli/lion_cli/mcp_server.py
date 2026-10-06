"""`lion mcp` (ADR-0026 C5, D5): a stdio MCP server over an actor's directory with three tools and no
other. `run` and `flow` start `lion run` or `lion flow` as a child in a session of its own, the server's
actor as the submitter, and return the id as soon as the child prints it; the child runs on, and its
result reaches the submitter by mail. `result` reads a run's or a flow's rows back."""

from __future__ import annotations

import asyncio
import os
import selectors
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from lion1 import Store
from lion1.khive import Khive, KhiveError

from .submit import SubmitError, _submitter

__all__ = ("LION", "server")

LION = (sys.executable, "-m", "lion_cli.main")  # the command a child runs: this interpreter's `lion`


def _first_line(child: subprocess.Popen[bytes]) -> tuple[str, str]:
    """The child's first line on standard output, and what it wrote on standard error meanwhile; an
    empty line when standard output ended without one. Both pipes are read together, so a child that
    says much on standard error before its id never waits on a full pipe."""
    assert child.stdout is not None and child.stderr is not None
    out = err = b""
    with selectors.DefaultSelector() as ends:
        ends.register(child.stdout, selectors.EVENT_READ)
        ends.register(child.stderr, selectors.EVENT_READ)
        while ends.get_map():
            for key, _ in ends.select():
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    ends.unregister(key.fileobj)
                elif key.fileobj is child.stderr:
                    err += chunk
                elif b"\n" in (out := out + chunk):
                    return out.split(b"\n", 1)[0].decode(), err.decode(errors="replace")
    return "", err.decode(errors="replace")


def server(directory: Path, lion: Sequence[str] = LION) -> MCPServer:
    """The server for the actor of `directory`: its store opened first, which refuses without the lion
    database (C8), and the three tools. `lion` is the command a child runs."""
    directory = directory.resolve()
    own, _ = _submitter(directory, None)
    try:
        store = Store(Khive(directory, own))
    except KhiveError as e:
        raise SubmitError(str(e)) from None

    async def started(argv: list[str]) -> dict[str, str]:
        child = subprocess.Popen(
            [*lion, *argv, f"--dir={directory}", f"--notify={own}"],
            stdin=subprocess.DEVNULL,  # the server's own standard input is the protocol
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # the caller's end, or its signal, is not the child's
        )
        line, err = await asyncio.to_thread(_first_line, child)
        child.stdout.close()  # type: ignore[union-attr]  # the child's lines after the id are its own
        child.stderr.close()  # type: ignore[union-attr]
        if not line:
            code = await asyncio.to_thread(child.wait)
            raise ToolError(err.strip() or f"lion {argv[0]} ended with {code} before printing an id")
        threading.Thread(target=child.wait, daemon=True).start()  # reaped when it ends, whenever that is
        return {"id": line}

    def options(variables: dict[str, str] | None, thread: str | None) -> list[str]:
        # each value joined to its flag, so a value that starts with a dash is never read as one
        argv = [f"--var={k}={v}" for k, v in (variables or {}).items()]
        return argv + ([f"--thread={thread}"] if thread else [])

    async def run(
        profile: str,
        say: str,
        files: list[str] | None = None,
        variables: dict[str, str] | None = None,
        thread: str | None = None,
    ) -> dict[str, str]:
        """Start one job of `[profile.<profile>]` of the directory's profiles.toml, on the profile's own
        model, and return its id once its row exists. `say` is the job, a template of `variables` when
        any is given; each of `files` is read as an input after it. The result is mailed to this server's
        actor, on `thread` when given; `result` reads it at any time."""
        argv = ["run", f"--profile={profile}", f"--say={say}", *(f"--file={f}" for f in files or [])]
        return await started(argv + options(variables, thread))

    async def flow(
        name: str, variables: dict[str, str] | None = None, thread: str | None = None
    ) -> dict[str, str]:
        """Start `[flow.<name>]` of the directory's flows.toml and return the flow's id once its row
        exists. Each step is a run of its own profile; the result is mailed to this server's actor, on
        `thread` when given; `result` reads the flow's row and its steps at any time."""
        return await started(["flow", f"--name={name}", *options(variables, thread)])

    async def result(id: str, history: bool = False) -> dict[str, Any]:
        """A run's or a flow's row with its output, `outcome` empty while it runs; a flow's step rows
        beside it; and with `history` the run's messages, in order."""
        try:
            row = await store.result(id)
            got: dict[str, Any] = {"row": row}
            if str(row.get("profile") or "").startswith("flow:"):
                got["steps"] = await store.steps(id)
            if history:
                got["messages"] = await store.messages(id)
        except KhiveError as e:
            raise ToolError(str(e)) from None
        return got

    mcp = MCPServer("lion", instructions=f"Runs of the actor {own}'s profiles and flows, from {directory}.")
    for tool in (run, flow, result):
        mcp.add_tool(tool)
    return mcp
