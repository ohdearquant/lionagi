"""Claude Code as a backend (ADR-0009), in both shapes.

`ClaudeCode` is the chat-completion shape: one `claude -p` per round, tools off, one turn, so the
only way the model acts is LNDL in its text and every command goes through this runtime's handlers.

`ClaudeCodeAgentic` is the agentic shape: one `claude -p` with its own tools on for a whole task.
It is what a user gets by running Claude Code; it is the baseline the bench compares against, and
the place a hook-riding backend starts from.

Both pass `--setting-sources ""` and `--strict-mcp-config`. Without the first the CLI loads every
CLAUDE.md and settings file it can find, measured at 34k cached input tokens per call on this machine
against 3.9k without. Without the second the account's claude.ai-connected MCP servers stay in the
request even under `--tools ""`, and a model with any tool defined can emit a tool_use, which a
one-turn call then dies on (measured: Sonnet invented a `grep` tool it was never given).

A call that fails lands on `calls` as a failed envelope before the error is raised (ADR-0009/C5):
the CLI's own result envelope when it printed one (a refused turn carries its cost), else one whose
cost is unknown. The error carries the CLI's whole detail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import time
import uuid
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from lionagi.actor import Actor, Message, Reply, Result, Run, SessionPlan
from lionagi.lndl import Lact
from lionagi.record import Kind

__all__ = (
    "ClaudeCodeError",
    "ClaudeCode",
    "ClaudeCodeSession",
    "ClaudeCodeAgentic",
)


class ClaudeCodeError(RuntimeError):
    def __init__(self, message: str, envelope: dict | None = None) -> None:
        super().__init__(message)
        self.envelope = envelope  # the CLI's result envelope, minus the text, when a failed call printed one


@dataclass
class ClaudeCode:
    model: str | None = None  # None: the CLI's default
    cli: str | None = None
    timeout: float = 300.0
    calls: list[dict] = field(default_factory=list)  # the CLI's result envelope per call, minus the text

    def __post_init__(self) -> None:
        self.cli = self.cli or shutil.which("claude")
        if not self.cli:
            raise ClaudeCodeError("claude CLI not found on PATH")

    async def __call__(self, messages: list[Message]) -> str:
        system = messages[0].content if messages and messages[0].role == "system" else ""
        prompt = _transcript(messages[1:] if system else messages)
        args = ["--tools", "", "--max-turns", "1"]
        if system:
            args += ["--system-prompt", system]
        try:
            text, envelope = await _exec(self.cli, args, prompt, model=self.model, timeout=self.timeout)
        except ClaudeCodeError as e:
            self.calls.append({**(e.envelope or {"total_cost_usd": None}), "error": str(e)})
            raise
        self.calls.append(envelope)
        return Reply(text, prompt_tokens=_prompt_tokens(envelope))


@dataclass
class _Point:
    """A point a session can be resumed from: what one call left. Each call forks the session it
    resumes, so every earlier point stays on disk as it was and resumable."""

    id: str
    # (entry name, hash, characters) of each message it holds, in order; a reply is (_REPLY, hash, chars)
    content: tuple[tuple[str | None, int, int], ...]
    chars: int  # the characters it holds: every prompt sent and every reply
    prompt: int  # the characters its call's prompt stood in
    count: int | None  # that prompt's count, as the CLI reported it
    envelope: dict  # the call's envelope: the CLI's running totals follow the fork chain
    read: float  # when the provider last read or wrote its prefix: that call's first request, else its reply


@dataclass
class _Step:
    """What a call does: open the session, go on from the current point, go on from an earlier point
    (`fork`), or start over from the whole view (`full`); with what it sends and what it expects."""

    kind: str
    base: _Point | None
    sends: list[Message]
    prompt: str
    frees: int  # characters starting over takes off every later prompt (negative: it adds them)
    tokens: int | None


@dataclass
class ClaudeCodeSession:
    """The chat-completion shape on one resumed CLI session: each round sends only what the view
    gained since the last round, and the CLI keeps the conversation, so the provider's prompt cache
    can hit on the unchanged prefix. The model's view is then the CLI's history, tool turns included.

    Without tools, each call forks the session it resumes (`--fork-session`), so every call leaves a
    point the session can go on from. When the view changes under what the session holds (a
    context.hide, context.show or context.summarize, or the runtime's own fold event), the session
    either keeps what it holds, or starts over from a point the change left intact (one past the
    cache's hour priced as written again), sending the changed rest as one transcript, or from the
    whole view. It starts over only when that pays: what it frees, read on each of the next
    `payback` turns, must cost at least what it writes again (a cache write costs 20 reads, 40 on
    Opus 5.5 and 80 on Fable 5.1, by the model the CLI reports serving).
    The fold event always starts it over, and with it the CLI's tool-turn history goes (the fold is
    what bounds a chair's context; before it, one call read 14.8M tokens over its tool turns). A
    session given tools goes on in place, keeping no earlier point, and starts over from the whole
    view on a context directive's result or the fold event, unpriced: this backend does not estimate
    the tool turns its CLI holds. A reopened entry the session lacks is sent as new, or read back
    from a point that still holds it when that is cheaper."""

    model: str | None = None
    cli: str | None = None
    timeout: float = 300.0
    # the Claude Code provider: with `tools` the CLI runs its own tools inside each round (Bash, Read,
    # Edit, Write, Grep, Glob), up to `max_turns` of them (0: no cap, the model runs its tools until it
    # stops), in `cwd` (khive signs as that directory's
    # actor) with `add_dirs` reachable; `allowed` adds permission rules beyond the tools (an MCP name);
    # `mcp_config` is the only MCP the CLI loads (the exec passes --strict-mcp-config). Empty tools =
    # the chat shape: the model answers in text and the runtime runs the tools.
    tools: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()
    max_turns: int = 1
    effort: str | None = None  # the CLI's --effort (low, medium, high, xhigh, max); None = its default
    cwd: str | Path | None = None
    add_dirs: tuple[str, ...] = ()
    mcp_config: str | Path | None = None
    # the gate over an acting session (ADR-0009/C6): with `actor`, every tool call the CLI makes asks
    # the CLI's pre-tool hook, which asks that actor's privilege check (`requires`: a tool's privileges
    # by tool name) and before hooks for the run named on the messages; a refusal stops the tool, and
    # the call lands on the run's record as a RESULT under the CLI's tool-use id
    actor: Actor | None = None
    requires: dict[str, tuple[str, ...]] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    # the turns a restart the view asks for must repay itself in: the characters it frees, read on each
    # of them, against the characters it writes again at a cache write's price (_write_over_read reads)
    payback: int = 10
    # a prompt count and the characters it stood in, from a session before this backend's (a resumed
    # chat's last call): the first start's figure stands on it; each start after sets its own
    base: tuple[int, int] | None = None
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sent: dict[str, int] = field(default_factory=dict)  # entry name -> hash of what the session got
    restarts: int = 0  # sessions started over because the view changed under them
    _last: dict | None = field(default=None, repr=False)
    _system: str | None = field(default=None, repr=False)
    _points: list[_Point] = field(default_factory=list, repr=False)  # the current session's last
    _run: str | None = field(default=None, repr=False)  # the record the current session serves
    _runs: dict[str, dict] = field(default_factory=dict, repr=False)  # the other records' sessions

    def __post_init__(self) -> None:
        self.cli = self.cli or shutil.which("claude")
        if not self.cli:
            raise ClaudeCodeError("claude CLI not found on PATH")

    async def __call__(self, messages: list[Message]) -> str:
        system = messages[0].content if messages and messages[0].role == "system" else ""
        body = messages[1:] if system else messages
        run = messages[0].run if messages else None
        if run is not None:
            # one session per record: the actor names the run on every message, and this backend
            # keeps a session per run id, so two runs through one backend never share a session
            self._switch(run)
        first = next((m for m in body if m.name is not None), None)
        if (
            run is None
            and self.sent
            and (
                system != self._system
                or (
                    first is not None
                    and first.name in self.sent
                    and self.sent[first.name] != hash(first.content)
                )
            )
        ):
            # a caller that names no run: entry names start over at _in0 in every run, so a first
            # entry already delivered with other content, or another system prompt, is a different
            # run on this backend. It gets its own session; nothing is treated as sent.
            self.session_id, self.sent, self._last, self._points = str(uuid.uuid4()), {}, None, []
            self.base = None
        self._system = system
        # decided on the view without its last entry when that entry is new (the notification the
        # actor wrote after asking `plan`), so the call does what the notification told
        held = {(n, h) for n, h, _ in self._points[-1].content} if self._points else set()
        tail: list[Message] = []
        if body and (body[-1].name, hash(body[-1].content)) not in held:
            body, tail = body[:-1], body[-1:]
        forced = any(m.spec == "fold" and (m.name, hash(m.content)) not in held for m in body + tail)
        step = self._step(body, tail, forced)
        if step.kind in ("fork", "full"):
            self.restarts += 1
        args = ["--tools", ",".join(self.tools)]
        if self.max_turns > 0:
            args += ["--max-turns", str(self.max_turns)]
        if self.effort:
            args += ["--effort", self.effort]
        if self.tools or self.allowed:
            args += ["--allowedTools", ",".join(self.tools + self.allowed)]
        for d in self.add_dirs:
            args += ["--add-dir", str(d)]
        if self.mcp_config:
            args += ["--mcp-config", str(self.mcp_config)]
        sid = self.session_id if step.kind == "start" else str(uuid.uuid4())
        if step.base is None:
            args += ["--session-id", sid]
            if system:
                args += ["--system-prompt", system]
        elif self.tools:  # an acting session goes on in place: its points past the latest are not kept
            sid = step.base.id
            args += ["--resume", sid]
        else:  # a fork: the point resumed stays as it was, and the call's session is a new one
            args += ["--resume", step.base.id, "--fork-session", "--session-id", sid]
        prompt = step.prompt
        # this run's own state, bound before the await: another run's call can switch the backend
        # to its session while this one is out, and what comes back belongs to this run
        points = self._points
        prev = step.base.envelope if step.base is not None else None
        failed: ClaudeCodeError | None = None
        async with contextlib.AsyncExitStack() as stack:
            if self.actor is not None:
                where = stack.enter_context(tempfile.TemporaryDirectory(prefix="lion-gate-"))
                sock = os.path.join(where, "gate")
                # the call owns what its hook asked: at its end, however it ends, a gate still deciding
                # is cancelled and joined, its tool call landing not executed before the call returns.
                # The call accepts its hooks' connections itself, so every accepted one is a task it
                # holds, and the listener closes with no accept in flight (a server closed under one
                # leaves the accepted socket open)
                asks: set[asyncio.Task] = set()
                arrived: dict[asyncio.Task, asyncio.Event] = {}  # set once the ask has read its request
                writers: dict[asyncio.Task, asyncio.StreamWriter] = {}  # each ask's side of its hook
                closed = asyncio.Event()
                loop = asyncio.get_running_loop()
                listener = stack.enter_context(socket.socket(socket.AF_UNIX))
                listener.bind(sock)
                listener.listen()
                listener.setblocking(False)

                async def ask(conn: socket.socket, came: asyncio.Event) -> None:
                    reader = asyncio.StreamReader()
                    protocol = asyncio.StreamReaderProtocol(reader)
                    try:
                        transport, _ = await loop.connect_accepted_socket(lambda: protocol, conn)
                    except BaseException:
                        conn.close()
                        raise
                    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
                    writers[asyncio.current_task()] = writer
                    await _ask(self.actor, run, self.requires, closed, came, reader, writer)

                def done(task: asyncio.Task) -> None:
                    # by callback, not in the task: one cancelled before its first step never runs
                    asks.discard(task)
                    arrived.pop(task, None)
                    writers.pop(task, None)

                def accept() -> list[asyncio.Task]:
                    # every connection waiting on the listener, each its own task; synchronous, so
                    # none is accepted and then lost between an accept and its task
                    taken = []
                    with contextlib.suppress(BlockingIOError):
                        while True:
                            conn, _ = listener.accept()
                            conn.setblocking(False)
                            came = asyncio.Event()
                            task = asyncio.create_task(ask(conn, came))
                            arrived[task] = came
                            task.add_done_callback(done)
                            taken.append(task)
                    asks.update(taken)
                    return taken

                loop.add_reader(listener.fileno(), accept)

                async def settle() -> None:
                    closed.set()
                    loop.remove_reader(listener.fileno())
                    accept()  # a hook queued on the listener as the call ended is taken too
                    listener.close()
                    try:
                        # a request still on its way lands not executed once it arrives, within the
                        # read's own bound (C6); one at the gate is cancelled below and lands cancelled
                        coming = [e.wait() for e in arrived.values() if not e.is_set()]
                        if coming:
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(asyncio.gather(*coming), _READ_S)
                            await asyncio.sleep(0)  # the arrivals land before the cancel pass
                    finally:
                        # a caller cancelling the call again while it settles still leaves no ask
                        # able to answer: each is cancelled and joined, the caller's cancel raised
                        # after. The join is bounded: an ask that does not end within _JOIN_S has
                        # its hook connection dropped (the hook reads EOF and refuses, `_HOOK`), is
                        # abandoned with a warning, and the call returns, so a cancel (or the SIGINT
                        # that becomes one) is not swallowed on every turn of an unending join
                        cancelled = False
                        until = loop.time() + _JOIN_S
                        # only an ask still running is waited on: one that has finished leaves `asks`
                        # by its done callback, which runs only once this loop yields, and a gather
                        # of finished tasks completes without yielding (3.12+), so waiting on it spins
                        while pending := [task for task in asks if not task.done()]:
                            for task in pending:
                                task.cancel()
                            join = asyncio.gather(*pending, return_exceptions=True)
                            while not join.done() and (left := until - loop.time()) > 0:
                                try:
                                    await asyncio.wait_for(asyncio.shield(join), left)
                                except asyncio.CancelledError:
                                    cancelled = True
                                except TimeoutError:
                                    break
                            if not join.done():
                                stuck = [t.get_name() for t in pending if not t.done()]
                                for task in pending:
                                    if not task.done() and (writer := writers.get(task)):
                                        writer.transport.abort()  # whatever it answers later goes nowhere
                                warnings.warn(
                                    f"{len(stuck)} gate ask(s) did not end within {_JOIN_S}s of the "
                                    f"call's end and are abandoned: {', '.join(stuck)}",
                                    RuntimeWarning,
                                    stacklevel=2,
                                )
                                break
                        if cancelled:
                            raise asyncio.CancelledError

                stack.push_async_callback(settle)
                hook = {"type": "command", "command": shlex.join([sys.executable, "-I", "-c", _HOOK, sock])}
                hook["timeout"] = math.ceil(self.timeout)  # the call's deadline, never the hook's, ends it
                settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}}
                args += ["--settings", json.dumps(settings)]
            started = _clock()
            try:
                text, envelope = await _exec(
                    self.cli, args, prompt, model=self.model, timeout=self.timeout, cwd=self.cwd, persist=True
                )
            except ClaudeCodeError as e:
                # a failed call lands its envelope before the error goes on (ADR-0009/C5); what the CLI
                # printed accumulates over the fork chain like an answer's, so it is taken the same way
                if e.envelope is None:
                    self.calls.append({"total_cost_usd": None, "error": str(e)})
                    raise
                failed, envelope = e, e.envelope
        # over a resumed session the CLI's cost and api time accumulate along the forks, its usage does
        # not: the share is read against the point resumed. A failed call's fork is never gone on from
        call = {**_delta(envelope, prev), "prompt_chars": (step.base.chars if step.base else 0) + len(prompt)}
        self.calls.append(call if failed is None else {**call, "error": str(failed)})
        now = _clock()
        # the provider's hour runs from a request's read of the cache. The CLI times its turn from after
        # it started (which can wait on MCP servers) and prints when in that turn its first request went
        # out; both are this call's own (2.1.287), no running total. The reply's time less the rest of
        # the turn is that request, late by what the CLI takes from printing its answer to exiting (0.34
        # to 0.38 s measured with no MCP server, longer while servers shut down): the startup does not
        # age an entry, and the exit can leave one reading that much warmer than it is. A call making
        # several requests (an acting session's tool turns) is stamped at its first, which an acting
        # session never prices by. With either time missing or not a number, a first request after the
        # turn's end, or a turn longer than the call, the hour runs from the reply, so timing this backend
        # does not know never makes an entry the provider still holds read as expired
        turn_ms, first_ms = envelope.get("duration_ms"), envelope.get("time_to_request_ms")
        read_at = now
        if (
            all(isinstance(t, int | float) and not isinstance(t, bool) for t in (turn_ms, first_ms))
            and 0 <= first_ms <= turn_ms
            and turn_ms / 1000 <= now - started
        ):
            read_at = now - (turn_ms - first_ms) / 1000
        if failed is not None:
            # billed for reading the point's prefix, it starts that entry's hour again all the same
            read = (envelope.get("usage") or {}).get("cache_read_input_tokens")
            if step.base is not None and int(read or 0):
                step.base.read = read_at
            if self.tools and step.base is not None:
                # an acting session went on in place, so its running totals now hold this call's
                # spend: the next call's share is read against the totals printed here. A total not
                # printed stays the point's (that spend lands in the next share, once), and one the
                # session did not keep reads lower next time, so that share is unknown, never twice
                printed = {
                    k: envelope[k]
                    for k in ("total_cost_usd", "duration_api_ms")
                    if envelope.get(k) is not None
                }
                step.base.envelope = {**step.base.envelope, **printed}
            raise failed
        count = _prompt_tokens(envelope)
        if step.base is not None:
            # its prefix was read again: the provider keeps that entry longer. A request refreshes the
            # entry it hits, never the shorter prefixes inside it, so an earlier point's hour runs on
            step.base.read = read_at
        base = step.base
        content = (base.content if base else ()) + tuple(
            (m.name, hash(m.content), len(m.content)) for m in step.sends
        )
        content += ((_REPLY, hash(text), len(text)),)
        chars = (base.chars if base else 0) + len(prompt)
        point = _Point(
            str(envelope.get("session_id") or sid),
            content,
            chars + len(text),
            chars,
            count,
            envelope,
            read_at,
        )
        # past the cache's life a point costs its whole prefix again, as starting over does: it goes
        points[:] = [p for p in points if now - p.read < _CACHE_TTL_S and not self.tools] + [point]
        state = {"session_id": point.id, "sent": _named(content), "_last": envelope}
        if step.kind in ("start", "full") and count is not None:
            state["base"] = (count, chars)
        for key, value in state.items():
            if run is None or run == self._run:
                setattr(self, key, value)
            else:
                self._runs[run][key] = value  # parked by the other run's switch
        return Reply(text, prompt_tokens=count)

    def plan(self, view: list[Message], *, forced: bool = False, run: str | None = None) -> SessionPlan:
        """What the next call on this view does, asked before the notification is written: whether
        the session starts over, what it holds beyond the view, and the count it will stand in
        (ADR-0009/C4). The call decides the same on the same view, unless a point's cache hour ends
        between the two (S13). `run` names the record even when its view is still empty (a run with
        no inputs, before its first call), so another run's session is never the one planned."""
        run = run if run is not None else view[0].run if view else None
        if run is not None:
            self._switch(run)
        step = self._step(list(view), [], forced)
        # an acting session also holds its CLI's tool turns, which this backend does not estimate: no
        # figure beats a low one
        beyond = _beyond(view, self._points[-1].content) if self._points and not self.tools else 0
        return SessionPlan(step.kind in ("fork", "full"), max(0, beyond) // 4, step.tokens)

    def _step(self, view: list[Message], tail: list[Message], forced: bool) -> _Step:
        """The call's step. `view` decides; `tail`, new either way, rides whatever is sent."""
        if not self._points:
            prompt = _prompt(view + tail)
            tokens = None
            if self.base is not None:
                tokens = self.base[0] + (len(_prompt(view)) - self.base[1]) // 4
            return _Step("start", None, view + tail, prompt, 0, tokens)
        cur = self._points[-1]
        new = _new(view, cur.content)
        sends = new + [m for m in _new(tail, cur.content) if m not in new] or (view + tail)[-1:]
        cont = _Step("continue", cur, sends, _plain(sends), 0, None)
        kept = cur.chars + len(_plain(new))  # what the session holds going on, the tail aside
        if cur.count is not None:
            cont.tokens = cur.count + (kept - cur.prompt) // 4
        if self.tools:
            # an acting session's CLI holds its own tool turns, which starting over is there to drop and
            # this backend does not estimate, so no price can be set: as before, a context directive's
            # result or the fold starts it over whole, and it goes on through any other change
            if not forced and not any(m.spec in _CONTEXT for m in new + tail):
                return cont
        elif not forced and _prefix(cur.content, view) is not None:
            return cont
        whole = len(_prompt(view))
        tokens = None if self.base is None else self.base[0] + (whole - self.base[1]) // 4
        full = _Step("full", None, view + tail, _prompt(view + tail), kept - whole, tokens)
        if self.tools:
            return full
        # the view changed under the session: each way of starting over, priced against going on. The
        # characters written beyond going on's: a point past the cache's life is written again whole,
        # system prompt and all, and so is the current one, which going on would then write too
        options: list[tuple[int, _Step]] = []
        now = _clock()
        system = len(self._system or "")  # a new session writes it again; a warm point holds it
        stale = system + cur.chars if now - cur.read >= _CACHE_TTL_S else 0
        for p in reversed(self._points):
            k = _prefix(p.content, view)
            if k is None:
                continue
            rest = _prompt(view[k:])
            size = p.chars + len(rest)
            cold = system + p.chars if now - p.read >= _CACHE_TTL_S else 0
            writes = len(rest) - len(_plain(new)) - stale + cold
            tokens = None if p.count is None else p.count + (size - p.prompt) // 4
            step = _Step("fork", p, view[k:] + tail, _prompt(view[k:] + tail), kept - size, tokens)
            options.append((writes, step))
        options.append((system + whole - len(_plain(new)) - stale, full))
        ratio = _write_over_read(cur.envelope, self.model)
        writes, best = min(options, key=lambda o: ratio * o[0] - self.payback * o[1].frees)
        if forced or (best.frees > 0 and self.payback * best.frees >= ratio * writes):
            return best
        return cont

    def _switch(self, run: str) -> None:
        """Make `run`'s session the current one, parking the session of the run served before."""
        if run == self._run:
            return
        keys = ("session_id", "sent", "_last", "_system", "_points", "base")
        if self._run is not None:
            self._runs[self._run] = {k: getattr(self, k) for k in keys}
        saved = self._runs.pop(run, None)
        if saved is not None:
            for k in keys:
                setattr(self, k, saved[k])
        elif self._run is not None or self.sent:
            self.session_id, self.sent, self._last, self._system = str(uuid.uuid4()), {}, None, None
            self._points, self.base = [], None
        self._run = run


@dataclass
class ClaudeCodeAgentic:
    """One whole task per call, the CLI running its own tools. Returns the final text."""

    model: str | None = None
    tools: tuple[str, ...] = ("Read", "Grep", "Glob")
    max_turns: int = 12
    cwd: str | Path | None = None
    system: str | None = None  # None: the CLI's own prompt; else appended to it
    cli: str | None = None
    timeout: float = 600.0
    calls: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cli = self.cli or shutil.which("claude")
        if not self.cli:
            raise ClaudeCodeError("claude CLI not found on PATH")

    async def __call__(self, prompt: str) -> str:
        args = [
            "--tools",
            ",".join(self.tools),
            "--allowedTools",
            ",".join(self.tools),
            "--max-turns",
            str(self.max_turns),
        ]
        if self.system:
            args += ["--append-system-prompt", self.system]
        try:
            text, envelope = await _exec(
                self.cli, args, prompt, model=self.model, timeout=self.timeout, cwd=self.cwd
            )
        except ClaudeCodeError as e:
            self.calls.append({**(e.envelope or {"total_cost_usd": None}), "error": str(e)})
            raise
        self.calls.append(envelope)
        return text


def _write_over_read(envelope: dict, model: str | None) -> int:
    """A cache write's price in reads for the model the CLI reported serving the call (the one that
    took the most input when it names several), else for the model asked for, a family alias read as
    the model the CLI resolves it to. A served name that is no model id falls through to the one asked
    for; a model id the table does not name prices at 20, the table's usual, and so does nothing named."""
    served = envelope.get("modelUsage")
    if isinstance(served, dict) and served:

        def taken(item: tuple[str, object]) -> int:
            entry = item[1] if isinstance(item[1], dict) else {}
            return sum(
                int(entry.get(k) or 0)
                for k in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
            )

        name, entry = max(served.items(), key=taken)
        for said in ((entry.get("canonicalModel") if isinstance(entry, dict) else None), name):
            if (ratio := _ratio(said)) is not None:
                return ratio
    ratio = _ratio(model)
    return 20 if ratio is None else ratio


def _ratio(model: object) -> int | None:
    """The table's price for a model id, a family alias resolved first; None for a name that is
    neither, which says nothing about the model."""
    if not isinstance(model, str):
        return None
    model = model.removesuffix("[1m]")  # the same model with the long context
    if model in _ALIASES:
        model = os.environ.get(f"ANTHROPIC_DEFAULT_{model.upper()}_MODEL") or _ALIASES[model]
    if not model.startswith("claude-"):
        return None
    # an entry names its model and an id continuing it after a hyphen (a dated id), never another id
    # that only begins alike
    return next(
        (r for name, r in _WRITE_OVER_READ.items() if model == name or model.startswith(f"{name}-")), 20
    )


def _prompt_tokens(envelope: dict) -> int | None:
    """The last model turn's prompt: fresh input, cache read and cache write, the CLI's own context
    included. The envelope's `usage` sums the call's turns (what the call cost); the context the next
    turn stands in is the last turn's, which `usage.iterations` carries when the CLI printed it (one
    `message` entry, measured 2026-09-24: 31895 of a 188926 total over 6 turns), else the sum over
    `num_turns`, an average that reads low by up to half the call's own growth."""
    usage = envelope.get("usage") or {}
    parts = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if not any(k in usage for k in parts):
        return None
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations and isinstance(iterations[-1], dict):
        last = iterations[-1]
        if any(k in last for k in parts):
            return sum(int(last.get(k) or 0) for k in parts)
    turns = max(1, int(envelope.get("num_turns") or 1))
    return sum(int(usage.get(k) or 0) for k in parts) // turns


def _delta(envelope: dict, prev: dict | None) -> dict:
    """What this call alone spent. Measured on a resumed session (three calls, 2026-09-20): `usage`
    is this call's own, while `total_cost_usd`, `duration_api_ms` and `modelUsage` accumulate over
    the session, so only those two are taken as deltas; the running totals stay under `cumulative`."""
    out = dict(envelope)
    out["cumulative"] = {
        "total_cost_usd": envelope.get("total_cost_usd"),
        "duration_api_ms": envelope.get("duration_api_ms"),
    }
    if prev is None:
        return out
    for k in ("total_cost_usd", "duration_api_ms"):
        # a running total missing on either side, or one below the last, leaves this call's share
        # unknown, never negative: a total that went down was not accumulated over the last one
        now, before = envelope.get(k), prev.get(k)
        out[k] = None if now is None or before is None or now < before else now - before
    return out


async def _exec(
    cli: str,
    args: list[str],
    prompt: str,
    *,
    model: str | None,
    timeout: float,
    cwd: str | Path | None = None,
    persist: bool = False,
) -> tuple[str, dict]:
    argv = [cli, "-p", "--output-format", "json"]
    if not persist:
        argv.append("--no-session-persistence")
    argv += ["--setting-sources", "", "--strict-mcp-config"]
    if model:
        argv += ["--model", model]
    argv += args
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        start_new_session=True,  # the CLI's whole process group: a kill ends its children too
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as e:
        # the child is this backend's to stop: a cancelled task must not leave it running, nor a
        # grandchild holding the pipes open past the loop's end
        # the group, whether or not its leader has exited: a child the leader left behind can hold
        # the pipes open, and the drain below would wait on it with no deadline
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        await proc.communicate()  # drains and closes the pipes the cancelled read left open
        if isinstance(e, asyncio.CancelledError):
            raise
        raise ClaudeCodeError(f"claude did not answer within {timeout}s") from None
    if proc.returncode != 0:
        # the CLI reports some failures as a JSON result on stdout with nothing on stderr: a turn the
        # model ended with stop_reason "refusal" and no text is one (seen on a code-review prompt);
        # that result carries what the call cost, so it rides on the error
        try:
            data = json.loads(out.decode(errors="replace"))
        except json.JSONDecodeError:
            data = None
        envelope = {k: v for k, v in data.items() if k != "result"} if isinstance(data, dict) else None
        detail = err.decode(errors="replace").strip()
        if not detail:
            stop = envelope.get("stop_reason") if envelope else None
            detail = f"stop_reason {stop}, no text" if stop else out.decode(errors="replace").strip()
        raise ClaudeCodeError(f"claude exited {proc.returncode}: {detail}", envelope)
    try:
        data = json.loads(out.decode())
    except json.JSONDecodeError as e:
        raise ClaudeCodeError(f"unparseable claude output: {e}: {out!r}") from None
    if data.get("is_error"):
        envelope = {k: v for k, v in data.items() if k != "result"}
        raise ClaudeCodeError(f"claude reported {data.get('subtype')}: {data.get('result')}", envelope)
    text = data.pop("result", "") or ""
    return text, data


# a restart's price, a character written to the hour-long cache in reads of one, by model: a write is 2
# times the model's input price and a read 0.1 times, except 0.05 on Opus 5.5 and 0.025 on Fable 5.1 and
# Mythos 5.1 (Anthropic's published pricing table)
_WRITE_OVER_READ = {"claude-opus-5-5": 40, "claude-fable-5-1": 80, "claude-mythos-5-1": 80}
# what the CLI runs for a family alias unless ANTHROPIC_DEFAULT_<FAMILY>_MODEL names another (the
# model Claude Code 2.1.287 reported serving for each, 2026-10-02); `--setting-sources ""` keeps user
# and project settings from moving it
_ALIASES = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5",
}
_CONTEXT = frozenset({"context.hide", "context.show", "context.summarize"})
_CACHE_TTL_S = 3600.0  # the provider's cache life the CLI writes for (`ephemeral_1h`), from its last read
_clock = time.monotonic
_REPLY = "\x00reply"  # a point's entry for the model's reply: no record entry is named so


_Held = tuple[tuple[str | None, int, int], ...]


def _new(messages: list[Message], held: _Held) -> list[Message]:
    """What the session lacks: the model's replies it has, and a stand-in (a placeholder, a summary)
    stands for what it holds or a restart already sent."""
    have = {(n, h) for n, h, _ in held}
    return [
        m
        for m in messages
        if m.role != "assistant" and not m.folded and (m.name, hash(m.content)) not in have
    ]


def _prefix(held: _Held, view: list[Message]) -> int | None:
    """How many of the view's messages a point holds as they are, in order, or None when the view
    changed inside what it holds. A reply matches the model's turn that recorded it."""
    if len(held) > len(view):
        return None
    for (name, h, _), m in zip(held, view, strict=False):
        reply = m.role == "assistant" and name == _REPLY and h == hash(m.content)
        if (name, h) != (m.name, hash(m.content)) and not reply:
            return None
    return len(held)


def _beyond(view: list[Message], held: _Held) -> int:
    """The characters a session holds beyond the view: what it holds that the view no longer shows,
    less the stand-ins the view shows in their place."""
    shown = Counter((m.name, hash(m.content)) for m in view)
    replies = Counter(hash(m.content) for m in view if m.role == "assistant")  # two alike are two
    have = {(n, h) for n, h, _ in held}
    gone = 0
    for n, h, c in held:
        pool, key = (replies, h) if n == _REPLY else (shown, (n, h))
        if pool[key] > 0:
            pool[key] -= 1
        else:
            gone += c
    return gone - sum(len(m.content) for m in view if m.folded and (m.name, hash(m.content)) not in have)


def _named(held: _Held) -> dict[str, int]:
    return {name: h for name, h, _ in held if name is not None and name != _REPLY}


def _prompt(messages: list[Message]) -> str:
    """A start's prompt: the conversation as a transcript when it has the model's turns, else plain."""
    return _transcript(messages) if any(m.role == "assistant" for m in messages) else _plain(messages)


# the CLI's pre-tool hook, run by this interpreter isolated: the tool call on stdin goes to the gate's
# socket and the verdict comes back. Exit 2 stops the tool with the reason; the CLI runs a tool whose
# hook failed any other way, so a failure to ask exits 2 too
_READ_S = 5.0  # how long a hook has to finish sending its request, once it has connected
_JOIN_S = 5.0  # how long the call's end waits for its cancelled asks to end before abandoning them
_HOOK = """import json, socket, sys
try:
    s = socket.socket(socket.AF_UNIX)
    s.connect(sys.argv[1])
    s.sendall(sys.stdin.buffer.read())
    s.shutdown(socket.SHUT_WR)
    answer = json.loads(b"".join(iter(lambda: s.recv(65536), b"")))
except Exception as e:
    answer = {"refuse": f"the runtime's gate could not be asked: {type(e).__name__}: {e}"}
if "refuse" in answer:
    sys.stderr.write(answer["refuse"])
    sys.exit(2)
out = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": answer["allow"]}
print(json.dumps({"hookSpecificOutput": out}))
"""


class _ToolInput(BaseModel):
    # a CLI tool's input as a request: its fields as the CLI sent them
    model_config = ConfigDict(extra="allow")


async def _ask(
    actor: Actor,
    run: str | None,
    requires: dict[str, tuple[str, ...]],
    closed: asyncio.Event,
    arrived: asyncio.Event,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """One tool call from the pre-tool hook: the actor's gate decides it, and it lands on the run's
    record, admitted with the arguments the hooks left or refused with the reason. A gate that fails
    admits nothing; a call that ends before the gate decides lands the tool call not executed."""
    try:
        try:
            call = json.loads(await asyncio.wait_for(reader.read(), _READ_S))
        except TimeoutError:  # a hook that never finishes sending has asked nothing: it hears nothing
            writer.close()
            return
        arrived.set()
        live = actor.runs.get(run)
        if live is None:
            raise LookupError(f"{actor.name} has no live run {run!r}")
        la = Lact(call["tool_use_id"], call["tool_name"], call["tool_input"])
        # the CLI's ids are its own and never repeat; a repeat is refused, and the refusal still
        # lands, under the id suffixed by the record's next seq, so the call is on the record
        ended = Result(la.name, False, error="cancelled before it started", reason="cancelled")
        if live.record.has_alias(la.alias):
            verdict: Result | BaseModel = _repeat(la)
        elif closed.is_set():  # the call is ending: no gate, and the call still lands (C6)
            verdict = ended
        else:
            try:
                verdict = await actor.gate(
                    live, la, _ToolInput.model_validate(la.args), frozenset(requires.get(la.name, ()))
                )
            except asyncio.CancelledError:
                _land(live, la, ended)
                raise
            if closed.is_set():  # the call is over: its CLI runs nothing, whatever the gate decided
                verdict = ended
        if not isinstance(verdict, Result):
            try:
                args = verdict.model_dump(mode="json")
            except Exception as e:  # what the hooks left cannot reach the CLI: a refusal, and it lands
                why = f"refused: the request cannot be sent, {type(e).__name__}: {e}"
                verdict = Result(la.name, False, error=why, reason="refused")
        if not isinstance(verdict, Result):
            verdict = Result(la.name, True, value=args)
        verdict = _land(live, la, verdict)
        answer = {"allow": args} if verdict.ok else {"refuse": verdict.error}
    except asyncio.CancelledError:  # the call ended while the request was read: the hook hears nothing
        writer.close()
        raise
    except Exception as e:
        answer = {"refuse": f"the runtime's gate failed: {type(e).__name__}: {e}"}
    await _answer(writer, answer)


def _repeat(la: Lact) -> Result:
    return Result(
        la.name, False, error=f"refused: tool-use id {la.alias!r} is already on the record", reason="refused"
    )


def _land(live: Run, la: Lact, verdict: Result) -> Result:
    """The tool call on the run's record: started when admitted, else not executed. A twin that landed
    while this one was at the gate holds the id: this one lands as a repeat, under the id suffixed by
    the record's next seq, so an id is admitted no more than once. What landed is returned."""
    alias = la.alias
    if live.record.has_alias(alias):
        if verdict.ok:
            verdict = _repeat(la)
        alias = f"{alias}_{live.record.seq}"
        while live.record.get(alias) is not None:  # the CLI named one like the suffix
            alias += "_"
    execution = "started" if verdict.ok else "not executed"
    live.record.append(Kind.RESULT, verdict, alias=alias, round=live.round, execution=execution)
    return verdict


async def _answer(writer: asyncio.StreamWriter, answer: dict) -> None:
    with contextlib.suppress(ConnectionError):  # a hook the call's end already stopped hears nothing
        writer.write(json.dumps(answer).encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


def _plain(messages: list[Message]) -> str:
    return "\n\n".join(m.content for m in messages)


def _transcript(messages: list[Message]) -> str:
    """The view as one prompt. Each entry keeps its role, so the model reads it as the conversation it is."""
    parts = []
    for m in messages:
        label = {"user": "USER", "assistant": "ASSISTANT", "system": "SYSTEM"}.get(m.role, m.role.upper())
        parts.append(f"<{label}>\n{m.content}\n</{label}>")
    parts.append("<ASSISTANT>")
    return "\n\n".join(parts)
