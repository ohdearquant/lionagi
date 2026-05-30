# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Universal scheduler engine for lionagi.

Provides :class:`ScheduleItem`, :class:`SchedulerEngine`, :func:`parse_cron`,
and :func:`next_cron_fire` for scheduling recurring and one-shot flows.

Design
------
* In-memory store (``dict[str, ScheduleItem]``) used by default; an optional
  :class:`~lionagi.state.store.StateStore` can be supplied for persistence.
* Thread-safe via :class:`threading.Lock`.
* No external cron libraries — a minimal subset of cron syntax is implemented
  directly (``*/N``, ``*``, specific integers).

State machine for :class:`ScheduleItem`
---------------------------------------

::

    pending → active     (on add, item is set active immediately)
    active  → running    (mark_started)
    running → active     (mark_completed — reschedules if cron/interval)
    running → failed     (mark_failed)
    active  → paused     (pause)
    paused  → active     (resume)
    active  → completed  (max_runs reached after mark_completed)
    *       → cancelled  (remove)

Transition table
----------------

+----------+-----------+-----------+-------------------------------------+
| From     | Verb      | To        | Guard                               |
+==========+===========+===========+=====================================+
| pending  | add       | active    | always (add() sets active)          |
+----------+-----------+-----------+-------------------------------------+
| active   | start     | running   | item is due (next_run_at <= now)    |
+----------+-----------+-----------+-------------------------------------+
| running  | complete  | active    | run_count < max_runs (or unlimited) |
+----------+-----------+-----------+-------------------------------------+
| running  | complete  | completed | run_count >= max_runs               |
+----------+-----------+-----------+-------------------------------------+
| running  | fail      | failed    | terminal — no reschedule            |
+----------+-----------+-----------+-------------------------------------+
| active   | pause     | paused    | must not be running                 |
+----------+-----------+-----------+-------------------------------------+
| paused   | resume    | active    | next_run_at is recomputed           |
+----------+-----------+-----------+-------------------------------------+
| any      | remove    | cancelled | always                              |
+----------+-----------+-----------+-------------------------------------+

Cron expression reference
-------------------------

Supported subset (five fields: minute hour day month weekday)::

    *         any value
    */N       every N units  (e.g. */5 = every 5 minutes)
    V         specific value (e.g. 0, 15, 30)

Examples::

    "*/5 * * * *"    — every 5 minutes
    "0 * * * *"      — top of every hour
    "0 */2 * * *"    — every 2 hours on the hour
    "0 0 * * *"      — midnight every day
    "0 0 * * 1"      — midnight every Monday (weekday 1)
    "30 9 * * 1-5"   — 09:30 Monday–Friday  (range NOT supported; use specific
                        values or */N for that)
    "0 0 1 * *"      — first of every month at midnight
    "0 0 1 1 *"      — 1 January at midnight

Unsupported: ranges (1-5), lists (1,3,5), L/W/# modifiers.  These raise
:exc:`ValueError` at parse time.

Persistence model
-----------------

When a :class:`~lionagi.state.store.StateStore` is provided the engine writes
one row per :class:`ScheduleItem` to the ``scheduled_items`` table (see
``StateStore.execute_insert``).  Column layout::

    item_id TEXT PRIMARY KEY
    name TEXT NOT NULL
    cron_expr TEXT
    interval_seconds REAL
    next_run_at REAL NOT NULL
    last_run_at REAL
    status TEXT NOT NULL
    max_runs INTEGER
    run_count INTEGER NOT NULL DEFAULT 0
    flow_spec JSON NOT NULL DEFAULT '{}'
    created_at REAL NOT NULL
    updated_at REAL NOT NULL

Error handling strategy
-----------------------

* :func:`parse_cron` raises :exc:`ValueError` on any unsupported syntax.
* :func:`next_cron_fire` raises :exc:`ValueError` when it cannot find a next
  fire time within a 4-year search window (handles impossible expressions
  gracefully).
* Engine mutating methods return ``False`` (or raise) on precondition failure
  rather than silently succeeding — callers must check return values.
* The internal :class:`threading.Lock` is always acquired for the minimum
  necessary scope; callers must NOT hold the lock when calling engine methods.

Integration with PlayRunner
---------------------------

:meth:`SchedulerEngine.get_due_items` returns items whose ``next_run_at`` is
at or before ``time.time()``.  The external driver (e.g. Studio lifespan task,
``li schedule daemon``) should poll or sleep until the next due time, then:

1. Call ``engine.get_due_items()`` to collect all due :class:`ScheduleItem`s.
2. For each item call ``engine.mark_started(item.item_id)`` to record the run.
3. Build an argv list from ``item.flow_spec`` (e.g. ``["uv", "run", "li", ...]``).
4. Hand the argv to :class:`~lionagi.runtime.runner.LocalRunner` (or a
   :class:`~lionagi.runtime.runner.PlayRunner` implementation) and await the
   process exit code.
5. On exit code 0 call ``engine.mark_completed(item.item_id)``.
6. On non-zero exit call ``engine.mark_failed(item.item_id, error=...)``.

The engine itself does not spawn processes — that keeps it testable and
backend-agnostic.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from calendar import monthrange
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Public status constants
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}


# ---------------------------------------------------------------------------
# ScheduleItem
# ---------------------------------------------------------------------------


class ScheduleItem(BaseModel):
    """A single scheduled flow definition with lifecycle tracking.

    Parameters
    ----------
    item_id:
        UUID string.  Generated automatically by the engine.
    name:
        Human-readable label for the schedule.
    cron_expr:
        A five-field cron expression string or ``None`` when
        ``interval_seconds`` is used instead.
    interval_seconds:
        Repeat interval in fractional seconds, or ``None`` when
        ``cron_expr`` is used.  Exactly one of the two must be set.
    next_run_at:
        Unix epoch float of the next scheduled execution.  Set by the
        engine using :func:`next_cron_fire` or the current time plus
        ``interval_seconds``.
    last_run_at:
        Unix epoch float of the most recent execution start, or ``None``
        if the item has never run.
    status:
        One of ``"pending"``, ``"active"``, ``"running"``, ``"paused"``,
        ``"completed"``, ``"failed"``, ``"cancelled"``.
    max_runs:
        Maximum number of executions before auto-completing.  ``None``
        means unlimited.
    run_count:
        How many times this item has been executed so far.
    flow_spec:
        Arbitrary dict describing the flow to run (e.g. ``{"flow_type":
        "play", "playbook": "nightly-review"}``).  Interpreted by the
        calling driver, not the engine.
    created_at:
        Unix epoch float of creation time.
    updated_at:
        Unix epoch float of the most recent state change.
    """

    item_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    cron_expr: str | None = None
    interval_seconds: float | None = None
    next_run_at: float
    last_run_at: float | None = None
    status: str = STATUS_ACTIVE
    max_runs: int | None = None
    run_count: int = 0
    flow_spec: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Cron parser
# ---------------------------------------------------------------------------

_CRON_FIELDS = ("minute", "hour", "day", "month", "weekday")
_CRON_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "weekday": (0, 6),  # 0=Sunday, 6=Saturday
}


def parse_cron(expr: str) -> dict[str, Any]:
    """Parse a five-field cron expression into a structured dict.

    Parameters
    ----------
    expr:
        A whitespace-separated five-field cron string.

    Returns
    -------
    dict
        Keys are ``"minute"``, ``"hour"``, ``"day"``, ``"month"``,
        ``"weekday"``.  Each value is one of:

        * ``"*"`` — any value
        * ``{"step": N}`` — every N units (``*/N``)
        * ``int`` — a specific value

    Raises
    ------
    ValueError
        If the expression has fewer or more than five fields, if a field
        contains an unsupported modifier (ranges, lists), or if a specific
        integer is out of the allowed range for its field.

    Examples
    --------
    >>> parse_cron("*/5 * * * *")
    {'minute': {'step': 5}, 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}
    >>> parse_cron("0 */2 * * *")
    {'minute': 0, 'hour': {'step': 2}, 'day': '*', 'month': '*', 'weekday': '*'}
    >>> parse_cron("0 0 * * 1")
    {'minute': 0, 'hour': 0, 'day': '*', 'month': '*', 'weekday': 1}
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron expression must have exactly 5 fields, got {len(parts)}: {expr!r}")

    result: dict[str, Any] = {}
    for field_name, raw in zip(_CRON_FIELDS, parts, strict=False):
        lo, hi = _CRON_RANGES[field_name]

        if raw == "*":
            result[field_name] = "*"
            continue

        if raw.startswith("*/"):
            # Every-N step
            step_str = raw[2:]
            if not step_str.isdigit():
                raise ValueError(f"invalid step in cron field {field_name!r}: {raw!r}")
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"cron step must be >= 1 in field {field_name!r}: {raw!r}")
            if step > (hi - lo + 1):
                raise ValueError(
                    f"cron step {step} exceeds range for field {field_name!r} ({lo}-{hi})"
                )
            result[field_name] = {"step": step}
            continue

        # Reject unsupported modifiers early
        if "-" in raw or "," in raw or "L" in raw or "W" in raw or "#" in raw:
            raise ValueError(
                f"unsupported cron syntax in field {field_name!r}: {raw!r} "
                "(ranges, lists, and L/W/# modifiers are not supported)"
            )

        # Specific integer value
        if not raw.lstrip("-").isdigit():
            raise ValueError(f"invalid cron field {field_name!r} value: {raw!r}")
        val = int(raw)
        if not (lo <= val <= hi):
            raise ValueError(f"cron field {field_name!r} value {val} out of range {lo}-{hi}")
        result[field_name] = val

    return result


# ---------------------------------------------------------------------------
# next_cron_fire
# ---------------------------------------------------------------------------


def next_cron_fire(parsed: dict[str, Any], after: float) -> float:
    """Compute the next fire time for a parsed cron expression.

    Iterates forward in one-minute increments from ``after + 60`` seconds
    (i.e. the fire will be *strictly after* ``after``) until a match is
    found.  Searches up to four years ahead before raising.

    Parameters
    ----------
    parsed:
        The dict returned by :func:`parse_cron`.
    after:
        Unix epoch float.  The next fire time will be strictly after this.

    Returns
    -------
    float
        Unix epoch float of the next matching time (seconds at 0).

    Raises
    ------
    ValueError
        If no match is found within a four-year (2,102,400-minute) window.
    """

    def _matches_field(value: Any, field_name: str, t: int) -> bool:
        if value == "*":
            return True
        if isinstance(value, dict):  # {"step": N}
            step = value["step"]
            lo = _CRON_RANGES[field_name][0]
            return (t - lo) % step == 0
        # specific integer
        return t == value

    # Start from the next whole minute after ``after``
    start_ts = math.ceil(after / 60 + 1) * 60
    # Four years in minutes
    max_minutes = 4 * 365 * 24 * 60 + 1  # includes leap years

    for offset in range(max_minutes):
        candidate_ts = start_ts + offset * 60
        dt = datetime.fromtimestamp(candidate_ts, tz=timezone.utc)
        minute = dt.minute
        hour = dt.hour
        day = dt.day
        month = dt.month
        # Python weekday: Monday=0..Sunday=6 → convert to cron 0=Sun..6=Sat
        py_weekday = dt.weekday()  # 0=Mon
        weekday = (py_weekday + 1) % 7  # 0=Sun, 1=Mon … 6=Sat

        if not _matches_field(parsed["minute"], "minute", minute):
            continue
        if not _matches_field(parsed["hour"], "hour", hour):
            continue
        if not _matches_field(parsed["month"], "month", month):
            continue
        # Validate day against month length (cron skips over invalid days)
        _, max_day = monthrange(dt.year, month)
        if not _matches_field(parsed["day"], "day", day):
            continue
        if day > max_day:
            continue
        if not _matches_field(parsed["weekday"], "weekday", weekday):
            continue

        return float(candidate_ts)

    raise ValueError(f"no cron fire time found within 4 years for expression: {parsed!r}")


# ---------------------------------------------------------------------------
# SchedulerEngine
# ---------------------------------------------------------------------------


class SchedulerEngine:
    """In-memory scheduler engine with optional persistence via StateStore.

    Thread-safe — all public methods acquire ``_lock`` internally.

    Parameters
    ----------
    store:
        An optional :class:`~lionagi.state.store.StateStore` implementation.
        When provided the engine writes updated items back to the store after
        each state mutation.  The store is NOT used for initial population of
        the in-memory dict — callers must hydrate the engine on startup.
    """

    def __init__(self, store: Any | None = None) -> None:
        self._items: dict[str, ScheduleItem] = {}
        self._store = store
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_next_run(self, item: ScheduleItem) -> float | None:
        """Return the next run epoch float based on cron or interval.

        Returns ``None`` when the item has no scheduling expression (one-shot
        items that ran already have no next time).

        For cron items the next fire is computed relative to ``time.time()``.
        For interval items it is ``last_run_at + interval_seconds`` (or
        ``time.time() + interval_seconds`` when the item has never run).
        """
        now = time.time()
        if item.cron_expr is not None:
            try:
                parsed = parse_cron(item.cron_expr)
                return next_cron_fire(parsed, after=now)
            except ValueError:
                return None

        if item.interval_seconds is not None:
            base = item.last_run_at if item.last_run_at is not None else now
            return base + item.interval_seconds

        return None

    def _touch(self, item: ScheduleItem) -> None:
        """Update ``updated_at`` in place (lock must already be held)."""
        item.updated_at = time.time()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        name: str,
        flow_spec: dict[str, Any],
        *,
        cron_expr: str | None = None,
        interval_seconds: float | None = None,
        max_runs: int | None = None,
    ) -> ScheduleItem:
        """Create and register a new :class:`ScheduleItem`.

        Exactly one of *cron_expr* or *interval_seconds* should be provided.
        When neither is provided the item is treated as a one-shot scheduled
        for immediate execution (``next_run_at = now``).

        Parameters
        ----------
        name:
            Human-readable label.
        flow_spec:
            Arbitrary dict passed through to the driver for execution.
        cron_expr:
            Five-field cron string.  Parsed immediately; raises
            :exc:`ValueError` on invalid syntax.
        interval_seconds:
            Positive float interval in seconds.
        max_runs:
            Auto-complete after this many successful runs.  ``None`` = unlimited.

        Returns
        -------
        ScheduleItem
            The newly created item (``status="active"``).

        Raises
        ------
        ValueError
            If *cron_expr* is provided and fails to parse.
        ValueError
            If both *cron_expr* and *interval_seconds* are provided.
        """
        if cron_expr is not None and interval_seconds is not None:
            raise ValueError("specify at most one of cron_expr or interval_seconds, not both")
        if cron_expr is not None:
            # Validate eagerly so we fail at add time, not at run time.
            parse_cron(cron_expr)

        now = time.time()
        # Compute first fire time
        if cron_expr is not None:
            parsed = parse_cron(cron_expr)
            next_run = next_cron_fire(parsed, after=now)
        elif interval_seconds is not None:
            if interval_seconds <= 0:
                raise ValueError("interval_seconds must be positive")
            next_run = now + interval_seconds
        else:
            # One-shot: fire as soon as possible
            next_run = now

        item = ScheduleItem(
            name=name,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            next_run_at=next_run,
            status=STATUS_ACTIVE,
            max_runs=max_runs,
            flow_spec=dict(flow_spec),
            created_at=now,
            updated_at=now,
        )

        with self._lock:
            self._items[item.item_id] = item

        return item

    def remove(self, item_id: str) -> bool:
        """Remove an item from the engine regardless of current status.

        Returns ``True`` if the item existed and was removed, ``False``
        if no item with *item_id* was found.
        """
        with self._lock:
            if item_id not in self._items:
                return False
            del self._items[item_id]
        return True

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def pause(self, item_id: str) -> bool:
        """Transition an active item to ``"paused"``.

        Returns ``True`` on success.  Returns ``False`` if the item is not
        found or is not in ``"active"`` status (e.g. already running,
        paused, or terminal).
        """
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            if item.status != STATUS_ACTIVE:
                return False
            item.status = STATUS_PAUSED
            self._touch(item)
        return True

    def resume(self, item_id: str) -> bool:
        """Transition a paused item back to ``"active"``.

        Recomputes ``next_run_at`` relative to the current time so that a
        recently resumed interval item does not fire immediately unless it
        was already due before pausing.

        Returns ``True`` on success, ``False`` if the item is not found or
        is not ``"paused"``.
        """
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            if item.status != STATUS_PAUSED:
                return False
            item.status = STATUS_ACTIVE
            # Recompute next_run_at so the item is not immediately due
            next_run = self._compute_next_run(item)
            if next_run is not None:
                item.next_run_at = next_run
            self._touch(item)
        return True

    def mark_started(self, item_id: str) -> bool:
        """Record that execution of an active item has begun.

        Transitions ``active`` → ``running`` and sets ``last_run_at`` to now.

        Returns ``True`` on success.  Returns ``False`` when the item is not
        found or is not in ``"active"`` status.
        """
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            if item.status != STATUS_ACTIVE:
                return False
            now = time.time()
            item.status = STATUS_RUNNING
            item.last_run_at = now
            item.updated_at = now
        return True

    def mark_completed(self, item_id: str) -> bool:
        """Record successful completion of a running item.

        Increments ``run_count`` and either:

        * Transitions to ``"completed"`` when ``max_runs`` has been reached.
        * Transitions back to ``"active"`` and schedules the next run when
          there are remaining runs (or runs are unlimited).

        Returns ``True`` on success.  Returns ``False`` when the item is not
        found or is not in ``"running"`` status.
        """
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            if item.status != STATUS_RUNNING:
                return False

            item.run_count += 1
            now = time.time()
            item.updated_at = now

            if item.max_runs is not None and item.run_count >= item.max_runs:
                item.status = STATUS_COMPLETED
            else:
                next_run = self._compute_next_run(item)
                if next_run is None:
                    # One-shot item (no cron_expr and no interval_seconds):
                    # there is no future fire time, so complete it now rather
                    # than leaving it active and letting it re-fire immediately.
                    item.status = STATUS_COMPLETED
                else:
                    item.next_run_at = next_run
                    item.status = STATUS_ACTIVE
        return True

    def mark_failed(self, item_id: str, error: str = "") -> bool:
        """Record a failed run.

        Transitions ``running`` → ``"failed"`` (terminal).

        Parameters
        ----------
        item_id:
            Item identifier.
        error:
            Optional error description stored in ``flow_spec["_last_error"]``
            for diagnostic inspection.

        Returns ``True`` on success.  Returns ``False`` when the item is not
        found or is not in ``"running"`` status.
        """
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            if item.status != STATUS_RUNNING:
                return False
            item.status = STATUS_FAILED
            item.run_count += 1
            if error:
                item.flow_spec["_last_error"] = error
            self._touch(item)
        return True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_items(self, status: str | None = None) -> list[ScheduleItem]:
        """Return all items, optionally filtered by *status*.

        The returned list is a snapshot; mutations to returned items do not
        affect the engine's internal state.

        Parameters
        ----------
        status:
            When ``None`` all items are returned.  Otherwise only items whose
            ``status`` field equals *status* are included.

        Returns
        -------
        list[ScheduleItem]
            Copies of the matching items.
        """
        with self._lock:
            items = list(self._items.values())

        if status is not None:
            items = [it for it in items if it.status == status]

        return [it.model_copy() for it in items]

    def get_due_items(self) -> list[ScheduleItem]:
        """Return active items whose ``next_run_at`` is at or before now.

        Only items with ``status == "active"`` are considered.

        Returns
        -------
        list[ScheduleItem]
            Copies of all due items, sorted by ``next_run_at`` ascending.
        """
        now = time.time()
        with self._lock:
            due = [
                it.model_copy()
                for it in self._items.values()
                if it.status == STATUS_ACTIVE and it.next_run_at <= now
            ]
        due.sort(key=lambda it: it.next_run_at)
        return due

    def get_item(self, item_id: str) -> ScheduleItem | None:
        """Return a copy of the item with *item_id*, or ``None``."""
        with self._lock:
            item = self._items.get(item_id)
            return item.model_copy() if item is not None else None

    def find_by_prefix(self, prefix: str) -> list[str]:
        """Return item IDs that start with *prefix*.

        Parameters
        ----------
        prefix:
            ID prefix to match (at least 4 characters recommended).

        Returns
        -------
        list[str]
            List of matching item IDs (may be empty, one, or many).
        """
        with self._lock:
            return [iid for iid in self._items if iid.startswith(prefix)]


__all__ = [
    "ScheduleItem",
    "SchedulerEngine",
    "STATUS_ACTIVE",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PAUSED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "next_cron_fire",
    "parse_cron",
]
