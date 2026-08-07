# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li mirror` — stream Claude Code transcripts into StateDB so they appear live in studio."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi._auto import CliDeclaration, auto_register
from lionagi._paths import LIONAGI_HOME, ensure_lionagi_dir
from lionagi.ln._json_dump import raise_if_non_finite
from lionagi.state.session_naming import sanitize_prompt_name
from lionagi.studio.config import MIRROR_PREVIEW_CHARS

from ._logging import hint, log_error, progress, warn

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def _display_path(path: Path) -> str:
    """Render a path under the home directory as ~/... for display.

    Help text is the same on every machine this way. Printing the expanded
    absolute path puts the running user's home directory into --help output,
    which differs per machine and ends up in anything that captures it.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


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
        help=(
            "Do a single catch-up pass over existing transcripts and exit, instead of tailing "
            "for new ones. Use it to backfill history without leaving a process running."
        ),
    )
    p.add_argument(
        "--interval",
        type=float,
        default=3.0,
        metavar="SECS",
        help="Seconds between passes over the transcript directory while tailing (default 3).",
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
        help=f"Claude projects directory (default {_display_path(CLAUDE_PROJECTS_DIR)}).",
    )
    p.add_argument(
        "--codex-root",
        default=None,
        metavar="DIR",
        help=f"Codex sessions directory (default {_display_path(CODEX_SESSIONS_DIR)}).",
    )
    p.add_argument(
        "--source",
        choices=("both", "claude", "codex"),
        default="both",
        help=(
            "Which transcripts to mirror (default both). A full codex backfill is "
            "large; pair 'codex' with --since to bound it."
        ),
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
    # Most recent codex turn_context (model/effort/turn_id). Carried across passes
    # so a message mirrored after a resume is still attributed to the turn that
    # produced it rather than to nothing.
    turn: dict[str, str] = field(default_factory=dict)
    barren_reported: bool = False  # whether "read records, mirrored none" was surfaced
    # Rollout spawned headlessly by an orchestrator (originator in
    # SKIPPED_ORIGINATORS): never mirrored — the spawning run already has a
    # session of its own. Derived from the head peek, so it is re-derived after
    # a restart rather than persisted.
    orchestrated: bool = False


@dataclass
class _Lineage:
    """Cross-session conversation-lineage detector, kept across poll passes.

    Indexes each file's leaf uuid; when a file's root parent resolves to a
    different session's leaf (a continuation via ``parentUuid``), records the link.
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
    """Attribute a cwd that detect_project couldn't place, by its folder name
    (or "others" if that directory no longer exists)."""
    p = Path(cwd)
    if p.is_dir():
        return p.name, "cwd_dir"
    return "others", "cwd_missing"


def _resolve_project_for_mirror(cwd: str) -> tuple[str, str]:
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
    # Persist tool_names, leaf_uuid and the current codex turn alongside the byte
    # offset so a restart resumes without losing state that otherwise lives only in
    # process memory. Dropping the turn would leave every message written after a
    # restart unattributed until the next turn_context happens to arrive.
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
                turn={str(k): str(v) for k, v in (val.get("turn") or {}).items() if v is not None},
            )
    return states


def _save_states(states: dict[str, _FileState]) -> None:
    ensure_lionagi_dir(_OFFSETS_PATH.parent)
    payload = {
        key: {
            "offset": st.offset,
            "session_uid": st.session_uid,
            "tool_names": st.tool_names,
            "leaf_uuid": st.leaf_uuid,
            "turn": st.turn,
        }
        for key, st in states.items()
    }
    # Only the byte offset is this module's own arithmetic. The session uid, the
    # leaf uuid and the tool-name map are copied out of a transcript another
    # program wrote, through a json.loads that accepts NaN and Infinity, and none
    # of them is coerced to str on the way in. So a token no strict reader accepts
    # can reach this file, and once here it round-trips through the equally
    # permissive read above and never leaves. Refuse at the write instead.
    raise_if_non_finite(payload)
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
    Raises ValueError (not a silent unbounded-scan fallback) on a bad spec."""
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


def _read_new_events(
    path: Path, state: _FileState
) -> tuple[list[dict[str, Any]], list[tuple[int, int, str]], int, int]:
    """Read complete JSONL lines past the cursor.

    Returns ``(events, sources, new_offset, unreadable)``. ``sources`` is the
    per-event ``(byte_offset, byte_count, sha256)`` of each raw line, in the
    same order as ``events`` — the basis for a resolvable mirror source pointer
    (see ``_mirror_common.bound_mirror_content``).

    The cursor is NOT advanced here — the caller only commits it after the batch
    is durably mirrored, so a write failure re-reads the same lines next pass.

    ``unreadable`` counts lines that were not JSON, or were JSON but not a record
    object. It is reported rather than folded into the events count because a line
    that could not be read is not a line deliberately not mirrored, and a consumer
    comparing what a file held against what landed needs the difference between a
    damaged corpus and an uninteresting one.
    """
    size = path.stat().st_size
    if state.offset > size:  # file truncated/rotated — re-read from the top.
        state.offset = 0
    with path.open("rb") as fh:
        fh.seek(state.offset)
        chunk = fh.read()
    if b"\n" not in chunk:
        return [], [], state.offset, 0
    body, _, _ = chunk.rpartition(b"\n")
    new_offset = state.offset + len(body) + 1
    events: list[dict[str, Any]] = []
    sources: list[tuple[int, int, str]] = []
    unreadable = 0
    pos = state.offset
    for raw in body.split(b"\n"):
        line_offset = pos
        pos += len(raw) + 1  # +1 for the newline consumed by split
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            unreadable += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)
            sources.append((line_offset, len(raw), hashlib.sha256(raw).hexdigest()))
        else:
            unreadable += 1
    return events, sources, new_offset, unreadable


_COMMAND_NOISE = ("<command-", "<local-command-")


def _derive_metadata(state: _FileState, events: list[dict[str, Any]]) -> None:
    """Fill project/model/name from the transcript the first time we see them."""
    if state.project is None:
        cwd = next((e.get("cwd") for e in events if e.get("cwd")), None)
        if cwd:
            state.project, state.project_source = _resolve_project_for_mirror(cwd)
            if state.name is None:
                state.name = f"Claude · {state.project.split('/')[-1]}"
    if state.model is None:
        for e in events:
            if e.get("type") == "assistant" and isinstance(e.get("message"), dict):
                model = e["message"].get("model")
                if model:
                    state.model = model
                    break
    # Prefer the first real user prompt as the session name, sanitized so a
    # prompt that folds in the framework's system-message banner doesn't
    # leak it into the displayed name.
    if events and (state.name is None or state.name.startswith("Claude · ")):
        prompt = _first_prompt(events)
        if prompt:
            sanitized = sanitize_prompt_name(prompt)
            if sanitized:
                state.name = sanitized


def _peek_head(path: Path) -> tuple[str, str | None]:
    """Recover (sessionId, cwd) from a transcript's head without consuming the tail.

    Needed for idle files after a restart, which have no new events to derive these from.
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

    Covers sessions mirrored before project attribution existed, which have no new
    events to trigger the normal (streamed-event) attribution path.
    """
    from lionagi.state.claude_mirror import session_db_id

    state.project, state.project_source = _resolve_project_for_mirror(cwd)
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

    events, sources, new_offset, unreadable = _read_new_events(path, state)
    if unreadable:
        warn(f"{path.name}: {unreadable} unreadable line(s) skipped")
    if not events:
        state.offset = new_offset  # advance past blank/malformed-only lines
        return 0

    if not state.session_uid:
        state.session_uid = next((e["sessionId"] for e in events if e.get("sessionId")), path.stem)
    _derive_metadata(state, events)
    lineage.note_head(state, events)
    lineage.note_leaf(state, events)

    # Always created/kept running; the session-level idle sweep (after the whole
    # pass) is what flips it to completed.
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
        source_path=str(path),
        event_sources=sources,
        max_preview_chars=MIRROR_PREVIEW_CHARS,
    )
    # Advance the cursor only after the batch is durably mirrored, so a failed
    # write re-reads (idempotently) rather than losing the batch.
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
            # the head once to recover the session id and attribute the project.
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


def _peek_codex_head(path: Path) -> tuple[str, dict[str, Any] | None]:
    """Classify a rollout's first line without consuming the tail.

    Returns ``("meta", meta)`` for a parsed session_meta header,
    ``("headerless", None)`` for a complete first line that is not one, and
    ``("torn", None)`` when the line is still being written or unreadable.
    Completeness is decided by the trailing newline BEFORE any parse attempt:
    rollouts are append-only JSONL, so a line without its newline is still
    arriving and defers even when the bytes so far happen to parse — later
    appends can extend or corrupt it. A newline-terminated line that cannot
    parse is permanently corrupt and settles as headerless; the normal reader
    accounts for the bad line.
    """
    from lionagi.state.codex_mirror import session_meta

    try:
        with path.open("rb") as fh:
            line = fh.readline()
    except OSError:
        return "torn", None
    if not line.endswith(b"\n"):
        return "torn", None
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "headerless", None
    meta = session_meta(rec) if isinstance(rec, dict) else None
    return ("meta", meta) if meta else ("headerless", None)


def _derive_codex_metadata(state: _FileState, records: list[dict[str, Any]]) -> None:
    """Fill model and session name from the records seen so far."""
    if state.model is None:
        for r in records:
            if r.get("type") != "turn_context":
                continue
            model = (r.get("payload") or {}).get("model")
            if model:
                state.model = str(model)
                break
    if state.name is None:
        prompt = _first_codex_prompt(records)
        if prompt:
            sanitized = sanitize_prompt_name(prompt)
            if sanitized:
                state.name = sanitized


def _first_codex_prompt(records: list[dict[str, Any]]) -> str | None:
    """First real user turn in a rollout, skipping codex's injected context blocks."""
    from lionagi.state.codex_mirror import messages_for_record

    for r in records:
        for m in messages_for_record(r, "probe", {}):
            text = (m.content or {}).get("instruction") if isinstance(m.content, dict) else None
            if text:
                return " ".join(str(text).split())
    return None


async def _mirror_one_codex(db, path: Path, state: _FileState, threads: dict[str, str]) -> int:
    """Mirror new records from one rollout file; returns messages written."""
    from lionagi.state.codex_mirror import (
        SKIPPED_ORIGINATORS,
        absorb_orchestrated_session,
        link_session_lineage,
        mirror_session,
    )

    if state.orchestrated:
        return 0

    meta: dict[str, Any] | None = None
    if not state.head_checked:
        head_status, meta = _peek_codex_head(path)
        if head_status == "torn":
            # The first line is still being written (or unreadable), so the
            # rollout's identity is unknown. Mirroring waits along with the
            # classification: writing records now would key them under the path
            # stem while the header's real UID arrives next pass, splitting one
            # rollout into two sessions — and committing head_checked would let
            # an orchestrated rollout slip past the skip forever.
            return 0
        if meta and meta.get("originator") in SKIPPED_ORIGINATORS:
            # An orchestrator's own run (e.g. a lionagi agent leg) — its
            # session already exists under the agent's name; importing the
            # rollout too is the double entry. Absorb what an older version
            # may have imported (under either id this file was ever keyed
            # by), then never read this file again.
            prior_uid = state.session_uid  # a stem fallback from a pre-header pass
            resolved_uid = meta["rollout_uid"] or path.stem
            await absorb_orchestrated_session(db, resolved_uid)
            if prior_uid and prior_uid != resolved_uid:
                await absorb_orchestrated_session(db, prior_uid)
            # Nothing is committed to the state until both absorptions return,
            # so a failed attempt leaves exactly what the next one needs to
            # repeat it. Committing head_checked would stop the header being
            # re-read, and the rollout would then be mirrored after all —
            # the same failure the torn-header branch above declines to cause.
            # Committing orchestrated would retire the file with a row nobody
            # ever tore down. Overwriting session_uid would lose the stem the
            # second absorption call needs. Absorption fails for an ordinary
            # reason now that a contended teardown gives up rather than waiting,
            # so this path carries real traffic.
            state.session_uid = resolved_uid
            state.head_checked = True
            state.orchestrated = True
            return 0
        state.head_checked = True
        if meta:
            state.session_uid = meta["rollout_uid"] or path.stem
            if meta.get("cwd"):
                state.project, state.project_source = _resolve_project_for_mirror(meta["cwd"])
    if not state.session_uid:
        state.session_uid = path.stem

    records, sources, new_offset, unreadable = _read_new_events(path, state)
    if not records:
        state.offset = new_offset
        return 0

    _derive_codex_metadata(state, records)
    node_metadata = None
    if meta:
        thread = {k: v for k, v in meta.items() if k.endswith("_uid") and v}
        thread.pop("rollout_uid", None)
        if meta.get("originator"):
            thread["originator"] = meta["originator"]
        if thread:
            node_metadata = {"codex": thread}

    written, tally = await mirror_session(
        db,
        rollout_uid=state.session_uid,
        records=records,
        tool_names=state.tool_names,
        project=state.project,
        project_source=state.project_source,
        model=state.model,
        name=state.name,
        status="running",
        node_metadata=node_metadata,
        source_path=str(path),
        turn=state.turn,
        unparseable=unreadable,
        event_sources=sources,
        max_preview_chars=MIRROR_PREVIEW_CHARS,
    )
    # Advance only after a durable write, so a failed batch is re-read next pass.
    state.offset = new_offset

    if meta and meta.get("thread_uid"):
        parent = threads.setdefault(meta["thread_uid"], state.session_uid)
        if parent != state.session_uid:
            await link_session_lineage(db, child_uid=state.session_uid, parent_uid=parent)
    if written and not state.created:
        state.created = True
        progress(f"  mirror: {state.name or state.session_uid[:8]} (+{written} msgs)")
    elif not written and records and not state.barren_reported:
        # A file the mirror read in full and produced nothing from is a finding, not
        # a quiet skip: without this it is indistinguishable from a file not yet
        # reached. The durable half is the session row the writer keeps for such a
        # file; this only surfaces it in the run that saw it. Measured cause on the
        # local corpus: 6 of 29,652 rollouts (all 2025-09-01/02) predate the
        # enveloped record format and match no record type this mirror reads.
        state.barren_reported = True
        warn(
            f"{path.name}: read {sum(tally.seen.values())} record(s), mirrored none "
            f"(types: {', '.join(sorted(tally.seen)) or 'none'})"
        )
    return written


async def _codex_pass(db, root: Path, states, offsets, *, since, live_window, threads) -> int:
    """One sweep over the codex rollout tree; mirrors new records and reconciles status."""
    from lionagi.state.codex_mirror import reconcile_session_status

    now = time.time()
    total = 0
    seen: set[str] = set()
    for path in sorted(root.rglob("rollout-*.jsonl")):
        try:
            if since is not None and (now - path.stat().st_mtime) > since:
                continue
            key = str(path)
            state = states.get(key)
            if state is None:
                state = _FileState(session_uid="", offset=offsets.get(key, 0))
                states[key] = state
            total += await _mirror_one_codex(db, path, state, threads)
            offsets[key] = state.offset
            if state.session_uid and not state.orchestrated:
                seen.add(state.session_uid)
        except FileNotFoundError:
            continue
        except Exception as exc:  # one bad rollout must not kill the tail
            log_error(f"mirror failed for {path.name}: {exc}")
    for uid in seen:
        await reconcile_session_status(db, uid, now=now, live_window=live_window)
    return total


async def _absorb_backfill(db) -> bool:
    """Remove previously-imported rows for orchestrator-spawned rollouts.

    It reads recorded provenance rather than the rollout tree, so it also
    reaches rows whose files fall outside the sweep window; per-file absorption
    in ``_mirror_one_codex`` covers the rest. Returns whether the sweep
    completed cleanly — a caller that runs this once per process must only
    stand down on True, else one bad pass would retire the backfill for the
    process lifetime. Errors are logged, never raised: reconciliation must not
    keep the mirror down.
    """
    from lionagi.state.codex_mirror import absorb_orchestrated_backfill

    try:
        removed, failed = await absorb_orchestrated_backfill(db)
    except Exception:
        _log.exception("codex mirror orchestrated-session backfill failed")
        return False
    if removed:
        progress(f"  mirror: absorbed {removed} orchestrator-spawned codex session(s)")
    if failed:
        warn(f"codex mirror backfill: {failed} row(s) failed to reconcile; will retry")
        return False
    return True


async def mirror_forever(
    stop: asyncio.Event,
    *,
    root: Path | None = None,
    codex_root: Path | None = None,
    source: str = "claude",
    since: str | None = "24h",
    interval: float = 5.0,
    live_window: float = _DEFAULT_LIVE_WINDOW,
) -> None:
    """Tail recent transcripts into StateDB until ``stop`` is set.

    ``source`` selects which transcript trees to read ("claude", "codex", or
    "both") and defaults to claude alone. A caller that scopes ``root`` has
    scoped the mirror, and must not silently acquire an unscoped codex tree
    under the home directory; asking for codex is therefore explicit.

    Studio's in-process entry point; ``li mirror`` keeps its own loop in ``_run``.
    """
    from lionagi.state.db import StateDB

    if source not in ("both", "claude", "codex"):
        raise ValueError(f"unknown mirror source: {source!r}")
    want_claude = source in ("both", "claude")
    want_codex = source in ("both", "codex")

    root = Path(root).expanduser().resolve() if root else CLAUDE_PROJECTS_DIR
    codex_root = Path(codex_root).expanduser().resolve() if codex_root else CODEX_SESSIONS_DIR
    if not (want_claude and root.exists()) and not (want_codex and codex_root.exists()):
        return
    since_secs = _parse_window(since) if since else None
    states = _load_states()
    offsets = {key: st.offset for key, st in states.items()}  # _one_pass new-file seed
    lineage = _Lineage()
    threads: dict[str, str] = {}
    _seed_lineage(lineage, states)
    # The connection lives inside the supervise loop so a failure to open it (e.g. a
    # locked/half-migrated state.db at studio startup) is retried, not fatal.
    backfilled = False
    while not stop.is_set():
        try:
            async with StateDB() as db:
                while not stop.is_set():
                    try:
                        if want_codex and not backfilled:
                            backfilled = await _absorb_backfill(db)
                        if want_claude and root.exists():
                            await _one_pass(
                                db,
                                root,
                                states,
                                offsets,
                                since=since_secs,
                                live_window=live_window,
                                lineage=lineage,
                            )
                        if want_codex and codex_root.exists():
                            await _codex_pass(
                                db,
                                codex_root,
                                states,
                                offsets,
                                since=since_secs,
                                live_window=live_window,
                                threads=threads,
                            )
                        _save_states(states)
                    except Exception:  # a single bad pass must never kill the tail
                        _log.exception("transcript mirror pass failed")
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

    source = getattr(args, "source", "both")
    want_claude = source in ("both", "claude")
    want_codex = source in ("both", "codex")

    root = Path(args.root).expanduser().resolve() if args.root else CLAUDE_PROJECTS_DIR
    codex_root = (
        Path(args.codex_root).expanduser().resolve()
        if getattr(args, "codex_root", None)
        else CODEX_SESSIONS_DIR
    )
    # A requested source whose tree is missing drops out with a warning; only when
    # nothing requested is readable is there no work to do at all.
    if want_claude and not root.exists():
        warn(f"no Claude projects directory at {root}")
        want_claude = False
    if want_codex and not codex_root.exists():
        warn(f"no Codex sessions directory at {codex_root}")
        want_codex = False
    if not want_claude and not want_codex:
        return 1

    since = args.since  # argparse already parsed --since to seconds (or None)
    states = _load_states()
    offsets = {key: st.offset for key, st in states.items()}  # _one_pass new-file seed
    lineage = _Lineage()
    threads: dict[str, str] = {}
    _seed_lineage(lineage, states)

    mode = "catch-up pass" if args.once else f"tailing (every {args.interval:g}s)"
    trees = ", ".join(str(p) for p, want in ((root, want_claude), (codex_root, want_codex)) if want)
    hint(f"li mirror: {mode} over {trees}")

    async with StateDB() as db:
        # Retried every pass until one sweep completes cleanly, same as the
        # studio's mirror_forever — a transient failure on the first attempt
        # must not retire the backfill for the process lifetime.
        backfilled = not want_codex
        while True:
            if not backfilled:
                backfilled = await _absorb_backfill(db)
            n = 0
            if want_claude:
                n += await _one_pass(
                    db,
                    root,
                    states,
                    offsets,
                    since=since,
                    live_window=args.live_window,
                    lineage=lineage,
                )
            if want_codex:
                n += await _codex_pass(
                    db,
                    codex_root,
                    states,
                    offsets,
                    since=since,
                    live_window=args.live_window,
                    threads=threads,
                )
            _save_states(states)
            if n:
                progress(f"  mirrored {n} new message(s)")
            if args.once:
                break
            await anyio.sleep(args.interval)
    return 0


@auto_register(
    area="mirror", cli=CliDeclaration(seed="mirror", parser_factory=add_mirror_subparser)
)
def run_mirror(args: argparse.Namespace) -> int:
    from lionagi.ln.concurrency import run_async

    try:
        return run_async(_run(args))
    except KeyboardInterrupt:
        hint("li mirror: stopped")
        return 0
