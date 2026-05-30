# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li schedule` — manage scheduled flow definitions.

The schedule command provides sub-commands to add, list, remove, pause, and
resume scheduled items backed by the in-process :class:`SchedulerEngine`.

Examples
--------
::

    # Add a cron schedule — every night at 02:00
    li schedule add nightly-review --cron "0 2 * * *" --flow-type play --playbook nightly-review

    # Add an interval schedule — every 30 minutes
    li schedule add heartbeat --interval 1800 --flow-type agent --prompt "check health"

    # One-shot item (fires once immediately, max-runs defaults to 1)
    li schedule add deploy-once --flow-type shell --argv "uv,run,li,state,prune" --max-runs 1

    # List all schedules
    li schedule list

    # List only active schedules
    li schedule list --status active

    # Pause a schedule
    li schedule pause <item-id>

    # Resume a paused schedule
    li schedule resume <item-id>

    # Remove a schedule
    li schedule remove <item-id>

Notes
-----
The ``li schedule`` command operates on the *current process's*
:class:`SchedulerEngine` instance.  For durable, cross-process scheduling
(including Studio integration) the engine should be backed by a
:class:`~lionagi.state.store.StateStore` and embedded in the Studio lifespan
or a long-running daemon.  The CLI surface here is intentionally thin — it
validates inputs and delegates all state mutations to the engine.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from ._logging import log_error

# ---------------------------------------------------------------------------
# Module-level default engine (used by the CLI)
# ---------------------------------------------------------------------------

# A single shared SchedulerEngine instance for the CLI process.
# Tests and external callers may replace this reference or instantiate their
# own engine independently.
_DEFAULT_ENGINE: Any = None


def _get_engine() -> Any:
    """Return the default :class:`SchedulerEngine`, creating it on first use."""
    global _DEFAULT_ENGINE  # noqa: PLW0603
    if _DEFAULT_ENGINE is None:
        from lionagi.runtime.scheduler import SchedulerEngine

        _DEFAULT_ENGINE = SchedulerEngine()
    return _DEFAULT_ENGINE


def set_engine(engine: Any) -> None:
    """Replace the default engine (e.g. in tests or daemon setup)."""
    global _DEFAULT_ENGINE  # noqa: PLW0603
    _DEFAULT_ENGINE = engine


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_STATUS_MARKER = {
    "active": "[active]",
    "running": "[running]",
    "paused": "[paused]",
    "completed": "[done]",
    "failed": "[FAILED]",
    "cancelled": "[cancelled]",
    "pending": "[pending]",
}


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _print_item(item: Any) -> None:
    """Print a single :class:`ScheduleItem` in human-readable form."""
    marker = _STATUS_MARKER.get(item.status, f"[{item.status}]")
    trigger = (
        f"cron:{item.cron_expr}"
        if item.cron_expr
        else f"interval:{item.interval_seconds}s"
        if item.interval_seconds
        else "one-shot"
    )
    runs = f"{item.run_count}/{item.max_runs}" if item.max_runs is not None else str(item.run_count)
    print(
        f"{marker:<12} {item.item_id[:16]}  {item.name:<24}  "
        f"{trigger:<30}  runs:{runs:<6}  "
        f"next:{_fmt_ts(item.next_run_at)}"
    )


# ---------------------------------------------------------------------------
# Sub-command implementations
# ---------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    """Add a new scheduled item."""
    from lionagi.runtime.scheduler import parse_cron

    engine = _get_engine()

    if args.cron and args.interval:
        log_error("specify at most one of --cron and --interval")
        return 1

    if args.cron:
        # Validate before handing to engine
        try:
            parse_cron(args.cron)
        except ValueError as exc:
            log_error(f"invalid cron expression: {exc}")
            return 1

    interval: float | None = None
    if args.interval is not None:
        if args.interval <= 0:
            log_error("--interval must be a positive number of seconds")
            return 1
        interval = float(args.interval)

    # Build flow_spec from remaining flags
    flow_spec: dict[str, Any] = {}
    if args.flow_type:
        flow_spec["flow_type"] = args.flow_type
    if args.playbook:
        flow_spec["playbook"] = args.playbook
    if args.prompt:
        flow_spec["prompt"] = args.prompt
    if args.argv:
        # Accept comma-separated argv or repeated --argv flags
        flow_spec["argv"] = args.argv

    # Allow raw JSON spec override
    if args.spec:
        try:
            extra = json.loads(args.spec)
        except json.JSONDecodeError as exc:
            log_error(f"--spec is not valid JSON: {exc}")
            return 1
        if not isinstance(extra, dict):
            log_error("--spec must be a JSON object (dict)")
            return 1
        flow_spec.update(extra)

    max_runs: int | None = args.max_runs
    if max_runs is not None and max_runs < 1:
        log_error("--max-runs must be >= 1")
        return 1

    try:
        item = engine.add(
            name=args.name,
            flow_spec=flow_spec,
            cron_expr=args.cron or None,
            interval_seconds=interval,
            max_runs=max_runs,
        )
    except ValueError as exc:
        log_error(str(exc))
        return 1

    print(f"scheduled: {item.item_id}")
    _print_item(item)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """List scheduled items, optionally filtered by status."""
    engine = _get_engine()
    status_filter: str | None = args.status
    items = engine.list_items(status=status_filter)

    if not items:
        msg = "(no scheduled items)"
        if status_filter:
            msg = f"(no scheduled items with status={status_filter!r})"
        print(msg)
        return 0

    for item in sorted(items, key=lambda it: it.next_run_at):
        _print_item(item)
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    """Remove a scheduled item by id (prefix allowed)."""
    engine = _get_engine()
    item_id = _resolve_id(engine, args.item_id)
    if item_id is None:
        log_error(f"no scheduled item found for id: {args.item_id!r}")
        return 1

    removed = engine.remove(item_id)
    if removed:
        print(f"removed: {item_id[:16]}")
        return 0
    log_error(f"item {item_id[:16]!r} not found")
    return 1


def _cmd_pause(args: argparse.Namespace) -> int:
    """Pause an active scheduled item."""
    engine = _get_engine()
    item_id = _resolve_id(engine, args.item_id)
    if item_id is None:
        log_error(f"no scheduled item found for id: {args.item_id!r}")
        return 1

    ok = engine.pause(item_id)
    if ok:
        print(f"paused: {item_id[:16]}")
        return 0
    # Give a more specific error if possible
    item = engine.get_item(item_id)
    if item is None:
        log_error(f"item {item_id[:16]!r} not found")
    else:
        log_error(
            f"cannot pause item {item_id[:16]!r} with status={item.status!r} "
            "(only active items can be paused)"
        )
    return 1


def _cmd_resume(args: argparse.Namespace) -> int:
    """Resume a paused scheduled item."""
    engine = _get_engine()
    item_id = _resolve_id(engine, args.item_id)
    if item_id is None:
        log_error(f"no scheduled item found for id: {args.item_id!r}")
        return 1

    ok = engine.resume(item_id)
    if ok:
        print(f"resumed: {item_id[:16]}")
        return 0
    item = engine.get_item(item_id)
    if item is None:
        log_error(f"item {item_id[:16]!r} not found")
    else:
        log_error(
            f"cannot resume item {item_id[:16]!r} with status={item.status!r} "
            "(only paused items can be resumed)"
        )
    return 1


# ---------------------------------------------------------------------------
# ID resolution helper (support short id prefixes)
# ---------------------------------------------------------------------------


def _resolve_id(engine: Any, id_or_prefix: str) -> str | None:
    """Resolve a full UUID or a unique prefix to a canonical item_id.

    Uses the public :meth:`~lionagi.work.engine.WorkEngine.get_item` and
    :meth:`~lionagi.work.engine.WorkEngine.find_by_prefix` methods instead
    of accessing private engine internals.
    """
    # Exact match first via the public get_item accessor.
    if engine.get_item(id_or_prefix) is not None:
        return id_or_prefix

    # Prefix match (at least 4 characters for safety) via find_by_prefix.
    if len(id_or_prefix) >= 4:
        matches = engine.find_by_prefix(id_or_prefix)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            log_error(
                f"ambiguous prefix {id_or_prefix!r} matches {len(matches)} items; "
                "use a longer prefix or the full id"
            )
            return None

    return None


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def add_schedule_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Register ``li schedule`` and its sub-commands with argparse."""
    sched = subparsers.add_parser(
        "schedule",
        help="Manage scheduled flow definitions.",
        description=(
            "Add, list, pause, resume, and remove scheduled flows.\n\n"
            "Examples:\n"
            "  li schedule add nightly --cron '0 2 * * *' --flow-type play "
            "--playbook nightly-review\n"
            "  li schedule add pulse --interval 1800 --flow-type agent "
            "--prompt 'health check'\n"
            "  li schedule list\n"
            "  li schedule list --status active\n"
            "  li schedule pause <id>\n"
            "  li schedule resume <id>\n"
            "  li schedule remove <id>\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sched_sub = sched.add_subparsers(dest="schedule_cmd", required=True)

    # -- add ----------------------------------------------------------------
    add_p = sched_sub.add_parser(
        "add",
        help="Add a new scheduled item.",
        description="Create and register a new scheduled flow.",
    )
    add_p.add_argument("name", help="Human-readable name for this schedule.")
    add_p.add_argument(
        "--cron",
        default=None,
        metavar="EXPR",
        help=(
            "Five-field cron expression (e.g. '0 2 * * *' for 02:00 daily). "
            "Mutually exclusive with --interval."
        ),
    )
    add_p.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Repeat interval in seconds (e.g. 1800 for every 30 minutes). "
            "Mutually exclusive with --cron."
        ),
    )
    add_p.add_argument(
        "--flow-type",
        default=None,
        dest="flow_type",
        choices=["agent", "fanout", "flow", "play", "team", "shell", "webhook", "chain"],
        help="Type of flow to execute.",
    )
    add_p.add_argument(
        "--playbook",
        default=None,
        help="Playbook name (for --flow-type play).",
    )
    add_p.add_argument(
        "--prompt",
        default=None,
        help="Prompt text (for --flow-type agent).",
    )
    add_p.add_argument(
        "--argv",
        nargs="+",
        default=None,
        metavar="ARG",
        help="Command argv list (for --flow-type shell or custom execution).",
    )
    add_p.add_argument(
        "--spec",
        default=None,
        metavar="JSON",
        help="Raw JSON object merged into flow_spec (overrides individual flags).",
    )
    add_p.add_argument(
        "--max-runs",
        type=int,
        default=None,
        dest="max_runs",
        metavar="N",
        help="Auto-complete after N successful runs. Omit for unlimited.",
    )

    # -- list ---------------------------------------------------------------
    list_p = sched_sub.add_parser("list", help="List scheduled items.")
    list_p.add_argument(
        "--status",
        default=None,
        choices=["pending", "active", "running", "paused", "completed", "failed", "cancelled"],
        help="Filter by status (default: all).",
    )

    # -- remove -------------------------------------------------------------
    rm_p = sched_sub.add_parser("remove", help="Remove a scheduled item.")
    rm_p.add_argument("item_id", help="Item id or unique prefix.")

    # -- pause --------------------------------------------------------------
    pause_p = sched_sub.add_parser("pause", help="Pause an active scheduled item.")
    pause_p.add_argument("item_id", help="Item id or unique prefix.")

    # -- resume -------------------------------------------------------------
    resume_p = sched_sub.add_parser("resume", help="Resume a paused scheduled item.")
    resume_p.add_argument("item_id", help="Item id or unique prefix.")


def run_schedule(args: argparse.Namespace) -> int:
    """Dispatch ``li schedule`` sub-command."""
    cmd = args.schedule_cmd
    if cmd == "add":
        return _cmd_add(args)
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "remove":
        return _cmd_remove(args)
    if cmd == "pause":
        return _cmd_pause(args)
    if cmd == "resume":
        return _cmd_resume(args)

    log_error(f"unknown schedule sub-command: {cmd!r}")
    return 1


__all__ = [
    "add_schedule_subcommand",
    "run_schedule",
    "set_engine",
]
