# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Versioned ScheduleSet declaration models and the atomic validate/diff/apply service.

A ScheduleSet is a versioned document: one trigger plus one typed run target per
member, closed schemas throughout, and declaration-time static resolution of
every property the fire-time engine would otherwise have to guess at (cwd,
timezone, agent model, flow/playbook content, command allowlist membership).
Applying a set reconciles it against the rows it owns -- CREATE / UPDATE /
UNCHANGED / DISABLE -- atomically: any invalid member means zero writes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lionagi._paths import _find_git_root
from lionagi.state.db import SCHEDULE_RUN_TERMINAL_STATUSES, StateDB

SPEC_VERSION = "lionagi.io/v1alpha1"
SPEC_KIND = "ScheduleSet"

_NOTIFY_ALLOWED_STATUSES = frozenset(SCHEDULE_RUN_TERMINAL_STATUSES) | {"completed"}


# ---------------------------------------------------------------------------
# Trigger models
# ---------------------------------------------------------------------------


class CronTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str
    timezone: str


class GithubTriggerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    filter: dict[str, Any] | None = None


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron: CronTrigger | None = None
    every: str | None = None
    at: str | None = None
    github: GithubTriggerSpec | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Trigger:
        set_fields = [f for f in ("cron", "every", "at", "github") if getattr(self, f) is not None]
        if len(set_fields) != 1:
            raise ValueError(
                f"trigger must set exactly one of cron/every/at/github, got {set_fields or 'none'}"
            )
        return self


# ---------------------------------------------------------------------------
# Target models (discriminated by 'kind')
# ---------------------------------------------------------------------------


class AgentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["agent"]
    profile: str
    prompt: str
    model: str | None = None


class FlowTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["flow"]
    file: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class PlaybookTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["playbook"]
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class CommandTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["command"]
    executable: str
    args: list[str] = Field(default_factory=list)


Target = Annotated[
    AgentTarget | FlowTarget | PlaybookTarget | CommandTarget,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Execution / policies / notify
# ---------------------------------------------------------------------------


class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cwd: str | None = None
    project: str | None = None


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usd: float | None = None
    tokens: int | None = None

    @model_validator(mode="after")
    def _positive(self) -> Budget:
        if self.usd is not None and self.usd <= 0:
            raise ValueError(f"policies.budget.usd must be positive, got {self.usd!r}")
        if self.tokens is not None and self.tokens <= 0:
            raise ValueError(f"policies.budget.tokens must be positive, got {self.tokens!r}")
        return self


class Policies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    missedFire: Literal["skip", "run_once"] = "skip"  # noqa: N815 - matches the YAML document's camelCase field
    overlap: Literal["skip", "allow"] = "skip"
    maxRuns: int | None = None  # noqa: N815 - matches the YAML document's camelCase field
    budget: Budget | None = None
    rateLimit: dict[str, Any] | None = None  # noqa: N815 - matches the YAML document's camelCase field

    @model_validator(mode="after")
    def _positive_max_runs(self) -> Policies:
        if self.maxRuns is not None and self.maxRuns < 1:
            raise ValueError(f"policies.maxRuns must be a positive integer, got {self.maxRuns!r}")
        return self


class Notify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: list[str] = Field(default_factory=list)
    command: str

    @model_validator(mode="after")
    def _known_statuses(self) -> Notify:
        unknown = sorted(set(self.on) - _NOTIFY_ALLOWED_STATUSES)
        if unknown:
            raise ValueError(
                f"notify.on has unknown status value(s) {unknown}; allowed: "
                f"{sorted(_NOTIFY_ALLOWED_STATUSES)}"
            )
        return self


# ---------------------------------------------------------------------------
# Schedule member / set document
# ---------------------------------------------------------------------------


class ScheduleMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    enabled: bool = True
    trigger: Trigger
    target: Target
    execution: Execution = Field(default_factory=Execution)
    policies: Policies = Field(default_factory=Policies)
    notify: Notify | None = None


class ScheduleSetMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    project: str
    scope: Literal["global"] | None = None


class ScheduleSetDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiVersion: Literal["lionagi.io/v1alpha1"]  # noqa: N815 - matches the YAML document's camelCase field
    kind: Literal["ScheduleSet"]
    metadata: ScheduleSetMetadata
    schedules: dict[str, ScheduleMember]


class ScheduleSetError(ValueError):
    """Aggregated per-member validation failure -- callers must write nothing."""

    def __init__(self, errors: list[tuple[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{name}: {message}" for name, message in errors))


def parse_schedule_set(text: str, *, source: str = "<string>") -> ScheduleSetDocument:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{source}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{source}: must be a YAML mapping (dict), got {type(data).__name__}")
    return ScheduleSetDocument.model_validate(data)


# ---------------------------------------------------------------------------
# Trigger resolution
# ---------------------------------------------------------------------------

_EVERY_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_EVERY_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EVERY_MAX_SECONDS = 30 * 86400


def _parse_every(raw: str) -> int:
    m = _EVERY_RE.match(raw.strip())
    if not m:
        raise ValueError(
            f"trigger.every must be a strict positive duration like '30s'/'15m'/'6h'/'2d', got {raw!r}"
        )
    seconds = int(m.group(1)) * _EVERY_UNIT_SECONDS[m.group(2)]
    if seconds <= 0:
        raise ValueError(f"trigger.every must be strictly positive, got {raw!r}")
    if seconds > _EVERY_MAX_SECONDS:
        raise ValueError(f"trigger.every exceeds the maximum bound of 30d, got {raw!r}")
    return seconds


def _parse_at(raw: str) -> datetime:
    s = raw.strip()
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(f"trigger.at must be RFC 3339, got {raw!r} ({exc})") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"trigger.at must include an explicit UTC offset (e.g. '+00:00' or 'Z'), got {raw!r}"
        )
    return dt


def _resolve_trigger(trigger: Trigger) -> dict[str, Any]:
    if trigger.cron is not None:
        from croniter import croniter

        if not croniter.is_valid(trigger.cron.expression):
            raise ValueError(f"trigger.cron.expression is invalid: {trigger.cron.expression!r}")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(trigger.cron.timezone)
        except Exception as exc:  # noqa: BLE001 - surfaced as a ValueError to the caller
            raise ValueError(
                f"trigger.cron.timezone is not a valid IANA timezone: {trigger.cron.timezone!r} ({exc})"
            ) from exc
        return {
            "kind": "cron",
            "expression": trigger.cron.expression,
            "timezone": trigger.cron.timezone,
        }
    if trigger.every is not None:
        return {"kind": "every", "interval_sec": _parse_every(trigger.every), "raw": trigger.every}
    if trigger.at is not None:
        dt = _parse_at(trigger.at)
        return {"kind": "at", "at": dt.isoformat()}
    if trigger.github is not None:
        from lionagi.studio.services.schedules import (
            _svc_validate_github_filter,
            _svc_validate_github_repo,
        )

        _svc_validate_github_repo(trigger.github.repo)
        _svc_validate_github_filter(trigger.github.filter)
        return {"kind": "github", "repo": trigger.github.repo, "filter": trigger.github.filter}
    raise ValueError("trigger must set exactly one of cron/every/at/github")  # pragma: no cover


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _digest_of(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _resolve_path(raw: str, manifest_dir: Path) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = manifest_dir / p
    return p.resolve()


def _resolve_agent_target(target: AgentTarget) -> dict[str, Any]:
    from lionagi.cli._providers import load_agent_profile

    try:
        profile = load_agent_profile(target.profile)
    except FileNotFoundError as exc:
        raise ValueError(f"target.agent profile {target.profile!r} does not exist: {exc}") from exc

    resolved_model = target.model or profile.model
    if not resolved_model:
        raise ValueError(
            f"target.agent profile {target.profile!r} has no default model and target.model "
            "was not set -- a v1 schedule must resolve to a concrete model at declaration time"
        )
    profile_digest = _digest_of(
        {
            "model": profile.model,
            "effort": profile.effort,
            "role": profile.extra.get("role"),
            "yolo": profile.yolo,
            "bypass": profile.bypass,
            "fast_mode": profile.fast_mode,
        }
    )
    return {
        "kind": "agent",
        "profile": target.profile,
        "prompt": target.prompt,
        "model": resolved_model,
        "profile_digest": profile_digest,
    }


def _resolve_flow_target(target: FlowTarget, manifest_dir: Path) -> dict[str, Any]:
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    path = _resolve_path(target.file, manifest_dir)
    if not path.is_file():
        raise ValueError(f"target.flow file not found: {path}")
    content = path.read_text()
    err = _validate_flow_yaml_spec(content)
    if err:
        raise ValueError(f"target.flow file {path} failed validation: {err}")
    return {
        "kind": "flow",
        "file": str(path),
        "inputs": target.inputs,
        "content_digest": _digest_of(content),
    }


def _resolve_playbook_target(target: PlaybookTarget) -> dict[str, Any]:
    from lionagi.studio.scheduler.subprocess import _validate_identifier

    _validate_identifier(target.name, "target.playbook.name")
    return {"kind": "playbook", "name": target.name, "args": target.args}


def _resolve_command_target(target: CommandTarget) -> dict[str, Any]:
    from lionagi.studio.scheduler.subprocess import (
        _validate_action_command,
        _validate_command_allowlisted,
        _validate_extra_args,
    )

    _validate_action_command(target.executable)
    _validate_command_allowlisted(target.executable)
    _validate_extra_args(target.args)
    return {"kind": "command", "executable": target.executable, "args": list(target.args)}


def _resolve_target(target: Target, manifest_dir: Path) -> dict[str, Any]:
    if isinstance(target, AgentTarget):
        return _resolve_agent_target(target)
    if isinstance(target, FlowTarget):
        return _resolve_flow_target(target, manifest_dir)
    if isinstance(target, PlaybookTarget):
        return _resolve_playbook_target(target)
    if isinstance(target, CommandTarget):
        return _resolve_command_target(target)
    raise ValueError(f"unsupported target kind: {target!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# cwd / project resolution
# ---------------------------------------------------------------------------


def _resolve_cwd(execution: Execution, *, manifest_dir: Path, is_global: bool) -> Path:
    if execution.cwd:
        cwd = _resolve_path(execution.cwd, manifest_dir)
        if not cwd.is_dir():
            raise ValueError(f"execution.cwd does not exist or is not a directory: {cwd}")
        return cwd
    if is_global:
        raise ValueError("global-scope schedules require an explicit execution.cwd on every member")
    root = _find_git_root(manifest_dir) or manifest_dir
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"detected project root does not exist: {root}")
    return root


# ---------------------------------------------------------------------------
# Resolved member + full-set resolution
# ---------------------------------------------------------------------------


@dataclass
class ResolvedMember:
    qualified_name: str
    member_name: str
    authored: dict[str, Any]
    resolved: dict[str, Any]
    digest: str
    cwd: str
    timezone: str | None
    db_fields: dict[str, Any] = field(default_factory=dict)


_TARGET_KIND_TO_ACTION_KIND = {
    "agent": "agent",
    "flow": "flow",
    "playbook": "play",
    "command": "command",
}


def _to_db_fields(
    resolved_trigger: dict[str, Any],
    resolved_target: dict[str, Any],
    member: ScheduleMember,
    cwd: Path,
    project: str | None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "action_kind": _TARGET_KIND_TO_ACTION_KIND[resolved_target["kind"]],
        "action_cwd": str(cwd),
        "action_project": project,
        "enabled": 1 if member.enabled else 0,
        "missed_fire_policy": member.policies.missedFire,
        "overlap_policy": member.policies.overlap,
        "max_runs": member.policies.maxRuns,
        "budget_usd": member.policies.budget.usd if member.policies.budget else None,
        "budget_tokens": member.policies.budget.tokens if member.policies.budget else None,
        "rate_limit": member.policies.rateLimit,
        "description": member.description,
        # Cleared up-front so a re-apply that changes trigger/target kind
        # doesn't leave a stale value from the previous kind behind.
        "cron_expr": None,
        "interval_sec": None,
        "github_repo": None,
        "github_filter": None,
        "action_model": None,
        "action_prompt": None,
        "action_agent": None,
        "action_playbook": None,
        "action_command": None,
        "action_command_args": [],
    }

    trigger_kind = resolved_trigger["kind"]
    if trigger_kind == "cron":
        fields["trigger_type"] = "cron"
        fields["cron_expr"] = resolved_trigger["expression"]
    elif trigger_kind == "every":
        fields["trigger_type"] = "interval"
        fields["interval_sec"] = resolved_trigger["interval_sec"]
    elif trigger_kind == "at":
        fields["trigger_type"] = "at"
        fields["cron_expr"] = None
    elif trigger_kind == "github":
        fields["trigger_type"] = "github_poll"
        fields["github_repo"] = resolved_trigger["repo"]
        fields["github_filter"] = resolved_trigger.get("filter")

    if resolved_target["kind"] == "agent":
        fields["action_model"] = resolved_target["model"]
        fields["action_prompt"] = resolved_target["prompt"]
        fields["action_agent"] = resolved_target["profile"]
    elif resolved_target["kind"] == "playbook":
        fields["action_playbook"] = resolved_target["name"]
    elif resolved_target["kind"] == "command":
        fields["action_command"] = resolved_target["executable"]
        fields["action_command_args"] = resolved_target["args"]

    return fields


def resolve_member(
    name: str,
    member: ScheduleMember,
    *,
    manifest_dir: Path,
    project: str,
    is_global: bool,
) -> ResolvedMember:
    qualified_name = f"{'global' if is_global else project}/{name}"
    resolved_trigger = _resolve_trigger(member.trigger)
    resolved_target = _resolve_target(member.target, manifest_dir)
    cwd = _resolve_cwd(member.execution, manifest_dir=manifest_dir, is_global=is_global)
    resolved_project = member.execution.project or (None if is_global else project)

    resolved = {
        "trigger": resolved_trigger,
        "target": resolved_target,
        "execution": {"cwd": str(cwd), "project": resolved_project},
        "policies": member.policies.model_dump(mode="json"),
        "notify": member.notify.model_dump(mode="json") if member.notify else None,
        "enabled": member.enabled,
    }
    digest = _digest_of(resolved)
    authored = member.model_dump(mode="json")
    db_fields = _to_db_fields(resolved_trigger, resolved_target, member, cwd, resolved_project)

    return ResolvedMember(
        qualified_name=qualified_name,
        member_name=name,
        authored=authored,
        resolved=resolved,
        digest=digest,
        cwd=str(cwd),
        timezone=resolved_trigger.get("timezone"),
        db_fields=db_fields,
    )


def resolve_schedule_set(doc: ScheduleSetDocument, manifest_dir: Path) -> dict[str, ResolvedMember]:
    """Resolve every member. Raises ``ScheduleSetError`` aggregating every
    failing member's message -- callers must not write anything on error."""
    is_global = doc.metadata.scope == "global"
    resolved: dict[str, ResolvedMember] = {}
    errors: list[tuple[str, str]] = []
    for name, member in doc.schedules.items():
        try:
            resolved[name] = resolve_member(
                name,
                member,
                manifest_dir=manifest_dir,
                project=doc.metadata.project,
                is_global=is_global,
            )
        except ValueError as exc:
            errors.append((name, str(exc)))
    if errors:
        raise ScheduleSetError(errors)
    return resolved


# ---------------------------------------------------------------------------
# Diff / apply
# ---------------------------------------------------------------------------


@dataclass
class PlanEntry:
    qualified_name: str
    action: str  # CREATE | UPDATE | UNCHANGED | DISABLE | ERROR
    detail: str | None = None
    resolved: ResolvedMember | None = None
    existing_id: str | None = None


ADOPT_NOT_SUPPORTED_MESSAGE = (
    "--adopt is not yet supported: migrating a same-named row from another "
    "owner (another ScheduleSet or a CLI quick-create) into this set "
    "requires an explicit adoption path that does not exist yet. Rename "
    "the member or resolve the ownership conflict out of band."
)


async def build_plan(
    db: StateDB,
    doc: ScheduleSetDocument,
    manifest_dir: Path,
    *,
    adopt: bool = False,
) -> tuple[list[PlanEntry], dict[str, ResolvedMember]]:
    """Resolve every member and diff against the rows this set currently owns.

    Raises ``ScheduleSetError`` (zero writes performed) if any member fails
    static resolution. Ownership collisions surface as ``ERROR`` plan
    entries rather than raising, so callers can report the whole plan.
    """
    owner_key = f"{doc.metadata.project}/{doc.metadata.name}"
    resolved_members = resolve_schedule_set(doc, manifest_dir)

    existing_owned = {row["name"]: row for row in await db.list_schedules_by_owner_key(owner_key)}

    plan: list[PlanEntry] = []
    seen_qualified: set[str] = set()
    for resolved in resolved_members.values():
        qualified = resolved.qualified_name
        seen_qualified.add(qualified)
        existing_row = await db.get_schedule_by_name(qualified)
        if existing_row is None:
            plan.append(PlanEntry(qualified, "CREATE", resolved=resolved))
            continue
        if existing_row.get("owner_key") != owner_key:
            if adopt:
                plan.append(PlanEntry(qualified, "ERROR", detail=ADOPT_NOT_SUPPORTED_MESSAGE))
            else:
                owner_desc = existing_row.get("owner_key") or (
                    "a CLI quick-create"
                    if existing_row.get("managed_by") == "cli"
                    else "a legacy schedule"
                )
                plan.append(
                    PlanEntry(
                        qualified,
                        "ERROR",
                        detail=(
                            f"{qualified} is already owned by {owner_desc}, not by "
                            f"{owner_key}; use --adopt to migrate it explicitly"
                        ),
                    )
                )
            continue
        if existing_row.get("resolved_digest") == resolved.digest:
            plan.append(
                PlanEntry(qualified, "UNCHANGED", resolved=resolved, existing_id=existing_row["id"])
            )
        else:
            plan.append(
                PlanEntry(qualified, "UPDATE", resolved=resolved, existing_id=existing_row["id"])
            )

    for name, row in existing_owned.items():
        if name not in seen_qualified:
            plan.append(PlanEntry(name, "DISABLE", existing_id=row["id"]))

    return plan, resolved_members


@dataclass
class ApplyResult:
    plan: list[PlanEntry]
    created: int
    updated: int
    unchanged: int
    disabled: int


async def apply_schedule_set(
    db: StateDB,
    doc: ScheduleSetDocument,
    manifest_dir: Path,
    *,
    adopt: bool = False,
) -> ApplyResult:
    """Validate + diff + commit atomically. Any ERROR plan entry (invalid
    member, or an ownership collision) raises and writes nothing."""
    owner_key = f"{doc.metadata.project}/{doc.metadata.name}"
    plan, _resolved = await build_plan(db, doc, manifest_dir, adopt=adopt)

    errors = [(e.qualified_name, e.detail or "") for e in plan if e.action == "ERROR"]
    if errors:
        raise ScheduleSetError(errors)

    now = time.time()
    creates: list[dict[str, Any]] = []
    updates: list[tuple[str, dict[str, Any]]] = []
    disables: list[str] = []

    for entry in plan:
        if entry.action == "CREATE":
            resolved = entry.resolved
            creates.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "name": entry.qualified_name,
                    "spec_version": SPEC_VERSION,
                    "managed_by": "declaration",
                    "owner_key": owner_key,
                    "authored_spec": resolved.authored,
                    "resolved_target": resolved.resolved,
                    "resolved_digest": resolved.digest,
                    "resolved_timezone": resolved.timezone,
                    "created_at": now,
                    "updated_at": now,
                    **resolved.db_fields,
                }
            )
        elif entry.action == "UPDATE":
            resolved = entry.resolved
            updates.append(
                (
                    entry.existing_id,
                    {
                        "spec_version": SPEC_VERSION,
                        "authored_spec": resolved.authored,
                        "resolved_target": resolved.resolved,
                        "resolved_digest": resolved.digest,
                        "resolved_timezone": resolved.timezone,
                        **resolved.db_fields,
                    },
                )
            )
        elif entry.action == "DISABLE":
            disables.append(entry.existing_id)

    await db.apply_schedule_set(creates=creates, updates=updates, disables=disables)

    return ApplyResult(
        plan=plan,
        created=sum(1 for e in plan if e.action == "CREATE"),
        updated=sum(1 for e in plan if e.action == "UPDATE"),
        unchanged=sum(1 for e in plan if e.action == "UNCHANGED"),
        disabled=sum(1 for e in plan if e.action == "DISABLE"),
    )
