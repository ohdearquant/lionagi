# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li mirror` — stream Claude Code transcripts into StateDB so they appear live in studio."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi._paths import LIONAGI_HOME

from ._logging import hint, log_error, progress, warn

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
_OFFSETS_PATH = LIONAGI_HOME / "mirror" / "offsets.json"

# A session whose newest message is within this window counts as live (running);
# past it, the next pass flips it to completed.
_DEFAULT_LIVE_WINDOW = 300.0

_log = logging.getLogger(__name__)


def add_mirror_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li mirror` with argparse."""
    p = subparsers.add_parser(
        "mirror",
        help="Mirror Claude Code sessions into studio (live).",
        description=(
            "Tail ~/.claude/projects transcripts and write them to the lionagi "
            "state DB so every Claude Code session shows up — and streams live — "
            "in studio and the VS Code extension. Resumable and idempotent."
        ),
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Do a single catch-up pass and exit (backfill), instead of tailing.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=3.0,
        metavar="SECS",
        help="Poll interval while tailing (default 3).",
    )
    p.add_argument(
        "--since",
        type=_since_window,
        default=None,
        metavar="WINDOW",
        help="Only mirror transcripts modified within this window (e.g. 12h, 7d). Default: all.",
    )
    p.add_argument(
        "--root",
        default=None,
        metavar="DIR",
        help=f"Claude projects directory (default {CLAUDE_PROJECTS_DIR}).",
    )
    p.add_argument(
        "--live-window",
        type=float,
        default=_DEFAULT_LIVE_WINDOW,
        metavar="SECS",
        help="Idle gap since the last message after which a session is marked completed (default 300).",
    )


@dataclass
class _FileState:
    """Per-transcript cursor + derived session metadata, kept across poll passes."""

    session_uid: str
    offset: int = 0
    tool_names: dict[str, str] = field(default_factory=dict)
    project: str | None = None
    project_source: str | None = None
    model: str | None = None
    name: str | None = None
    created: bool = False
    leaf_uuid: str | None = None  # this file's newest event uuid (lineage index)
    head_checked: bool = False  # whether the file's root parentUuid was examined
    attr_peeked: bool = False  # whether idle project attribution was attempted


@dataclass
class _Lineage:
    """Cross-session conversation-lineage detector, kept across poll passes.

    A continued conversation (after compaction, ``--resume``, or a new window that
    resumes an earlier thread) opens a fresh transcript whose first message points
    — via ``parentUuid`` — at the last message of the session it continues. We
    index each file's current leaf uuid and, when a file's root parent resolves to
    a *different* session's leaf, record a provenance link. It almost never fires
    on today's transcripts (continuations are rare), so it is insurance + the
    substrate for showing conversation provenance, not a hot path.
    """

    leaf_owner: dict[str, str] = field(default_factory=dict)  # event uuid -> session_uid
    pending: dict[str, str] = field(default_factory=dict)  # child session_uid -> parent uuid
    linked: set[str] = field(default_factory=set)  # child session_uids already linked

    def note_leaf(self, state: _FileState, events: list[dict[str, Any]]) -> None:
        """Index this file's newest event uuid as a candidate continuation point."""
        last = next((str(e["uuid"]) for e in reversed(events) if e.get("uuid")), None)
        if not last:
            return
        prev = state.leaf_uuid
        if prev and self.leaf_owner.get(prev) == state.session_uid:
            self.leaf_owner.pop(prev, None)  # only the current leaf stays indexed
        self.leaf_owner[last] = state.session_uid
        state.leaf_uuid = last

    def note_head(self, state: _FileState, events: list[dict[str, Any]]) -> None:
        """If the file's thread root has a parent, queue it for cross-session resolution."""
        if state.head_checked or state.session_uid in self.linked:
            return
        for e in events:
            if "parentUuid" not in e:  # summary/file-history events have no parent
                continue
            state.head_checked = True
            parent = e.get("parentUuid")
            if parent:  # null parent == self-rooted, no lineage
                self.pending[state.session_uid] = str(parent)
            return

    def resolve(self) -> list[tuple[str, str, str]]:
        """Match pending roots against indexed leaves; return new (child, parent, uuid) links."""
        links: list[tuple[str, str, str]] = []
        for child, parent_uuid in list(self.pending.items()):
            owner = self.leaf_owner.get(parent_uuid)
            if owner is None:
                continue  # parent not yet indexed (older pass, or outside the window)
            del self.pending[child]
            if owner == child:
                continue  # same session spread across files — not cross-session lineage
            self.linked.add(child)
            links.append((child, owner, parent_uuid))
        return links


def _fallback_project(cwd: str) -> tuple[str, str]:
    """Attribute a cwd that detect_project couldn't place to a project.

    Bucket it by the cwd's own folder name, or "others" when that directory no
    longer exists (e.g. a transcript mirrored from a machine/path that is gone).
    """
    p = Path(cwd)
    if p.is_dir():
        return p.name, "cwd_dir"
    return "others", "cwd_missing"


def _resolve_project(cwd: str) -> tuple[str, str]:
    """Project + source for a cwd: detect_project, else the folder-name fallback."""
    from ._project import detect_project

    try:
        project, source = detect_project(Path(cwd))
    except Exception:  # detection is best-effort; never block the mirror
        project, source = None, None
    if not project:
        return _fallback_project(cwd)
    return project, source


def _load_states() -> dict[str, _FileState]:
    # Persist tool_names + leaf_uuid alongside the byte offset so a restart resumes
    # mid-conversation without dropping a later tool_result's function name or
    # losing cross-session lineage — both live only in process memory otherwise.
    # Legacy {path: int} caches (offset only) load as bare cursors, then upgrade.
    try:
        raw = json.loads(_OFFSETS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    states: dict[str, _FileState] = {}
    for key, val in raw.items():
        if isinstance(val, int):
            states[key] = _FileState(session_uid="", offset=val)
        elif isinstance(val, dict):
            states[key] = _FileState(
                session_uid=val.get("session_uid") or "",
                offset=val.get("offset", 0),
                tool_names=dict(val.get("tool_names") or {}),
                leaf_uuid=val.get("leaf_uuid"),
            )
    return states


def _save_states(states: dict[str, _FileState]) -> None:
    _OFFSETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        key: {
            "offset": st.offset,
            "session_uid": st.session_uid,
            "tool_names": st.tool_names,
            "leaf_uuid": st.leaf_uuid,
        }
        for key, st in states.items()
    }
    tmp = _OFFSETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(_OFFSETS_PATH)


def _seed_lineage(lineage: _Lineage, states: dict[str, _FileState]) -> None:
    # Re-index persisted leaves so a continuation opened after a restart still
    # resolves its parent, whose transcript is now at EOF and streams no events.
    for st in states.values():
        if st.leaf_uuid and st.session_uid:
            lineage.leaf_owner[st.leaf_uuid] = st.session_uid


_WINDOW_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _parse_window(spec: str) -> float | None:
    """Seconds for a window like '30m'/'12h'/'7d', or bare seconds; None if empty.

    Raises ValueError on a non-empty but unparseable spec so callers fail loudly
    instead of silently falling back to an unbounded scan.
    """
    spec = spec.strip().lower()
    if not spec:
        return None
    try:
        if spec[-1] in _WINDOW_UNITS:
            return float(spec[:-1]) * _WINDOW_UNITS[spec[-1]]
        return float(spec)
    except ValueError:
        raise ValueError(
            f"unrecognized --since window {spec!r} (expected e.g. 30m, 12h, 7d, or seconds)"
        ) from None


def _since_window(spec: str) -> float:
    """argparse type for --since: parse to seconds, or reject with a clean CLI error."""
    try:
        secs = _parse_window(spec)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if secs is None:
        raise argparse.ArgumentTypeError("--since must be a non-empty window, e.g. 30m, 12h, 7d")
    return secs


def _read_new_events(path: Path, state: _FileState) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSONL lines past the cursor; return (events, new_offset).

    The cursor is NOT advanced here — the caller sets ``state.offset`` to the
    returned offset only after the batch is durably mirrored, so a write failure
    re-reads the same lines next pass instead of skipping them. Non-object JSON (a
    bare ``[]`` or a scalar) is dropped as malformed rather than handed on as an
    event.
    """
    size = path.stat().st_size
    if state.offset > size:  # file truncated/rotated — re-read from the top.
        state.offset = 0
    with path.open("rb") as fh:
        fh.seek(state.offset)
        chunk = fh.read()
    if b"\n" not in chunk:
        return [], state.offset
    body, _, _ = chunk.rpartition(b"\n")
    new_offset = state.offset + len(body) + 1
    events = []
    for raw in body.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, new_offset


_COMMAND_NOISE = ("<command-", "<local-command-")


def _derive_metadata(state: _FileState, events: list[dict[str, Any]]) -> None:
    """Fill project/model/name from the transcript the first time we see them."""
    if state.project is None:
        cwd = next((e.get("cwd") for e in events if e.get("cwd")), None)
        if cwd:
            state.project, state.project_source = _resolve_project(cwd)
            if state.name is None:
                state.name = f"Claude · {state.project.split('/')[-1]}"
    if state.model is None:
        for e in events:
            if e.get("type") == "assistant" and isinstance(e.get("message"), dict):
                model = e["message"].get("model")
                if model:
                    state.model = model
                    break
    # Prefer the first real user prompt as the session name.
    if events and (state.name is None or state.name.startswith("Claude · ")):
        prompt = _first_prompt(events)
        if prompt:
            state.name = prompt[:72]


def _peek_head(path: Path) -> tuple[str, str | None]:
    """Recover (sessionId, cwd) from a transcript's head without consuming the tail.

    Needed for idle files after a restart: with no new events to read, the session
    id is otherwise unknown (the liveness sweep would never flip it completed) and
    the cwd needed to attribute it to a project is otherwise unavailable.
    """
    uid = ""
    cwd: str | None = None
    try:
        with path.open("rb") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):  # non-dict JSON (e.g. `[]`) — skip, don't .get()
                    continue
                if not uid and ev.get("sessionId"):
                    uid = str(ev["sessionId"])
                if cwd is None and ev.get("cwd"):
                    cwd = str(ev["cwd"])
                if uid and cwd is not None:
                    break
    except OSError:
        pass
    return uid or path.stem, cwd


async def _attribute_idle(db, state: _FileState, cwd: str) -> None:
    """Attribute an idle/already-read transcript and backfill its session row.

    The activity path attributes a project from streamed events, but a session
    fully mirrored before project attribution existed (or before its cwd could be
    placed) has no new events to trigger that. This derives the project from the
    head cwd and backfills the existing row — without moving the liveness clock.
    """
    from lionagi.state.claude_mirror import session_db_id

    state.project, state.project_source = _resolve_project(cwd)
    row = await db.get_session(session_db_id(state.session_uid))
    if row is not None and not row.get("project"):
        await db.set_session_provenance(
            session_db_id(state.session_uid),
            project=state.project,
            project_source=state.project_source,
        )


def _first_prompt(events: list[dict[str, Any]]) -> str | None:
    for e in events:
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        msg = e.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        text = text.strip()
        if text and not text.startswith(_COMMAND_NOISE):
            return " ".join(text.split())
    return None


async def _mirror_one(db, path: Path, state: _FileState, lineage: _Lineage) -> int:
    from lionagi.state.claude_mirror import mirror_session

    events, new_offset = _read_new_events(path, state)
    if not events:
        state.offset = new_offset  # advance past blank/malformed-only lines
        return 0

    if not state.session_uid:
        state.session_uid = next((e["sessionId"] for e in events if e.get("sessionId")), path.stem)
    _derive_metadata(state, events)
    lineage.note_head(state, events)
    lineage.note_leaf(state, events)

    # Always created/kept running; the session-level idle sweep (after the whole
    # pass) is what flips it to completed, so a fresh transcript anywhere in a
    # multi-file session keeps the whole session live.
    written = await mirror_session(
        db,
        session_uid=state.session_uid,
        events=events,
        tool_names=state.tool_names,
        project=state.project,
        project_source=state.project_source,
        model=state.model,
        name=state.name,
        status="running",
    )
    # Advance the cursor only after the batch is durably mirrored: if the write
    # above raised, state.offset is unchanged and the batch is re-read (idempotently)
    # next pass rather than silently lost.
    state.offset = new_offset
    if written and not state.created:
        state.created = True
        progress(f"  mirror: {state.name or state.session_uid[:8]} (+{written} msgs)")
    return written


async def _one_pass(db, root: Path, states, offsets, *, since, live_window, lineage=None) -> int:
    now = time.time()
    total = 0
    seen: set[str] = set()
    if lineage is None:
        lineage = _Lineage()
    for path in sorted(root.glob("*/*.jsonl")):
        if "_precompact_" in path.name:
            continue  # PreCompact-hook backups duplicate the live transcript (same sessionId)
        try:
            if since is not None and (now - path.stat().st_mtime) > since:
                continue
            key = str(path)
            state = states.get(key)
            if state is None:
                state = _FileState(session_uid="", offset=offsets.get(key, 0))
                states[key] = state
            total += await _mirror_one(db, path, state, lineage)
            offsets[key] = state.offset
            # Idle/already-read files have no streamed events to derive from: peek
            # the head once to recover the session id and (one-time) attribute the
            # project, backfilling a row left as "(no project)" by an earlier pass.
            if not state.session_uid or (state.project is None and not state.attr_peeked):
                uid, cwd = _peek_head(path)
                if not state.session_uid:
                    state.session_uid = uid
                if state.project is None and not state.attr_peeked:
                    state.attr_peeked = True
                    if cwd:
                        await _attribute_idle(db, state, cwd)
            seen.add(state.session_uid)
        except FileNotFoundError:
            continue
        except Exception as exc:  # one bad transcript must not kill the tail
            log_error(f"mirror failed for {path.name}: {exc}")
    from lionagi.state.claude_mirror import link_session_lineage, reconcile_session_status

    for uid in seen:
        await reconcile_session_status(db, uid, now=now, live_window=live_window)
    for child_uid, parent_uid, parent_event_uuid in lineage.resolve():
        await link_session_lineage(
            db, child_uid=child_uid, parent_uid=parent_uid, parent_event_uuid=parent_event_uuid
        )
        progress(f"  mirror: {child_uid[:8]} continues {parent_uid[:8]} (lineage)")
    return total


async def mirror_forever(
    stop: asyncio.Event,
    *,
    root: Path | None = None,
    since: str | None = "24h",
    interval: float = 5.0,
    live_window: float = _DEFAULT_LIVE_WINDOW,
) -> None:
    """Tail recent Claude transcripts into StateDB until ``stop`` is set.

    ``since`` bounds the scan to the recent window, so it catches up and tails
    live without ever backfilling full history. Studio's in-process entry point;
    ``li mirror`` keeps its own loop in ``_run``.
    """
    from lionagi.state.db import StateDB

    root = Path(root).expanduser() if root else CLAUDE_PROJECTS_DIR
    if not root.exists():
        return
    since_secs = _parse_window(since) if since else None
    states = _load_states()
    offsets = {key: st.offset for key, st in states.items()}  # _one_pass new-file seed
    lineage = _Lineage()
    _seed_lineage(lineage, states)
    # The connection lives inside the supervise loop so a failure to OPEN it —
    # e.g. a locked or half-migrated state.db during first-run startup, when the
    # studio is creating the schema and checkpointing on another connection — is
    # retried, not fatal. Opening it once outside the loop meant a single
    # transient open error silently ended the in-process mirror for the whole
    # life of the studio process.
    while not stop.is_set():
        try:
            async with StateDB() as db:
                while not stop.is_set():
                    try:
                        await _one_pass(
                            db,
                            root,
                            states,
                            offsets,
                            since=since_secs,
                            live_window=live_window,
                            lineage=lineage,
                        )
                        _save_states(states)
                    except Exception:  # a single bad pass must never kill the tail
                        _log.exception("claude mirror pass failed")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=interval)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
        except Exception:  # connection open/teardown failed — retry, never die
            _log.exception("claude mirror connection failed; retrying")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except (asyncio.TimeoutError, TimeoutError):
                pass


async def _run(args: argparse.Namespace) -> int:
    import anyio

    from lionagi.state.db import StateDB

    root = Path(args.root).expanduser() if args.root else CLAUDE_PROJECTS_DIR
    if not root.exists():
        warn(f"no Claude projects directory at {root}")
        return 1

    since = args.since  # argparse already parsed --since to seconds (or None)
    states = _load_states()
    offsets = {key: st.offset for key, st in states.items()}  # _one_pass new-file seed
    lineage = _Lineage()
    _seed_lineage(lineage, states)

    mode = "catch-up pass" if args.once else f"tailing (every {args.interval:g}s)"
    hint(f"li mirror: {mode} over {root}")

    async with StateDB() as db:
        while True:
            n = await _one_pass(
                db,
                root,
                states,
                offsets,
                since=since,
                live_window=args.live_window,
                lineage=lineage,
            )
            _save_states(states)
            if n:
                progress(f"  mirrored {n} new message(s)")
            if args.once:
                break
            await anyio.sleep(args.interval)
    return 0


def run_mirror(args: argparse.Namespace) -> int:
    from lionagi.ln.concurrency import run_async

    try:
        return run_async(_run(args))
    except KeyboardInterrupt:
        hint("li mirror: stopped")
        return 0
