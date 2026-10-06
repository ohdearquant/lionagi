"""`lion mcp` (ADR-0026 C5, D5): a stdio MCP server over an actor's directory with three tools and no
other. `run` and `flow` start `lion run` or `lion flow` as a child in a session of its own, the server's
actor as the submitter, and return the id as soon as the child prints it; the child runs on, and its
result reaches the submitter by mail. `result` reads a run's or a flow's rows back, and for a child this
server started, how it ended: what it said on standard error is where a mail that failed is named."""

from __future__ import annotations

import asyncio
import fcntl
import os
import selectors
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from lion1 import Store
from lion1.khive import Khive, KhiveError

from .submit import SubmitError, _submitter

__all__ = ("LION", "server")

LION = (sys.executable, "-m", "lion_cli.main")  # the command a child runs: this interpreter's `lion`


_SAID = 8192  # bytes of a child's standard error the server keeps: its last
_LEFT_S = 1.0  # how long what an ended child left in its pipes is read: a process it started may hold one


def _unread(pipe: Any) -> int:
    """The bytes written to `pipe` and not yet read."""
    return struct.unpack("i", fcntl.ioctl(pipe.fileno(), termios.FIONREAD, b"\0\0\0\0"))[0]


def _watch(child: subprocess.Popen[bytes], first: Callable[[str, str], None], ended: dict[str, Any]) -> None:
    """A child read to its end, in a thread of its own started with it: `first` gets the first line of its
    standard output, or all of it when it ended without a newline, and what it wrote on standard error
    meanwhile, or an empty line when it printed nothing; the rest is drained, so the child never waits on
    a full pipe. Its pipes are read until they end, or, once the child has ended, for `_LEFT_S` more,
    for a process it started may hold them open. Its end is seen as it is reaped, and only what its
    standard output held then can be its id: what comes after is that process's. Then the pipes close,
    and its exit and the last of its standard error are kept in `ended` under its id. A call cancelled
    while it waited for the id changes none of this."""
    assert child.stdout is not None and child.stderr is not None
    out = err = b""
    line: str | None = None
    told = False
    owed: int | None = None  # once the child has ended: the bytes of its standard output still to read
    left: float | None = None  # once the child has ended: when reading what it left stops
    woken, wake = os.pipe()  # ends as the child is reaped, so the reading sees its end at once

    def reaped() -> None:
        child.wait()
        os.close(wake)

    def tell(said: str | None) -> None:
        nonlocal line, told
        line, told = said, True
        if said is not None:
            ended[said] = None  # running
        first(said or "", err.decode(errors="replace"))

    threading.Thread(target=reaped, daemon=True).start()
    try:
        with selectors.DefaultSelector() as ends:
            for pipe in (child.stdout, child.stderr, woken):
                ends.register(pipe, selectors.EVENT_READ)
            while ends.get_map():
                if not told and owed == 0:  # all it wrote is read, with no newline: all of it is its id
                    tell(out.decode() or None)
                if left is not None and time.monotonic() >= left:
                    break
                ready = ends.select(None if left is None else left - time.monotonic())
                if any(key.fileobj == woken for key, _ in ready):  # measured before any of its pipes is read
                    ends.unregister(woken)
                    left, owed = time.monotonic() + _LEFT_S, _unread(child.stdout)
                    continue
                for key, _ in ready:
                    mine = key.fileobj is child.stdout and not told  # may hold its id
                    chunk = os.read(key.fd, owed if mine and owed is not None else 65536)
                    if not chunk:
                        ends.unregister(key.fileobj)
                        if mine:
                            tell(out.decode() or None)
                    elif key.fileobj is child.stderr:
                        err = (err + chunk)[-_SAID:]
                    elif mine:
                        out += chunk
                        owed = None if owed is None else owed - len(chunk)
                        if b"\n" in out:
                            tell(out.split(b"\n", 1)[0].decode())
    finally:
        child.stdout.close()
        child.stderr.close()
        os.close(woken)
        if not told:
            tell(out.decode() or None)
        code = child.wait()
        if line is not None:
            ended[line] = {"exit": code, "said": err.decode(errors="replace")}


def server(directory: Path, lion: Sequence[str] = LION) -> MCPServer:
    """The server for the actor of `directory`: its store opened first, which refuses without the lion
    database (C8), and the three tools. `lion` is the command a child runs."""
    directory = directory.resolve()
    own, _ = _submitter(directory, None)
    try:
        store = Store(Khive(directory, own))
    except KhiveError as e:
        raise SubmitError(str(e)) from None

    children: dict[str, Any] = {}  # id: None while the child runs, then its exit and what it said

    async def started(argv: list[str]) -> dict[str, str]:
        child = subprocess.Popen(
            [*lion, *argv, f"--dir={directory}", f"--notify={own}"],
            stdin=subprocess.DEVNULL,  # the server's own standard input is the protocol
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # the caller's end, or its signal, is not the child's
        )
        loop = asyncio.get_running_loop()
        said: asyncio.Future[tuple[str, str]] = loop.create_future()

        def first(line: str, err: str) -> None:
            def give() -> None:
                if not said.done():  # a call cancelled while it waited has no reader left
                    said.set_result((line, err))

            try:
                loop.call_soon_threadsafe(give)
            except RuntimeError:  # the loop is gone: no call waits
                pass

        threading.Thread(target=_watch, args=(child, first, children), daemon=True).start()
        line, err = await said
        if not line:
            code = await asyncio.to_thread(child.wait)
            raise ToolError(err.strip() or f"lion {argv[0]} ended with {code} before printing an id")
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
        beside it; with `history` the run's messages, in order; and for a run or flow this server
        started, `child`: `running`, or its `exit` and the last of what it `said` on standard error."""
        try:
            row = await store.result(id)
            got: dict[str, Any] = {"row": row}
            if str(row.get("profile") or "").startswith("flow:"):
                got["steps"] = await store.steps(id)
            if history:
                got["messages"] = await store.messages(id)
        except KhiveError as e:
            raise ToolError(str(e)) from None
        if id in children:  # a child this server started: its exit, and what it said, once it ended
            got["child"] = children[id] if children[id] is not None else {"running": True}
        return got

    mcp = MCPServer("lion", instructions=f"Runs of the actor {own}'s profiles and flows, from {directory}.")
    for tool in (run, flow, result):
        mcp.add_tool(tool)
    return mcp
