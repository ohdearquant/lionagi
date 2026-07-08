# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 schedules service — backs /api/schedules endpoints."""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

from fastapi import HTTPException, Query
from pydantic import BaseModel

from lionagi.service.providers import EFFORT_LEVELS as _VALID_EFFORT_LEVELS
from lionagi.state.db import DEFAULT_DB_PATH, StateDB

from ..registry import studio_route

_log = logging.getLogger(__name__)

_PRESERVE_DASHED: frozenset[str] = frozenset({"argument-hint"})

# schedules columns declared NOT NULL — a PATCH that explicitly sets one of
# these to null must be rejected (400) rather than passed through to a DB
# constraint violation (or, worse, silently dropped).
_NON_NULLABLE_SCHEDULE_FIELDS: frozenset[str] = frozenset(
    {"name", "trigger_type", "action_kind", "missed_fire_policy", "overlap_policy"}
)


def _svc_validate_action_model(model: str | None) -> None:
    """Service-boundary check: reject action_model values that inject CLI flags.

    Delegates to subprocess._validate_action_model so the allowed-character
    rule is defined in exactly one place.
    """
    if not model:
        return
    from lionagi.studio.scheduler.subprocess import _validate_action_model

    _validate_action_model(model)


def _svc_validate_identifier(value: str | None, field_name: str) -> None:
    """Service-boundary check: reject identifier fields (agent/project/playbook) starting with '-'.

    Identifier fields are not freeform text — they name profiles, projects, or
    playbooks and must not start with '-'.  A leading '-' causes argparse to
    treat the value as a flag, producing either a flag toggle or a usage error
    depending on the subcommand.  Reject both outcomes at write time.
    """
    if not value:
        return
    from lionagi.studio.scheduler.subprocess import _validate_identifier

    _validate_identifier(value, field_name)


def _svc_validate_extra_args(extra: list | None) -> None:
    """Service-boundary check: reject action_extra_args elements that inject CLI flags.

    Delegates to subprocess._validate_extra_args so the flag-rejection rule is
    defined in exactly one place.
    """
    if not extra:
        return
    from lionagi.studio.scheduler.subprocess import _validate_extra_args

    _validate_extra_args(extra)


def _svc_validate_cron_expr(expr: str | None, *, required: bool = False) -> None:
    """Service-boundary check: reject a syntactically invalid cron expression.

    `required=True` also rejects a missing/empty expression — callers pass
    this when trigger_type == "cron", since a cron-triggered schedule with no
    expression commits fine but never fires (next_fire_at stays None forever).
    """
    if not expr:
        if required:
            raise ValueError("cron_expr is required when trigger_type is 'cron'")
        return
    from croniter import croniter

    if not croniter.is_valid(expr):
        raise ValueError(f"Invalid cron expression: {expr!r}")


def _svc_validate_max_runs(max_runs: Any) -> None:
    """Service-boundary check: reject a non-positive max_runs.

    None means unlimited (the column default) and is always accepted.
    """
    if max_runs is None:
        return
    if isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs < 1:
        raise ValueError(f"max_runs must be a positive integer, got {max_runs!r}")


def _svc_validate_budget_usd(budget_usd: Any) -> None:
    """Service-boundary check: reject a non-positive budget_usd.

    None means unlimited (the column default) and is always accepted.
    """
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
    """Service-boundary check: reject a non-positive budget_tokens.

    None means unlimited (the column default) and is always accepted.
    """
    if budget_tokens is None:
        return
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be a positive integer, got {budget_tokens!r}")


def _svc_validate_interval_sec(interval: Any, *, required: bool = False) -> None:
    """Service-boundary check: reject a missing or non-positive interval.

    `required=True` rejects a missing/null value — callers pass this when
    trigger_type == "interval", since an interval-triggered schedule without
    interval_sec commits fine but never fires (next-fire computation returns
    None forever), the same enabled-but-dead shape as a cron schedule with
    no expression.
    """
    if interval is None:
        if required:
            raise ValueError("interval_sec is required when trigger_type is 'interval'")
        return
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError(f"interval_sec must be a positive integer, got {interval!r}")


async def _svc_recompute_next_fire_guarded(effective: dict[str, Any], context: str) -> None:
    """Recompute next_fire_at after a committed write, without raising.

    The caller's DB write has already committed, so a recompute failure must
    not surface as an unhandled 500. One immediate retry covers transient DB
    contention. If both attempts fail the row keeps its stale next_fire_at:
    the tick loop only touches rows that are due or have no next_fire_at, so
    a stale *future* timestamp is healed only by the daemon-startup recompute
    (or fires once on the old timestamp and recomputes from there).
    """
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


def _svc_validate_github_repo(repo: str | None) -> None:
    """Service-boundary check: reject github_repo values that would manipulate the API path.

    Delegates to github._validate_github_repo so the owner/name regex is defined
    in exactly one place (CWE-918 — path manipulation in URL construction).

    None means the field was not supplied (no-op); an empty string is an
    explicit invalid value and is forwarded to the validator for rejection.
    """
    if repo is None:
        return
    from lionagi.studio.scheduler.github import _validate_github_repo

    _validate_github_repo(repo)


# github_filter's known keys. "event" narrows *which* PR lifecycle moment
# fires the trigger; the other three narrow *which PRs* are considered at
# all. Only "pr_merged" has real dispatch semantics in github_poll() today
# (state=closed poll, fires on newly-merged PRs only) -- "pr_opened",
# "pr_updated", and "pr_closed" are accepted because the Studio frontend's
# create-schedule form already ships all four as event choices (and defaults
# new schedules to "pr_updated"; see apps/studio/frontend/src/components/
# schedules/CreateScheduleModal.tsx), but are currently inert server-side:
# github_poll() only branches on "pr_merged" and otherwise polls open PRs
# unfiltered by event, the same as omitting the key entirely.
_GITHUB_FILTER_ALLOWED_KEYS: frozenset[str] = frozenset({"state", "base", "draft", "event"})
_GITHUB_FILTER_ALLOWED_EVENTS: frozenset[str] = frozenset(
    {"pr_merged", "pr_opened", "pr_updated", "pr_closed"}
)


def _svc_validate_github_filter(github_filter: Any) -> None:
    """Service-boundary check: reject unknown github_filter keys/values.

    None means the field was not supplied (no-op). An empty dict matches
    every PR, same as omitting the field, and is accepted. Unknown keys are
    rejected outright rather than silently ignored -- a typo'd or
    speculative filter key would otherwise match everything and fire on
    every poll instead of failing loudly at create/update time.
    """
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


def _svc_validate_prompt(prompt: str | None) -> None:
    """Service-boundary check: reject action_prompt == '--'.

    The literal end-of-options token '--' is silently consumed by argparse and
    would not reach the runner as prompt text.  All other prompt content —
    including values starting with '-' — is unrestricted because the structural
    argv fix places a '--' sentinel before all positionals.  Delegates to
    subprocess._validate_prompt so the rule is defined in one place.
    """
    if not prompt:
        return
    from lionagi.studio.scheduler.subprocess import _validate_prompt

    _validate_prompt(prompt)


def _validate_flow_yaml_spec(yaml_text: str) -> str | None:
    """Parse and validate an inline YAML flow spec.

    Returns an error message string on failure, or None on success.
    Mirrors lionagi/cli/orchestrate/__init__.py::_validate_spec_fields() and
    lionagi/studio/services/playbooks.py::_check_spec_fields() — implemented
    inline to avoid loading fastapi or the full orchestrate module at import time.
    Authoritative source for field rules: _validate_spec_fields() in the CLI.
    """
    import yaml  # lazy — not needed on every import of this module

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return f"flow_yaml spec is not valid YAML: {exc}"

    if not isinstance(data, dict):
        return f"flow_yaml spec must be a YAML mapping (dict), got {type(data).__name__}"

    # Normalize hyphenated keys (e.g. max-ops → max_ops) before field checks.
    spec: dict[str, Any] = {}
    for key, value in data.items():
        if key in _PRESERVE_DASHED or "-" not in key:
            spec[key] = value
        else:
            spec[key.replace("-", "_")] = value

    if "workers" in spec:
        workers = spec["workers"]
        if not isinstance(workers, int) or isinstance(workers, bool):
            return f"spec field 'workers' must be an integer, got {type(workers).__name__}"
        if not (1 <= workers <= 32):
            return f"spec field 'workers' must be in [1, 32], got {workers}"

    for key in ("max_ops", "max_agents"):
        if key not in spec:
            continue
        value = spec[key]
        if not isinstance(value, int) or isinstance(value, bool):
            return f"spec field {key!r} must be an integer, got {type(value).__name__}"
        if not (0 <= value <= 50):
            return f"spec field {key!r} must be in [0, 50] (0 = unlimited), got {value}"

    effort = spec.get("effort")
    if effort is not None:
        if not isinstance(effort, str):
            return f"spec field 'effort' must be a string, got {type(effort).__name__}"
        if effort not in _VALID_EFFORT_LEVELS:
            return (
                f"spec field 'effort' must be one of {sorted(_VALID_EFFORT_LEVELS)}, got {effort!r}"
            )

    if "with_synthesis" in spec:
        val = spec["with_synthesis"]
        if not isinstance(val, bool | str):
            return (
                f"spec field 'with_synthesis' must be bool or str (model spec), "
                f"got {type(val).__name__}"
            )

    return None


_ENSURE_SCHEDULES_SQL = """
CREATE TABLE IF NOT EXISTS schedules (
    id                  TEXT    PRIMARY KEY,
    name                TEXT    NOT NULL UNIQUE,
    description         TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    trigger_type        TEXT    NOT NULL,
    cron_expr           TEXT,
    interval_sec        INTEGER,
    github_repo         TEXT,
    github_filter       JSON,
    github_cursor       TEXT,
    poll_interval_sec   INTEGER,
    action_kind         TEXT    NOT NULL,
    action_model        TEXT,
    action_prompt       TEXT,
    action_agent        TEXT,
    action_playbook     TEXT,
    action_project      TEXT,
    action_extra_args   JSON    DEFAULT '[]',
    on_success          JSON,
    on_fail             JSON,
    last_fired_at       REAL,
    next_fire_at        REAL,
    missed_fire_policy  TEXT    NOT NULL DEFAULT 'skip',
    overlap_policy      TEXT    NOT NULL DEFAULT 'skip',
    max_runs            INTEGER,
    budget_usd          REAL,
    budget_tokens       INTEGER,
    project             TEXT,
    created_at          REAL    NOT NULL,
    updated_at          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled
    ON schedules(enabled, next_fire_at) WHERE enabled = 1;
CREATE INDEX IF NOT EXISTS idx_schedules_name
    ON schedules(name);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id                  TEXT    PRIMARY KEY,
    schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    invocation_id       TEXT,
    trigger_context     JSON    NOT NULL,
    action_kind         TEXT    NOT NULL,
    action_args         JSON    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'running',
    exit_code           INTEGER,
    chain_parent_id     TEXT,
    chain_depth         INTEGER NOT NULL DEFAULT 0,
    fired_at            REAL    NOT NULL,
    ended_at            REAL,
    error_detail        TEXT,
    created_at          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_runs_schedule
    ON schedule_runs(schedule_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_sched_runs_status
    ON schedule_runs(status) WHERE status = 'running';
"""


async def _ensure_table(db) -> None:
    await db.executescript(_ENSURE_SCHEDULES_SQL)


async def list_schedules(
    *,
    enabled: bool | None = None,
    trigger_type: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        rows = await db.list_schedules(enabled=enabled, trigger_type=trigger_type, project=project)
        for row in rows:
            if row.get("max_runs"):
                used = await db.count_schedule_runs(row["id"], chain_depth=0)
                row["remaining_runs"] = max(row["max_runs"] - used, 0)
            streak, last_status = await db.schedule_run_streak(row["id"])
            row["consecutive_failures"] = streak
            row["last_status"] = last_status
    return rows


async def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        row = await db.get_schedule(schedule_id)
        if not row:
            return None
        runs = await db.list_schedule_runs(schedule_id, limit=10)
        if row.get("max_runs"):
            used = await db.count_schedule_runs(schedule_id, chain_depth=0)
            row["remaining_runs"] = max(row["max_runs"] - used, 0)
        streak, last_status = await db.schedule_run_streak(schedule_id)
        row["consecutive_failures"] = streak
        row["last_status"] = last_status
    row["recent_runs"] = runs
    return row


async def get_schedule_by_name(name: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        return await db.get_schedule_by_name(name)


async def create_schedule(data: dict[str, Any]) -> dict[str, Any]:
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
    _svc_validate_extra_args(data.get("action_extra_args"))
    _svc_validate_github_repo(data.get("github_repo"))
    _svc_validate_github_filter(data.get("github_filter"))
    _svc_validate_max_runs(data.get("max_runs"))
    _svc_validate_budget_usd(data.get("budget_usd"))
    _svc_validate_budget_tokens(data.get("budget_tokens"))
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

    schedule_id = uuid.uuid4().hex[:12]
    now = time.time()
    schedule = {
        "id": schedule_id,
        "created_at": now,
        "updated_at": now,
        **data,
    }
    async with StateDB() as db:
        await db.create_schedule(schedule)
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
        if "action_extra_args" in fields:
            _svc_validate_extra_args(fields["action_extra_args"])
        if "github_repo" in fields:
            _svc_validate_github_repo(fields["github_repo"])
        if "github_filter" in fields:
            _svc_validate_github_filter(fields["github_filter"])
        if "max_runs" in fields:
            _svc_validate_max_runs(fields["max_runs"])
        if "budget_usd" in fields:
            _svc_validate_budget_usd(fields["budget_usd"])
        if "budget_tokens" in fields:
            _svc_validate_budget_tokens(fields["budget_tokens"])

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
        touches_trigger = "cron_expr" in fields or "trigger_type" in fields
        if touches_trigger and effective.get("trigger_type") == "cron":
            _svc_validate_cron_expr(effective.get("cron_expr"), required=True)
        touches_interval = "interval_sec" in fields or "trigger_type" in fields
        if touches_interval and effective.get("trigger_type") == "interval":
            _svc_validate_interval_sec(effective.get("interval_sec"), required=True)

        await db.update_schedule(schedule_id, **fields)

    # A PATCH that touches cron_expr (or trigger_type) must take effect on
    # next_fire_at immediately rather than waiting for the next fire — the
    # stored `effective` dict already reflects the post-update schedule, so
    # this recomputes under the new interpretation and logs iff it shifted.
    # The field update above already committed; a recompute failure here
    # (e.g. a transient DB error) must not turn an already-committed PATCH
    # into an unhandled 500 — it degrades to a stale next_fire_at (retried
    # once; healed at daemon startup if both attempts fail).
    if effective.get("trigger_type") == "cron":
        await _svc_recompute_next_fire_guarded(effective, "update")
    return True


async def delete_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        return await db.delete_schedule(schedule_id)


async def enable_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        if schedule.get("trigger_type") == "cron":
            _svc_validate_cron_expr(schedule.get("cron_expr"), required=True)
        if schedule.get("trigger_type") == "interval":
            _svc_validate_interval_sec(schedule.get("interval_sec"), required=True)
        # Re-enable semantics: a bounded schedule that has already consumed
        # its max_runs budget stays refused rather than silently resetting
        # the counter. There is no per-enable-cycle counting — max_runs is a
        # lifetime cap on the schedule's id, not a cap per enabled period —
        # so enabling it would either immediately re-trip _check_max_runs on
        # the next fire (confusing) or require introducing new schema state
        # to track "cycles", which is more machinery than this footgun
        # warrants. Raise so the caller can increase/clear max_runs first.
        max_runs = schedule.get("max_runs")
        if max_runs:
            used = await db.count_schedule_runs(schedule_id, chain_depth=0)
            if used >= max_runs:
                raise ValueError(
                    f"Schedule '{schedule_id}' has already reached its max_runs="
                    f"{max_runs} limit ({used} terminal run(s) recorded). "
                    "Increase or clear max_runs before re-enabling."
                )
        await db.update_schedule(schedule_id, enabled=1)

    # A schedule can sit disabled for a long time; its stored next_fire_at
    # may be stale (in the past, or computed under an old interpretation).
    # Recompute now so re-enabling never fires immediately on stale data —
    # it only fires immediately if the *current* cron interpretation says so.
    # The enabled flag above already committed; a recompute failure here
    # must not turn an already-committed enable into an unhandled 500.
    effective = {**schedule, "enabled": 1}
    if effective.get("trigger_type") == "cron":
        await _svc_recompute_next_fire_guarded(effective, "enable")
    return True


async def disable_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        await db.update_schedule(schedule_id, enabled=0)
    return True


async def list_schedule_runs(
    schedule_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        return await db.list_schedule_runs(schedule_id, status=status, limit=limit, offset=offset)


async def get_schedule_run(run_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
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
    return run


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateScheduleRequest(BaseModel):
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
    action_extra_args: list[str] | None = None
    on_success: dict | None = None
    on_fail: dict | None = None
    missed_fire_policy: str = "skip"
    overlap_policy: str = "skip"
    max_runs: int | None = None
    budget_usd: float | None = None
    budget_tokens: int | None = None
    project: str | None = None


class UpdateScheduleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    cron_expr: str | None = None
    interval_sec: int | None = None
    github_repo: str | None = None
    github_filter: dict | None = None
    poll_interval_sec: int | None = None
    action_kind: str | None = None
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_flow_yaml: str | None = None
    action_project: str | None = None
    action_extra_args: list[str] | None = None
    on_success: dict | None = None
    on_fail: dict | None = None
    missed_fire_policy: str | None = None
    overlap_policy: str | None = None
    max_runs: int | None = None
    budget_usd: float | None = None
    budget_tokens: int | None = None
    project: str | None = None


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
    # A literal path registered before the /{schedule_id} param route below
    # so "limits" resolves here rather than being captured as a schedule id
    # (routes are matched in registration order — see registry.py).
    from lionagi.studio import config

    from ..scheduler.engine import scheduler

    return {
        "max_scheduled_concurrent": config.MAX_SCHEDULED_CONCURRENT,
        "current_inflight": scheduler._global_inflight,
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
async def create_schedule_route(body: CreateScheduleRequest) -> dict[str, Any]:
    try:
        return await create_schedule(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@studio_route(
    "/schedules/{schedule_id}",
    method="PATCH",
    area="schedules",
    name="update_schedule",
)
async def update_schedule_route(schedule_id: str, body: UpdateScheduleRequest) -> dict[str, Any]:
    # exclude_unset (not exclude_none): a field the client never mentioned is
    # left untouched, while a field explicitly sent as null is passed through
    # so update_schedule can clear it (where the column allows) or reject it
    # (where it doesn't). exclude_none would silently drop explicit nulls,
    # making an all-null PATCH indistinguishable from an empty one.
    fields = body.model_dump(exclude_unset=True)
    try:
        ok = await update_schedule(schedule_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True}


@studio_route(
    "/schedules/{schedule_id}",
    method="DELETE",
    area="schedules",
    name="delete_schedule",
)
async def delete_schedule_route(schedule_id: str) -> dict[str, Any]:
    ok = await delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True}


@studio_route(
    "/schedules/{schedule_id}/enable",
    method="POST",
    area="schedules",
    name="enable_schedule",
)
async def enable_schedule_route(schedule_id: str) -> dict[str, Any]:
    try:
        ok = await enable_schedule(schedule_id)
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
async def disable_schedule_route(schedule_id: str) -> dict[str, Any]:
    ok = await disable_schedule(schedule_id)
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
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = await list_schedule_runs(schedule_id, status=status, limit=limit, offset=offset)
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
