"""`lion run`, `lion flow` and `lion result` (ADR-0026 D1, D2, D4): a run of a named profile or a named
flow, submitted from an actor's directory, its rows in that actor's lion database, its result mailed
to the submitter when it ends, and read back from the rows by id."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
import tomllib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, TypeVar

from hub import BoxTree, DaytonaSandbox, Sandbox, guidance
from hub.flow import Ended, Step, flow_from, read, render, run_flow
from hub.flow import Flow as _Flow
from lion1 import Actor, Profile, RunRequest, Store
from lion1.actor import ProfileError
from lion1.khive import Khive, KhiveError

from .agent import backend_for
from .chat import NoSigner, console, end_box, prepare_box, signer

__all__ = (
    "SubmitError",
    "Shape",
    "profile_from",
    "cast",
    "variables",
    "result_mail",
    "run_main",
    "flow_main",
    "result_main",
)

TOOLS = ("files", "code", "shell", "khive", "send")
_KEYS = frozenset({"system", "system_file", "tools", "model", "box", "image", "offline", "defaults"})
_VAR = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
HEAD = 40  # lines of the output a result mail carries


class SubmitError(ValueError):
    """A submission refused before any row: the directory, the profile file, or the command line."""


@dataclass(frozen=True)
class Shape:
    """A profile as the profile file gives it: what `lion chat`'s flags hold, under a name (D2)."""

    name: str
    role: str  # its own prose, ahead of the job's guidance
    tools: frozenset[str]
    model: str
    box: str | None  # where `shell` runs: vm (the default) or daytona
    image: str | None  # the Daytona box's image
    offline: bool
    defaults: dict[str, float] = field(default_factory=dict, hash=False)


def profile_from(directory: Path, name: str, model: str | None = None) -> Shape:
    """`[profile.<name>]` of the directory's `profiles.toml`, read now: `system` (or `system_file`),
    `tools` among files, code, shell, khive and send, `model` (`model` here wins), `box`, `image`,
    `offline` and `defaults`, the bounds. A key or a tool this record does not name refuses, with its
    name, and so does a profile with no model or with values that do not go together."""
    path = Path(directory) / "profiles.toml"
    try:
        doc = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise SubmitError(f"{path}: no profile file") from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SubmitError(f"{path}: {e}") from None
    profiles = doc.get("profile")
    if not isinstance(profiles, dict) or not isinstance(profiles.get(name), dict):
        there = ", ".join(sorted(profiles)) if isinstance(profiles, dict) and profiles else "none"
        raise SubmitError(f"{path}: no [profile.{name}]; there is {there}")
    p = profiles[name]
    where = f"{path}: [profile.{name}]"
    if unknown := sorted(set(p) - _KEYS):
        raise SubmitError(f"{where} {unknown}: a profile holds {', '.join(sorted(_KEYS))}")
    if "system" in p and "system_file" in p:
        raise SubmitError(f"{where}: system or system_file, not both")
    role = p.get("system", "")
    if "system_file" in p:
        try:
            role = (Path(directory) / str(p["system_file"])).read_text()
        except OSError as e:
            raise SubmitError(f"{where} system_file: {e}") from None
    if not isinstance(role, str):
        raise SubmitError(f"{where}: system is text")
    tools = p.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise SubmitError(f"{where}: tools is a list of names among {', '.join(TOOLS)}")
    if unknown := sorted(set(tools) - set(TOOLS)):
        raise SubmitError(f"{where}: tools {unknown}: the tools are {', '.join(TOOLS)}")
    chosen = model or p.get("model")
    if not isinstance(chosen, str) or not chosen:
        raise SubmitError(f"{where}: names no model, and none was given")
    box, image, offline = p.get("box"), p.get("image"), p.get("offline", False)
    if box not in (None, "vm", "daytona"):
        raise SubmitError(f"{where}: box {box!r}: vm or daytona")
    if box is not None and "shell" not in tools:
        raise SubmitError(f"{where}: box names where the shell runs, and tools has no shell")
    if image is not None and box != "daytona":
        raise SubmitError(f"{where}: image is the Daytona box's, and box is not daytona")
    if not isinstance(offline, bool) or (offline and ("shell" not in tools or box == "daytona")):
        raise SubmitError(
            f"{where}: offline takes true or false, and cuts the network of the VM's shell only"
        )
    defaults = p.get("defaults", {})
    if not isinstance(defaults, dict):
        raise SubmitError(f"{where}: defaults is a table of bounds")
    try:
        Profile(name=name, defaults=defaults)  # the name and the bounds as the runtime checks them
    except ProfileError as e:
        raise SubmitError(f"{where}: {e}") from None
    return Shape(name, role, frozenset(tools), chosen, box, image, offline, dict(defaults))


@asynccontextmanager
async def cast(
    directory: Path,
    shape: Shape,
    *,
    submitter: str,
    thread: str | None = None,
    say: Callable[[str], None] = lambda line: None,
) -> AsyncIterator[tuple[Actor, Profile, Any]]:
    """The actor, profile and backend a run of `shape` takes, built by `console`'s keyword form, and the
    box its shell needs for as long as the run: a VM over the directory, or a Daytona box holding a copy
    of it, whose diff is saved as `.lion/runs/<run id>.patch` and never applied. A Daytona box whose
    patch is not taken whole is kept, named through `say`, and its error raised (`end_box`)."""
    sandbox: Any = None
    tree = about_box = None
    boxed = shape.box == "daytona"
    if "shell" in shape.tools:
        if boxed:
            image = shape.image or "python:3.12"
            sandbox = DaytonaSandbox(image, workdir=str(directory))
            tree = BoxTree(sandbox)
            about_box = guidance("box", kind="Daytona", image=image, root=directory)
        else:
            sandbox = Sandbox(directory, f"lion-run-{uuid.uuid4().hex[:8]}", offline=shape.offline)
    try:
        actor, profile = console(
            directory,
            name=shape.name,
            model=shape.model,
            sandbox=sandbox,
            coding="code" in shape.tools,
            tree=tree,
            about_box=about_box,
            tools=shape.tools & {"files", "khive", "send"},
            role=shape.role,
            guide="job",
            defaults=shape.defaults,
            submitter=submitter,
            thread=thread,
        )
    except NoSigner as e:
        raise SubmitError(str(e)) from None
    backend = backend_for(shape.model, cwd=directory)
    runs: list[str] = []
    off = actor.bus.subscribe("run.started", lambda ev: runs.append(ev.payload["run"]))
    baseline = ""
    started = False
    try:
        if boxed:
            started = True
            baseline = await prepare_box(sandbox, directory, say)
        elif sandbox is not None:
            if (build := await sandbox.ensure_image()) is not None:
                raise SubmitError(f"the sandbox image {sandbox.image} did not build:\n{build}")
            started = True
            await sandbox.start()
        yield actor, profile, backend
    finally:
        off()
        if started:
            if boxed and runs:  # the patch first, named by the run: stop deletes the box
                await end_box(
                    sandbox,
                    directory,
                    directory / ".lion" / "runs",
                    runs[-1],
                    baseline=baseline,
                    apply=False,
                    say=say,
                    show=False,
                )
            else:
                await asyncio.shield(sandbox.stop())


def variables(given: list[str] | None) -> dict[str, str]:
    """`--var NAME=VALUE`, each name once."""
    out: dict[str, str] = {}
    for v in given or []:
        m = _VAR.fullmatch(v)
        if m is None:
            raise SubmitError(f"--var {v!r}: NAME=VALUE, the name a letter or _ then letters, digits and _")
        if m.group(1) in out:
            raise SubmitError(f"--var {m.group(1)} is given twice")
        out[m.group(1)] = m.group(2)
    return out


def _head(output: Any) -> str:
    text = output if isinstance(output, str) else json.dumps(output, indent=2, default=str)
    lines = text.splitlines()
    shown = "\n".join(lines[:HEAD])
    return shown + (f"\n[{len(lines) - HEAD} more lines]" if len(lines) > HEAD else "")


async def result_mail(
    khive: Khive,
    to: str,
    subject: str,
    output: Any,
    directory: Path,
    run_id: str,
    *,
    thread: str | None = None,
    error: str | None = None,
    unread: str | None = None,
) -> str | None:
    """The one result message (C4, D4): the subject names the id and the outcome, the body the output's
    first lines and the command that reads the rest, after why the row could not be read back when it
    could not: a store that was gone may hold it later. Sent after the rows close; a send that fails is
    returned as the error, never raised, for the rows stand without it."""
    body = _head(output) if output is not None else "(no output)"
    if error:
        body = f"error: {error}\n\n{body}"
    if unread is not None:
        body += f"\n\nits row could not be read back: {unread}"
    body += f"\n\nlion result --dir {shlex.quote(str(directory))} {run_id}"
    try:
        await khive.send(to, body, subject=subject, thread_id=thread, self_send=to == khive.actor)
    except Exception as e:  # KhiveError, a timeout, a transport that died
        return f"{type(e).__name__}: {e}"
    return None


_T = TypeVar("_T")


async def _through(aw: Awaitable[_T]) -> tuple[_T, BaseException | None]:
    """`aw` run to its end through one cancellation, which comes back beside the result for the caller to
    raise once its end is said: a run's read-back and its one mail are never cut off half done by it, and
    the mail is never sent twice. A second cancellation stops `aw` as well."""
    task = asyncio.ensure_future(aw)
    try:
        return await asyncio.shield(task), None
    except asyncio.CancelledError as e:
        return await task, e


def _stopped_before_its_end(what: str, directory: Path, run_id: str) -> None:
    """A second interrupt, through `_through`, stops the end at once, so the mail may not have gone:
    standard error says so, with the `lion result` command for the id, as the mail's body ends."""
    _emit(
        f"{what} stopped before its end: its result mail may not have gone; "
        f"lion result --dir {shlex.quote(str(directory))} {run_id}",
        sys.stderr,
    )


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _emit(line: str, stream: TextIO | None = None) -> None:
    """One line to standard output, or to `stream`. A reader that has gone, a caller that took the id
    and closed its end, costs the run nothing: the stream is pointed at the null device and the run goes
    on, for its rows and its result mail carry the rest (C1, C4)."""
    stream = stream or sys.stdout
    try:
        print(line, file=stream, flush=True)
    except BrokenPipeError:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, stream.fileno())
        os.close(null)


def _say(line: str) -> None:
    """A notice (a box's push, its patch): on standard error, for standard output holds the ids first
    and the end last, its line and then the output (C1)."""
    _emit(line, sys.stderr)


def _submitter(directory: Path, notify: str | None) -> tuple[str, str]:
    """The directory's own actor, and who the result mail goes to: `--notify`, else that actor."""
    own = signer(directory)
    if own is None:
        raise SubmitError(
            f"{directory / '.khive' / 'config.toml'}: no actor; a submission's rows are that actor's "
            "(ADR-0026 C8)"
        )
    return own, notify or own


def _ended(
    kind: str,
    run_id: str,
    outcome: str,
    output: Any,
    failed: str | None,
    mailed: str | None,
    error: str | None = None,
) -> int:
    _emit(f"{kind} {run_id} {outcome}" + (f" at {failed}" if failed else ""))
    if output is not None:
        _emit(output if isinstance(output, str) else json.dumps(output, indent=2, default=str))
    if error is not None:
        _emit(f"error: {error}", sys.stderr)
    if mailed is not None:
        _emit(f"the result mail was not sent: {mailed}", sys.stderr)
    return 0 if outcome == "success" and error is None and mailed is None else 1


async def run_main(a: argparse.Namespace) -> int:
    """One job of a profile (C1): the id prints as the run opens, before its first call, and the output
    when it ends, its box ended; then the result mail."""
    directory = Path(a.dir).resolve()
    own, to = _submitter(directory, a.notify)
    shape = profile_from(directory, a.profile, a.model)
    given = variables(a.var)
    say = a.say
    if given:  # with variables the text is a template of them (C3); without, it is taken as written
        read(_Flow("run", (Step("run", shape.name, say),), "run"), given)
        say = render(say, given, {})
    inputs = [say]
    for f in a.file or []:
        try:
            inputs.append(Path(f).read_text())
        except OSError as e:
            raise SubmitError(f"--file {f}: {e}") from None
    opened: list[str] = []

    def tell(ev: Any) -> None:
        opened.append(ev.payload["run"])
        _emit(ev.payload["run"])

    run = store = error = interrupted = None
    try:
        async with cast(directory, shape, submitter=to, thread=a.thread, say=_say) as (
            actor,
            profile,
            backend,
        ):
            store = actor.store
            off = actor.bus.subscribe("run.started", tell)
            try:
                run = await actor.run(profile, RunRequest(inputs=tuple(inputs)), backend)
            finally:
                off()
    except Exception as e:  # the run's, or its box's end: the actor closed the rows; the mail says so
        if not opened:
            raise
        error = _why(e)
    except BaseException as e:  # a cancellation: the same, and then it goes on up
        if not opened:
            raise
        error, interrupted = _why(e), e
    run_id = opened[0]
    outcome = type(run.outcome).__name__.lower() if run is not None and run.outcome is not None else "stopped"
    output = run.output if run is not None else None

    async def end() -> tuple[str | None, str | None]:
        unread = None
        if run is None:  # it ended in an error, which may have come before its row was written
            try:
                await store.result(run_id)
            except Exception as e:
                unread = _why(e)
        mailed = await result_mail(
            store.khive,
            to,
            f"run {run_id} {outcome}",
            output,
            directory,
            run_id,
            thread=a.thread,
            error=error,
            unread=unread,
        )
        return unread, mailed

    try:
        (unread, mailed), late = await _through(end())
    except (asyncio.CancelledError, KeyboardInterrupt):
        _stopped_before_its_end(f"run {run_id}", directory, run_id)
        raise
    interrupted = interrupted or late
    code = _ended("run", run_id, outcome, output, None, mailed, error)
    if unread is not None:
        _emit(f"its row could not be read back: {unread}", sys.stderr)
    if interrupted is not None:
        raise interrupted
    return code


async def flow_main(a: argparse.Namespace) -> int:
    """A flow (C1 to C3): every step's profile read before any row, the flow's id first, each step's as
    its run opens, then the flow's output; then the result mail."""
    directory = Path(a.dir).resolve()
    own, to = _submitter(directory, a.notify)
    flow = flow_from(directory, a.name)
    shapes = {s.profile: profile_from(directory, s.profile) for s in flow.steps}
    given = variables(a.var)
    read(flow, given)  # every name the templates read, before the flow's row
    try:
        store = Store(Khive(directory, own))
    except KhiveError as e:
        raise SubmitError(str(e)) from None

    flows: list[str] = []

    def opened(what: str, run_id: str) -> None:
        if what.startswith("flow "):
            flows.append(run_id)
        _emit(run_id if what.startswith("flow ") else f"{what} {run_id}")

    def step_cast(name: str) -> Any:
        return cast(directory, shapes[name], submitter=to, thread=a.thread, say=_say)

    ended = error = interrupted = None
    try:
        ended = await run_flow(store, flow, given, step_cast, opened=opened)
    except Exception as e:  # the driver closed the flow stopped; the mail says why
        if not flows:
            raise
        error = _why(e)
    except BaseException as e:  # a cancellation: the same, and then it goes on up
        if not flows:
            raise
        error, interrupted = _why(e), e
    if ended is None:
        ended = Ended(flows[0], "stopped", None, None)
    try:
        mailed, late = await _through(
            result_mail(
                store.khive,
                to,
                f"flow {flow.name} {ended.id} {ended.outcome}",
                ended.output,
                directory,
                ended.id,
                thread=a.thread,
                error=error,
            )
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        _stopped_before_its_end(f"flow {flow.name} {ended.id}", directory, ended.id)
        raise
    interrupted = interrupted or late
    code = _ended(f"flow {flow.name}", ended.id, ended.outcome, ended.output, ended.failed, mailed, error)
    if interrupted is not None:
        raise interrupted
    return code


async def result_main(a: argparse.Namespace) -> int:
    """A run's row with its output (C4), a flow's step rows beside it, and with `--history` its
    session's messages, in order."""
    directory = Path(a.dir).resolve()
    own, _ = _submitter(directory, None)
    try:
        store = Store(Khive(directory, own))
        row = await store.result(a.id)
    except KhiveError as e:
        raise SubmitError(str(e)) from None
    print(json.dumps(row, indent=2, default=str))
    if str(row.get("profile") or "").startswith("flow:"):
        for s in await store.steps(a.id):
            print(json.dumps(s, default=str))
    if a.history:
        for m in await store.messages(a.id):
            print(f"[{m.get('seq')}] {m.get('role')} {m.get('kind')}: {m.get('text')}")
    return 0
