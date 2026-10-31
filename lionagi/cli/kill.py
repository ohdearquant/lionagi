# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li kill` — terminate in-progress lionagi runs/sessions/plays/shows."""

from __future__ import annotations

import argparse
import time
from typing import Any

from lionagi._auto import CliDeclaration, auto_register
from lionagi.ln._proc import (
    current_pid_markers,  # noqa: F401  (re-exported: the kill command's historical import path)
)
from lionagi.state.db import PLAY_ACTIVE_STATUSES as _PLAY_ACTIVE_STATUSES
from lionagi.state.db import SESSION_TERMINAL_STATUSES

from ._kill_service import (
    _REFUSED_SIGNAL_REASONS,
    _STALE_SWEEP_ORDER,
    NOT_JUDGEABLE_HERE,
    NOT_STOPPED_SIGNALS,
    _degraded_run,
    _imported_degraded_process_identity,
    _kill_degraded_run,
    _persist_cancel,
    _pid_addressability,
    _same_cleanup_session,
    _try_recorded_worker_cleanup,
    check_pid_identity,
    kill_one,
    list_running_children,
    read_pid_from_entity,
    unaddressable_pid_reason,
)
from ._logging import log_error, warn
from ._util import _TABLE_TO_ENTITY_TYPE, AmbiguousIdError
from ._util import pid_alive as _pid_alive
from ._util import resolve_entity as _resolve_entity


async def _do_kill(
    id_or_short: str,
    *,
    user_reason: str = "",
    recursive: bool = False,
    grace_seconds: float = 5.0,
    verbose: bool = False,
) -> int:
    """Resolve entity, kill process, persist cancellation."""
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        try:
            resolved = await _resolve_entity(db, id_or_short)
        except AmbiguousIdError as exc:
            # Killing "whichever row matched first" would signal a process the
            # caller never named — refuse and show the candidates instead.
            log_error(str(exc))
            return 1
        if resolved is None:
            degraded = _degraded_run(id_or_short)
            if degraded is not None:
                return _kill_degraded_run(*degraded, grace_seconds=grace_seconds)
            log_error(f"entity not found for id: {id_or_short!r}")
            return 1

        table, entity_type, row = resolved
        current_status = row.get("status")
        if entity_type == "session" and current_status in SESSION_TERMINAL_STATUSES:
            recorded_result = await _try_recorded_worker_cleanup(
                db, row, user_reason=user_reason, grace_seconds=grace_seconds
            )
            if recorded_result is not None:
                return recorded_result
        frozen_snapshot = None
        worker_records_unread = False
        if (
            entity_type == "session"
            and current_status == "running"
            and callable(getattr(db, "snapshot_cli_worker_spawns", None))
        ):
            from lionagi.state.compat import StateCompatibilityRefused

            try:
                frozen_snapshot = await db.snapshot_cli_worker_spawns(row["id"])
            except StateCompatibilityRefused:
                pass
            except Exception as exc:
                warn(f"  session {row['id'][:12]}: worker records could not be read ({exc})")
                worker_records_unread = True
            else:
                if frozen_snapshot is None or not _same_cleanup_session(
                    row, frozen_snapshot["session"]
                ):
                    log_error(f"session {row['id'][:12]} changed during cleanup")
                    return 1
        # Shows never reach "running" (they persist as 'active' per the
        # shows.status vocabulary in state/db.py); every other entity type
        # uses "running" as its only killable status.
        killable_status = "active" if entity_type == "show" else "running"

        if current_status != killable_status:
            log_error(
                f"{entity_type} {row['id'][:12]} is already in terminal state: "
                f"{current_status!r} — nothing to kill"
            )
            return 1

        results = []
        blocked = []

        play_workers_unreachable = False

        if entity_type == "show":
            # ADR-0104 explicitly defers show-level reaping: a show kill only
            # marks the show row terminal. --recursive is a documented no-op
            # here rather than a partial reap of the show's plays/workers.
            children = []
            if recursive:
                warn(
                    f"show {row['id'][:12]}: --recursive does not reap a show's "
                    "plays or their workers (deferred per ADR-0104) — kill the "
                    "play or session ids directly to stop a show's workers"
                )
        elif entity_type == "play":
            # A play row carries no PID of its own, so its whole effect on the
            # running system is through the sessions it started. Resolving them
            # is the kill, not an extra step behind --recursive.
            children = await list_running_children(db, entity_type, row["id"])
            play_workers_unreachable = not children
        elif recursive:
            children = await list_running_children(db, entity_type, row["id"])
        else:
            children = []

        if children:
            for _child_table, child_type, child_row in children:
                r = await kill_one(
                    db,
                    child_type,
                    child_row["id"],
                    child_row,
                    user_reason=user_reason,
                    grace_seconds=grace_seconds,
                    verbose=verbose,
                )
                results.append(r)
                if r.get("cancel_error"):
                    blocked.append(r)
                    log_error(
                        f"  child {child_type} {child_row['id'][:12]}: "
                        f"signal={r['signal']}, but cancellation was not recorded: "
                        f"{r['cancel_error']}"
                    )
                elif r["signal"] in NOT_STOPPED_SIGNALS:
                    blocked.append(r)
                else:
                    print(
                        f"  killed child {child_type} {child_row['id'][:12]} (signal={r['signal']})"
                    )

        if entity_type == "session" and not worker_records_unread:
            recorded_result = await _try_recorded_worker_cleanup(
                db,
                row,
                user_reason=user_reason,
                grace_seconds=grace_seconds,
                snapshot=frozen_snapshot,
            )
            if recorded_result is not None:
                return 1 if blocked else recorded_result

        r = await kill_one(
            db,
            entity_type,
            row["id"],
            row,
            user_reason=user_reason,
            grace_seconds=grace_seconds,
            verbose=verbose,
        )
        results.append(r)
        if r.get("cancel_error"):
            blocked.append(r)
            log_error(
                f"{entity_type} {row['id'][:12]}: signal={r['signal']}, "
                f"but cancellation was not recorded: {r['cancel_error']}"
            )
        elif r["signal"] in NOT_STOPPED_SIGNALS:
            blocked.append(r)
        else:
            print(f"killed {entity_type} {row['id'][:12]} (signal={r['signal']}, pid={r['pid']})")

        if play_workers_unreachable:
            log_error(
                f"play {row['id'][:12]} is marked blocked, but no worker "
                "process was stopped: the row records no running session, so "
                "any workers it started cannot be resolved from the play id. "
                "Find the running sessions with `li monitor` and kill those "
                "ids directly."
            )
            return 1

        if worker_records_unread:
            log_error(
                f"session {row['id'][:12]}: its recorded worker groups were not handled, "
                "because the worker records could not be read"
            )
            return 1

    return 1 if blocked else 0


async def _play_child_stale(db: Any, play_row: dict[str, Any]) -> bool | None:
    """Whether the play's linked session has terminated.

    ``None`` means the question could not be asked: the row records no session
    to look up, or it points at one that is no longer in the table. That is a
    different answer from "the session is still running", and the sweep reports
    it differently.
    """
    session_id = play_row.get("session_id")
    if not session_id:
        return None
    row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (session_id,))
    if row is None:
        return None
    return row["status"] != "running"


async def _show_children_all_terminal(db: Any, show_id: str) -> bool:
    """True if the show has >= 1 child play and all are terminal."""
    rows = await db.fetch_all("SELECT status FROM plays WHERE show_id = ?", (show_id,))
    if not rows:
        return False
    return all(row["status"] not in _PLAY_ACTIVE_STATUSES for row in rows)


async def _do_kill_all_stale(
    *,
    threshold_seconds: int,
    user_reason: str = "",
    grace_seconds: float = 5.0,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Sweep stale sessions/invocations whose PIDs are dead."""
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    cutoff = time.time() - threshold_seconds
    killed = 0
    skipped_live = 0
    skipped_recent = 0
    skipped_unverifiable = 0
    skipped_unjudgeable = 0
    skipped_unlinked_plays = 0
    unverifiable_tracked = 0
    unknown_reasons = {
        "start_mismatch": 0,
        "boot_disagreement": 0,
        "permission_denied": 0,
        "identity_unknown": 0,
    }

    live_status_for: dict[str, str] = {
        "sessions": "running",
        "invocations": "running",
    }

    async with StateDB() as db:

        async def keep_unverifiable(
            entity_type: str,
            entity_id: str,
            meta: dict[str, Any],
            reason: str | None,
        ) -> None:
            nonlocal skipped_unverifiable, unverifiable_tracked
            skipped_unverifiable += 1
            if reason in unknown_reasons:
                unknown_reasons[reason] += 1
            if not dry_run:
                since = meta.get("unverifiable_since")
                if not isinstance(since, (int, float)):
                    since = time.time()
                count = meta.get("unverifiable_count")
                count = (count + 1) if isinstance(count, int) else 1
                marker_patch = {"unverifiable_since": since, "unverifiable_count": count}
                if entity_type == "session":
                    await db.merge_session_node_metadata(entity_id, marker_patch)
                else:
                    await db.merge_invocation_node_metadata(entity_id, marker_patch)
                unverifiable_tracked += 1
            if verbose:
                print(
                    f"  skip {entity_type} {entity_id[:12]}: process identity "
                    f"unverifiable ({reason or 'unknown'}) — retained"
                )

        for table in _STALE_SWEEP_ORDER:
            entity_type = _TABLE_TO_ENTITY_TYPE[table]
            live_status = live_status_for[table]
            rows = await db.fetch_all(
                f"SELECT * FROM {table} WHERE status = ?",  # noqa: S608
                (live_status,),
            )

            for row_dict in (db._row_to_dict(r) for r in rows):
                entity_id = row_dict["id"]

                started = (
                    row_dict.get("started_at")
                    or row_dict.get("updated_at")
                    or row_dict.get("created_at")
                    or 0
                )
                if started > cutoff:
                    skipped_recent += 1
                    if verbose:
                        print(
                            f"  skip {entity_type} {entity_id[:12]}: "
                            f"started recently (< {threshold_seconds}s ago)"
                        )
                    continue

                row_meta_for_host = (
                    row_dict.get("node_metadata")
                    if isinstance(row_dict.get("node_metadata"), dict)
                    else {}
                )
                unjudgeable = unaddressable_pid_reason(row_meta_for_host)
                if unjudgeable in NOT_JUDGEABLE_HERE:
                    # Another host or unmanaged runtime cannot be judged here.
                    skipped_unjudgeable += 1
                    if verbose:
                        print(
                            f"  skip {entity_type} {entity_id[:12]}: "
                            f"{_REFUSED_SIGNAL_REASONS[unjudgeable]} — not judgeable from here"
                        )
                    continue
                pid = read_pid_from_entity(row_dict)
                unjudgeable, pid_alive_now = _pid_addressability(unjudgeable, pid)
                if unjudgeable == "boot_unverifiable":
                    await keep_unverifiable(
                        entity_type, entity_id, row_meta_for_host, "boot_disagreement"
                    )
                    continue

                if (
                    entity_type == "session"
                    and pid is None
                    and row_dict.get("source_kind") == "imported_fs"
                ):
                    imported_verdict, imported_reason = _imported_degraded_process_identity(
                        entity_id
                    )
                    if imported_verdict == "ours":
                        skipped_live += 1
                        if verbose:
                            print(
                                f"  skip {entity_type} {entity_id[:12]}: "
                                "process recorded in run.json is still alive"
                            )
                        continue
                    if imported_verdict == "unverifiable":
                        await keep_unverifiable(
                            entity_type, entity_id, row_meta_for_host, imported_reason
                        )
                        continue
                # A live pid alone isn't enough — the OS may have reused it. Correlate against
                # the row's own session id/create_time, same as the direct-kill path.
                if pid_alive_now is None:
                    pid_alive_now = pid is not None and _pid_alive(pid)
                verdict: str | None = None
                if pid_alive_now:
                    meta = (
                        row_dict.get("node_metadata")
                        if isinstance(row_dict.get("node_metadata"), dict)
                        else {}
                    )
                    expected_session_id = entity_id if entity_type == "session" else None
                    raw_ct = meta.get("pid_create_time")
                    try:
                        expected_create_time = float(raw_ct) if raw_ct is not None else None
                    except (TypeError, ValueError):
                        expected_create_time = None

                    reasons: list[str] = []
                    verdict = check_pid_identity(
                        pid,
                        "lionagi",
                        expected_session_id=expected_session_id,
                        expected_create_time=expected_create_time,
                        _reason_out=reasons,
                    )
                    if verdict == "ours":
                        skipped_live += 1
                        if verbose:
                            print(
                                f"  skip {entity_type} {entity_id[:12]}: "
                                f"process {pid} is still alive"
                            )
                        continue
                    if verdict == "unverifiable":
                        await keep_unverifiable(
                            entity_type, entity_id, meta, reasons[0] if reasons else None
                        )
                        continue
                    # "not_ours" (the pid was recycled by an unrelated process)
                    # and "zombie" (our process exited and nobody reaped it)
                    # both mean the recorded run is gone: fall through and
                    # sweep the row.

                if dry_run:
                    print(
                        f"  (dry-run) would cancel {entity_type} {entity_id[:12]} "
                        f"(pid={pid}, started_at={started:.0f})"
                    )
                    killed += 1
                    continue

                evidence: dict[str, Any] = {
                    "kind": "stale_kill",
                    "pid": pid,
                    # Numeric liveness and identity are two different questions
                    # (issue: a live-but-recycled pid used to hardcode False
                    # here, indistinguishable from a genuinely dead pid).
                    "pid_alive": pid_alive_now,
                    "identity_verdict": verdict,
                    "killed_at": time.time(),
                    "threshold_seconds": threshold_seconds,
                }
                if user_reason:
                    evidence["user_reason"] = user_reason

                reason_summary = f"Stale auto-cancel: process dead or no PID. {user_reason}".strip()

                await _persist_cancel(
                    db,
                    entity_type,
                    entity_id,
                    reason_code=RunReasons.CANCELLED_STALE_AUTO,
                    reason_summary=reason_summary,
                    evidence=evidence,
                )
                killed += 1
                print(f"  cancelled stale {entity_type} {entity_id[:12]} (pid={pid})")

        play_rows = await db.fetch_all("SELECT * FROM plays WHERE status = 'running'", ())
        for row_dict in (db._row_to_dict(r) for r in play_rows):
            play_id = row_dict["id"]

            started = row_dict.get("started_at") or row_dict.get("created_at") or 0
            if started > cutoff:
                skipped_recent += 1
                if verbose:
                    print(
                        f"  skip play {play_id[:12]}: started recently (< {threshold_seconds}s ago)"
                    )
                continue

            child_stale = await _play_child_stale(db, row_dict)
            if child_stale is None:
                # No session to check. Age alone would not distinguish a play
                # whose workers are gone from one still doing hours of work, so
                # the row is left alone and counted for the closing summary.
                skipped_unlinked_plays += 1
                if verbose:
                    print(
                        f"  skip play {play_id[:12]}: row records no worker session, "
                        "so staleness cannot be determined"
                    )
                continue
            if not child_stale:
                if verbose:
                    print(f"  skip play {play_id[:12]}: worker session still running")
                continue

            if dry_run:
                print(f"  (dry-run) would cancel stale play {play_id[:12]} (child-derived)")
                killed += 1
                continue

            evidence = {
                "kind": "child_stale_kill",
                "reason": "child_session_terminal",
                "killed_at": time.time(),
                "threshold_seconds": threshold_seconds,
            }
            if user_reason:
                evidence["user_reason"] = user_reason
            await _persist_cancel(
                db,
                "play",
                play_id,
                reason_code=RunReasons.CANCELLED_STALE_AUTO,
                reason_summary=(
                    f"Stale auto-cancel: child session terminated. {user_reason}".strip()
                ),
                evidence=evidence,
            )
            killed += 1
            print(f"  cancelled stale play {play_id[:12]} (child-derived)")

        show_rows = await db.fetch_all("SELECT * FROM shows WHERE status = 'active'", ())
        for row_dict in (db._row_to_dict(r) for r in show_rows):
            show_id = row_dict["id"]

            started = (
                row_dict.get("started_at")
                or row_dict.get("updated_at")
                or row_dict.get("created_at")
                or 0
            )
            if started > cutoff:
                skipped_recent += 1
                if verbose:
                    print(
                        f"  skip show {show_id[:12]}: started recently (< {threshold_seconds}s ago)"
                    )
                continue

            if not await _show_children_all_terminal(db, show_id):
                if verbose:
                    print(f"  skip show {show_id[:12]}: has active child plays or no plays")
                continue

            if dry_run:
                print(f"  (dry-run) would cancel stale show {show_id[:12]} (child-derived)")
                killed += 1
                continue

            evidence = {
                "kind": "child_stale_kill",
                "reason": "all_child_plays_terminal",
                "killed_at": time.time(),
                "threshold_seconds": threshold_seconds,
            }
            if user_reason:
                evidence["user_reason"] = user_reason
            await _persist_cancel(
                db,
                "show",
                show_id,
                reason_code=RunReasons.CANCELLED_STALE_AUTO,
                reason_summary=(
                    f"Stale auto-cancel: all child plays terminated. {user_reason}".strip()
                ),
                evidence=evidence,
            )
            killed += 1
            print(f"  cancelled stale show {show_id[:12]} (child-derived)")

    prefix = "(dry-run) would cancel" if dry_run else "cancelled"
    print(
        f"\n{prefix} {killed} stale entities "
        f"[skipped_recent={skipped_recent}, skipped_live_pid={skipped_live}, "
        f"skipped_unverifiable_pid={skipped_unverifiable}, "
        f"skipped_linux_start_mismatch={unknown_reasons['start_mismatch']}, "
        f"skipped_linux_boot_disagreement={unknown_reasons['boot_disagreement']}, "
        f"skipped_permission_denied={unknown_reasons['permission_denied']}, "
        f"skipped_identity_unknown={unknown_reasons['identity_unknown']}, "
        f"skipped_unjudgeable={skipped_unjudgeable}, "
        f"skipped_unlinked_plays={skipped_unlinked_plays}]"
    )
    if unverifiable_tracked:
        # Durable evidence, not just this sweep's counter: a row that stays
        # uninspectable forever (e.g. permission denied) never reaches any
        # other outcome, so its unverifiable_since/count are now visible on
        # the row itself rather than only inferred from repeated sweeps.
        warn(
            f"{unverifiable_tracked} running row(s) recorded "
            "unverifiable-pid evidence this sweep (node_metadata.unverifiable_since / "
            "unverifiable_count) — no automatic disposition is applied; inspect "
            "with `li status` and resolve manually."
        )
    if skipped_unlinked_plays:
        # One line per sweep, not one per row: a play created by a live run
        # never records the sessions it started, so this is a property of how
        # plays are written rather than an observation about these rows.
        warn(
            f"{skipped_unlinked_plays} running play row(s) were not swept: "
            "plays created by live runs record no link to the sessions they started, "
            "so their worker state cannot be determined. Sweep the worker session ids instead "
            "(`li monitor` lists them)."
        )
    return 0


def add_kill_subparser(subparsers: argparse._SubParsersAction) -> None:
    kill = subparsers.add_parser(
        "kill",
        help="Terminate a running entity (run/session/play/show).",
        description=(
            "Kill a running lionagi entity by id, or sweep all stale running "
            "entities whose underlying OS process is dead.\n\n"
            "The entity's status is set to 'cancelled' (sessions/invocations), "
            "'blocked' (plays), or 'aborted' (shows) with reason tracking per "
            "ADR-0028.\n\n"
            "Recursion boundary: --recursive kills a session's or invocation's "
            "direct children, which are the PID-bearing workers. A PLAY kill "
            "reaches the play's worker chain only when the row records the "
            "session it started; a play created by a live run does not, so that "
            "kill marks the row terminal and exits non-zero to say no worker was "
            "stopped. A SHOW kill only marks the show row terminal. To stop work "
            "either one cannot reach, kill the session ids directly; --all-stale "
            "marks a play 'blocked' only when the row records a worker session "
            "that has gone terminal, and leaves an unlinked play running.\n\n"
            "Examples:\n"
            "  li kill abc123                        # kill by id prefix\n"
            "  li kill <session-id>                  # stop a worker process\n"
            "  li kill abc123 --reason 'stuck'\n"
            "  li kill abc123 --recursive            # kill + direct children\n"
            "  li kill --all-stale                   # sweep dead-PID rows\n"
            "  li kill --all-stale --threshold 3600  # only rows older than 1h\n"
            "  li kill --all-stale --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    kill.add_argument(
        "id",
        nargs="?",
        help=(
            "Entity id to kill: session_id / invocation_id / play_id / show_id. "
            "Accepts a full UUID, or an id prefix, which is refused when it "
            "matches more than one row rather than resolved to one of them. "
            "A run_id can reach a degraded run when persistence setup failed. It "
            "stops the process only when run.json records a process identity. "
            "`li agent` writes that identity and prints its run id when it starts. "
            "`li state import` rows have no pid; `li kill` uses a matching degraded "
            "run.json when it can identify a process and otherwise refuses. Use "
            "`li monitor` for the session ids a run started."
        ),
    )
    kill.add_argument(
        "--reason",
        default="",
        help="Optional human-readable reason recorded in status_transitions.",
    )
    kill.add_argument(
        "--recursive",
        action="store_true",
        help=(
            "Also kill direct child entities (e.g. invocations spawned by a session). "
            "Has no effect on a play kill, which already reaps whatever worker chain "
            "the row records, or on a show kill, which cannot reach one -- kill the "
            "session ids directly to stop a show's workers."
        ),
    )
    kill.add_argument(
        "--all-stale",
        action="store_true",
        help=(
            "Sweep stale sessions and invocations with dead PIDs older than --threshold. "
            "A play older than --threshold whose recorded worker session has gone "
            "terminal is marked 'blocked'; a play that records no worker session is "
            "left running, because age alone cannot tell it apart from one still "
            "working. A show is marked 'aborted' only once it is older than --threshold "
            "and ALL of its plays are terminal."
        ),
    )
    kill.add_argument(
        "--threshold",
        type=int,
        default=3600,
        help=(
            "Stale threshold in seconds (default 3600 = 1h). Only entities "
            "started more than this many seconds ago are swept."
        ),
    )
    kill.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be killed/cancelled without making any changes.",
    )
    kill.add_argument(
        "--grace",
        type=float,
        default=5.0,
        help="Seconds to wait after SIGTERM before escalating to SIGKILL (default 5).",
    )


@auto_register(area="kill", cli=CliDeclaration(seed="kill", parser_factory=add_kill_subparser))
def run_kill(args: argparse.Namespace) -> int:
    from lionagi.ln.concurrency import run_async

    verbose = getattr(args, "verbose", False)

    if args.all_stale:
        return run_async(
            _do_kill_all_stale(
                threshold_seconds=args.threshold,
                user_reason=args.reason,
                grace_seconds=args.grace,
                dry_run=args.dry_run,
                verbose=verbose,
            )
        )

    if not args.id:
        log_error("specify an entity id or use --all-stale")
        return 1

    if args.dry_run:
        log_error("--dry-run is only meaningful with --all-stale")
        return 1

    return run_async(
        _do_kill(
            args.id,
            user_reason=args.reason,
            recursive=args.recursive,
            grace_seconds=args.grace,
            verbose=verbose,
        )
    )
