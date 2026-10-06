"""`lion mcp` (ADR-0026 C5, D5): a stdio MCP server over an actor's directory with three tools and no
other. `run` and `flow` start `lion run` or `lion flow` as a child in a session of its own, the server's
actor as the submitter, and return the id as soon as the child prints it; the child runs on, and its
result reaches the submitter by mail. `result` reads a run's or a flow's rows back, and for a child this
server started, how it ended: what it said on standard error is where a mail that failed is named."""

from __future__ import annotations

import asyncio
import fcntl
import os
import select
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


def _ending(pid: int) -> tuple[Callable[[], Any], Callable[[], None]] | None:
    """A wait that returns once process `pid` has ended, before it is reaped, and how to close it; None
    when it has ended already. Opened while nothing can have reaped it, so the pid names it still."""
    if hasattr(os, "pidfd_open"):  # Linux
        fd = os.pidfd_open(pid)
        readable = select.poll()
        readable.register(fd, select.POLLIN)
        return readable.poll, lambda: os.close(fd)
    kq = select.kqueue()  # macOS
    try:
        kq.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)], 0)
    except ProcessLookupError:  # it has ended: it waits, unreaped, for its reaper
        kq.close()
        return None
    except BaseException:
        kq.close()
        raise
    return lambda: kq.control(None, 1, None), kq.close


def _watch(child: subprocess.Popen[bytes], first: Callable[[str, str], None], ended: dict[str, Any]) -> None:
    """A child read to its end, in a thread of its own started with it: `first` gets the first line of its
    standard output, or all of it when it ended without a newline, and what it wrote on standard error
    meanwhile, or an empty line when it printed nothing; the rest is drained, so the child never waits on
    a full pipe. Its pipes are read until they end, or, once the child has ended, for `_LEFT_S` more,
    for a process it started may hold them open. Its end is seen before it is reaped, and only what its
    standard output held then can be its id: what comes after is that process's. Then the pipes close,
    and its exit and the last of its standard error are kept in `ended` under its id. A call cancelled
    while it waited for the id changes none of this. With no thread to spare for the reaping, or no way
    to see its end, its pipes are read to their end and it is reaped after."""
    assert child.stdout is not None and child.stderr is not None
    out = err = b""
    line: str | None = None
    told = False
    taken = 0  # the bytes of its standard output read while they may hold its id
    held: int | None = None  # once it has ended, before its reap: the bytes its standard output had then
    lock = threading.Lock()  # a read of its standard output and its count, or that measure, never between
    left: float | None = None  # once the child has ended: when reading what it left stops
    end: tuple[Callable[[], Any], Callable[[], None]] | None = None
    woken: int | None
    woken, wake = os.pipe()  # ends as the child is reaped, so the reading sees its end at once

    def reaped() -> None:
        nonlocal held
        if end is not None:
            end[0]()  # it has ended, and nothing has reaped it
            end[1]()
        with lock:
            if not child.stdout.closed:
                held = taken + _unread(child.stdout)
        child.wait()
        os.close(wake)

    def tell(said: str | None) -> None:
        nonlocal line, told, err
        said = said or None  # a blank first line is no id either
        if said is None and not child.stderr.closed:  # what it said first is read: it may say why
            n = _unread(child.stderr)
            while n > 0 and (chunk := os.read(child.stderr.fileno(), n)):
                err, n = (err + chunk)[-_SAID:], n - len(chunk)
        line, told = said, True
        if said is not None:
            ended[said] = None  # running
        first(said or "", err.decode(errors="replace"))

    os.set_blocking(child.stderr.fileno(), False)  # drained when no id came, before the selector says so
    try:
        end = _ending(child.pid)
        threading.Thread(target=reaped, daemon=True).start()
    except (RuntimeError, OSError):  # no thread or no watch to spare: its pipes are read to their end
        if end is not None:
            end[1]()
        os.close(wake)
        os.close(woken)
        woken = None
    try:
        with selectors.DefaultSelector() as ends:
            for pipe in (child.stdout, child.stderr, woken):
                if pipe is not None:
                    ends.register(pipe, selectors.EVENT_READ)
            while ends.get_map():
                if not told and held is not None and held == taken:  # all it held is read, with no newline
                    tell(out.decode() or None)
                if left is not None and time.monotonic() >= left:
                    break
                ready = ends.select(None if left is None else left - time.monotonic())
                if any(key.fileobj == woken for key, _ in ready):
                    ends.unregister(woken)
                    left = time.monotonic() + _LEFT_S
                    continue
                for key, _ in ready:
                    if key.fileobj is child.stdout and not told:  # may hold its id: no more than it held
                        with lock:
                            room = 65536 if held is None else held - taken
                            chunk = os.read(key.fd, room) if room else None
                            taken += len(chunk or b"")
                        if chunk is None:  # all it held as it ended is read, with no newline
                            tell(out.decode() or None)
                        elif not chunk:
                            ends.unregister(key.fileobj)
                            tell(out.decode() or None)
                        else:
                            out += chunk
                            if b"\n" in out:
                                tell(out.split(b"\n", 1)[0].decode())
                        continue
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:  # its standard error, read as no id came
                        continue
                    if not chunk:
                        ends.unregister(key.fileobj)
                    elif key.fileobj is child.stderr:
                        err = (err + chunk)[-_SAID:]
    finally:
        with lock:
            child.stdout.close()
        child.stderr.close()
        if woken is not None:
            os.close(woken)
        if not told:
            tell(out.decode() or None)
        code = child.wait()
        if line is not None:
            ended[line] = {"exit": code, "said": err.decode(errors="replace")}


async def _reaped(child: subprocess.Popen[bytes], within: float | None) -> int | None:
    """The child's exit once it is reaped, or None once `within` seconds pass first. Polled on the loop:
    a thread of the shared pool can be seconds away when many calls wait at once."""
    deadline = None if within is None else time.monotonic() + within
    while (code := child.poll()) is None:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.02 if deadline is not None else 1.0)
    return code


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
    reaping: set[asyncio.Future[int | None]] = set()  # children no thread reads, reaped by the loop

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

        try:
            threading.Thread(target=_watch, args=(child, first, children), daemon=True).start()
        except RuntimeError as e:  # no thread reads it: its pipes close, and the loop reaps it when it ends
            assert child.stdout is not None and child.stderr is not None
            child.stdout.close()
            child.stderr.close()
            unread = f"lion {argv[0]} started and could not be read ({e})"
            if (code := child.poll()) is not None:
                raise ToolError(f"{unread}; it ended with {code}") from None
            reaper = asyncio.ensure_future(_reaped(child, None))
            reaping.add(reaper)
            reaper.add_done_callback(reaping.discard)
            raise ToolError(f"{unread}; it runs on as pid {child.pid}") from None
        line, err = await said
        if not line:  # no id can come: the call fails now, and the child is reaped whenever it ends
            code = await _reaped(child, _LEFT_S)
            if code is None:
                closed = f"lion {argv[0]} closed its standard output before printing an id"
                said_all = filter(None, (err.strip(), f"{closed}; it runs on as pid {child.pid}"))
                raise ToolError("\n".join(said_all)) from None
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
