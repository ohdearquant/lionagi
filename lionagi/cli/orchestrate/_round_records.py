# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable per-leg and per-round records for a manifest round.

``{run_dir}/legs/{label}.json`` records what one leg was dispatched with and
how it ended; ``{run_dir}/round.json`` records the round summary
(``pending_harvest`` at spawn, flipped to ``complete`` as finalization's last
act). Every write is temp-file-plus-atomic-rename, and first write wins for a
COMPLETE leg record. See ``docs/internals/cli.md`` (`_round_records.py` /
`_quiescence.py`) for why the dispatch half is written before the leg runs
and what a reaper reconstructs from these files alone.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = (
    "ROUND_VERSION",
    "ROUND_STATE_PENDING",
    "ROUND_STATE_COMPLETE",
    "RESULT_COMPLETED",
    "RESULT_PARTIAL",
    "RESULT_FAILED",
    "LEG_SUCCEEDED",
    "LEG_FAILED",
    "LEG_TIMED_OUT",
    "LEG_CANCELLED",
    "LEG_KILLED",
    "RECORDED_BY_RUNNER",
    "RECORDED_BY_KILL_REAPER",
    "RECORDED_BY_ORPHAN_REAPER",
    "LegDispatch",
    "legs_dir",
    "round_path",
    "write_leg_dispatch",
    "complete_leg_record",
    "read_leg_records",
    "ControlGroupDomain",
    "control_group_domain",
    "recorded_control_groups",
    "write_round_summary",
    "read_round_summary",
    "flip_round_complete",
    "round_result",
)

ROUND_VERSION = 1

ROUND_STATE_PENDING = "pending_harvest"
ROUND_STATE_COMPLETE = "complete"

RESULT_COMPLETED = "completed"
RESULT_PARTIAL = "partial"
RESULT_FAILED = "failed"

# Every leg ends in exactly one of these, orthogonally to its harvest state.
LEG_SUCCEEDED = "succeeded"
LEG_FAILED = "failed"
LEG_TIMED_OUT = "timed_out"
LEG_CANCELLED = "cancelled"
LEG_KILLED = "killed"

RECORDED_BY_RUNNER = "runner"
RECORDED_BY_KILL_REAPER = "kill-reaper"
RECORDED_BY_ORPHAN_REAPER = "orphan-reaper"


@dataclass(frozen=True)
class LegDispatch:
    """The facts that exist before a leg can produce anything.

    ``pgid`` (the leg's process group) and ``pid_create_time`` (when the
    leading process started) are both read at spawn, before the leg runs —
    a group id read later can belong to a different process the kernel has
    since reissued it to. A record with ``pid_create_time is None`` carries
    no identity; a sweep must refuse to signal on it.
    """

    label: str
    cwd: str
    model: str | None
    env_keys: tuple[str, ...]
    brief_hash: str
    started_at: str
    pid: int | None
    pgid: int | None
    pid_create_time: float | None


def legs_dir(run_dir: Path | str) -> Path:
    return Path(run_dir) / "legs"


def round_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / "round.json"


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON so a reader sees either the old file or the whole new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    """Return the parsed object, or None for absent, unreadable, or not-an-object.

    A damaged record is not an empty one, but the distinction belongs to the
    caller's own reporting: everything here treats "cannot read it" as "have
    no facts from it" and never as "the leg produced nothing".
    """
    try:
        with path.open() as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_leg_dispatch(run_dir: Path | str, dispatch: LegDispatch) -> Path:
    """Write a leg's first record, at spawn. Values are never re-printed later."""
    path = legs_dir(run_dir) / f"{dispatch.label}.json"
    _write_atomic(
        path,
        {
            "label": dispatch.label,
            "cwd": dispatch.cwd,
            "model": dispatch.model,
            # Names only. The manifest snapshot is the durable source for the
            # values, and a record any caller can read back is not.
            "env_keys": list(dispatch.env_keys),
            "brief_hash": dispatch.brief_hash,
            "started_at": dispatch.started_at,
            "pid": dispatch.pid,
            "pgid": dispatch.pgid,
            "pid_create_time": dispatch.pid_create_time,
            "status": None,
            "finished_at": None,
            "harvest_state": None,
            "harvest_detail": None,
            "artifacts": None,
            "recorded_by": None,
        },
    )
    return path


def complete_leg_record(
    run_dir: Path | str,
    label: str,
    *,
    status: str,
    finished_at: str,
    harvest_state: str,
    harvest_detail: dict[str, Any] | None,
    artifacts: list[str],
    recorded_by: str,
) -> bool:
    """Fill in how a leg ended. Returns False when a complete record already exists.

    First write wins. Two finalizers cannot both be live (the claim is
    exclusive), but a claim can change hands between a dead holder and its
    successor, and the successor must not overwrite what the dead one had
    already established.
    """
    path = legs_dir(run_dir) / f"{label}.json"
    existing = _read_json(path) or {}
    if existing.get("status") is not None:
        return False

    existing.update(
        {
            "label": label,
            "status": status,
            "finished_at": finished_at,
            "harvest_state": harvest_state,
            "harvest_detail": harvest_detail,
            # Never an empty list standing in for "could not establish": a
            # harvest that failed says so in harvest_state, and its artifact
            # list is what it did manage to copy.
            "artifacts": list(artifacts),
            "recorded_by": recorded_by,
        }
    )
    _write_atomic(path, existing)
    return True


def read_leg_records(run_dir: Path | str) -> dict[str, dict[str, Any] | None]:
    """Every leg record on disk, by label. A value of None is an unreadable one.

    One unreadable record must not poison the others or masquerade as a leg
    that was never dispatched, which is why the key survives with a null value
    rather than being dropped.
    """
    directory = legs_dir(run_dir)
    records: dict[str, dict[str, Any] | None] = {}
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return records
    for entry in entries:
        if entry.suffix != ".json" or not entry.is_file():
            continue
        records[entry.stem] = _read_json(entry)
    return records


@dataclass(frozen=True)
class ControlGroupDomain:
    """The groups a sweep may observe, and what it was not given.

    ``groups`` is what can be swept. ``unpinned`` and ``unreadable`` count the
    leg records that contributed nothing, and they exist because a sweep over a
    domain nobody joined looks exactly like a clean sweep: three quiet groups
    out of three recorded legs and three quiet groups out of nine are the same
    sentence unless the shortfall is carried alongside. A caller that publishes
    on the strength of a sweep must read them.
    """

    groups: tuple[int, ...]
    unpinned: int
    unreadable: int
    records: int

    @property
    def complete(self) -> bool:
        """Whether every recorded leg contributed a sweepable group."""
        return not self.unpinned and not self.unreadable


def control_group_domain(run_dir: Path | str) -> ControlGroupDomain:
    """Process groups a quiescence sweep must observe empty, read from disk.

    Read from the run directory, never a live process's memory. A group is
    only included when its record also carries the start time of the process
    that led it — otherwise the group id may name whatever the kernel has
    since reissued it to, and observing it either empty or not would report a
    false result. Such a record is counted as ``unpinned`` rather than
    silently dropped, so a sweep never pretends a bare number is a domain.
    """
    groups: list[int] = []
    unpinned = 0
    unreadable = 0
    records = read_leg_records(run_dir)
    for record in records.values():
        if not record:
            unreadable += 1
            continue
        pgid = record.get("pgid")
        created = record.get("pid_create_time")
        if not isinstance(created, int | float) or not isinstance(pgid, int) or pgid <= 0:
            unpinned += 1
            continue
        if pgid not in groups:
            groups.append(pgid)
    return ControlGroupDomain(
        groups=tuple(groups),
        unpinned=unpinned,
        unreadable=unreadable,
        records=len(records),
    )


def recorded_control_groups(run_dir: Path | str) -> list[int]:
    """The sweepable groups alone. See :func:`control_group_domain` for what a
    caller publishing on the strength of a sweep also has to read."""
    return list(control_group_domain(run_dir).groups)


def write_round_summary(
    run_dir: Path | str,
    *,
    labels: list[str],
    round_state: str = ROUND_STATE_PENDING,
    result: str | None = None,
    legs_succeeded: int = 0,
) -> Path:
    path = round_path(run_dir)
    _write_atomic(
        path,
        {
            "round_version": ROUND_VERSION,
            "round_state": round_state,
            "result": result,
            "legs_total": len(labels),
            "legs_succeeded": legs_succeeded,
            "legs": list(labels),
        },
    )
    return path


def read_round_summary(run_dir: Path | str) -> dict[str, Any] | None:
    return _read_json(round_path(run_dir))


def flip_round_complete(run_dir: Path | str, *, result: str, legs_succeeded: int) -> bool:
    """Publish the round as complete. Returns False if there is no summary to flip.

    The caller owes the ordering this cannot enforce: `complete` is published
    only after a quiescence sweep has observed every recorded group empty and
    every leg's harvest and record has landed.
    """
    summary = read_round_summary(run_dir)
    if summary is None:
        return False
    summary.update(
        {
            "round_state": ROUND_STATE_COMPLETE,
            "result": result,
            "legs_succeeded": legs_succeeded,
        }
    )
    _write_atomic(round_path(run_dir), summary)
    return True


def round_result(statuses: list[str], harvest_failed: list[bool]) -> str:
    """The round's result from its legs' terminal and harvest states.

    Total by construction: the three rules below cover every combination, so
    no mixed round is undecided. `dir-empty` and `dir-absent` never degrade a
    result by themselves — a leg whose whole answer is its final message
    legitimately writes no artifact — while `harvest_failed` always degrades
    below `completed`, because artifacts were, or may have been, written and
    cannot be served.
    """
    succeeded = [s == LEG_SUCCEEDED for s in statuses]
    if all(succeeded) and statuses and not any(harvest_failed):
        return RESULT_COMPLETED
    if any(succeeded):
        return RESULT_PARTIAL
    return RESULT_FAILED
