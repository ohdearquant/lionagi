# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 schedules service — backs /api/schedules endpoints."""

from __future__ import annotations

import time
import uuid
from typing import Any

from lionagi.state.db import DEFAULT_DB_PATH, StateDB

_VALID_EFFORT_LEVELS: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_PRESERVE_DASHED: frozenset[str] = frozenset({"argument-hint"})

# Re-use the same validation helpers as subprocess.py so the service boundary
# and the spawn boundary enforce identical rules.  Imported lazily inside the
# validation helpers to avoid circular imports at module load time.


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
    return rows


async def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        row = await db.get_schedule(schedule_id)
        if not row:
            return None
        runs = await db.list_schedule_runs(schedule_id, limit=10)
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

    # Reject flag-injection vectors at write time so bad specs never reach storage.
    _svc_validate_action_model(data.get("action_model"))
    _svc_validate_prompt(data.get("action_prompt"))
    _svc_validate_identifier(data.get("action_agent"), "action_agent")
    _svc_validate_identifier(data.get("action_project"), "action_project")
    _svc_validate_identifier(data.get("action_playbook"), "action_playbook")
    _svc_validate_extra_args(data.get("action_extra_args"))
    _svc_validate_github_repo(data.get("github_repo"))

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
    if not fields:
        return False
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False

        # Reject flag-injection vectors in the patched fields before writing.
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

        # Merge proposed fields over the existing schedule and re-validate
        # flow_yaml constraints so a PATCH cannot create invalid state that
        # would only surface as a silent empty-YAML write at fire time.
        effective = {**schedule, **fields}
        if effective.get("action_kind") == "flow_yaml":
            yaml_text = effective.get("action_flow_yaml") or ""
            if not yaml_text.strip():
                raise ValueError(
                    "action_flow_yaml is required and must not be empty for action_kind='flow_yaml'"
                )
            spec_err = _validate_flow_yaml_spec(yaml_text)
            if spec_err:
                raise ValueError(f"Invalid flow_yaml spec: {spec_err}")

        await db.update_schedule(schedule_id, **fields)
    return True


async def delete_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        return await db.delete_schedule(schedule_id)


async def enable_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        await db.update_schedule(schedule_id, enabled=1)
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
            cur = await db.db.execute(
                "SELECT * FROM schedule_runs WHERE chain_parent_id = ? ORDER BY chain_depth, fired_at",
                (run_id,),
            )
            rows = await cur.fetchall()
            run["chain_children"] = [db._row_to_dict(r) for r in rows]
    return run
