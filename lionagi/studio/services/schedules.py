# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0070 schedules service — backs /api/schedules endpoints."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from lionagi._flow_spec import normalize_flow_spec_keys, validate_flow_spec_fields
from lionagi.state.db import StateDB, read_only_open_supported, state_db_known_absent

from ..registry import studio_route
from . import run_view

_log = logging.getLogger(__name__)


class NameConflictError(Exception):
    """Raised when a schedule name already exists."""


def _request_lifecycle_identity(request: Request | None) -> tuple[str, str | None, bool]:
    """Resolve the same lightweight request identity used by Studio writes.

    The actor header already identifies attention-disposition deletes. Schedule
    lifecycle writes reuse it, and additionally retain the submitting process's
    resolved cwd when the CLI provides one. Both values are bounded and control
    characters are rejected before they reach the durable audit ledger.

    The third member says whether the actor came off the request. Studio
    authenticates with one shared bearer token, or with none at all, so there
    is no per-caller principal to bind this header to and any authorized caller
    can send any name. Rather than let the ledger present that name the way it
    presents a verified one, the claim is recorded as a claim: a reader can
    then tell "this process said it was X" from "X is who it was", which is the
    distinction an audit trail exists to preserve.
    """

    def _safe_header(name: str, *, limit: int) -> str | None:
        value = (request.headers.get(name) or "").strip() if request is not None else ""
        if not value or len(value) > limit or any(ord(char) < 32 for char in value):
            return None
        return value

    claimed = _safe_header("x-lionagi-actor", limit=256)
    return claimed or "operator", _safe_header("x-lionagi-cwd", limit=4096), claimed is not None


def _lifecycle_metadata(
    request_cwd: str | None, *, actor_self_reported: bool = False
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if request_cwd is not None:
        metadata["request_cwd"] = request_cwd
    if actor_self_reported:
        # Present on exactly the records whose actor came from the request and
        # was never verified against anything.
        metadata["actor_attribution"] = "self-reported"
    return metadata


# schedules columns declared NOT NULL — a PATCH that explicitly sets one of
# these to null must be rejected (400) rather than passed through to a DB
# constraint violation (or, worse, silently dropped).
_NON_NULLABLE_SCHEDULE_FIELDS: frozenset[str] = frozenset(
    {"name", "trigger_type", "action_kind", "missed_fire_policy", "overlap_policy"}
)


def _svc_validate_action_model(model: str | None) -> None:
    """Service-boundary check: reject action_model values that inject CLI flags."""
    if not model:
        return
    from lionagi.studio.scheduler.subprocess import _validate_action_model

    _validate_action_model(model)


def _svc_validate_identifier(value: str | None, field_name: str) -> None:
    """Service-boundary check: reject identifier fields (agent/project/playbook)
    starting with '-' — a leading '-' would make argparse treat it as a flag."""
    if not value:
        return
    from lionagi.studio.scheduler.subprocess import _validate_identifier

    _validate_identifier(value, field_name)


_GITHUB_CURSOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _svc_validate_github_cursor(cursor: str | None) -> None:
    """Service-boundary check: a github_cursor must be a UTC ISO-8601 instant
    spelled exactly as GitHub spells it, ``YYYY-MM-DDTHH:MM:SSZ``.

    The poller compares cursors as STRINGS against the API's own timestamps, so
    the format is a correctness contract rather than a presentation choice: a
    space separator, a fractional part, or a ``+00:00`` offset all denote the
    right instant and all order wrongly against ``2026-07-20T15:21:57Z``, which
    silently makes the poller skip or replay events.

    ``None`` is allowed and clears the cursor, meaning "no bookmark". That is a
    legitimate operator action and a consequential one -- an unbookmarked
    merged-mode poll dispatches everything its scan reaches.
    """
    if cursor is None:
        return
    if not isinstance(cursor, str) or not _GITHUB_CURSOR_RE.match(cursor):
        raise ValueError(
            "github_cursor must be a UTC ISO-8601 timestamp of the form "
            f"YYYY-MM-DDTHH:MM:SSZ (got {cursor!r})"
        )
    try:
        datetime.strptime(cursor, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"github_cursor is not a real timestamp: {cursor!r}") from exc


def _svc_validate_action_cwd(cwd: str | None) -> None:
    """Service-boundary check: an explicit action_cwd (ADR-0070 delta 1's persisted
    execution root) must be an existing absolute directory.

    ``None`` means "no execution root configured" and is allowed. A supplied
    but empty/whitespace value is rejected rather than persisted: it is neither
    a usable directory nor a clear, and the scheduler now fails closed on any
    non-``None`` root it cannot resolve, so an empty root would only ever
    surface later as a refused run.

    The two conditions below are exactly
    ``lionagi.studio.scheduler.engine._is_usable_execution_root``, spelled out
    rather than called. That is deliberate and is the one intended exception to
    routing every usability decision through that predicate: this is an input
    validator whose product is the error message, and it tells a caller which
    of the two rules they broke, where the predicate can only say "no". Keep
    the two in step -- if the predicate gains a condition, this gains a
    matching branch."""
    if cwd is None:
        return
    p = Path(cwd)
    if not p.is_absolute():
        raise ValueError(f"action_cwd must be an absolute path, got {cwd!r}")
    if not p.is_dir():
        raise ValueError(f"action_cwd does not exist or is not a directory: {cwd!r}")


def _svc_validate_extra_args(extra: list | None) -> None:
    """Service-boundary check: reject action_extra_args elements that inject CLI flags."""
    if not extra:
        return
    from lionagi.studio.scheduler.subprocess import _validate_extra_args

    _validate_extra_args(extra)


def _svc_validate_action_command(command: str | None) -> None:
    """Service-boundary check: reject an action_command that is unsafe or not
    allow-listed. ``build_argv`` re-checks the allow-list again at spawn time
    since ``LIONAGI_SCHEDULER_COMMAND_ALLOWLIST`` can change between create and fire.
    """
    if not command:
        return
    from lionagi.studio.scheduler.subprocess import (
        _validate_action_command,
        _validate_command_allowlisted,
    )

    _validate_action_command(command)
    _validate_command_allowlisted(command)


def _svc_validate_command_args(args: list | None) -> None:
    """Service-boundary check: action_command_args must be a list. Elements are
    ``{{var}}`` templates rendered against trigger_context at fire time, not here."""
    if args is None:
        return
    if not isinstance(args, list):
        raise ValueError(f"action_command_args must be a list of strings, got {args!r}")


def _svc_validate_cron_expr(expr: str | None, *, required: bool = False) -> None:
    """Service-boundary check: reject a syntactically invalid cron expression.
    `required=True` also rejects a missing/empty one — otherwise the schedule
    commits fine but never fires (next_fire_at stays None forever)."""
    if not expr:
        if required:
            raise ValueError("cron_expr is required when trigger_type is 'cron'")
        return
    from croniter import croniter

    if not croniter.is_valid(expr):
        raise ValueError(f"Invalid cron expression: {expr!r}")


def _svc_validate_max_runs(max_runs: Any) -> None:
    """Service-boundary check: reject a non-positive max_runs. None (unlimited) is always accepted."""
    if max_runs is None:
        return
    if isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs < 1:
        raise ValueError(f"max_runs must be a positive integer, got {max_runs!r}")


def _svc_validate_budget_usd(budget_usd: Any) -> None:
    """Service-boundary check: reject a non-positive budget_usd. None (unlimited) is always accepted."""
    if budget_usd is None:
        return
    if (
        isinstance(budget_usd, bool)
        or not isinstance(budget_usd, int | float)
        or not math.isfinite(budget_usd)
        or budget_usd <= 0
    ):
        raise ValueError(f"budget_usd must be a finite positive number, got {budget_usd!r}")


def _svc_validate_budget_tokens(budget_tokens: Any) -> None:
    """Service-boundary check: reject a non-positive budget_tokens. None (unlimited) is always accepted."""
    if budget_tokens is None:
        return
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be a positive integer, got {budget_tokens!r}")


def _svc_validate_rate_limit(rate_limit: Any) -> None:
    """Service-boundary check for the optional rolling-window fire cap."""
    from lionagi.studio.scheduler.admit import validate_rate_limit

    validate_rate_limit(rate_limit)


def _svc_validate_interval_sec(interval: Any, *, required: bool = False) -> None:
    """Service-boundary check: reject a missing or non-positive interval.
    `required=True` rejects a missing/null value — otherwise the schedule
    commits fine but never fires (next_fire_at stays None forever)."""
    if interval is None:
        if required:
            raise ValueError("interval_sec is required when trigger_type is 'interval'")
        return
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError(f"interval_sec must be a positive integer, got {interval!r}")


async def _svc_recompute_next_fire_guarded(effective: dict[str, Any], context: str) -> None:
    """Recompute next_fire_at after a committed write, without raising — the
    write already committed, so a recompute failure must not surface as a 500."""
    from ..scheduler.engine import scheduler

    for attempt in range(2):
        try:
            await scheduler.recompute_next_fire(effective)
            return
        except Exception:
            # A recovered first attempt is not warning-worthy noise; only the
            # final failure (stale next_fire_at until restart) warrants one.
            log = _log.warning if attempt else _log.debug
            log(
                "Failed to recompute next_fire_at for schedule %s after %s (attempt %d)",
                effective.get("id"),
                context,
                attempt + 1,
                exc_info=True,
            )


def _svc_validate_threshold_config(config: dict | None) -> None:
    """Service-boundary check: validate a metric-threshold alert config.
    None (no threshold configured) is always accepted."""
    if config is None:
        return
    from lionagi.studio.scheduler.threshold import validate_threshold_config

    validate_threshold_config(config)


def _svc_validate_github_repo(repo: str | None) -> None:
    """Service-boundary check: reject github_repo values that would manipulate
    the API path (CWE-918). None is a no-op; an empty string is rejected."""
    if repo is None:
        return
    from lionagi.studio.scheduler.github import _validate_github_repo

    _validate_github_repo(repo)


# github_filter's known keys. Only "pr_merged" has real dispatch semantics in
# github_poll() today; "pr_opened"/"pr_updated"/"pr_closed" are accepted (the
# frontend ships all four) but currently inert server-side.
_GITHUB_FILTER_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"state", "base", "draft", "event", "same_repo_only"}
)
_GITHUB_FILTER_ALLOWED_EVENTS: frozenset[str] = frozenset(
    {"pr_merged", "pr_opened", "pr_updated", "pr_closed"}
)


def _svc_validate_github_filter(github_filter: Any) -> None:
    """Service-boundary check: reject unknown github_filter keys/values — a typo'd
    key would otherwise match everything and fire on every poll instead of
    failing loudly at create/update time."""
    if github_filter is None:
        return
    if not isinstance(github_filter, dict):
        raise ValueError(f"github_filter must be an object, got {type(github_filter).__name__!r}")
    unknown = set(github_filter) - _GITHUB_FILTER_ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"github_filter has unknown key(s) {sorted(unknown)}; allowed keys are "
            f"{sorted(_GITHUB_FILTER_ALLOWED_KEYS)}"
        )
    event = github_filter.get("event")
    if event is not None and event not in _GITHUB_FILTER_ALLOWED_EVENTS:
        raise ValueError(
            f"github_filter.event {event!r} is not a supported value; allowed values "
            f"are {sorted(_GITHUB_FILTER_ALLOWED_EVENTS)} (or omit the key)"
        )
    if "same_repo_only" in github_filter:
        same_repo_only = github_filter["same_repo_only"]
        if not isinstance(same_repo_only, bool):
            raise ValueError(
                "github_filter.same_repo_only must be a boolean, got "
                f"{type(same_repo_only).__name__!r}"
            )


def _svc_validate_prompt(prompt: str | None) -> None:
    """Service-boundary check: reject action_prompt == '--', the literal
    end-of-options token that argparse would silently swallow."""
    if not prompt:
        return
    from lionagi.studio.scheduler.subprocess import _validate_prompt

    _validate_prompt(prompt)


def _validate_flow_yaml_spec(yaml_text: str) -> str | None:
    """Parse and validate an inline YAML flow spec; returns an error message on
    failure or None on success. Mirrors _validate_spec_fields() in the CLI —
    that's the authoritative source for field rules."""
    import yaml  # lazy — not needed on every import of this module

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return f"flow_yaml spec is not valid YAML: {exc}"

    if not isinstance(data, dict):
        return f"flow_yaml spec must be a YAML mapping (dict), got {type(data).__name__}"

    for key in data:
        if not isinstance(key, str):
            return f"flow_yaml spec keys must be strings, got {type(key).__name__}"

    return validate_flow_spec_fields(normalize_flow_spec_keys(data))


# Health severity is computed from cadence + observed schedule_runs rows and,
# for threshold alerts, the completed-evaluation watermark. It is never based
# on next_fire_at -- next_fire_at is a promise the scheduler made, not evidence
# that anything happened. A missed-fire/overlap/capacity skip advances the
# cursor while recording no execution, so silence hidden behind a pile of skips
# must still read as overdue, not healthy.
_HEALTH_OVERDUE_MULTIPLIER = 3
_HEALTH_OVERDUE_GRACE_FLOOR_SEC = 300  # 5 minutes

# Failing contract: the single latest executed run's outcome is enough to
# call a schedule failing -- N=1, deliberately. This is a demo-facing badge;
# one failed run is worth a glance, not something to smooth over by waiting
# for a streak to build. A newer execution that isn't failed/timed_out resets
# straight back to healthy, since only the latest execution is evaluated.
_HEALTH_FAILING_THRESHOLD = 1
_HEALTH_FAILING_OUTCOMES = ("failed", "timed_out")


def _schedule_cadence_seconds(row: dict[str, Any], *, reference_at: float) -> float | None:
    """Return the schedule's expected occurrence gap at ``reference_at``.

    Fixed-period triggers share the scheduler's cadence resolver. Cron is not
    fixed-period, so derive its local expected gap from two consecutive
    occurrences after ``reference_at`` -- the newest liveness evidence the
    caller has, which is the last execution for most schedules and the last
    threshold evaluation for a detector that evaluates without firing. This
    uses the same timezone resolver as the scheduler and never trusts
    ``next_fire_at`` -- a stored cursor is a promise, not evidence of work.
    """
    from ..scheduler.engine import resolve_schedule_cadence_seconds, resolve_schedule_timezone

    cadence = resolve_schedule_cadence_seconds(row)
    if cadence is not None or row.get("trigger_type") != "cron":
        return cadence

    expr = row.get("cron_expr")
    if not expr:
        return None
    try:
        from croniter import croniter

        start = datetime.fromtimestamp(reference_at, tz=resolve_schedule_timezone(row).tzinfo)
        occurrences = croniter(expr, start_time=start)
        first = occurrences.get_next(float)
        second = occurrences.get_next(float)
        gap = second - first
        return gap if math.isfinite(gap) and gap > 0 else None
    except Exception:
        # Creation/update validation prevents this for current rows. Preserve
        # list/detail availability for malformed legacy rows instead of making
        # a read-only health badge take the whole schedules endpoint down.
        return None


def compute_schedule_health(
    row: dict[str, Any], evidence: dict[str, Any], *, now: float
) -> dict[str, Any]:
    """Derive a read-only health verdict for one schedule.

    States: disabled (not enabled), never-fired (enabled, zero schedule_runs
    rows recorded at all AND no retained last_fired_at watermark -- the
    closest thing to a confident "never ran" this table can support),
    no-evidence (enabled, and either recorded rows exist with none of them
    execution evidence -- e.g. a skip/queue-only history -- or zero rows are
    recorded but schedules.last_fired_at shows the schedule executed before
    its schedule_runs history was pruned by retention; this table cannot
    distinguish those shapes from each other, so it reports "cannot tell"
    rather than guessing either way), overdue (enabled, cadence known, and
    no liveness evidence within grace of the expected cadence), failing (the
    single latest executed run's outcome was failed/timed_out -- see
    _HEALTH_FAILING_THRESHOLD), healthy (otherwise). For a threshold alert,
    ``last_evaluated_at`` is liveness evidence even when the metric did not
    breach and therefore no schedule_run was created. It does not replace the
    latest executed outcome used for the failing verdict.

    ``schedules.last_fired_at`` is a retained per-schedule column written by
    the normal occurrence paths -- it survives schedule_runs retention
    pruning even after every run row for a schedule is gone. never-fired is
    the strongest claim this table can make ("nothing ever happened"), so it
    must require BOTH signals to agree that nothing was recorded: zero rows
    (last_recorded_run_at is None) AND no surviving watermark (last_fired_at
    is None). Either one being non-null means the schedule executed at some
    point and the honest verdict is "cannot tell" (no-evidence), not
    "never-fired".
    """
    last_executed_at = evidence.get("last_executed_run_at")
    last_executed_status = evidence.get("last_executed_status")
    last_recorded_at = evidence.get("last_recorded_run_at")
    last_fired_at = row.get("last_fired_at")
    last_evaluated_at = row.get("last_evaluated_at") if row.get("threshold_config") else None

    if not row.get("enabled"):
        state = "disabled"
    elif last_executed_at is None and last_evaluated_at is None:
        state = (
            "never-fired" if last_recorded_at is None and last_fired_at is None else "no-evidence"
        )
    else:
        liveness_at = max(
            timestamp
            for timestamp in (last_executed_at, last_evaluated_at)
            if timestamp is not None
        )
        cadence_seconds = _schedule_cadence_seconds(row, reference_at=liveness_at)
        overdue = (
            cadence_seconds is not None
            and cadence_seconds > 0
            and (
                now - liveness_at
                > max(
                    cadence_seconds * _HEALTH_OVERDUE_MULTIPLIER,
                    cadence_seconds + _HEALTH_OVERDUE_GRACE_FLOOR_SEC,
                )
            )
        )
        if overdue:
            state = "overdue"
        elif last_executed_status in _HEALTH_FAILING_OUTCOMES:
            state = "failing"
        else:
            state = "healthy"

    return {
        "health_state": state,
        "health_last_outcome": last_executed_status,
        "health_last_outcome_at": last_executed_at,
        "health_since": row.get("created_at"),
    }


async def list_schedules(
    *,
    enabled: bool | None = None,
    trigger_type: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    if state_db_known_absent():
        return []
    async with StateDB(readonly=read_only_open_supported()) as db:
        rows = await db.list_schedules(enabled=enabled, trigger_type=trigger_type, project=project)
        ids = [row["id"] for row in rows]
        used_by_id = await db.count_schedule_runs_batch(ids, chain_depth=0)
        streaks_by_id = await db.schedule_run_streaks(ids)
        health_evidence_by_id = await db.schedule_health_evidence(ids)
        now = time.time()
        for row in rows:
            if row.get("max_runs"):
                row["remaining_runs"] = max(row["max_runs"] - used_by_id[row["id"]], 0)
            if row.get("budget_usd") or row.get("budget_tokens"):
                await _attach_spend(db, row)
            streak, last_status = streaks_by_id[row["id"]]
            row["consecutive_failures"] = streak
            row["last_status"] = last_status
            row.update(compute_schedule_health(row, health_evidence_by_id[row["id"]], now=now))
    return rows


async def _attach_spend(db: StateDB, row: dict[str, Any]) -> None:
    """Attach the spend rollup to *row* in place, for schedules with a configured budget.

    ``spend_is_partial`` (derived from ``unreported_sessions``) is what a caller/UI
    should branch on to render "unknown/partial" instead of trusting ``spend_usd``
    as a complete total -- see sum_schedule_spend's docstring for why an unreported
    session's cost is not the same as a $0 one.
    """
    spend = await db.sum_schedule_spend(row["id"])
    row["spend_usd"] = spend["cost_usd"]
    row["spend_tokens"] = spend["tokens"]
    row["unreported_sessions"] = spend["unreported_sessions"]
    row["spend_is_partial"] = spend["unreported_sessions"] > 0


async def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    if state_db_known_absent():
        return None
    async with StateDB(readonly=read_only_open_supported()) as db:
        row = await db.get_schedule(schedule_id)
        if not row:
            return None
        lifecycle_history = await db.list_schedule_lifecycle(schedule_id)
        runs = await db.list_schedule_runs(schedule_id, limit=10)
        if row.get("max_runs"):
            used = await db.count_schedule_runs(schedule_id, chain_depth=0)
            row["remaining_runs"] = max(row["max_runs"] - used, 0)
        if row.get("budget_usd") or row.get("budget_tokens"):
            await _attach_spend(db, row)
        streak, last_status = await db.schedule_run_streak(schedule_id)
        row["consecutive_failures"] = streak
        row["last_status"] = last_status
        evidence = (await db.schedule_health_evidence([schedule_id]))[schedule_id]
        row.update(compute_schedule_health(row, evidence, now=time.time()))
    row["recent_runs"] = runs
    row["lifecycle_history"] = lifecycle_history
    row["last_lifecycle_change"] = lifecycle_history[0] if lifecycle_history else None
    return row


async def get_schedule_by_name(name: str) -> dict[str, Any] | None:
    if state_db_known_absent():
        return None
    async with StateDB(readonly=read_only_open_supported()) as db:
        return await db.get_schedule_by_name(name)


async def create_schedule(
    data: dict[str, Any],
    *,
    actor: str = "operator",
    request_cwd: str | None = None,
    actor_self_reported: bool = False,
) -> dict[str, Any]:
    if not data.get("name"):
        raise ValueError("Schedule name is required")
    if not data.get("trigger_type"):
        raise ValueError("trigger_type is required")
    if not data.get("action_kind"):
        raise ValueError("action_kind is required")

    _svc_validate_action_model(data.get("action_model"))
    _svc_validate_prompt(data.get("action_prompt"))
    _svc_validate_identifier(data.get("action_agent"), "action_agent")
    _svc_validate_identifier(data.get("action_project"), "action_project")
    _svc_validate_identifier(data.get("action_playbook"), "action_playbook")
    _svc_validate_action_cwd(data.get("action_cwd"))
    _svc_validate_extra_args(data.get("action_extra_args"))
    _svc_validate_action_command(data.get("action_command"))
    _svc_validate_command_args(data.get("action_command_args"))
    _svc_validate_github_repo(data.get("github_repo"))
    _svc_validate_github_filter(data.get("github_filter"))
    _svc_validate_max_runs(data.get("max_runs"))
    _svc_validate_budget_usd(data.get("budget_usd"))
    _svc_validate_budget_tokens(data.get("budget_tokens"))
    _svc_validate_rate_limit(data.get("rate_limit"))
    _svc_validate_threshold_config(data.get("threshold_config"))
    if data.get("trigger_type") == "cron":
        _svc_validate_cron_expr(data.get("cron_expr"), required=True)
    if data.get("trigger_type") == "interval":
        _svc_validate_interval_sec(data.get("interval_sec"), required=True)
    if data.get("trigger_type") == "github_poll" and not data.get("github_repo"):
        raise ValueError("github_repo is required when trigger_type is 'github_poll'")
    poll_interval_sec = data.get("poll_interval_sec")
    if poll_interval_sec is not None and poll_interval_sec < 1:
        raise ValueError("poll_interval_sec must be a positive integer")

    if data.get("action_kind") == "flow_yaml":
        yaml_text = data.get("action_flow_yaml") or ""
        if not yaml_text.strip():
            raise ValueError(
                "action_flow_yaml is required and must not be empty for action_kind='flow_yaml'"
            )
        spec_err = _validate_flow_yaml_spec(yaml_text)
        if spec_err:
            raise ValueError(f"Invalid flow_yaml spec: {spec_err}")

    if data.get("action_kind") == "command" and not (data.get("action_command") or "").strip():
        raise ValueError(
            "action_command is required and must not be empty for action_kind='command'"
        )

    # ADR-0070 delta 1: snapshot a stable execution root once at creation time
    # (not re-resolved at every fire) so later project-registry or daemon-cwd
    # changes can't move this schedule's spawn cwd out from under it.
    action_cwd = data.get("action_cwd")
    if not action_cwd and data.get("action_project"):
        from lionagi.studio.services._db import StoreNotAddressableError
        from lionagi.studio.services.projects import get_project

        from ..scheduler.engine import _is_usable_execution_root

        # This lookup is a best-effort cwd snapshot on an otherwise
        # StateDB-only write (server-reachable). The projects catalog is
        # SQLite-only, so a server-backed store makes it unreadable here --
        # same as the project simply not being found, not a reason to refuse
        # a schedule create that does not itself need SQLite.
        try:
            project = await get_project(data["action_project"])
        except StoreNotAddressableError:
            project = None
        project_path = project.get("path") if project else None
        # The same rule the resolver applies, so a root is never persisted here
        # that the resolver would refuse to honor later. Registered project
        # paths are not validated when the project is registered, so a relative
        # one reaches this point; persisting it would snapshot "wherever the
        # daemon started" as this schedule's execution root.
        if _is_usable_execution_root(project_path):
            action_cwd = project_path

    schedule_id = uuid.uuid4().hex[:12]
    now = time.time()
    schedule = {
        "id": schedule_id,
        "created_at": now,
        "updated_at": now,
        **data,
        "action_cwd": action_cwd,
    }
    async with StateDB() as db:
        try:
            await db.create_schedule(
                schedule,
                lifecycle_actor=actor,
                lifecycle_source="operator",
                lifecycle_reason_summary="Schedule created through Studio.",
                lifecycle_metadata=_lifecycle_metadata(
                    request_cwd, actor_self_reported=actor_self_reported
                ),
            )
        except (sqlite3.IntegrityError, SAIntegrityError) as exc:
            raise NameConflictError(f"Schedule name {data['name']!r} already exists") from exc
    return {"id": schedule_id, "name": data["name"], "created_at": now}


async def update_schedule(schedule_id: str, fields: dict[str, Any]) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        if not fields:
            # Nothing was explicitly set on the PATCH body — a genuine no-op
            # on a schedule that exists, not a 404.
            return True

        cleared = {
            key for key in _NON_NULLABLE_SCHEDULE_FIELDS if key in fields and fields[key] is None
        }
        if cleared:
            raise ValueError(f"Field(s) {sorted(cleared)} cannot be cleared to null")

        if "action_model" in fields:
            _svc_validate_action_model(fields["action_model"])
        if "action_prompt" in fields:
            _svc_validate_prompt(fields["action_prompt"])
        if "action_agent" in fields:
            _svc_validate_identifier(fields["action_agent"], "action_agent")
        if "action_project" in fields:
            _svc_validate_identifier(fields["action_project"], "action_project")
        if "action_playbook" in fields:
            _svc_validate_identifier(fields["action_playbook"], "action_playbook")
        if "action_cwd" in fields:
            _svc_validate_action_cwd(fields["action_cwd"])
        if "action_extra_args" in fields:
            _svc_validate_extra_args(fields["action_extra_args"])
        if "action_command" in fields:
            _svc_validate_action_command(fields["action_command"])
        if "action_command_args" in fields:
            _svc_validate_command_args(fields["action_command_args"])
        if "github_repo" in fields:
            _svc_validate_github_repo(fields["github_repo"])
        if "github_filter" in fields:
            _svc_validate_github_filter(fields["github_filter"])
        if "github_cursor" in fields:
            _svc_validate_github_cursor(fields["github_cursor"])
        if "max_runs" in fields:
            _svc_validate_max_runs(fields["max_runs"])
        if "budget_usd" in fields:
            _svc_validate_budget_usd(fields["budget_usd"])
        if "budget_tokens" in fields:
            _svc_validate_budget_tokens(fields["budget_tokens"])
        if "rate_limit" in fields:
            _svc_validate_rate_limit(fields["rate_limit"])
        if "threshold_config" in fields:
            _svc_validate_threshold_config(fields["threshold_config"])

        effective = {**schedule, **fields}
        effective_repo = effective.get("github_repo")
        if effective_repo is not None:
            _svc_validate_github_repo(effective_repo)
        if effective.get("action_kind") == "flow_yaml":
            yaml_text = effective.get("action_flow_yaml") or ""
            if not yaml_text.strip():
                raise ValueError(
                    "action_flow_yaml is required and must not be empty for action_kind='flow_yaml'"
                )
            spec_err = _validate_flow_yaml_spec(yaml_text)
            if spec_err:
                raise ValueError(f"Invalid flow_yaml spec: {spec_err}")
        if (
            effective.get("action_kind") == "command"
            and not (effective.get("action_command") or "").strip()
        ):
            raise ValueError(
                "action_command is required and must not be empty for action_kind='command'"
            )
        touches_trigger = "cron_expr" in fields or "trigger_type" in fields
        if touches_trigger and effective.get("trigger_type") == "cron":
            _svc_validate_cron_expr(effective.get("cron_expr"), required=True)
        touches_interval = "interval_sec" in fields or "trigger_type" in fields
        if touches_interval and effective.get("trigger_type") == "interval":
            _svc_validate_interval_sec(effective.get("interval_sec"), required=True)

        await db.update_schedule(schedule_id, **fields)

    # A PATCH touching cron_expr/trigger_type must recompute next_fire_at
    # immediately rather than waiting for the next fire. The field update
    # already committed, so a recompute failure here degrades to a stale
    # next_fire_at rather than turning the PATCH into an unhandled 500.
    if effective.get("trigger_type") == "cron":
        await _svc_recompute_next_fire_guarded(effective, "update")
    return True


async def delete_schedule(
    schedule_id: str,
    *,
    actor: str = "operator",
    request_cwd: str | None = None,
    actor_self_reported: bool = False,
) -> bool:
    async with StateDB() as db:
        return await db.delete_schedule(
            schedule_id,
            lifecycle_actor=actor,
            lifecycle_source="operator",
            lifecycle_reason_summary="Schedule deleted through Studio.",
            lifecycle_metadata=_lifecycle_metadata(
                request_cwd, actor_self_reported=actor_self_reported
            ),
        )


async def enable_schedule(
    schedule_id: str,
    *,
    actor: str = "operator",
    request_cwd: str | None = None,
    actor_self_reported: bool = False,
) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        if schedule.get("trigger_type") == "cron":
            _svc_validate_cron_expr(schedule.get("cron_expr"), required=True)
        if schedule.get("trigger_type") == "interval":
            _svc_validate_interval_sec(schedule.get("interval_sec"), required=True)
        # max_runs is a lifetime cap on the schedule id, not per-enabled-period —
        # re-enabling a schedule that already hit it stays refused rather than
        # silently resetting the counter.
        max_runs = schedule.get("max_runs")
        if max_runs:
            used = await db.count_schedule_runs(schedule_id, chain_depth=0)
            if used >= max_runs:
                raise ValueError(
                    f"Schedule '{schedule_id}' has already reached its max_runs="
                    f"{max_runs} limit ({used} terminal run(s) recorded). "
                    "Increase or clear max_runs before re-enabling."
                )
        await db.update_schedule(
            schedule_id,
            enabled=1,
            lifecycle_actor=actor,
            lifecycle_source="operator",
            lifecycle_reason_summary="Schedule enabled through Studio.",
            lifecycle_metadata=_lifecycle_metadata(
                request_cwd, actor_self_reported=actor_self_reported
            ),
        )

    # A long-disabled schedule's next_fire_at may be stale; recompute now so
    # re-enabling only fires immediately if the *current* interpretation says so.
    effective = {**schedule, "enabled": 1}
    if effective.get("trigger_type") == "cron":
        await _svc_recompute_next_fire_guarded(effective, "enable")
    return True


async def disable_schedule(
    schedule_id: str,
    *,
    reason: str | None = "Schedule disabled by a direct service call.",
    actor: str = "operator",
    request_cwd: str | None = None,
    actor_self_reported: bool = False,
) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("A reason is required to disable a schedule")
        await db.update_schedule(
            schedule_id,
            enabled=0,
            lifecycle_actor=actor,
            lifecycle_source="operator",
            lifecycle_reason_summary=reason,
            lifecycle_metadata=_lifecycle_metadata(
                request_cwd, actor_self_reported=actor_self_reported
            ),
        )
    return True


async def list_schedule_runs(
    schedule_id: str,
    *,
    status: str | list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if state_db_known_absent():
        return []
    async with StateDB(readonly=read_only_open_supported()) as db:
        return await db.list_schedule_runs(schedule_id, status=status, limit=limit, offset=offset)


async def list_schedule_run_views(
    schedule_id: str,
    *,
    status: str | list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """RunView list — each row additionally carries a reconciled ``outcome``."""
    if state_db_known_absent():
        return []
    async with StateDB(readonly=read_only_open_supported()) as db:
        return await run_view.list_run_views(
            db, schedule_id, status=status, limit=limit, offset=offset
        )


async def get_schedule_run(run_id: str) -> dict[str, Any] | None:
    if state_db_known_absent():
        return None
    async with StateDB(readonly=read_only_open_supported()) as db:
        run = await db.get_schedule_run(run_id)
        if not run:
            return None
        # Include chain children
        if run.get("chain_depth", 0) == 0:
            rows = await db.fetch_all(
                "SELECT * FROM schedule_runs WHERE chain_parent_id = ? ORDER BY chain_depth, fired_at",
                (run_id,),
            )
            run["chain_children"] = rows
        # Layer the RunView-reconciled fields on top of the SAME row already
        # fetched above, additively — chain_children (legacy) and
        # outcome/duration_ms/... coexist without a second, independent read
        # of schedule_runs that could observe a different row state.
        view = await run_view.build_run_view_for(db, run)
        run = {**run, **view}
    return run


async def get_schedule_status(schedule_id: str) -> dict[str, Any] | None:
    """'Did it work?' view: schedule header + latest RunView + shared exit code."""
    if state_db_known_absent():
        return None
    async with StateDB(readonly=read_only_open_supported()) as db:
        return await run_view.get_schedule_status_view(db, schedule_id)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateScheduleRequest(BaseModel):
    # An unknown key is a caller mistake, and silently dropping it means the
    # request reports success for a change that never happened. The schedule
    # declaration models next door already forbid extras; these two were the
    # holdouts.
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    trigger_type: str
    cron_expr: str | None = None
    interval_sec: int | None = None
    github_repo: str | None = None
    github_filter: dict | None = None
    poll_interval_sec: int | None = None
    action_kind: str
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_flow_yaml: str | None = None
    action_project: str | None = None
    action_cwd: str | None = None
    action_extra_args: list[str] | None = None
    action_command: str | None = None
    action_command_args: list[str] | None = None
    on_success: dict | None = None
    on_fail: dict | None = None
    missed_fire_policy: str = "skip"
    overlap_policy: str = "skip"
    max_runs: int | None = None
    budget_usd: float | None = None
    budget_tokens: int | None = None
    rate_limit: dict | None = None
    project: str | None = None
    threshold_config: dict | None = None


class UpdateScheduleRequest(BaseModel):
    # An unknown key is a caller mistake, and silently dropping it means the
    # request reports success for a change that never happened. The schedule
    # declaration models next door already forbid extras; these two were the
    # holdouts.
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    cron_expr: str | None = None
    interval_sec: int | None = None
    github_repo: str | None = None
    github_filter: dict | None = None
    # The poller's own bookmark, patchable so an operator can move it
    # deliberately -- forward to skip a backlog the schedule would
    # otherwise dispatch all at once, or back to replay one.
    github_cursor: str | None = None
    poll_interval_sec: int | None = None
    action_kind: str | None = None
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_flow_yaml: str | None = None
    action_project: str | None = None
    action_cwd: str | None = None
    action_extra_args: list[str] | None = None
    action_command: str | None = None
    action_command_args: list[str] | None = None
    on_success: dict | None = None
    on_fail: dict | None = None
    missed_fire_policy: str | None = None
    overlap_policy: str | None = None
    max_runs: int | None = None
    budget_usd: float | None = None
    budget_tokens: int | None = None
    rate_limit: dict | None = None
    project: str | None = None
    threshold_config: dict | None = None


class DisableScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


# ---------------------------------------------------------------------------
# Route handlers — schedules area
# ---------------------------------------------------------------------------


@studio_route("/schedules/", method="GET", area="schedules", name="list_schedules")
async def list_schedules_route(
    enabled: bool | None = Query(default=None),
    trigger_type: str | None = Query(default=None),
    project: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = await list_schedules(enabled=enabled, trigger_type=trigger_type, project=project)
    return {"schedules": rows}


@studio_route("/schedules/limits", method="GET", area="schedules", name="schedule_limits")
async def schedule_limits_route() -> dict[str, Any]:
    # Registered before /{schedule_id} so "limits" resolves here, not as an id.
    from lionagi.studio import config

    from ..scheduler.engine import scheduler

    # The scheduled and ad-hoc lanes draw from independent capacity pools
    # (see MAX_ADHOC_CONCURRENT's own docstring in config.py) -- an operator
    # reading only the scheduled cap would under-provision by the ad-hoc
    # lane's own additive capacity, since the daemon can run both caps'
    # worth of executions at once.
    return {
        "max_scheduled_concurrent": config.MAX_SCHEDULED_CONCURRENT,
        "current_inflight": scheduler._global_inflight,
        "max_adhoc_concurrent": config.MAX_ADHOC_CONCURRENT,
        "current_adhoc_inflight": scheduler._adhoc_inflight,
    }


@studio_route("/schedules/{schedule_id}", method="GET", area="schedules", name="get_schedule")
async def get_schedule_route(schedule_id: str) -> dict[str, Any]:
    data = await get_schedule(schedule_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return data


@studio_route(
    "/schedules/",
    method="POST",
    area="schedules",
    status_code=201,
    name="create_schedule",
)
async def create_schedule_route(
    body: CreateScheduleRequest,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    actor, request_cwd, actor_self_reported = _request_lifecycle_identity(request)
    try:
        return await create_schedule(
            body.model_dump(exclude_none=True),
            actor=actor,
            request_cwd=request_cwd,
            actor_self_reported=actor_self_reported,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@studio_route(
    "/schedules/{schedule_id}",
    method="PATCH",
    area="schedules",
    name="update_schedule",
)
async def update_schedule_route(schedule_id: str, body: UpdateScheduleRequest) -> dict[str, Any]:
    # exclude_unset (not exclude_none): an explicit null must pass through so
    # update_schedule can clear/reject it, distinct from a field never sent.
    fields = body.model_dump(exclude_unset=True)
    try:
        ok = await update_schedule(schedule_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    # Naming what was applied: an empty body is a legitimate no-op on an
    # existing schedule, but a bare "ok" reads the same as a change that
    # landed. The caller can tell the two apart from the list.
    return {"ok": True, "updated": sorted(fields)}


@studio_route(
    "/schedules/{schedule_id}",
    method="DELETE",
    area="schedules",
    name="delete_schedule",
)
async def delete_schedule_route(
    schedule_id: str,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    actor, request_cwd, actor_self_reported = _request_lifecycle_identity(request)
    ok = await delete_schedule(
        schedule_id,
        actor=actor,
        request_cwd=request_cwd,
        actor_self_reported=actor_self_reported,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True}


@studio_route(
    "/schedules/{schedule_id}/enable",
    method="POST",
    area="schedules",
    name="enable_schedule",
)
async def enable_schedule_route(
    schedule_id: str,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    actor, request_cwd, actor_self_reported = _request_lifecycle_identity(request)
    try:
        ok = await enable_schedule(
            schedule_id,
            actor=actor,
            request_cwd=request_cwd,
            actor_self_reported=actor_self_reported,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True, "enabled": True}


@studio_route(
    "/schedules/{schedule_id}/disable",
    method="POST",
    area="schedules",
    name="disable_schedule",
)
async def disable_schedule_route(
    schedule_id: str,
    request: Request = None,  # type: ignore[assignment]
    body: DisableScheduleRequest | None = None,
) -> dict[str, Any]:
    actor, request_cwd, actor_self_reported = _request_lifecycle_identity(request)
    try:
        ok = await disable_schedule(
            schedule_id,
            reason=body.reason if body is not None else None,
            actor=actor,
            request_cwd=request_cwd,
            actor_self_reported=actor_self_reported,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True, "enabled": False}


@studio_route(
    "/schedules/{schedule_id}/trigger",
    method="POST",
    area="schedules",
    name="trigger_schedule",
)
async def trigger_schedule_route(schedule_id: str) -> dict[str, Any]:
    from ..scheduler.engine import scheduler

    try:
        run_id = await scheduler.fire_now(schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True, "run_id": run_id}


@studio_route(
    "/schedules/{schedule_id}/runs",
    method="GET",
    area="schedules",
    name="list_schedule_runs",
)
async def list_schedule_runs_route(
    schedule_id: str,
    status: list[str] | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    # RunView-enriched rows (adds outcome/duration_ms/session_ids/artifacts
    # additively) — status is repeatable (?status=failed&status=timed_out).
    rows = await list_schedule_run_views(schedule_id, status=status, limit=limit, offset=offset)
    return {"runs": rows, "limit": limit, "offset": offset, "has_next": len(rows) == limit}


# Top-level schedule-runs endpoint for looking up a single run by ID
@studio_route(
    "/schedules/runs/{run_id}",
    method="GET",
    area="schedules",
    tags=["schedules", "schedule-runs"],
    name="get_schedule_run",
)
async def get_schedule_run_route(run_id: str) -> dict[str, Any]:
    data = await get_schedule_run(run_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")
    return data


@studio_route(
    "/schedules/{schedule_id}/status",
    method="GET",
    area="schedules",
    name="get_schedule_status",
)
async def get_schedule_status_route(schedule_id: str) -> dict[str, Any]:
    data = await get_schedule_status(schedule_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return data
