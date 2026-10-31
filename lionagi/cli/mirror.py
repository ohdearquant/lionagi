# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li mirror` — stream Claude Code transcripts into StateDB so they appear live in studio."""

from __future__ import annotations

import argparse
from pathlib import Path

from lionagi._auto import CliDeclaration, auto_register

from ._logging import hint, progress, warn

# The loop below uses some of these; the rest stay importable from this module for its callers.
from ._mirror_service import (  # noqa: F401
    _DEFAULT_LIVE_WINDOW,
    CLAUDE_PROJECTS_DIR,
    CODEX_SESSIONS_DIR,
    _absorb_backfill,
    _codex_pass,
    _derive_metadata,
    _display_path,
    _fallback_project,
    _FileState,
    _first_prompt,
    _Lineage,
    _load_states,
    _mirror_one_codex,
    _one_pass,
    _parse_window,
    _peek_codex_head,
    _read_new_events,
    _save_states,
    _seed_lineage,
    _since_window,
    mirror_forever,
)


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


async def _run(args: argparse.Namespace) -> int:
    from lionagi.ln.concurrency import sleep
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
    # A requested source whose tree is missing drops out with a warning; only
    # when nothing requested is readable is there no work to do.
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
    lineage = _Lineage()
    threads: dict[str, str] = {}
    _seed_lineage(lineage, states)

    mode = "catch-up pass" if args.once else f"tailing (every {args.interval:g}s)"
    trees = ", ".join(str(p) for p, want in ((root, want_claude), (codex_root, want_codex)) if want)
    hint(f"li mirror: {mode} over {trees}")

    async with StateDB() as db:
        # Retried every pass until one sweep completes cleanly (see
        # _absorb_backfill), same as studio's mirror_forever.
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
                    since=since,
                    live_window=args.live_window,
                    lineage=lineage,
                )
            if want_codex:
                n += await _codex_pass(
                    db,
                    codex_root,
                    states,
                    since=since,
                    live_window=args.live_window,
                    threads=threads,
                )
            _save_states(states)
            if n:
                progress(f"  mirrored {n} new message(s)")
            if args.once:
                break
            await sleep(args.interval)
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
