"""Hooks from a file: shell commands at three points of a run, so what a person wants checked, logged
or shown needs no Python. `hooks.toml` beside the thing being run:

    [[before]]                      # a gate on a command, before its handler
    name = "no-secrets"
    match = "read"                  # fnmatch over the command name; default "*"
    run = "scripts/no_secrets.py"   # the shell line; cwd = the directory the hooks came from
    timeout = 10                    # seconds; default 30

    [[after]]                       # sees a command that ran, ok or not
    run = "scripts/audit.sh"

    [[round]]                       # adds its stdout to the round's notice
    run = "date"

Every hook gets one JSON object on stdin: `event`, `run`, `round`, and for a command `alias`,
`command`, `args` (the validated request), and for `after` the `result` (ok, value, error, reason).
A `before` hook exits 0 to let the command run (a JSON object on stdout replaces its arguments), 2
to refuse it (stdout, else stderr, is the reason the model reads), anything else or a timeout refuses
it too: a gate that fails admits nothing. An `after` hook that exits non-zero is told in the next
notice; the result stands. A `round` hook's stdout is the notice line; a failure is said there.

A bundled script's row, `python -m hub.hooks.scripts.<name>`, runs with this interpreter under `-I`, so
neither the directory nor PYTHONPATH supplies its code; `install` refuses it when that interpreter or
`hub` itself lies inside the directory, which the model writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from lionagi import Actor, Result, Run
from lionagi.lndl import Lact

__all__ = ("HERE", "REFUSE", "EVENTS", "Hook", "load", "run_hook", "install")

HERE = Path(__file__).resolve().parent  # the reference hooks.toml and scripts/ live here
REFUSE = 2  # a before hook's exit code that refuses the command; its stdout is the reason
EVENTS = ("before", "after", "round")
# a reference row, as hooks.toml writes it
_BUNDLED = re.compile(r"python[0-9.]*\s+-m\s+(hub\.hooks\.scripts\.\w+)")
_PROBE = """import importlib.util as u, json, sys
found = [(n, s) for n in ("hub", "lionagi") if (s := u.find_spec(n))]
where = [[n, p] for n, s in found for p in ([s.origin] if s.origin else s.submodule_search_locations)]
print(json.dumps(where + [["an import path", p] for p in sys.path]))"""  # finds, never imports


@dataclass(frozen=True)
class Hook:
    event: str  # before | after | round
    run: str  # the shell line
    name: str = ""
    match: str = "*"  # fnmatch over the command name; before and after only
    timeout: float = 30.0
    module: str = ""  # a bundled script's module: `run` is this interpreter isolated, its origin checked


def load(path: Path) -> list[Hook]:
    """The hooks a `hooks.toml` declares, in file order per event."""
    path = Path(path)
    cfg = tomllib.loads(path.read_text())
    hooks: list[Hook] = []
    for event in EVENTS:
        for i, t in enumerate(cfg.get(event, []), 1):
            if "run" not in t:
                raise ValueError(f"{path}: [[{event}]] #{i} has no `run`")
            run = str(t["run"])
            module = m[1] if (m := _BUNDLED.fullmatch(run.strip())) else ""
            if module:  # this interpreter, isolated: the cwd and PYTHONPATH never supply the code
                run = shlex.join([sys.executable, "-I", "-m", module])
            hooks.append(
                Hook(
                    event,
                    run,
                    str(t.get("name") or f"{event}#{i}"),
                    str(t.get("match", "*")),
                    float(t.get("timeout", 30)),
                    module,
                )
            )
    return hooks


async def run_hook(hook: Hook, payload: dict, cwd: Path) -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr); the code is None when the hook was killed at its timeout."""
    data = json.dumps(payload, default=str).encode()  # a payload that cannot be encoded starts nothing
    proc = await asyncio.create_subprocess_shell(
        hook.run,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        start_new_session=True,  # the hook's whole process group: a timeout ends its children too
    )
    done = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), hook.timeout)
        done = True
    except TimeoutError:
        return None, "", ""
    finally:  # timed out, cancelled or failed: the group goes and is reaped before the hook returns
        if not done:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            # drains and closes the pipes the cancelled read left open; the child is reaped by now,
            # so a pipe that fails to drain changes nothing the caller is told
            with contextlib.suppress(Exception):
                await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def install(actor: Actor, hooks: Iterable[Hook], cwd: Path) -> int:
    """Register each hook on the actor at its point; returns how many. A bundled script runs on this
    machine, so its code must come from outside `cwd`, which the model writes: refused otherwise."""
    hooks = list(hooks)
    if bundled := ", ".join(h.name for h in hooks if h.module):
        where = Path(cwd).resolve()

        def origins():  # the interpreter first: the probe runs it
            yield "the interpreter", sys.executable
            yield "its prefix", sys.prefix
            probe = [sys.executable, "-I", "-c", _PROBE]
            done = subprocess.run(probe, cwd=where, capture_output=True, text=True, timeout=30)
            if done.returncode != 0:
                raise ValueError(
                    f"{bundled}: `{shlex.join(probe[:2])}` could not say where hub comes from: {done.stderr}"
                )
            yield from map(tuple, json.loads(done.stdout))

        for what, p in origins():  # where the file sits (a venv's python is a link) and where it leads
            at = Path(p)
            if any(q.is_relative_to(where) for q in (at.parent.resolve() / at.name, at.resolve())):
                raise ValueError(
                    f"{bundled}: {what} is {p}, inside {where}, which the model can write; the hook "
                    "would run the model's code on this machine. Run lion from an install outside it, "
                    "or drop those rows."
                )
    n = 0
    for h in hooks:
        if h.event == "before":
            actor.before(_before(h, cwd))
        elif h.event == "after":
            actor.after(_after(h, cwd))
        elif h.event == "round":
            actor.section(_round(h, cwd))
        else:
            raise ValueError(f"hook {h.name!r}: event {h.event!r} is not one of {', '.join(EVENTS)}")
        n += 1
    return n


def _payload(
    event: str,
    run: Run,
    la: Lact | None = None,
    request: BaseModel | None = None,
    result: Result | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {"event": event, "run": run.id, "round": run.round}
    if la is not None and request is not None:
        d |= {"alias": la.alias, "command": la.name, "args": request.model_dump(mode="json")}
    if result is not None:
        d["result"] = {"ok": result.ok, "value": result.value, "error": result.error, "reason": result.reason}
    return d


def _before(h: Hook, cwd: Path):
    async def before(run: Run, la: Lact, request: BaseModel) -> str | BaseModel | None:
        if not fnmatch.fnmatchcase(la.name, h.match):
            return None
        rc, out, err = await run_hook(h, _payload("before", run, la, request), cwd)
        if rc == 0:  # exit 0 lets it run; only a JSON object on stdout replaces the arguments
            try:
                replaced = json.loads(out) if out.strip() else None
            except ValueError:
                replaced = None  # a log line, or an object with text after it: the command runs as asked
            return type(request).model_validate(replaced) if isinstance(replaced, dict) else None
        if rc == REFUSE:
            return out.strip() or err.strip() or f"{h.name} refused it"
        if rc is None:
            return f"{h.name} timed out after {h.timeout:g}s"
        return f"{h.name} failed (exit {rc}): {err.strip() or out.strip()}"

    before.__name__ = h.name
    return before


def _after(h: Hook, cwd: Path):
    async def after(run: Run, la: Lact, request: BaseModel, result: Result) -> None:
        if not fnmatch.fnmatchcase(la.name, h.match):
            return None
        rc, out, err = await run_hook(h, _payload("after", run, la, request, result), cwd)
        if rc is None:
            raise RuntimeError(f"timed out after {h.timeout:g}s")
        if rc != 0:
            raise RuntimeError(f"exit {rc}: {err.strip() or out.strip()}")
        return None

    after.__name__ = h.name
    return after


def _round(h: Hook, cwd: Path):
    async def section(run: Run) -> str | None:
        rc, out, err = await run_hook(h, _payload("round", run), cwd)
        if rc == 0:
            return out.strip() or None
        if rc is None:
            return f"hook {h.name} timed out after {h.timeout:g}s"
        return f"hook {h.name} failed (exit {rc}): {err.strip() or out.strip()}"

    section.__name__ = h.name
    return section
