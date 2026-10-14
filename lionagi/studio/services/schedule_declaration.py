# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Versioned ScheduleSet declaration models and the atomic validate/diff/apply service.

A ScheduleSet is a versioned document: one trigger plus one typed run target per
member, closed schemas throughout, and declaration-time static resolution of
every property the fire-time engine would otherwise have to guess at (cwd,
timezone, agent model, flow/playbook content, command allowlist membership).
Applying a set reconciles it against the rows it owns -- CREATE / UPDATE /
UNCHANGED / DISABLE -- atomically: any invalid member means zero writes.
Unowned rows whose declared behavior already matches can be adopted in place.
"""

from __future__ import annotations

import collections.abc
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
from lionagi.state.db import (
    INVOCATION_TERMINAL_STATUSES,
    ScheduleOwnershipConflictError,
    StateDB,
)

SPEC_VERSION = "lionagi.io/v1alpha1"
SPEC_KIND = "ScheduleSet"

# notify fires on the spawned invocation's own terminal transition (see
# _register_schedule_notify in scheduler/engine.py), so the status filter is
# validated against the invocation vocabulary, not the schedule_run one.
_NOTIFY_ALLOWED_STATUSES = frozenset(INVOCATION_TERMINAL_STATUSES)


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

    @model_validator(mode="before")
    @classmethod
    def _valid(cls, data: Any) -> Any:
        # Reuse the service-boundary checks rather than forking them, and run
        # them on the RAW values: pydantic coercion would otherwise admit
        # bool/whole-float/numeric-string tokens the shared validator rejects.
        from lionagi.studio.services.schedules import (
            _svc_validate_budget_tokens,
            _svc_validate_budget_usd,
        )

        if isinstance(data, dict):
            try:
                _svc_validate_budget_usd(data.get("usd"))
                _svc_validate_budget_tokens(data.get("tokens"))
            except ValueError as exc:
                raise ValueError(f"policies.budget: {exc}") from exc
        return data


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

    @model_validator(mode="after")
    def _valid_rate_limit(self) -> Policies:
        # Same validator the fire-time engine admission path uses
        # (lionagi.studio.scheduler.admit.validate_rate_limit) -- an open
        # dict[str, Any] would otherwise let a malformed rateLimit commit
        # and only fail (or silently no-op) at fire time.
        from lionagi.studio.scheduler.admit import validate_rate_limit

        try:
            validate_rate_limit(self.rateLimit)
        except ValueError as exc:
            raise ValueError(f"policies.rateLimit: {exc}") from exc
        return self


class Notify(BaseModel):
    """Registers the existing run terminal-callback machinery on the
    invocation this schedule spawns, filtered to `on`. Replaces on_fail --
    follow-up work belongs in a flow, not a notification command."""

    model_config = ConfigDict(extra="forbid")
    on: list[str]
    command: str

    @model_validator(mode="after")
    def _valid_on(self) -> Notify:
        if not self.on:
            raise ValueError("notify.on must not be empty")
        unknown = sorted(set(self.on) - _NOTIFY_ALLOWED_STATUSES)
        if unknown:
            raise ValueError(
                f"notify.on has unknown status value(s) {unknown}; allowed: "
                f"{sorted(_NOTIFY_ALLOWED_STATUSES)}"
            )
        if len(self.on) != len(set(self.on)):
            raise ValueError(f"notify.on must not contain duplicate statuses, got {self.on!r}")
        return self

    @model_validator(mode="after")
    def _valid_command(self) -> Notify:
        if not self.command.strip():
            raise ValueError("notify.command must not be empty")
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


class _ScheduleSetLoader(yaml.SafeLoader):
    """SafeLoader variant scoped to this parse boundary only: a mapping key
    that YAML 1.1's *implicit* bool resolver would collapse (bare on/off/
    yes/no/true/false) keeps its original text instead; values are
    untouched. Without this, `notify:\n  on: [...]` parses its `on` key as
    `True`, and the closed-schema rejection names `True` instead of pointing
    at the quoting workaround. A key with an *explicit* bool tag
    (`!!bool on:`) is left alone -- explicitness is tracked by stamping
    each composed node with whether the source scalar event carried a tag.
    """

    def compose_scalar_node(self, anchor):
        explicit_tag = self.peek_event().tag is not None
        node = super().compose_scalar_node(anchor)
        node.lionagi_explicit_tag = explicit_tag
        return node

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:bool" and not self._is_explicitly_tagged(
                key_node
            ):
                key = key_node.value
            else:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, collections.abc.Hashable):
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "found unhashable key",
                        key_node.start_mark,
                    )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping

    @staticmethod
    def _is_explicitly_tagged(node: yaml.Node) -> bool:
        """True if the source wrote a tag for this node (recorded at compose
        time; see class docstring)."""
        return getattr(node, "lionagi_explicit_tag", False)


def parse_schedule_set(text: str, *, source: str = "<string>") -> ScheduleSetDocument:
    try:
        # _ScheduleSetLoader subclasses SafeLoader and only overrides key
        # construction -- no unsafe tag resolution is added.
        data = yaml.load(text, Loader=_ScheduleSetLoader)  # noqa: S506
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
    # RFC 3339 mandates 'T' (or lowercase 't') between the date and time;
    # datetime.fromisoformat() also accepts a plain space there (a common
    # ISO 8601 variant), which would silently admit a non-conformant value.
    if len(s) < 11 or s[10] not in ("T", "t"):
        raise ValueError(f"trigger.at must be RFC 3339 with a 'T' date/time separator, got {raw!r}")
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
        return {"kind": "at", "at": dt.isoformat(), "epoch": dt.timestamp()}
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
    """Resolve a manifest-relative path to an absolute one. A manifest may
    write relative paths, unlike a stored schedule row: they mean "relative
    to this manifest", a location that exists, not "relative to wherever the
    daemon started". The declarative path converts to absolute here instead
    of consulting ``_is_usable_execution_root`` before writing
    ``action_cwd``, so what reaches storage already satisfies the
    predicate; ``Path.resolve()`` always returns absolute even when
    *manifest_dir* is itself relative."""
    p = Path(raw)
    if not p.is_absolute():
        p = manifest_dir / p
    return p.resolve()


def _resolve_agent_target(target: AgentTarget, cwd: Path) -> dict[str, Any]:
    from lionagi.cli._providers import load_agent_profile

    # The launched child reads its profile from its own working directory, so
    # the profile recorded here is the one found from the execution root.
    try:
        profile = load_agent_profile(target.profile, cwd=cwd)
    except FileNotFoundError as exc:
        raise ValueError(
            f"target.agent profile {target.profile!r} does not exist (searched from {cwd}): {exc}"
        ) from exc

    resolved_model = target.model or profile.model
    if not resolved_model:
        raise ValueError(
            f"target.agent profile {target.profile!r} has no default model and target.model "
            "was not set -- a v1 schedule must resolve to a concrete model at declaration time"
        )
    # The stored model and prompt are passed to the launched child as arguments, and
    # the launch refuses a model outside the model identifier character set and a
    # prompt it cannot pass through. A row holding either would fail at every fire,
    # so refuse it here instead.
    from lionagi.studio.scheduler.subprocess import _validate_action_model, _validate_prompt

    source = "target.model" if target.model else f"profile {target.profile!r}"
    if not isinstance(resolved_model, str):
        raise ValueError(
            f"target.agent model {resolved_model!r} (from {source}) is not a model name. "
            "Set target.model to a model the launch accepts."
        )
    try:
        _validate_action_model(resolved_model)
    except ValueError as exc:
        raise ValueError(
            f"target.agent model {resolved_model!r} (from {source}) cannot be passed to a "
            f"scheduled launch: {exc} Set target.model to a model the launch accepts."
        ) from exc
    try:
        _validate_prompt(target.prompt)
    except ValueError as exc:
        raise ValueError(f"target.prompt cannot be passed to a scheduled launch: {exc}") from exc
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

    if target.inputs:
        # The flow_yaml launch path (li o flow -f <snapshot>) takes no
        # positionals and rejects extra args -- there is no field in the
        # flow spec format (or _run_flow's kwargs surface) for a separate
        # "inputs" mapping to land in. Fail closed at declaration time
        # rather than silently dropping the declared inputs at fire time.
        raise ValueError(
            "target.flow.inputs is not supported: the flow_yaml launch path has no "
            "positionals or spec field to merge extra inputs into. Fold the values "
            "directly into the flow YAML file's own top-level fields instead."
        )
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
        "content": content,
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
    )
    from lionagi.studio.services.schedules import _svc_validate_command_args

    _validate_action_command(target.executable)
    _validate_command_allowlisted(target.executable)
    # Persisted as action_command_args, held to that field's contract, not
    # action_extra_args' leading-'-' rejection: a literal `--repo` here is
    # author-written, the point of a command runner. Flag safety for a
    # `{{var}}` element is enforced against the rendered value at fire time.
    _svc_validate_command_args(list(target.args))
    return {"kind": "command", "executable": target.executable, "args": list(target.args)}


def _resolve_target(target: Target, manifest_dir: Path, cwd: Path) -> dict[str, Any]:
    if isinstance(target, AgentTarget):
        return _resolve_agent_target(target, cwd)
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
    # Declaration flow targets always launch via the flow_yaml path (the
    # validated file snapshot captured at resolution time), never
    # action_kind='flow' -- that kind builds `li o flow -- <model> <prompt>`
    # positionals, which have nothing to do with a target.flow file.
    "flow": "flow_yaml",
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
        "notify_on": member.notify.on if member.notify else None,
        "notify_command": member.notify.command if member.notify else None,
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
        "action_flow_yaml": None,
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
        # The fire-time engine has no notion of "at" beyond a single due
        # instant: persist the resolved epoch so the row is due exactly
        # once, and force max_runs=1 so the claim-before-fire gate
        # (SchedulerEngine._reserve_max_runs_budget) refuses a second fire
        # after the first one runs.
        #
        # It does not cover a fire that never ran: a miss under
        # missed_fire_policy=skip records "skipped", which is neither status.
        # apply_schedule_set closes that by declining to write a past instant
        # back. Counting "skipped" would not: capacity and overlap skips share
        # the status and are meant to retry. See ADR-0070 D2.
        fields["next_fire_at"] = resolved_trigger["epoch"]
        fields["max_runs"] = 1
    elif trigger_kind == "github":
        fields["trigger_type"] = "github_poll"
        fields["github_repo"] = resolved_trigger["repo"]
        fields["github_filter"] = resolved_trigger.get("filter")

    if resolved_target["kind"] == "agent":
        fields["action_model"] = resolved_target["model"]
        fields["action_prompt"] = resolved_target["prompt"]
        fields["action_agent"] = resolved_target["profile"]
    elif resolved_target["kind"] == "flow":
        fields["action_flow_yaml"] = resolved_target["content"]
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
    project: str | None,
    is_global: bool,
) -> ResolvedMember:
    qualified_name = f"{'global' if is_global else project}/{name}"
    resolved_trigger = _resolve_trigger(member.trigger)
    cwd = _resolve_cwd(member.execution, manifest_dir=manifest_dir, is_global=is_global)
    resolved_target = _resolve_target(member.target, manifest_dir, cwd)
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


def _declared_columns(resolved: ResolvedMember) -> dict[str, Any]:
    """Every schedule column written for a resolved declaration member."""
    return {
        "spec_version": SPEC_VERSION,
        "authored_spec": resolved.authored,
        "resolved_target": resolved.resolved,
        "resolved_digest": resolved.digest,
        "resolved_timezone": resolved.timezone,
        **resolved.db_fields,
    }


async def create_quick_schedule(
    db: StateDB,
    name: str,
    member: ScheduleMember,
    *,
    cwd: Path,
    project: str | None,
) -> tuple[str, ResolvedMember]:
    """Compile + persist one `li schedule create <kind>` quick-create row
    through the exact same static-resolution path (``resolve_member``) a
    ``ScheduleSet`` member uses -- there is no second, forked validator.

    Persisted with ``managed_by='cli'`` and no ``owner_key``, so a later
    ``ScheduleSet apply`` targeting the same qualified name stops at the
    ownership check in ``build_plan()`` unless it runs with ``--adopt`` and
    declares the row's behavior exactly. Raises ``ScheduleSetError`` (zero
    writes) on a resolution failure or a name collision.
    """
    qualified_name = f"{project}/{name}" if project else name
    try:
        resolved = resolve_member(name, member, manifest_dir=cwd, project=project, is_global=False)
    except ValueError as exc:
        raise ScheduleSetError([(qualified_name, str(exc))]) from exc

    existing = await db.get_schedule_by_name(qualified_name)
    if existing is not None:
        raise ScheduleSetError(
            [
                (
                    qualified_name,
                    f"a schedule named {qualified_name!r} already exists "
                    f"(id={existing['id']}, managed_by={existing.get('managed_by')!r})",
                )
            ]
        )

    schedule_id = uuid.uuid4().hex[:12]
    now = time.time()
    row = {
        "id": schedule_id,
        "name": qualified_name,
        "managed_by": "cli",
        "owner_key": None,
        "created_at": now,
        "updated_at": now,
        **_declared_columns(resolved),
    }
    await db.create_schedule(row)
    resolved.qualified_name = qualified_name
    return schedule_id, resolved


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
    action: str  # CREATE | UPDATE | UNCHANGED | DISABLE | ADOPT | ERROR
    detail: str | None = None
    resolved: ResolvedMember | None = None
    existing_id: str | None = None
    adopted_from: str | None = None
    claimed_owner_key: str | None = None
    # The row's updated_at when adoption compared it: every schedule write moves
    # it, so the commit refuses a row written to after the comparison.
    claimed_updated_at: float | None = None
    # (stored, declared) action_model when an UPDATE changes it.
    model_change: tuple[str | None, str | None] | None = None


_ADOPT_DECLARATION_FIELDS = frozenset(
    {"spec_version", "authored_spec", "resolved_target", "resolved_digest"}
)
# Columns the scheduler reads that a declaration never writes. A row setting one
# keeps that behavior after adoption and through every later apply while the
# declaration does not show it, so adoption refuses such a row.
_ADOPT_UNDECLARED_COLUMNS = (
    "on_success",
    "on_fail",
    "action_extra_args",
    "threshold_config",
    "poll_interval_sec",
)
# The engine polls a github_poll row every poll_interval_sec, else interval_sec,
# else this many seconds; a poll_interval_sec at this value changes nothing.
_GITHUB_POLL_DEFAULT_SEC = 300
_PROJECT_FLAG_ACTION_KINDS = frozenset({"agent", "flow", "fanout", "flow_yaml"})


def _is_adoptable(row: dict[str, Any]) -> bool:
    """Only rows without an owner may be taken into a declaration set."""
    return row.get("owner_key") is None


async def _adoption_execution_root(
    schedule: dict[str, Any],
) -> tuple[tuple[str, ...], str | None, str]:
    """Resolve the root a schedule subprocess would inherit from the engine."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    try:
        root = await _resolve_action_cwd(schedule)
    except SchedulerCwdInheritRefusedError as exc:
        return ("refused",), None, f"refuses to spawn for configured root {exc.configured_root!r}"
    if root is None:
        return ("daemon-cwd",), None, "inherits the scheduler working directory"
    resolved = str(Path(root).resolve())
    return ("path", resolved), resolved, str(root)


async def _adopt_model_pair(row: dict[str, Any], declared: Any) -> tuple[Any, Any]:
    current = row.get("action_model")
    if current or row.get("action_kind") != "agent" or not row.get("action_agent"):
        return current, declared
    root_key, root_path, _display = await _adoption_execution_root(row)
    if root_key[0] != "path" or root_path is None:
        return current, declared
    from lionagi.cli._providers import load_agent_profile

    try:
        current = load_agent_profile(row["action_agent"], cwd=Path(root_path)).model or current
    except (FileNotFoundError, OSError, ValueError):
        pass
    return current, declared


async def _adopt_project_pair(
    row: dict[str, Any], declared_fields: dict[str, Any]
) -> tuple[Any, Any] | None:
    declared_row = {**row, **declared_fields}
    row_root = await _adoption_execution_root(row)
    declared_root = await _adoption_execution_root(declared_row)
    changed_row: list[str] = []
    changed_declared: list[str] = []

    def root_text(root: tuple[tuple[str, ...], str | None, str]) -> str:
        # A path is quoted; the other roots are described in words.
        return (
            f"execution root {root[2]!r}" if root[0][0] == "path" else f"execution root: {root[2]}"
        )

    if row_root[0] != declared_root[0]:
        changed_row.append(root_text(row_root))
        changed_declared.append(root_text(declared_root))

    async def project_effect(
        schedule: dict[str, Any], root: tuple[tuple[str, ...], str | None, str]
    ) -> tuple[tuple[str, ...], str]:
        if schedule.get("action_kind") not in _PROJECT_FLAG_ACTION_KINDS:
            return ("unused",), "none, the action takes no project argument"
        if root[0][0] == "refused":
            return ("not-launched",), "none, the action does not launch"
        project = schedule.get("action_project")
        if project:
            return ("project", str(project)), f"{project!r}, set explicitly"
        if root[1] is None:
            return ("unknown",), "detected from the scheduler working directory"
        try:
            from lionagi.cli._project import detect_project

            project, _source = detect_project(Path(root[1]))
        except (OSError, RuntimeError, ValueError):
            return ("unknown",), "could not be detected from the execution root"
        if not project:
            return ("project", project), "none detected from the execution root"
        return ("project", project), f"{project!r}, detected from the execution root"

    row_project = await project_effect(row, row_root)
    declared_project = await project_effect(declared_row, declared_root)
    if row_project[0] != declared_project[0]:
        changed_row.append(f"run project: {row_project[1]}")
        changed_declared.append(f"run project: {declared_project[1]}")

    if not changed_row:
        return None
    return "; ".join(changed_row), "; ".join(changed_declared)


def row_cron_timezone(row: dict[str, Any]) -> str | None:
    """The zone a cron row fires in, as far as a process other than the scheduler can tell.

    A stored zone is read the way the scheduler reads it. A row without one is
    read in the scheduler daemon's default zone, which this process need not
    share, so the zone the scheduler last recorded on the row stands for it.
    None when the row stores no zone and no scheduler has recorded one.
    """
    from lionagi.studio.scheduler.engine import resolve_schedule_timezone

    if row.get("resolved_timezone"):
        return resolve_schedule_timezone(row).name
    return row.get("effective_timezone") or None


def _adopt_timezone_pair(row: dict[str, Any], declared: Any) -> tuple[str | None, str]:
    from lionagi.studio.scheduler.engine import resolve_schedule_timezone

    return (
        row_cron_timezone(row),
        resolve_schedule_timezone({**row, "resolved_timezone": declared}).name,
    )


def _adopt_unconfirmed_recorded_zone(row: dict[str, Any]) -> tuple[str, str] | None:
    """(recorded, local) when a zone-less row's recorded zone is not this process's scheduler zone.

    A row without a stored zone fires in the zone of whichever scheduler runs it.
    The zone the scheduler recorded may be from a run that has since stopped, and
    this process may be configured differently from the scheduler, so adoption
    pins a zone only when both say the same one.
    """
    from lionagi.studio.scheduler.engine import resolve_schedule_timezone

    recorded = row.get("effective_timezone")
    if row.get("resolved_timezone") or not recorded:
        return None
    local = resolve_schedule_timezone({"id": row.get("id")}).name
    if recorded == local:
        return None
    return recorded, local


def _adopt_comparable(name: str, schedule: dict[str, Any], value: Any) -> Any:
    """A column value as the scheduler acts on it, for columns where two stored values act alike."""
    if name == "notify_on":
        # No notification is registered for an empty list or for none, and the
        # events are matched as a set, so neither their order nor a repeat changes
        # what fires.
        return sorted(set(value)) if value else None
    if name == "notify_command":
        # A command with no event to run on never runs.
        return value if schedule.get("notify_on") else None
    if name == "cron_expr":
        # Only a cron trigger reads the expression.
        return value if schedule.get("trigger_type") == "cron" else None
    if name in ("github_repo", "github_filter"):
        # Only a github_poll trigger reads the repository and its filter.
        return value if schedule.get("trigger_type") == "github_poll" else None
    if name == "interval_sec":
        trigger_type = schedule.get("trigger_type")
        if trigger_type == "github_poll":
            from lionagi.studio.scheduler.engine import resolve_schedule_cadence_seconds

            return resolve_schedule_cadence_seconds({**schedule, "interval_sec": value})
        # Only an interval or github_poll trigger reads the interval.
        return value if trigger_type == "interval" else None
    if name == "action_cwd" and value:
        # The launch runs in the resolved directory, so two spellings of one
        # directory (a symlink, a trailing slash) act alike.
        return str(Path(value).resolve())
    return value


def _adopt_undeclared_values(row: dict[str, Any]) -> list[tuple[str, Any]]:
    """Columns from ``_ADOPT_UNDECLARED_COLUMNS`` the row sets, with their values."""
    found: list[tuple[str, Any]] = []
    for name in _ADOPT_UNDECLARED_COLUMNS:
        value = row.get(name)
        if name == "poll_interval_sec":
            # Only a github_poll trigger reads it.
            if (
                row.get("trigger_type") == "github_poll"
                and value is not None
                and value != _GITHUB_POLL_DEFAULT_SEC
            ):
                found.append((name, value))
        elif value:
            found.append((name, value))
    return found


async def _adopt_spec_differences(
    row: dict[str, Any], resolved: ResolvedMember
) -> list[tuple[str, Any, Any]]:
    """Declared columns whose behavior or stored value differs on the row."""
    declared_fields = _declared_columns(resolved)
    differences: list[tuple[str, Any, Any]] = []
    for name, declared in sorted(declared_fields.items()):
        if name in _ADOPT_DECLARATION_FIELDS:
            continue
        if name == "resolved_timezone" and (
            row.get("trigger_type") == "cron" and declared_fields.get("trigger_type") == "cron"
        ):
            current, adopted = _adopt_timezone_pair(row, declared)
            if current != adopted:
                differences.append((name, current, adopted))
            continue
        if name == "action_model":
            current, adopted = await _adopt_model_pair(row, declared)
            if current != adopted:
                differences.append((name, current, adopted))
            continue
        if name == "action_project":
            pair = await _adopt_project_pair(row, declared_fields)
            if pair is not None:
                differences.append((name, *pair))
            continue
        if (
            name == "next_fire_at"
            and declared_fields.get("trigger_type") == "at"
            and (declared or 0.0) <= time.time()
        ):
            # The write leaves the row's instant alone once the declared one has passed.
            continue
        current = row.get(name)
        if _adopt_comparable(name, row, current) != _adopt_comparable(
            name, {**row, **declared_fields}, declared
        ):
            differences.append((name, current, declared))
    return differences


async def _adopt_undeclarable(
    row: dict[str, Any], name: str, current: Any, *, is_global: bool
) -> bool:
    """True when no member of this set could declare the row's value for this column."""
    if name == "resolved_timezone":
        if current is None:
            return False  # reported with its own remedy: let the scheduler record a zone
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(current)
        except (ZoneInfoNotFoundError, ValueError):
            return True
        return False
    if name == "interval_sec":
        # A github trigger declares no interval; the scheduler applies its own cadence.
        return row.get("trigger_type") == "github_poll"
    if name == "action_cwd":
        # A member always resolves an execution root.
        return current is None
    if name in ("action_prompt", "action_model") and row.get("action_kind") != "agent":
        # Only an agent member declares a prompt or a model, and it declares the agent kind.
        return current is not None
    if name == "action_model" and current is None:
        # An agent member always resolves a model, its own or its profile's.
        return True
    if name == "action_prompt" and isinstance(current, str):
        # A member's prompt is refused when the launch would refuse it.
        from lionagi.studio.scheduler.subprocess import _validate_prompt

        try:
            _validate_prompt(current)
        except ValueError:
            return True
        return False
    if name == "action_model" and isinstance(current, str):
        # A member's model, its own or its profile's, is refused when the launch would
        # refuse it.
        from lionagi.studio.scheduler.subprocess import _validate_action_model

        try:
            _validate_action_model(current)
        except ValueError:
            return True
        return False
    if name == "action_project":
        root_key, root_path, _display = await _adoption_execution_root(row)
        if root_key[0] != "path" or root_path is None:
            return True
        if is_global or row.get("action_project"):
            return False
        # Every member of a project's set runs with a project, its own or the set's, so a
        # row that sets none and detects none from its root cannot be matched by one.
        try:
            from lionagi.cli._project import detect_project

            project, _source = detect_project(Path(root_path))
        except (OSError, RuntimeError, ValueError):
            return True
        return not project
    return False


async def _adopt_plan_entry(
    row: dict[str, Any],
    resolved: ResolvedMember,
    qualified: str,
    owner_key: str,
    *,
    is_global: bool,
) -> PlanEntry:
    if not _is_adoptable(row):
        return PlanEntry(
            qualified,
            "ERROR",
            detail=(
                f"{row['name']} is owned by ScheduleSet {row['owner_key']!r}; "
                f"--adopt only accepts rows with no owner_key, so it will not move this row"
            ),
        )
    differences = await _adopt_spec_differences(row, resolved)
    undeclared = _adopt_undeclared_values(row)
    target = resolved.resolved["target"]
    play_args = target.get("args") if target.get("kind") == "playbook" else None
    reasons: list[str] = []
    if differences:
        rendered = "; ".join(
            f"{name}: row={current!r} declared={declared!r}"
            for name, current, declared in differences
        )
        # A zone nobody has recorded is not one the member can be edited to match; it has
        # its own remedy below. Any other value no member can declare makes the row
        # unadoptable as it stands, and the member remedy would be one nobody can follow.
        unrecorded_zone = [d for d in differences if d[0] == "resolved_timezone" and d[1] is None]
        undeclarable = [
            name
            for name, current, _ in differences
            if await _adopt_undeclarable(row, name, current, is_global=is_global)
        ]
        if undeclarable:
            remedy = (
                f"; no member can declare the row's {', '.join(undeclarable)}, so this row "
                "cannot be adopted as it stands."
            )
        elif len(unrecorded_zone) == len(differences):
            remedy = "."
        else:
            remedy = "; edit the member to match the row."
        reasons.append(
            f"the declared member differs from the existing row in {len(differences)} "
            f"field(s) -- {rendered}" + remedy
        )
        timezone = next((d for d in differences if d[0] == "resolved_timezone"), None)
        if timezone is not None:
            reasons.append(
                "resolved_timezone compares the zone each side's cron is read in; the row "
                "stores no zone and no scheduler has recorded the zone it fires in, so start "
                "the scheduler once to record it."
                if timezone[1] is None
                else "resolved_timezone compares the zone each side's cron is read in."
            )
    unconfirmed_zone = (
        _adopt_unconfirmed_recorded_zone(row)
        if row.get("trigger_type") == "cron"
        and _declared_columns(resolved).get("trigger_type") == "cron"
        else None
    )
    if unconfirmed_zone is not None:
        recorded, local = unconfirmed_zone
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(recorded)
            remedy = (
                "run the apply with LIONAGI_SCHEDULER_TZ set to the scheduler's zone, or "
                "start the scheduler so it records its zone again."
            )
        except (ZoneInfoNotFoundError, ValueError):
            # This process cannot read its cron in that zone, so naming it here cannot help.
            remedy = (
                f"this host cannot load {recorded!r}, so start the scheduler so it records "
                "its zone again."
            )
        reasons.append(
            f"the row stores no zone, so it fires in the zone of the scheduler that runs it; "
            f"the scheduler last recorded {recorded!r} and this process reads the scheduler "
            f"zone as {local!r}. Adoption stores a zone only when the two agree: {remedy}"
        )
    if undeclared:
        rendered = "; ".join(f"{name}={value!r}" for name, value in undeclared)
        reasons.append(
            f"the row sets {rendered}, which a declaration cannot express; clear "
            f"{'it' if len(undeclared) == 1 else 'them'} on the row first."
        )
    if play_args:
        reasons.append(
            f"the member declares playbook args {play_args!r}, which a scheduled play "
            "launch does not pass; remove them from the member."
        )
    if reasons:
        return PlanEntry(
            qualified,
            "ERROR",
            detail=(
                f"--adopt will not overwrite {row['name']}: {' '.join(reasons)} Otherwise "
                "rename the member so the row is left alone."
            ),
        )
    return PlanEntry(
        qualified,
        "ADOPT",
        resolved=resolved,
        existing_id=row["id"],
        adopted_from=row["name"] if row["name"] != qualified else None,
        claimed_owner_key=row.get("owner_key"),
        claimed_updated_at=row.get("updated_at"),
    )


def _refuse_shared_rows(plan: list[PlanEntry]) -> None:
    claimants: dict[str, list[int]] = {}
    for index, entry in enumerate(plan):
        if entry.existing_id is not None:
            claimants.setdefault(entry.existing_id, []).append(index)
    for schedule_id, indexes in claimants.items():
        if len(indexes) < 2:
            continue
        names = sorted(plan[index].qualified_name for index in indexes)
        for index in indexes:
            plan[index] = PlanEntry(
                plan[index].qualified_name,
                "ERROR",
                detail=(
                    f"schedule row {schedule_id} is claimed by {len(indexes)} members of this "
                    f"set ({', '.join(names)}); one row cannot back more than one member"
                ),
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
    is_global = doc.metadata.scope == "global"
    resolved_members = resolve_schedule_set(doc, manifest_dir)

    existing_owned = {row["name"]: row for row in await db.list_schedules_by_owner_key(owner_key)}

    plan: list[PlanEntry] = []
    seen_qualified: set[str] = set()
    for resolved in resolved_members.values():
        qualified = resolved.qualified_name
        seen_qualified.add(qualified)
        existing_row = await db.get_schedule_by_name(qualified)
        if existing_row is None:
            candidate = (
                await db.get_schedule_by_name(resolved.member_name)
                if adopt and resolved.member_name != qualified
                else None
            )
            if candidate is None:
                plan.append(PlanEntry(qualified, "CREATE", resolved=resolved))
                continue
            plan.append(
                await _adopt_plan_entry(
                    candidate, resolved, qualified, owner_key, is_global=is_global
                )
            )
            continue
        if existing_row.get("owner_key") != owner_key:
            if adopt:
                plan.append(
                    await _adopt_plan_entry(
                        existing_row, resolved, qualified, owner_key, is_global=is_global
                    )
                )
            else:
                other_owner = existing_row.get("owner_key")
                owner_desc = other_owner or (
                    "a CLI quick-create"
                    if existing_row.get("managed_by") == "cli"
                    else "a legacy schedule"
                )
                # --adopt takes only unowned rows, so it is offered only for one.
                remedy = (
                    "; another set's row cannot be adopted"
                    if other_owner
                    else "; use --adopt to migrate it explicitly"
                )
                plan.append(
                    PlanEntry(
                        qualified,
                        "ERROR",
                        detail=(
                            f"{qualified} is already owned by {owner_desc}, not by "
                            f"{owner_key}{remedy}"
                        ),
                    )
                )
            continue
        # Digest alone is not enough: a member disabled by omission keeps its
        # old digest, so re-adding identical content must UPDATE to re-enable.
        enabled_matches = existing_row.get("enabled") == resolved.db_fields.get("enabled")
        if existing_row.get("resolved_digest") == resolved.digest and enabled_matches:
            plan.append(
                PlanEntry(
                    qualified,
                    "UNCHANGED",
                    resolved=resolved,
                    existing_id=existing_row["id"],
                    claimed_owner_key=existing_row.get("owner_key"),
                )
            )
        else:
            # The model is read from the profile the execution root sees, so an
            # update can change it without the member changing; the plan says so.
            stored_model = existing_row.get("action_model")
            declared_model = _declared_columns(resolved).get("action_model")
            plan.append(
                PlanEntry(
                    qualified,
                    "UPDATE",
                    resolved=resolved,
                    existing_id=existing_row["id"],
                    claimed_owner_key=existing_row.get("owner_key"),
                    claimed_updated_at=existing_row.get("updated_at"),
                    model_change=(
                        (stored_model, declared_model) if stored_model != declared_model else None
                    ),
                )
            )

    _refuse_shared_rows(plan)

    for name, row in existing_owned.items():
        if name not in seen_qualified:
            plan.append(
                PlanEntry(
                    name,
                    "DISABLE",
                    existing_id=row["id"],
                    claimed_owner_key=row.get("owner_key"),
                    claimed_updated_at=row.get("updated_at"),
                )
            )

    return plan, resolved_members


@dataclass
class ApplyResult:
    plan: list[PlanEntry]
    created: int
    updated: int
    unchanged: int
    disabled: int
    adopted: int = 0


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
    claims: dict[str, str | None] = {}
    updated_at_claims: dict[str, float] = {}

    for entry in plan:
        if entry.action == "CREATE":
            resolved = entry.resolved
            creates.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "name": entry.qualified_name,
                    "managed_by": "declaration",
                    "owner_key": owner_key,
                    "created_at": now,
                    "updated_at": now,
                    **_declared_columns(resolved),
                }
            )
        elif entry.action in ("UPDATE", "ADOPT"):
            resolved = entry.resolved
            fields = _declared_columns(resolved)
            # An instant that has passed cannot recur, so writing the declared
            # epoch back would run the schedule after the moment it named. A
            # re-apply happens for unrelated reasons: an edited sibling, a
            # member re-enabled after omission. Moving it forward still lands.
            if fields.get("trigger_type") == "at" and (fields.get("next_fire_at") or 0.0) <= now:
                fields.pop("next_fire_at", None)
            claims[entry.existing_id] = entry.claimed_owner_key
            if entry.claimed_updated_at is not None:
                updated_at_claims[entry.existing_id] = entry.claimed_updated_at
            if entry.action == "ADOPT":
                fields.update(
                    {
                        "name": entry.qualified_name,
                        "owner_key": owner_key,
                        "managed_by": "declaration",
                        # Adoption accepts a poll interval only where the row's trigger
                        # does not read it. Cleared, so a later apply that makes the
                        # member a github trigger polls at the cadence it declares.
                        "poll_interval_sec": None,
                    }
                )
            updates.append((entry.existing_id, fields))
        elif entry.action == "DISABLE":
            claims[entry.existing_id] = entry.claimed_owner_key
            if entry.claimed_updated_at is not None:
                updated_at_claims[entry.existing_id] = entry.claimed_updated_at
            disables.append(entry.existing_id)

    names_by_id = {entry.existing_id: entry.qualified_name for entry in plan if entry.existing_id}
    try:
        await db.apply_schedule_set(
            creates=creates,
            updates=updates,
            disables=disables,
            expect_owner_keys=claims,
            expect_updated_at=updated_at_claims,
        )
    except ScheduleOwnershipConflictError as exc:
        raise ScheduleSetError(
            [
                (
                    names_by_id.get(schedule_id, schedule_id),
                    f"schedule row {schedule_id} changed while this apply was planning "
                    "(its owner or its last update is no longer the one this plan read); "
                    "nothing was written -- re-run to plan against the row as it stands now",
                )
                for schedule_id in exc.schedule_ids
            ]
        ) from exc

    return ApplyResult(
        plan=plan,
        created=sum(1 for e in plan if e.action == "CREATE"),
        updated=sum(1 for e in plan if e.action == "UPDATE"),
        unchanged=sum(1 for e in plan if e.action == "UNCHANGED"),
        disabled=sum(1 for e in plan if e.action == "DISABLE"),
        adopted=sum(1 for e in plan if e.action == "ADOPT"),
    )
