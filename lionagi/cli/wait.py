# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li wait <id>...` — the ADR-0035 run-completion contract.

Blocks until every named run (an agent session, a play, a flow invocation, or
a scheduled run — any kind, mixed freely) reaches a terminal state, then
prints one frozen, tab-delimited line per run on stdout:

    <run_id>\tstatus=<terminal_status>\treason=<reason_code>\t
        artifact_dir=<run_dir>\texit_code=<n>

Stdout carries contract lines only; every diagnostic goes to stderr via
``_logging``. `wait_for_terminal()` is the reusable, importable core (no
argv, signal handling, or printing) so other surfaces can await completion
without shelling out; `run_wait()` is the thin CLI shim that resolves
argv, drives the poll loop with clean SIGINT/SIGTERM handling, and prints.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from lionagi._paths import RUNS_ROOT
from lionagi.state.db import TERMINAL_STATUSES_BY_ENTITY_TYPE
from lionagi.state.reasons import VALID_REASON_CODES

from ._logging import log_error
from .monitor import _resolve_schedule_run, _split_watched_ids
from .status import EXIT_RUNNING, EXIT_UNKNOWN, _resolve_any_target, _resolve_primary_session

__all__ = (
    "wait_for_terminal",
    "run_wait",
)

# reason surfaced when a terminal record carries no (or an unrecognized)
# reason_code — an explicit unknown, never an invented VALID_REASON_CODES value.
_UNKNOWN_REASON = "unknown"

# Per-kind "waited run succeeded" predicate, for the aggregate exit code.
# Mirrors status.py's _SESSION_SUCCESS / _PLAY_SUCCESS: a session/invocation
# that completed with no evidence reads as a failure, not a success.
_SUCCESS_STATUS_BY_ENTITY_TYPE: dict[str, frozenset[str]] = {
    "session": frozenset({"completed"}),
    "invocation": frozenset({"completed"}),
    "play": frozenset({"merged"}),
    "schedule_run": frozenset({"completed"}),
}


async def _resolve_wait_target(db: Any, raw_id: str) -> tuple[str, dict[str, Any]] | None:
    """Any-kind resolver: session, invocation, play (`_resolve_any_target`,
    which also falls back to a branch_id), then schedule_run. Terminal-state
    definitions live in TERMINAL_STATUSES_BY_ENTITY_TYPE (lionagi/state/db.py, ADR-0035).
    """
    hit = await _resolve_any_target(db, raw_id)
    if hit is not None:
        return hit
    row = await _resolve_schedule_run(db, raw_id)
    if row is not None:
        return "schedule_run", row
    return None


async def _refetch(db: Any, kind: str, entity_id: str) -> dict[str, Any] | None:
    """Re-read one entity row by its canonical id, dispatched by kind."""
    if kind == "session":
        return await db.get_session(entity_id)
    if kind == "invocation":
        return await db.get_invocation(entity_id)
    if kind == "play":
        return await db.get_play(entity_id)
    if kind == "schedule_run":
        return await db.get_schedule_run(entity_id)
    return None


async def _artifact_dir_for(db: Any, kind: str, row: dict[str, Any]) -> str | None:
    """The run directory backing *row* — always ``RUNS_ROOT / <session id>``,
    resolved via the backing/primary session id (may not exist on disk yet).
    Returns ``None`` only when there is no backing session id to anchor on."""
    if kind == "session":
        return str(RUNS_ROOT / row["id"])
    if kind == "invocation":
        primary = await _resolve_primary_session(db, "invocation", row)
        return str(RUNS_ROOT / primary["id"]) if primary else None
    if kind == "play":
        primary = await _resolve_primary_session(db, "play", row)
        return str(RUNS_ROOT / primary["id"]) if primary else None
    if kind == "schedule_run":
        invocation_id = row.get("invocation_id")
        if not invocation_id:
            return None
        inv = await db.get_invocation(invocation_id)
        if inv is None:
            return None
        primary = await _resolve_primary_session(db, "invocation", inv)
        return str(RUNS_ROOT / primary["id"]) if primary else None
    return None


def _reason_for(row: dict[str, Any]) -> str:
    code = row.get("status_reason_code")
    return code if code in VALID_REASON_CODES else _UNKNOWN_REASON


async def _build_outcome(db: Any, kind: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row["id"],
        "kind": kind,
        "status": row["status"],
        "reason": _reason_for(row),
        "artifact_dir": await _artifact_dir_for(db, kind, row),
        "exit_code": row.get("exit_code"),
        "success": row["status"] in _SUCCESS_STATUS_BY_ENTITY_TYPE.get(kind, frozenset()),
    }


def format_wait_line(outcome: dict[str, Any]) -> str:
    """Render one outcome as the frozen ADR-0035 contract line."""
    exit_code = outcome.get("exit_code")
    exit_str = "-" if exit_code is None else str(exit_code)
    artifact_dir = outcome.get("artifact_dir") or "-"
    return (
        f"{outcome['run_id']}\t"
        f"status={outcome['status']}\t"
        f"reason={outcome['reason']}\t"
        f"artifact_dir={artifact_dir}\t"
        f"exit_code={exit_str}"
    )


async def wait_for_terminal(
    ids: list[str],
    *,
    interval: float = 1.0,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Block until every id in *ids* reaches a terminal state; return one
    outcome dict per id, in the order given (ADR-0035 completion contract).

    Importable and awaitable directly — no CLI concerns (argv, signals,
    printing) live here; `run_wait()` below is the CLI shim over this core.

    *on_result* is called once per resolved run, the moment it goes terminal
    (or is found unresolvable), so a caller can print incrementally instead
    of waiting for the whole set to drain. *should_stop* is polled between
    ticks so a CLI wrapper can wire SIGINT/SIGTERM into a clean early return.
    """
    from lionagi.state.db import StateDB

    order: list[str] = []
    outcomes: dict[str, dict[str, Any]] = {}
    pending: dict[str, str] = {}  # canonical id -> kind

    async with StateDB() as db:
        for raw_id in ids:
            hit = await _resolve_wait_target(db, raw_id)
            if hit is None:
                outcome = {
                    "run_id": raw_id,
                    "kind": None,
                    "status": "not_found",
                    "reason": _UNKNOWN_REASON,
                    "artifact_dir": None,
                    "exit_code": None,
                    "success": False,
                }
                outcomes[raw_id] = outcome
                order.append(raw_id)
                if on_result is not None:
                    on_result(outcome)
                continue
            kind, row = hit
            canonical_id = row["id"]
            order.append(canonical_id)
            terminal_statuses = TERMINAL_STATUSES_BY_ENTITY_TYPE.get(kind, frozenset())
            if row["status"] in terminal_statuses:
                outcome = await _build_outcome(db, kind, row)
                outcomes[canonical_id] = outcome
                if on_result is not None:
                    on_result(outcome)
            else:
                pending[canonical_id] = kind

        while pending and not (should_stop is not None and should_stop()):
            for run_id in list(pending):
                kind = pending[run_id]
                row = await _refetch(db, kind, run_id)
                if row is None:
                    # Existed at resolution time but is gone now (e.g. cascade
                    # delete of its parent) — resolve as unknown rather than
                    # hang the wait on state that never comes back.
                    outcome = {
                        "run_id": run_id,
                        "kind": kind,
                        "status": "unknown",
                        "reason": _UNKNOWN_REASON,
                        "artifact_dir": None,
                        "exit_code": None,
                        "success": False,
                    }
                    outcomes[run_id] = outcome
                    del pending[run_id]
                    if on_result is not None:
                        on_result(outcome)
                    continue
                terminal_statuses = TERMINAL_STATUSES_BY_ENTITY_TYPE.get(kind, frozenset())
                if row["status"] not in terminal_statuses:
                    continue
                outcome = await _build_outcome(db, kind, row)
                outcomes[run_id] = outcome
                del pending[run_id]
                if on_result is not None:
                    on_result(outcome)
            if pending and not (should_stop is not None and should_stop()):
                import asyncio

                await asyncio.sleep(interval)

    return [outcomes[rid] for rid in order if rid in outcomes]


def run_wait(argv: list[str]) -> int:
    """Entry point for `li wait <id> [<id2> ...] [--interval SECS]`.

    run_async (lionagi.ln.concurrency) installs its own SIGINT/SIGTERM
    handlers for the duration of this call, mirroring how monitor.py's
    `_dispatch_wait` drives its own poll loop, so Ctrl-C/SIGTERM interrupt
    the wait cleanly instead of leaving a stray process.
    """
    from lionagi.ln.concurrency import SigtermInterrupt, run_async
    from lionagi.state.db import DEFAULT_DB_PATH

    parser = argparse.ArgumentParser(prog="li wait", add_help=True)
    parser.add_argument(
        "ids",
        nargs="+",
        help=(
            "Run ID(s) (or short prefixes) to wait for — any kind (agent "
            "session, play, flow invocation, scheduled run), comma- or "
            "space-separated, mixed freely."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SECS",
        help="Poll interval in seconds (default 1).",
    )
    args = parser.parse_args(argv)
    watched_ids = _split_watched_ids(args.ids)
    if not watched_ids:
        parser.error("no run ids given (only empty/comma-only tokens)")

    if not DEFAULT_DB_PATH.exists():
        log_error("state.db not found — no runs recorded yet")
        return EXIT_UNKNOWN

    outcomes: list[dict[str, Any]] = []

    def _on_result(outcome: dict[str, Any]) -> None:
        if outcome["status"] == "not_found":
            log_error(f"run {outcome['run_id']!r} not found")
            return
        print(format_wait_line(outcome))

    # run_async installs its own SIGINT/SIGTERM handlers for the duration of
    # this call and raises KeyboardInterrupt/SigtermInterrupt if either lands
    # mid-wait — a still-in-progress wait is neither success nor failure.
    interrupted = False
    try:
        outcomes = run_async(
            wait_for_terminal(watched_ids, interval=args.interval, on_result=_on_result)
        )
    except (KeyboardInterrupt, SigtermInterrupt):
        interrupted = True

    if interrupted or len(outcomes) < len(watched_ids):
        return EXIT_RUNNING
    if any(o["status"] in ("not_found", "unknown") for o in outcomes):
        return EXIT_UNKNOWN
    return 0 if all(o["success"] for o in outcomes) else 1
