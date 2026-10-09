"""`lion context {set,checkpoint,restore,status,show}`: a Claude Code or Codex session's checkpoint and
restore, from the session's directory or as Claude Code's PreCompact hook (ADR-0019/D3, D5). The store
is the checkpoint: a take writes the session's exchanges since the last into the store, and a restore
reads them back and folds them; nothing lands in a file. One implementation: the areas server's
`/api/context/*` routes call these functions; a refused take exits non-zero with the reason on stderr."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from hub.backend.checkpoint import (
    declare,
    declared,
    exchanges_of,
    latest_session,
    record_exchanges,
    session_of,
)
from hub.backend.claude_code.checkpoint import (
    DIRECTIVES,
    PROJECTS,
    SESSION_KINDS,
    SESSION_WHYS,
    actor_id,
    context_figure,
    cursor,
    fold_turns,
    lanes,
    newest_transcript,
    posture_of,
    pulls,
    render_prompt,
    slug,
    snapshot,
    spawns,
    take_session,
    topic,
    transcript_exchanges,
    watches,
)
from hub.backend.codex import _THREAD
from hub.backend.codex import checkpoint as codex_checkpoint
from lion1.checkpoint import Checkpoint, CheckpointError, Var
from lion1.khive import Khive, KhiveError

__all__ = ("BUDGET", "set_var", "checkpoint", "taken", "restore", "status", "show", "context_main")

# tokens, estimated: the kept view is half of it, so a fresh session starts from a few thousand
BUDGET = 8_000


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _khive(d: Path, actor: str, khive: Khive | None) -> Khive:
    return khive if khive is not None else Khive(d, actor)


def set_var(directory: str | Path, name: str, kind: str, value: Any, *, khive: Khive | None = None) -> dict:
    """A value declared for the directory's actor, kept in the store until declared again; every restore
    carries it. The Var stored."""
    d = Path(directory).resolve()
    if kind not in SESSION_KINDS:
        raise CheckpointError(f"kind {kind!r}: one of {', '.join(SESSION_KINDS)}")
    if not name.strip():
        raise CheckpointError("a declared var needs a name")
    actor = actor_id(d)
    var = Var(name=name, kind=kind, value=value, source="declared", at=_now())
    try:
        declare(_khive(d, actor, khive), actor, var)
    except KhiveError as e:
        raise CheckpointError(f"the declaration did not complete: {e}") from None
    return asdict(var)


def _owned(path: Path, snap: Any, d: Path, session: str | None, chosen: bool) -> dict:
    """The rollout's `source` as the snapshot's one read saw its first row, refused unless that row
    names an owner and a thread id, the thread `session` when one is named, and, when the directory
    `chosen` it, a person's thread in `d` still: the directory chose it on an earlier read."""
    found = codex_checkpoint.rollout_source(snap)
    if found["thread_source"] not in codex_checkpoint.OWNERS:
        raise CheckpointError(
            f"{path}: its first row names no owner (thread_source {found['thread_source']!r}), so "
            "whether its prompts are a person's is not known; nothing was taken"
        )
    thread = found["session_id"]
    if not isinstance(thread, str) or not _THREAD.fullmatch(thread):
        raise CheckpointError(
            f"{path}: its first row names no thread id (id {thread!r:.60}), so whose session it is "
            "is not known; nothing was taken"
        )
    if session is not None and session != thread:
        raise CheckpointError(f"{path} is the rollout of thread {thread}, not {session}")
    cwd = found["session_cwd"]
    if chosen and (found["thread_source"] != "user" or not isinstance(cwd, str) or Path(cwd).resolve() != d):
        raise CheckpointError(
            f"{path} changed after it was chosen: its first row no longer says a person's thread in "
            f"{d}; nothing was taken"
        )
    return found


def _file(
    d: Path, *, session: str | None, transcript: str | Path | None, codex: bool
) -> tuple[Path, int, bool]:
    """The session's file: the transcript named, the Codex rollout of `session` or the directory's
    newest person's thread, or the directory's newest Claude Code transcript; how many newer rollouts
    were passed over for naming no owner; whether the file is a rollout."""
    skipped = 0
    if transcript:
        path = Path(transcript).expanduser()
    elif codex and session:
        path = codex_checkpoint.rollout_of(session)
    elif codex:
        path, skipped = codex_checkpoint.newest_rollout(d)
    else:
        path = newest_transcript(d)
    if not path.is_file():
        raise CheckpointError(f"no transcript at {path}")
    rollout = codex_checkpoint.is_rollout(path)
    if codex and not rollout:
        raise CheckpointError(f"{path} is not a Codex rollout: its first row is no session_meta")
    return path, skipped, rollout


def checkpoint(
    directory: str | Path,
    why: str,
    *,
    session: str | None = None,
    transcript: str | Path | None = None,
    budget: int = BUDGET,
    khive: Khive | None = None,
    now: datetime | None = None,
    codex: bool = False,
) -> dict:
    """Take a Claude Code or Codex session's checkpoint: its exchanges since the last take written into
    the store as its session's rows: `{id, session, turns, appended, context}`, the id `<session>@<turns>`.
    A Codex session is `codex`, its rollout `session`'s or the directory's newest person's thread, or a
    transcript whose first row says it is a rollout; its take adds `skipped_unowned`, the newer rollouts
    of the directory passed over because their first row names no owner, and refuses a rollout whose
    first row its read saw names no owner or no thread id. A transcript whose first row is a rollout's
    at its read is refused. The `budget` is what a restore folds to; a take keeps every exchange."""
    d = Path(directory).resolve()
    if why not in SESSION_WHYS:
        raise CheckpointError(f"why {why!r}: one of {', '.join(SESSION_WHYS)}")
    actor = actor_id(d)
    path, skipped, rollout = _file(d, session=session, transcript=transcript, codex=codex)
    if rollout:
        if why not in codex_checkpoint.CODEX_WHYS:
            whys = ", ".join(codex_checkpoint.CODEX_WHYS)
            raise CheckpointError(f"why {why!r}: a Codex session's is one of {whys}")
        snap = codex_checkpoint.snapshot(path)  # one file state for every read of the take
        found = _owned(path, snap, d, session, chosen=codex and not transcript and not session)
        sid, kind = str(found["session_id"]), "codex"
        exchanges, figure = codex_checkpoint.rollout_exchanges(snap), codex_checkpoint.context_figure(snap)
    else:
        # one file state for every read of the take; a rollout by its read's first row is refused
        snap = snapshot(path)
        sid, kind = session or path.stem, "claude-code"
        exchanges, figure = transcript_exchanges(snap), context_figure(sid)
    try:
        turns, appended = asyncio.run(
            record_exchanges(
                _khive(d, actor, khive),
                source=kind,
                session_id=sid,
                actor=actor,
                transcript=str(path),
                exchanges=exchanges,
                why=why,
            )
        )
    except KhiveError as e:
        raise CheckpointError(f"the take did not complete: {e}") from None
    done = {"id": f"{sid}@{turns}", "session": sid, "turns": turns, "appended": appended, "context": figure}
    return {**done, "skipped_unowned": skipped} if rollout else done


def _named(id: str | None) -> tuple[str | None, int | None]:
    """A checkpoint id as `restore` and `show` take it: a session id, or `<session>@<turns>`; None is
    the latest session at its last turn."""
    if id is None:
        return None, None
    sid, at, turns = id.partition("@")
    if not sid.strip() or (at and not turns.isdigit()):
        raise CheckpointError(f"checkpoint id {id!r} is not a session id, or one with a turn count after @")
    return sid, int(turns) if at else None


def _session(khive: Khive, actor: str, id: str | None) -> tuple[dict, int | None]:
    sid, turns = _named(id)
    row = asyncio.run(session_of(khive, sid) if sid else latest_session(khive, actor))
    if row is None:
        raise CheckpointError(
            f"no checkpoint: the store holds no session {sid}"
            if sid
            else f"no checkpoint: no session of {actor}"
        )
    return row, turns


def _take(d: Path, khive: Khive, actor: str, row: dict, turns: int | None, budget: int, now: datetime | None):
    """The checkpoint of the store's session at `turns` (its last when None), computed now: its
    exchanges folded, the harness's live state read beside them."""
    sid, path = row["id"], Path(str(row.get("transcript")))
    exchanges = asyncio.run(exchanges_of(khive, sid))
    if turns is not None:
        if turns > len(exchanges):
            raise CheckpointError(f"session {sid} holds {len(exchanges)} turn(s), not {turns}")
        exchanges = exchanges[:turns]
    now = now or datetime.now().astimezone()
    why = str(exchanges[-1].get("why") or "manual") if exchanges else "manual"
    source: dict = {"session_id": sid, "transcript": str(path), "cwd": str(d), "turns": len(exchanges)}
    # the posture is the newest answered exchange's text, an unanswered re-queued prompt at the end aside
    last = next(((e["text"], e["at"]) for e in reversed(exchanges) if e.get("text")), None)
    lane_vars, lane_note = asyncio.run(lanes(khive))
    pr_vars, pr_note = asyncio.run(pulls(d))
    source["lanes"] = f"{lane_note}; {pr_note}"
    codex = row.get("source") == "codex"
    if codex:
        # the rollout the store names is frozen (its session has ended): its own meta names the thread's
        # model and CLI the restore shows, the store's session_id, transcript and turns winning
        meta = codex_checkpoint.rollout_source(path) if path.is_file() else {}
        source = {**meta, **source}
        own = codex_checkpoint.spawns(path, now) if path.is_file() else []
        source["context"] = codex_checkpoint.context_figure(path if path.is_file() else None)
        taker = codex_checkpoint.take_codex
    else:
        own = [
            *topic(PROJECTS / slug(d) / "memory" / "sessions_registry.json"),
            *(watches(path, actor, now) if path.is_file() else []),
            *(spawns(path, now) if path.is_file() else []),
        ]
        source["context"] = context_figure(sid)
        taker = take_session
    # the person's words are the stored exchanges, the newest eight, an event that woke the actor aside
    words = [t for t in exchanges if t.get("kind") != "event"][-DIRECTIVES:]
    said = [Var(f"directive.{i}", "directive", t["input"], "store", t["at"]) for i, t in enumerate(words, 1)]
    harvested = [*said, *own, *lane_vars, *pr_vars, *posture_of(last, "store"), *cursor(d)]
    return taker(
        source,
        [*harvested, *declared(khive, actor)],
        actor=actor,
        why=why,
        budget=budget,
        view=fold_turns(exchanges, budget),
        now=now,
    )


def taken(
    directory: str | Path,
    id: str | None = None,
    *,
    budget: int = BUDGET,
    khive: Khive | None = None,
    now: datetime | None = None,
) -> Checkpoint:
    """The checkpoint `id` (a session, or `<session>@<turns>`), the latest session at its last turn by
    default, computed from the store now: the exchanges to that turn folded to `budget`, the harness's
    live state and the declared values beside them."""
    d = Path(directory).resolve()
    actor = actor_id(d)
    k = _khive(d, actor, khive)
    try:
        row, turns = _session(k, actor, id)
        return _take(d, k, actor, row, turns, budget, now)
    except KhiveError as e:
        raise CheckpointError(f"the store could not be read: {e}") from None


def restore(
    directory: str | Path, id: str | None = None, *, budget: int = BUDGET, khive: Khive | None = None
) -> dict:
    """`{id, first_prompt, watches, spawns}` of the checkpoint `id` (a session, or `<session>@<turns>`),
    the latest session at its last turn by default; a Codex session's renders on the Codex session's
    lists."""
    c = taken(directory, id, budget=budget, khive=khive)
    render = codex_checkpoint.render_prompt if c.source.get("kind") == "codex" else render_prompt
    return {
        "id": c.id,
        "first_prompt": render(c),
        "watches": [v.value for v in c.vars if v.kind == "watch"],
        "spawns": [v.value for v in c.vars if v.kind == "spawn"],
    }


def status(
    directory: str | Path, *, session: str | None = None, codex: bool = False, khive: Khive | None = None
) -> dict:
    """`{latest, taken_at, context_now, stale, declared}`. Stale: no session in the store, or the
    session's file holds exchanges the store does not. The figure now is a Codex session's with `codex`:
    `session`'s rollout, or the directory's newest, read once and its first row checked as a take checks
    it; one that fails is not known, and stale."""
    d = Path(directory).resolve()
    actor = actor_id(d)
    k = _khive(d, actor, khive)
    try:
        row = asyncio.run(session_of(k, session) if session else latest_session(k, actor))
        held = asyncio.run(exchanges_of(k, row["id"])) if row is not None else []
        count = len(declared(k, actor))
    except KhiveError as e:
        raise CheckpointError(f"the store could not be read: {e}") from None
    stale, now = True, {"used": None, "window": None, "pct": None}
    try:
        path, _, rollout = _file(d, session=session, transcript=None, codex=codex)
        if rollout:
            snap = codex_checkpoint.snapshot(path)
            _owned(path, snap, d, session, chosen=session is None)
            now, seen = codex_checkpoint.context_figure(snap), codex_checkpoint.rollout_exchanges(snap)
        else:
            snap = snapshot(path)
            now, seen = context_figure(session or path.stem), transcript_exchanges(snap)
        if row is not None and str(row.get("transcript")) == str(path):
            stale = len(seen) > len(held) or (
                seen[-1] != {k: v for k, v in held[-1].items() if k != "why"} if seen else False
            )
    except (OSError, CheckpointError):
        pass
    taken = max((e.get("at") for e in held if e.get("at")), default=None) if held else None
    return {
        "latest": f"{row['id']}@{len(held)}" if row is not None else None,
        "taken_at": taken,
        "context_now": now,
        "stale": stale,
        "declared": count,
    }


def show(directory: str | Path, name: str, id: str | None = None, *, khive: Khive | None = None) -> Any:
    """The value of the var `name` in the checkpoint `id`, computed as `taken` computes it: what a lazy
    var's line in the restore prompt points at."""
    c = taken(directory, id, khive=khive)
    found = [v for v in c.vars if v.name == name]
    if not found:
        raise CheckpointError(f"{c.id} has no var {name!r}")
    v = found[-1]
    if v.ref is not None and v.value is None:
        raise CheckpointError(f"{v.name!r} is not carried ({v.size} chars): {v.ref} loads it")
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
            done = checkpoint(
                where, why, session=session, transcript=transcript, budget=a.budget, codex=a.codex
            )
            print(json.dumps(done, ensure_ascii=False, indent=1))
        elif a.verb == "restore":
            got = restore(a.dir, a.id)
            print(json.dumps(got, ensure_ascii=False, indent=1) if a.json else got["first_prompt"])
        elif a.verb == "status":
            print(json.dumps(status(a.dir, session=a.session, codex=a.codex), ensure_ascii=False, indent=1))
        elif a.verb == "show":
            value = show(a.dir, a.var, a.id)
            print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1))
    except CheckpointError as e:
        print(f"lion context {a.verb}: {e}", file=sys.stderr)
        raise SystemExit(1) from None
