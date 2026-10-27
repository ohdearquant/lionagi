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

Adoption keeps the row: same id, same run history, and every column the
declaration does not write -- project, action_extra_args, on_success, on_fail,
threshold_config, last_fired_at, next_fire_at, github_cursor,
effective_timezone, effective_timezone_source, last_alert_at, last_evaluated_at,
last_healthy_poll_at, poller_consecutive_401, poller_consecutive_cap_no_dispatch,
predispatch_refusal_event,
predispatch_refusal_count, created_at and updated_at. An overdue next_fire_at
the adopted row already holds therefore still queues the recovery fire it would
have queued without the adoption, where a fresh create of the same member
starts clean.
"""

from __future__ import annotations

import collections.abc
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lionagi._paths import _find_git_root
from lionagi.ln import compute_hash, parse_duration, short_id
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
_EVERY_MAX_SECONDS = 30 * 86400


def _parse_every(raw: str) -> int:
    if not _EVERY_RE.match(raw.strip()):
        raise ValueError(
            f"trigger.every must be a strict positive duration like '30s'/'15m'/'6h'/'2d', got {raw!r}"
        )
    try:
        return int(parse_duration(raw, max_seconds=_EVERY_MAX_SECONDS))
    except ValueError as exc:
        raise ValueError(
            f"trigger.every must be positive and at most 30d, got {raw!r} ({exc})"
        ) from exc


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
    return compute_hash(json.dumps(obj, sort_keys=True, default=str), max_bytes=None)


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
    from lionagi.studio.scheduler.subprocess import (
        _PLAY_SUBCOMMANDS,
        _validate_identifier,
        _validate_playbook_args,
    )

    _validate_identifier(target.name, "target.playbook.name")
    if target.name in _PLAY_SUBCOMMANDS:
        raise ValueError(
            f"target.playbook.name {target.name!r} names a `li play` subcommand, not a playbook"
        )
    # The launch passes these args, so they are held to its rules here, where
    # validate and apply report them, rather than first failing when the row fires.
    _validate_playbook_args(target.args)
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


def _resolve_project(execution: Execution, *, project: str | None, is_global: bool) -> str | None:
    """The project a member runs under: its own, else the document's.

    A project written as YAML null (``null``, ``~`` or a blank value, which the parser
    reads alike) declares that the member has no project, which is what an export
    writes for a row stored without one; an omitted project or an empty string
    inherits the document's (none in a global document). The difference is real at
    spawn time: the project is the schedule's fallback execution root, used only
    when its own stored ``cwd`` is unusable, and is passed as ``--project`` to the
    agent and flow_yaml commands a declaration spawns, so reading an explicit null
    as "inherit" would change what an exported row does on re-apply.
    """
    if execution.project:
        return execution.project
    if execution.project is None and "project" in execution.model_fields_set:
        return None
    return None if is_global else project


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
    resolved_project = _resolve_project(member.execution, project=project, is_global=is_global)

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
    # The stored spec records the member as authored, so an omitted project stays
    # omitted rather than becoming a null, which now means "no project". The
    # managed export, the only reader of this spec, sets the project from the
    # row's stored action_project before it validates the member.
    authored["execution"] = member.execution.model_dump(mode="json", exclude_unset=True)
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

    schedule_id = short_id(12)
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
    prefix = "global" if is_global else doc.metadata.project
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
            # Every raise site carries the qualified name, so a caller reading
            # ``ScheduleSetError.errors`` (the text lines and the --json payload
            # alike) sees one naming scheme rather than a bare member key here and
            # a qualified name from the plan errors.
            errors.append((f"{prefix}/{name}", str(exc)))
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
# The launch kinds a declaration target resolves to: every member's target is an
# agent, a flow_yaml snapshot, a playbook or a command.
_MEMBER_ACTION_KINDS = frozenset(_TARGET_KIND_TO_ACTION_KIND.values())
# The column each of those launch kinds names itself in: the profile an agent
# launch runs, the snapshot a flow_yaml launch passes, the playbook a play launch
# runs, the executable a command launch spawns.
_MEMBER_TARGET_COLUMNS = {
    "agent": "action_agent",
    "flow_yaml": "action_flow_yaml",
    "play": "action_playbook",
    "command": "action_command",
}


# The columns build_argv validates before it branches on the kind, so a value the
# launch refuses fails at every fire whatever the kind that would otherwise drop
# it. Every action kind consults such a column; the table below names the kinds
# that then act on an accepted value. Kept beside the table rather than in it, so
# a table row stays the (kinds, triggers) pair it reads as.
_VALIDATED_EVERY_KIND = frozenset(
    {"action_project", "action_agent", "action_model", "action_prompt", "action_playbook"}
)


class _LaunchReading(NamedTuple):
    """Which launch consults one stored column, and how.

    ``kinds`` names the action kinds whose launch puts an accepted value into the
    argv or otherwise acts on it; ``triggers`` the trigger types whose path from a
    due row to a launched child consults it. ``None`` means every kind or every
    trigger; an empty set means no launch reads it. ``validated_every_kind`` marks
    a column the launch validates before it branches on the kind.
    """

    kinds: frozenset[str] | None
    triggers: frozenset[str] | None
    validated_every_kind: bool = False

    def consults(self, kind: str | None) -> bool:
        """Whether a launch of *kind* consults this column at all."""
        return self.validated_every_kind or self.kinds is None or kind in self.kinds

    def acts_on(self, kind: str | None) -> bool:
        """Whether a launch of *kind* acts on the value, beyond validating it."""
        return self.kinds is None or kind in self.kinds


# Which launch reads which stored column. For every column a declaration writes,
# this names the action kinds and the trigger types whose path from a due row to
# a launched child consults it -- to build the argv, to resolve the execution
# root, or to validate the value the launch would refuse at every fire. It is the
# single source for the adoption comparison: a column no launch of the row's kind
# and trigger reads changes nothing about a fire, so two stored values act alike
# there and adoption neither compares the column nor refuses the row for it. Read
# off _tick (which trigger dispatches), the cron arming, the missed-fire check,
# the fire path, build_argv and the github poll.
_LAUNCH_READERS: dict[str, tuple[frozenset[str] | None, frozenset[str] | None]] = {
    # _tick dispatches on trigger_type and lists only enabled rows; every fire
    # then builds an argv from action_kind and resolves its execution root.
    "trigger_type": (None, None),
    "enabled": (None, None),
    "action_kind": (None, None),
    "action_cwd": (None, None),
    # A cron row's expression and the zone its fire times are read in.
    "cron_expr": (None, frozenset({"cron"})),
    "resolved_timezone": (None, frozenset({"cron"})),
    # resolve_schedule_cadence_seconds reads the interval for an interval row's
    # next fire and as a github poll's own fallback.
    "interval_sec": (None, frozenset({"interval", "github_poll"})),
    # The github poll reads the repository, its filter and its poll interval.
    "github_repo": (None, frozenset({"github_poll"})),
    "github_filter": (None, frozenset({"github_poll"})),
    "poll_interval_sec": (None, frozenset({"github_poll"})),
    # next_fire_at is the due instant _tick reads, and the github poll reads it as
    # the claim it hands the fire; every trigger consults it.
    "next_fire_at": (None, None),
    # The missed-fire check and the overlap gate run for every trigger but the
    # github poll, which holds one pass at a time instead.
    "missed_fire_policy": (None, frozenset({"cron", "interval", "at"})),
    "overlap_policy": (None, frozenset({"cron", "interval", "at"})),
    # Admission and the pre-fire gates read these for every trigger. budget and
    # rate_limit gate every fire; max_runs gates every fire and every admission.
    "max_runs": (None, None),
    "budget_usd": (None, None),
    "budget_tokens": (None, None),
    "rate_limit": (None, None),
    # A threshold row evaluates its metric on the fire path and stamps its
    # watermark there, whatever the trigger that reached it.
    "threshold_config": (None, None),
    # Chained actions run after any fire, whatever its trigger.
    "on_success": (None, None),
    "on_fail": (None, None),
    # build_argv validates an action_project and extra tokens for every kind, and
    # passes --project on only for the agent and flow-yaml families.
    "action_project": (frozenset({"agent", "flow", "fanout", "flow_yaml"}), None),
    "action_extra_args": (None, None),
    # The launch-specific columns: build_argv validates every one of them before
    # it branches on the kind, so a value the launch refuses fails at every fire
    # whatever the kind, and only the kinds named here then put a value it accepts
    # into the argv. An agent launch passes action_agent as --agent and an engine
    # launch reads it as the engine kind; an agent launch passes action_model as a
    # positional, a flow or fanout launch as the model positional after '--', a
    # flow_yaml launch merges it into the spec, and an engine launch passes it as
    # --model; an agent, flow or fanout launch passes action_prompt as a
    # positional, a play launch appends it as the playbook's input, and an engine
    # launch passes it as the engine spec; a play launch appends action_playbook
    # as the playbook name. A flow_yaml launch carries its prompt inside the spec
    # and a command launch has no prompt slot, so both drop it.
    "action_agent": (frozenset({"agent", "engine"}), None),
    "action_model": (frozenset({"agent", "flow", "fanout", "flow_yaml", "engine"}), None),
    "action_prompt": (frozenset({"agent", "flow", "fanout", "play", "engine"}), None),
    "action_playbook": (frozenset({"play"}), None),
    # The columns only their own kind reads: a command launch spawns the
    # executable and renders its args, and a flow_yaml launch writes the snapshot
    # to the spec file it passes. No other kind consults them at all.
    "action_command": (frozenset({"command"}), None),
    "action_command_args": (frozenset({"command"}), None),
    "action_flow_yaml": (frozenset({"flow_yaml"}), None),
    # The fire registers a terminal callback on the invocation it spawns; only
    # both halves together register anything, which _adopt_comparable reads.
    "notify_on": (None, None),
    "notify_command": (None, None),
    # No launch reads a description.
    "description": (frozenset(), None),
}


def _row_action_kind(row: dict[str, Any]) -> str | None:
    """The canonical launch kind a row's action_kind names, aliases resolved."""
    from lionagi.studio.scheduler.subprocess import _ALIAS_ACTION_KINDS

    kind = row.get("action_kind")
    return _ALIAS_ACTION_KINDS.get(kind, kind)


def _launch_reading(name: str) -> _LaunchReading:
    """The launch reading for *name*, or one that names every kind and trigger.

    A name the table does not list reads as a column every launch consults, so a
    column added to the declaration without a table row is compared rather than
    silently adopted against a row that stores something else.
    """
    entry = _LAUNCH_READERS.get(name)
    if entry is None:
        return _LaunchReading(None, None)
    return _LaunchReading(*entry, name in _VALIDATED_EVERY_KIND)


def _column_read_by(name: str, schedule: dict[str, Any]) -> bool:
    """Whether any launch of *schedule*'s kind and trigger consults this column.

    A name the table does not list is compared, not skipped: a column added to
    the declaration without a table row would otherwise be adopted against a row
    that stores something else in silence.
    """
    reading = _launch_reading(name)
    if not reading.consults(_row_action_kind(schedule)):
        return False
    return reading.triggers is None or schedule.get("trigger_type") in reading.triggers


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


async def _adopt_model_pair(
    row: dict[str, Any], declared: Any, declared_fields: dict[str, Any]
) -> tuple[Any, Any]:
    current = row.get("action_model")
    if not current and row.get("action_kind") == "agent" and row.get("action_agent"):
        # A row that leaves the model to its profile runs on the profile's model,
        # read from the row's own execution root, which is where the launch reads it.
        root_key, root_path, _display = await _adoption_execution_root(row)
        if root_key[0] == "path" and root_path is not None:
            from lionagi.cli._providers import load_agent_profile

            try:
                profile_model = load_agent_profile(row["action_agent"], cwd=Path(root_path)).model
            except (FileNotFoundError, OSError, ValueError):
                pass
            else:
                # A profile's model is read only when it is a model name. Any other
                # value a profile file can carry -- a number, a mapping, a list --
                # names no model, so the row is read as leaving its model unset
                # rather than failing the whole plan here.
                if isinstance(profile_model, str):
                    current = profile_model or current
    return (
        _adopt_comparable("action_model", row, current),
        _adopt_comparable("action_model", {**row, **declared_fields}, declared),
    )


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
        project = schedule.get("action_project")
        if project:
            # The launch validates the project identifier whatever the kind, so a
            # value it refuses fails at every fire even where the kind drops it;
            # only a value the launch accepts is then unused.
            from lionagi.studio.scheduler.subprocess import _validate_identifier

            try:
                _validate_identifier(project, "action_project")
            except ValueError:
                return ("refused",), f"{project!r}, which the launch refuses"
            if not _launch_reading("action_project").acts_on(_row_action_kind(schedule)):
                return ("unused",), "none, the action takes no project argument"
            return ("project", str(project)), f"{project!r}, set explicitly"
        if not _launch_reading("action_project").acts_on(_row_action_kind(schedule)):
            return ("unused",), "none, the action takes no project argument"
        if root[0][0] == "refused":
            return ("not-launched",), "none, the action does not launch"
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
    """A column value as the launch acts on it, for stored values that act alike.

    A column no launch of *schedule*'s kind and trigger consults is None on both
    sides, so two stored values act alike there whatever they say. Within a column
    a launch does consult, values differ only where the launch would behave
    differently: a value it passes on counts, and a value it refuses counts as a
    difference whatever the kind, since a refusal fails at every fire.
    """
    if not _column_read_by(name, schedule):
        return None
    if name == "notify_on":
        # No notification is registered for an empty list or for none, and no
        # notification is registered without a command to run, so neither the
        # order nor a repeat of a status changes what fires, and neither does a
        # status with no command beside it.
        if not value or not schedule.get("notify_command"):
            return None
        return sorted(set(value))
    if name == "notify_command":
        # A command with no event to run on, and a command that is empty, never run.
        return value if value and schedule.get("notify_on") else None
    if name == "action_agent":
        # An empty identifier passes nothing on. Every launch validates a
        # non-empty one, so a value the launch refuses fails at every fire
        # whatever the kind; only the kinds whose launch passes it on then put it
        # into the argv, and the other kinds drop it after the check.
        if not value:
            return None
        from lionagi.studio.scheduler.subprocess import _validate_identifier

        try:
            _validate_identifier(value, "action_agent")
        except ValueError:
            return value
        return value if _launch_reading(name).acts_on(_row_action_kind(schedule)) else None
    if name == "action_model":
        # Same reading as action_agent: every launch validates the model, only the
        # named kinds then pass it on.
        if not value:
            return None
        from lionagi.studio.scheduler.subprocess import _validate_action_model

        try:
            _validate_action_model(value)
        except ValueError:
            return value
        return value if _launch_reading(name).acts_on(_row_action_kind(schedule)) else None
    if name == "action_prompt":
        # A launch reads the prompt only when it is set: an unset and an empty
        # prompt are passed alike (an agent launch passes one empty positional,
        # and a play launch appends nothing). A prompt the launch refuses, and a
        # play launch's leading-dash positional, fail at every fire whatever the
        # kind, so those count as written.
        if not value:
            return None
        from lionagi.studio.scheduler.subprocess import _validate_prompt

        try:
            _validate_prompt(value)
        except ValueError:
            return value
        if _row_action_kind(schedule) == "play" and value.startswith("-"):
            return value
        return value if _launch_reading(name).acts_on(_row_action_kind(schedule)) else None
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
    if name == "action_playbook":
        # Every kind validates the identifier; only the named kind then appends
        # the name as a positional.
        if not value:
            return None
        from lionagi.studio.scheduler.subprocess import _validate_identifier

        try:
            _validate_identifier(value, "action_playbook")
        except ValueError:
            return value
        return value if _launch_reading(name).acts_on(_row_action_kind(schedule)) else None
    if name == "action_command":
        # Only a command launch spawns the executable, and it validates the name
        # and its allow-list membership before it does; a name either check
        # refuses fails at every fire, so it counts as written.
        if not value:
            return None
        from lionagi.studio.scheduler.subprocess import (
            _validate_action_command,
            _validate_command_allowlisted,
        )

        try:
            _validate_action_command(value)
            _validate_command_allowlisted(value)
        except ValueError:
            return value
        return value if _row_action_kind(schedule) == "command" else None
    if name == "action_flow_yaml":
        # Only a flow_yaml launch writes the snapshot to a spec file.
        return value if _row_action_kind(schedule) == "flow_yaml" else None
    if name == "action_cwd" and value:
        # The launch runs in the resolved directory, so two spellings of one
        # directory (a symlink, a trailing slash) act alike.
        return str(Path(value).resolve())
    if name == "action_command_args":
        # A row may store no list at all; the launch and the export read that as
        # no arguments, and a member always declares a list.
        return list(value or [])
    return value


def _adopt_undeclared_values(row: dict[str, Any]) -> list[tuple[str, Any]]:
    """Columns from ``_ADOPT_UNDECLARED_COLUMNS`` the row sets and a launch reads.

    A column no launch of the row's kind and trigger reads keeps its stored value
    through an adoption and every later apply without ever reaching a fire, so it
    is neither expressed nor refused.
    """
    found: list[tuple[str, Any]] = []
    for name in _ADOPT_UNDECLARED_COLUMNS:
        value = row.get(name)
        if not _column_read_by(name, row):
            continue
        if name == "poll_interval_sec":
            # The engine's own default is what a row with no interval polls at, so
            # a stored default changes no cadence.
            if value is not None and value != _GITHUB_POLL_DEFAULT_SEC:
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
        if not _column_read_by(name, row):
            # No launch of the row's kind and trigger consults this column, so the
            # two stored values act alike and there is nothing to compare.
            continue
        if name == "resolved_timezone" and (
            row.get("trigger_type") == "cron" and declared_fields.get("trigger_type") == "cron"
        ):
            current, adopted = _adopt_timezone_pair(row, declared)
            if current != adopted:
                differences.append((name, current, adopted))
            continue
        if name == "action_model":
            current, adopted = await _adopt_model_pair(row, declared, declared_fields)
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
            and row.get("next_fire_at") is None
            and (row.get("missed_fire_policy") == "skip" or row.get("last_fired_at") is not None)
        ):
            # The write leaves the row's instant alone once the declared one has passed,
            # which is the same act only for a one-shot that already fired or was
            # skipped: those have no instant left to fire at. A row still holding an
            # instant fires at it, and a never-fired run_once row holds none at all but
            # would keep it that way -- it never fires, while the write of the declared
            # epoch (or the recovery the engine queues for a fresh apply) fires once --
            # so both are compared.
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
    if name == "next_fire_at" and current is None:
        # A declaration resolves a one-shot's own instant at declaration time, so
        # no member states a one-shot with none.
        return row.get("trigger_type") == "at"
    if name == "max_runs" and current is None:
        # A one-shot's run cap follows from its single instant, so the declaration
        # always writes one; no member states a one-shot with no cap.
        return row.get("trigger_type") == "at"
    if name == "interval_sec":
        # A github trigger declares no interval; the scheduler applies its own cadence.
        return row.get("trigger_type") == "github_poll"
    if name == "action_cwd":
        # A member always resolves an execution root.
        return current is None
    if name == "action_kind":
        # A member's target resolves to an agent, flow_yaml, play or command launch.
        return row.get("action_kind") not in _MEMBER_ACTION_KINDS
    if name == _MEMBER_TARGET_COLUMNS.get(_row_action_kind(row)) and not current:
        # The active kind's own target column names what the launch runs: the
        # profile, the snapshot, the playbook or the executable. A row that stores
        # none of it launches nothing in that column's place, and no member's target
        # resolves to an empty one, so the row cannot be matched by a member edit.
        return True
    if name == "action_agent" and current is not None:
        # The launch validates the identifier however the kind, and reads the
        # profile from the row's own execution root. A member always names a
        # profile, so a value the launch refuses or resolves nowhere is one no
        # member of this set declares.
        from lionagi.studio.scheduler.subprocess import _validate_identifier

        try:
            _validate_identifier(current, "action_agent")
        except ValueError:
            return True
        root_key, root_path, _display = await _adoption_execution_root(row)
        if root_key[0] != "path" or root_path is None:
            return True
        from lionagi.cli._providers import load_agent_profile

        try:
            load_agent_profile(current, cwd=Path(root_path))
        except (FileNotFoundError, OSError, ValueError):
            return True
        return False
    if name == "notify_on" and current:
        # The declaration checks the status vocabulary, so a status outside it
        # cannot be matched by editing the member.
        return any(status not in _NOTIFY_ALLOWED_STATUSES for status in current)
    if name == "action_playbook" and isinstance(current, str):
        # A member's playbook name is refused when the launch would refuse it.
        from lionagi.studio.scheduler.subprocess import _PLAY_SUBCOMMANDS, _validate_identifier

        try:
            _validate_identifier(current, "action_playbook")
        except ValueError:
            return True
        return _row_action_kind(row) == "play" and current in _PLAY_SUBCOMMANDS
    if name == "action_command" and isinstance(current, str):
        # A member's executable resolves only against the allow-list, so a name the
        # launch refuses, or one this process's allow-list does not carry, is not
        # one a member can be edited to name.
        from lionagi.studio.scheduler.subprocess import (
            _validate_action_command,
            _validate_command_allowlisted,
        )

        try:
            _validate_action_command(current)
            _validate_command_allowlisted(current)
        except ValueError:
            return True
        return False
    if name in ("action_prompt", "action_model") and row.get("action_kind") != "agent":
        # Only an agent member declares a prompt or a model, and it declares the agent kind.
        return current is not None
    if name == "action_model" and current is None:
        # An agent member always resolves a model, its own or its profile's.
        return True
    if name == "action_prompt" and current is None:
        # An agent member's prompt is a required string.
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
        stored = row.get("action_project")
        if isinstance(stored, str) and stored:
            # The launch validates the identifier whatever the kind, so a value it
            # refuses is not one a member can be edited to carry.
            from lionagi.studio.scheduler.subprocess import _validate_identifier

            try:
                _validate_identifier(stored, "action_project")
            except ValueError:
                return True
        root_key, root_path, _display = await _adoption_execution_root(row)
        if root_key[0] != "path" or root_path is None:
            return True
        if is_global or stored:
            return False
        # A member of a project's set that names no project inherits the set's, but a
        # member writing an explicit ``project: null`` declares no project, which
        # resolves to this row's None: a difference a member edit closes, so the refusal
        # names that edit rather than calling the value undeclarable.
        return False
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
        elif any(
            d[0] == "action_project"
            and "run project: none detected from the execution root" in d[1]
            and "set explicitly" in str(d[2])
            for d in differences
        ):
            # The row runs with no project and the member declares the set's, and a
            # member can close that: a project written as ``project: null`` declares
            # no project. Name the edit rather than the generic remedy. A row whose kind
            # takes no project argument differs in kind, which ``project: null`` does
            # not close.
            remedy = (
                "; edit the member to match the row, declaring no project with "
                "execution: {project: null}."
            )
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
                    "id": short_id(12),
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
                        #
                        # The row keeps its id, its runs, and every column the
                        # declaration does not write, which an in-place migration is
                        # meant to preserve. Those columns are project,
                        # action_extra_args, on_success, on_fail, threshold_config,
                        # last_fired_at, next_fire_at, github_cursor,
                        # effective_timezone, effective_timezone_source,
                        # last_alert_at, last_evaluated_at, last_healthy_poll_at,
                        # poller_consecutive_401, poller_consecutive_cap_no_dispatch,
                        # predispatch_refusal_event,
                        # predispatch_refusal_count, created_at, and updated_at. An
                        # overdue next_fire_at the row already holds still queues the
                        # recovery fire it would have queued without the adoption; a
                        # fresh create of the same member starts clean, so a row and
                        # its fresh twin can differ in that one recovery run.
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
