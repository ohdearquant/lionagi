# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li mirror` — stream Claude Code transcripts into StateDB so they appear live in studio."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi._paths import LIONAGI_HOME

from ._logging import hint, log_error, progress, warn

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
_OFFSETS_PATH = LIONAGI_HOME / "mirror" / "offsets.json"

# A transcript whose file changed within this window is treated as a live run.
_DEFAULT_LIVE_WINDOW = 120.0


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
        help="Idle gap after which a session is marked completed (default 120).",
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
    completed: bool = False


def _load_offsets() -> dict[str, int]:
    try:
        return json.loads(_OFFSETS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_offsets(offsets: dict[str, int]) -> None:
    _OFFSETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OFFSETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(offsets))
    tmp.replace(_OFFSETS_PATH)


_WINDOW_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _parse_window(spec: str) -> float | None:
    spec = spec.strip().lower()
    if not spec:
        return None
    try:
        if spec[-1] in _WINDOW_UNITS:
            return float(spec[:-1]) * _WINDOW_UNITS[spec[-1]]
        return float(spec)
    except ValueError:
        warn(f"unrecognized --since window {spec!r}; ignoring")
        return None


def _read_new_events(path: Path, state: _FileState) -> list[dict[str, Any]]:
    """Read complete JSONL lines past the cursor; advance the cursor past them."""
    size = path.stat().st_size
    if state.offset > size:  # file truncated/rotated — re-read from the top.
        state.offset = 0
    with path.open("rb") as fh:
        fh.seek(state.offset)
        chunk = fh.read()
    if b"\n" not in chunk:
        return []
    body, _, _ = chunk.rpartition(b"\n")
    state.offset += len(body) + 1
    events = []
    for raw in body.split(b"\n"):
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return events


_COMMAND_NOISE = ("<command-", "<local-command-")


def _derive_metadata(state: _FileState, events: list[dict[str, Any]]) -> None:
    """Fill project/model/name from the transcript the first time we see them."""
    from ._project import detect_project

    if state.project is None:
        cwd = next((e.get("cwd") for e in events if e.get("cwd")), None)
        if cwd:
            try:
                state.project, state.project_source = detect_project(Path(cwd))
            except Exception:  # detection is best-effort; never block the mirror
                state.project, state.project_source = None, None
            if state.name is None:
                base = state.project.split("/")[-1] if state.project else Path(cwd).name
                state.name = f"Claude · {base}"
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


async def _mirror_one(db, path: Path, state: _FileState, *, live_window: float) -> int:
    from lionagi.state.claude_mirror import mirror_session

    events = _read_new_events(path, state)
    if not events and state.completed:
        return 0

    if events and not state.session_uid:
        state.session_uid = next((e["sessionId"] for e in events if e.get("sessionId")), path.stem)
    if not state.session_uid:
        state.session_uid = path.stem
    _derive_metadata(state, events)

    idle = (time.time() - path.stat().st_mtime) > live_window
    status = "completed" if idle else "running"

    written = await mirror_session(
        db,
        session_uid=state.session_uid,
        events=events,
        tool_names=state.tool_names,
        project=state.project,
        project_source=state.project_source,
        model=state.model,
        name=state.name,
        status=status,
    )
    if written and not state.created:
        state.created = True
        progress(f"  mirror: {state.name or state.session_uid[:8]} (+{written} msgs)")
    if status == "completed":
        state.completed = True
    return written


async def _one_pass(db, root: Path, states, offsets, *, since, live_window) -> int:
    now = time.time()
    total = 0
    for path in sorted(root.glob("*/*.jsonl")):
        try:
            if since is not None and (now - path.stat().st_mtime) > since:
                continue
            key = str(path)
            state = states.get(key)
            if state is None:
                state = _FileState(session_uid="", offset=offsets.get(key, 0))
                states[key] = state
            total += await _mirror_one(db, path, state, live_window=live_window)
            offsets[key] = state.offset
        except FileNotFoundError:
            continue
        except Exception as exc:  # one bad transcript must not kill the tail
            log_error(f"mirror failed for {path.name}: {exc}")
    return total


async def _run(args: argparse.Namespace) -> int:
    import anyio

    from lionagi.state.db import StateDB

    root = Path(args.root).expanduser() if args.root else CLAUDE_PROJECTS_DIR
    if not root.exists():
        warn(f"no Claude projects directory at {root}")
        return 1

    since = _parse_window(args.since) if args.since else None
    offsets = _load_offsets()
    states: dict[str, _FileState] = {}

    mode = "catch-up pass" if args.once else f"tailing (every {args.interval:g}s)"
    hint(f"li mirror: {mode} over {root}")

    async with StateDB() as db:
        while True:
            n = await _one_pass(
                db, root, states, offsets, since=since, live_window=args.live_window
            )
            _save_offsets(offsets)
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
