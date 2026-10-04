"""`lion context {set,checkpoint,restore,status,show}`: a Claude Code session's checkpoint and restore,
from the session's directory or as its PreCompact hook (ADR-0019/D3). One implementation: the areas
server's `/api/context/*` routes call these functions; a refused take exits non-zero with the reason
on stderr."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from hub.backend.claude_code.checkpoint import (
    PROJECTS,
    SESSION_KINDS,
    SESSION_WHYS,
    TYPED,
    actor_id,
    context_figure,
    cursor,
    directives,
    lanes,
    newest_transcript,
    posture,
    pulls,
    render_prompt,
    slug,
    spawns,
    take_session,
    topic,
    transcript_source,
    transcript_turns,
    watches,
)
from hub.khive import Khive, KhiveError, assert_ignored, context_dir
from lion1.checkpoint import CheckpointError, Var, load, save

__all__ = ("BUDGET", "set_var", "checkpoint", "restore", "status", "show", "context_main")

# tokens, estimated: the kept view is half of it, so a fresh session starts from a few thousand
BUDGET = 8_000


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextlib.contextmanager
def _pending_lock(directory: Path):
    ctx = context_dir(directory)
    ctx.mkdir(parents=True, exist_ok=True)
    with (ctx / ".pending.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield ctx / "pending.jsonl"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _pending(directory: Path) -> tuple[list[Var], str]:
    """The declared vars waiting for a checkpoint, and the text they were read from."""
    path = context_dir(directory) / "pending.jsonl"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return [], ""
    out = []
    for i, line in enumerate(text.split("\n"), 1):  # LF only: a value may hold U+2028 unescaped
        if not line.strip():
            continue
        try:
            out.append(Var(**json.loads(line)))
        except (ValueError, TypeError) as e:
            raise CheckpointError(f"{path} line {i} is not a declared var ({e}); nothing was taken") from None
    return out, text


def _drain(directory: Path, taken: str) -> None:
    """The declared vars a checkpoint took leave `pending.jsonl`; any declared since stay."""
    if not taken:
        return
    with _pending_lock(directory) as path:
        text = path.read_text() if path.exists() else ""
        rest = text[len(taken) :] if text.startswith(taken) else text
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(rest)
        os.replace(tmp, path)


def set_var(directory: str | Path, name: str, kind: str, value: Any) -> dict:
    """A var declared mid-session, waiting in `.khive/context/pending.jsonl` for the next
    checkpoint; the Var stored."""
    d = Path(directory).resolve()
    if kind not in SESSION_KINDS:
        raise CheckpointError(f"kind {kind!r}: one of {', '.join(SESSION_KINDS)}")
    if not name.strip():
        raise CheckpointError("a declared var needs a name")
    assert_ignored(d)
    var = Var(name=name, kind=kind, value=value, source="declared", at=_now())
    with _pending_lock(d) as path, path.open("a") as f:
        f.write(json.dumps(asdict(var), ensure_ascii=False, default=str) + "\n")
    return asdict(var)


def checkpoint(
    directory: str | Path,
    why: str,
    *,
    session: str | None = None,
    transcript: str | Path | None = None,
    budget: int = BUDGET,
    khive: Khive | None = None,
    now: datetime | None = None,
) -> dict:
    """Take and save a Claude Code session's checkpoint: `{id, path, context, vars, kept, hidden}`. The
    gitignore assertion runs before anything is read; lanes khive cannot give are a value, not a refusal."""
    d = Path(directory).resolve()
    if why not in SESSION_WHYS:
        raise CheckpointError(f"why {why!r}: one of {', '.join(SESSION_WHYS)}")
    assert_ignored(d)
    # the stamp is read before the transcript is: a prompt landing between the read and the stamp is
    # after the take, and status calls it so
    now = now or datetime.now().astimezone()
    actor = actor_id(d)
    path = Path(transcript).expanduser() if transcript else newest_transcript(d)
    if not path.is_file():
        raise CheckpointError(f"no transcript at {path}")
    sid = session or path.stem
    lane_vars: list[Var] = []
    if khive is None:
        try:
            khive = Khive(d, actor)
        except KhiveError as e:
            lane_note = f"gtd.tasks and comm.inbox unavailable: {e}"
    if khive is not None:
        lane_vars, lane_note = asyncio.run(lanes(khive))
    pr_vars, pr_note = asyncio.run(pulls(d))
    declared, taken = _pending(d)
    harvested = [
        *topic(PROJECTS / slug(d) / "memory" / "sessions_registry.json"),
        *directives(sid, path, TYPED),
        *watches(path, actor, now),
        *spawns(path, now),
        *lane_vars,
        *pr_vars,
        *posture(path),
        *cursor(d),
    ]
    source = {
        "kind": "claude-code",
        "session_id": sid,
        **transcript_source(path),
        "cwd": str(d),
        "context": context_figure(sid),
        "lanes": f"{lane_note}; {pr_note}",
    }
    view = transcript_turns(path, budget)
    ckpt = take_session(
        source, [*harvested, *declared], actor=actor, why=why, budget=budget, view=view, now=now
    )
    written = save(ckpt, context_dir(d), kinds=SESSION_KINDS, whys=SESSION_WHYS)
    _drain(d, taken)
    return {
        "id": written.stem,
        "path": str(written),
        "context": source["context"],
        "vars": [v.name for v in ckpt.vars],
        "kept": len(view["kept"]),
        "hidden": len(view["hidden"]),
    }


def restore(directory: str | Path, id: str | None = None) -> dict:
    """`{id, first_prompt, watches, spawns}` of the checkpoint `id`, the latest by default."""
    c = load(context_dir(Path(directory).resolve()), id, kinds=SESSION_KINDS, whys=SESSION_WHYS)
    return {
        "id": c.id,
        "first_prompt": render_prompt(c),
        "watches": [v.value for v in c.vars if v.kind == "watch"],
        "spawns": [v.value for v in c.vars if v.kind == "spawn"],
    }


def status(directory: str | Path, *, session: str | None = None) -> dict:
    """`{latest, taken_at, context_now, stale, pending_vars}`. Stale: no checkpoint, or the transcript or
    record it points at changed after the take. A `taken_at` with no fraction is its second's start."""
    d = Path(directory).resolve()
    try:
        c = load(context_dir(d), kinds=SESSION_KINDS, whys=SESSION_WHYS)
    except CheckpointError:
        c = None
    if session is None:
        try:
            session = newest_transcript(d).stem
        except CheckpointError:
            session = None
    stale = True
    if c is not None:
        pointed = c.source.get("transcript") or c.source.get("record")
        try:
            changed = datetime.fromtimestamp(Path(str(pointed)).stat().st_mtime).astimezone()
            stale = changed > datetime.fromisoformat(c.taken_at)
        except (OSError, ValueError, TypeError):
            stale = True
    return {
        "latest": c.id if c else None,
        "taken_at": c.taken_at if c else None,
        "context_now": context_figure(session),
        "stale": stale,
        "pending_vars": len(_pending(d)[0]),
    }


def show(directory: str | Path, name: str, id: str | None = None) -> Any:
    """The value of the var `name` in the checkpoint `id`, the latest by default: what a lazy var's line
    in the restore prompt points at. A var whose value lives elsewhere (its ref) is refused, saying where."""
    c = load(context_dir(Path(directory).resolve()), id, kinds=SESSION_KINDS, whys=SESSION_WHYS)
    v = next((v for v in c.vars if v.name == name), None)
    if v is None:
        raise CheckpointError(f"{c.id} has no var {name!r}")
    if v.ref is not None and v.value is None:
        raise CheckpointError(f"{c.id} does not carry {name!r} ({v.size} chars): {v.ref} loads it")
    return v.value


def _value(text: str) -> Any:
    """A declared value: JSON when it reads as an object or a list, else the text as given."""
    if text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


def context_main(a: argparse.Namespace) -> None:
    try:
        if a.verb == "set":
            print(json.dumps(set_var(a.dir, a.name, a.kind, _value(a.value)), ensure_ascii=False))
        elif a.verb == "checkpoint":
            where, why, session, transcript = a.dir, a.why, a.session, a.transcript
            if a.hook:  # Claude Code's PreCompact hook: the session, its transcript and its cwd on stdin
                try:
                    given = json.loads(sys.stdin.read())
                except ValueError as e:
                    raise CheckpointError(f"the hook's input is not JSON ({e}); nothing was taken") from None
                event = given.get("hook_event_name") if isinstance(given, dict) else None
                if event != "PreCompact":
                    raise CheckpointError(
                        f"hook event {event!r}: only PreCompact takes a checkpoint; nothing was taken"
                    )
                # the event names its session, transcript and cwd; a field missing or blank is no
                # licence to guess (the newest transcript may be another session's)
                for field in ("session_id", "transcript_path", "cwd"):
                    v = given.get(field)
                    if not isinstance(v, str) or not v.strip() or "\0" in v:
                        raise CheckpointError(
                            f"the hook's {field} is missing or not a string; nothing was taken"
                        )
                where, why = given["cwd"], "precompact"
                session, transcript = given["session_id"], given["transcript_path"]
            done = checkpoint(where, why, session=session, transcript=transcript, budget=a.budget)
            print(json.dumps(done, ensure_ascii=False, indent=1))
        elif a.verb == "restore":
            got = restore(a.dir, a.id)
            print(json.dumps(got, ensure_ascii=False, indent=1) if a.json else got["first_prompt"])
        elif a.verb == "status":
            print(json.dumps(status(a.dir, session=a.session), ensure_ascii=False, indent=1))
        elif a.verb == "show":
            value = show(a.dir, a.var, a.id)
            print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1))
    except CheckpointError as e:
        print(f"lion context {a.verb}: {e}", file=sys.stderr)
        raise SystemExit(1) from None
