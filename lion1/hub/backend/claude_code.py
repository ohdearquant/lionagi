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
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from lionagi.actor import Actor, Message, Reply, Result, Run
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
class ClaudeCodeSession:
    """The chat-completion shape on one resumed CLI session: each round sends only what the view
    gained since the last round, and the CLI keeps the conversation, so the provider's prompt cache
    can hit on the unchanged prefix. The model's view is then the CLI's history, tool turns included,
    and that history is what a fold cuts: when a round brings a context.hide, context.show or
    context.summarize result, or the runtime's own fold event (the measured context past the view
    budget), the session is restarted with the folded view as one transcript, one cache miss, and
    the CLI's tool-turn history is gone with the old session (the fold is what bounds a chair's
    context; before it, one call read 14.8M tokens over its tool turns)."""

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
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sent: dict[str, int] = field(default_factory=dict)  # entry name -> hash of what the session got
    restarts: int = 0  # sessions started over because the model changed its view
    _last: dict | None = field(default=None, repr=False)
    _system: str | None = field(default=None, repr=False)
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
            self.session_id, self.sent, self._last = str(uuid.uuid4()), {}, None
        self._system = system
        new = [m for m in body if m.role != "assistant" and (m.name is None or m.name not in self.sent)]
        if self.sent and any(m.spec in _DIRECTIVES for m in new):
            # the model changed its own view, or the runtime folded it: start over from the folded view
            self.session_id, self.sent, self._last = str(uuid.uuid4()), {}, None
            self.restarts += 1
        if not new:  # a view that only shrank: nothing new to say, resend the notification
            new = body[-1:]
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
        if not self.sent:
            args += ["--session-id", self.session_id]
            if system:
                args += ["--system-prompt", system]
            prompt = _transcript(body) if any(m.role == "assistant" for m in body) else _plain(body)
        else:
            args += ["--resume", self.session_id]
            prompt = _plain(new)
        # this run's own state, bound before the await: another run's call can switch the backend
        # to its session while this one is out, and what comes back belongs to this run
        sent, prev = self.sent, self._last
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
                        while asks:
                            pending = list(asks)
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
            try:
                text, envelope = await _exec(
                    self.cli, args, prompt, model=self.model, timeout=self.timeout, cwd=self.cwd, persist=True
                )
            except ClaudeCodeError as e:
                # a failed call lands its envelope before the error goes on (ADR-0009/C5); what the CLI
                # printed accumulates over the session like an answer's, so it is taken the same way
                if e.envelope is None:
                    self.calls.append({"total_cost_usd": None, "error": str(e)})
                    raise
                failed, envelope = e, e.envelope
        # over a resumed session the CLI's cost and api time accumulate, its usage does not
        call = _delta(envelope, prev)
        self.calls.append(call if failed is None else {**call, "error": str(failed)})
        if run is None or run == self._run:
            self._last = envelope
        else:
            self._runs[run]["_last"] = envelope  # parked by the other run's switch
        if failed is not None:
            raise failed
        sent.update({m.name: hash(m.content) for m in body if m.name is not None})
        return Reply(text, prompt_tokens=_prompt_tokens(envelope))

    def _switch(self, run: str) -> None:
        """Make `run`'s session the current one, parking the session of the run served before."""
        if run == self._run:
            return
        if self._run is not None:
            self._runs[self._run] = {
                "session_id": self.session_id,
                "sent": self.sent,
                "_last": self._last,
                "_system": self._system,
            }
        saved = self._runs.pop(run, None)
        if saved is not None:
            self.session_id, self.sent = saved["session_id"], saved["sent"]
            self._last, self._system = saved["_last"], saved["_system"]
        elif self._run is not None or self.sent:
            self.session_id, self.sent, self._last, self._system = str(uuid.uuid4()), {}, None, None
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


_DIRECTIVES = frozenset({"context.hide", "context.show", "context.summarize", "fold"})

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
