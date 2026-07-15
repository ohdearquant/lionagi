# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read-only conversion of ``schedules`` rows into a ``ScheduleSet`` document.

Two modes, both never touching the database:

- Legacy conversion (``managed_by IS NULL`` -- rows created before the
  declaration layer): reconstructs a typed ``ScheduleMember`` per row from
  its raw ``action_*``/trigger columns, running each candidate through the
  same ``resolve_member`` static resolution a real ``apply`` would use so a
  malformed row is caught here rather than emitted half-valid. A row with
  ``on_success``/``on_fail`` is never converted -- chained follow-up actions
  have no v1 equivalent and must be redesigned as a flow by hand.
- Declaration/cli re-export (``managed_by IN ('cli', 'declaration')``):
  simply re-validates each row's already-typed ``authored_spec`` back into a
  ``ScheduleMember`` -- there is no structural conversion to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from lionagi.studio.services.schedule_declaration import (
    SPEC_KIND,
    SPEC_VERSION,
    AgentTarget,
    Budget,
    CommandTarget,
    CronTrigger,
    Execution,
    FlowTarget,
    GithubTriggerSpec,
    Notify,
    PlaybookTarget,
    Policies,
    ScheduleMember,
    ScheduleSetDocument,
    ScheduleSetMetadata,
    Target,
    Trigger,
    resolve_member,
)

# managed_by is NULL for every row created before the declaration layer; the
# schedules.managed_by CHECK constraint never actually admits the literal
# 'legacy' (only NULL/'cli'/'declaration'), but the predicate accepts it too
# in case a future migration starts stamping rows explicitly.
_LEGACY_MANAGED_BY = (None, "legacy")
_MANAGED_MANAGED_BY = ("cli", "declaration")

# Legacy action_kind -> the v1 target kind it can be expressed as. 'flow'
# (the `li o flow -- <model> <prompt>` positional launch) and 'fanout' have
# no v1 target equivalent -- FlowTarget always launches the typed
# flow-YAML-snapshot path, never the positional one.
_CONVERTIBLE_ACTION_KINDS = frozenset({"agent", "play", "flow_yaml", "command"})


def is_legacy_row(row: dict[str, Any]) -> bool:
    return row.get("managed_by") in _LEGACY_MANAGED_BY


def is_managed_row(row: dict[str, Any]) -> bool:
    return row.get("managed_by") in _MANAGED_MANAGED_BY


@dataclass
class ExportReportLine:
    qualified_name: str
    status: str  # "READY" | "BLOCKED"
    message: str | None = None


def _pick_project(rows: list[dict[str, Any]], fallback: str) -> str:
    """Deterministic single project for a document collapsing rows that may
    originally have belonged to different projects/owners: the
    lexicographically-first non-empty ``action_project`` among the rows, or
    *fallback* if none carry one. Each member's own ``execution.project`` --
    set explicitly below from that row's ``action_project`` -- is what
    actually governs a later re-apply; this value is metadata only."""
    projects = sorted({row["action_project"] for row in rows if row.get("action_project")})
    return projects[0] if projects else fallback


def _member_key(row: dict[str, Any], doc_project: str, used: set[str]) -> str:
    """The document's schedule-map key for *row*.

    Strips a leading ``"{action_project}/"`` from the row's (globally
    unique) ``name`` when that prefix matches *doc_project* -- reapplying
    then reconstructs the identical qualified name
    (``doc_project/local_name``), which is what lets a single-project export
    round-trip a row's original identity. Rows whose own project differs
    from *doc_project*, or whose stripped local name collides with one
    already used, fall back to the full original name (still unique) so no
    two members are ever silently merged.
    """
    name = row["name"]
    project = row.get("action_project")
    local = (
        name[len(project) + 1 :]
        if project == doc_project and name.startswith(f"{project}/")
        else name
    )
    if local in used:
        local = name
    used.add(local)
    return local


def _reusable_target_fields(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "action_kind",
        "action_model",
        "action_prompt",
        "action_agent",
        "action_playbook",
        "action_command",
        "action_command_args",
        "action_flow_yaml",
    )
    return {k: row[k] for k in keys if row.get(k) not in (None, "", [])}


def _convert_legacy_trigger(row: dict[str, Any]) -> Trigger:
    kind = row.get("trigger_type")
    if kind == "cron":
        expr = row.get("cron_expr")
        if not expr:
            raise ValueError("trigger_type='cron' row has no cron_expr")
        # Legacy rows never persisted a timezone (that field is new with the
        # declaration layer); UTC is the documented default for conversion.
        tz = row.get("resolved_timezone") or "UTC"
        return Trigger(cron=CronTrigger(expression=expr, timezone=tz))
    if kind == "interval":
        secs = row.get("interval_sec")
        if not secs or secs <= 0:
            raise ValueError(f"trigger_type='interval' row has invalid interval_sec={secs!r}")
        return Trigger(every=f"{int(secs)}s")
    if kind == "at":
        epoch = row.get("next_fire_at")
        if not epoch:
            raise ValueError(
                "trigger_type='at' row has no next_fire_at to reconstruct the "
                "one-shot fire time (already fired or disabled)"
            )
        return Trigger(at=datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat())
    if kind == "github_poll":
        repo = row.get("github_repo")
        if not repo:
            raise ValueError("trigger_type='github_poll' row has no github_repo")
        return Trigger(github=GithubTriggerSpec(repo=repo, filter=row.get("github_filter")))
    raise ValueError(f"unsupported trigger_type: {kind!r}")


def _flow_snapshot_path(flows_dir: Path, qualified_name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", qualified_name)
    return flows_dir / f"{safe}.flow.yaml"


def _convert_legacy_target(row: dict[str, Any], *, flows_dir: Path) -> Target:
    kind = row.get("action_kind")
    if kind not in _CONVERTIBLE_ACTION_KINDS:
        raise ValueError(f"action_kind {kind!r} has no v1 target equivalent -- not exportable")
    if kind == "agent":
        profile = row.get("action_agent")
        prompt = row.get("action_prompt")
        if not profile:
            raise ValueError(
                "action_kind='agent' row has no action_agent (profile) to "
                f"reference; reusable fields: {_reusable_target_fields(row)}"
            )
        if not prompt:
            raise ValueError("action_kind='agent' row has no action_prompt")
        return AgentTarget(
            kind="agent", profile=profile, prompt=prompt, model=row.get("action_model")
        )
    if kind == "play":
        name = row.get("action_playbook")
        if not name:
            raise ValueError("action_kind='play' row has no action_playbook")
        return PlaybookTarget(kind="playbook", name=name, args={})
    if kind == "flow_yaml":
        content = row.get("action_flow_yaml")
        if not content:
            raise ValueError("action_kind='flow_yaml' row has no action_flow_yaml content")
        flows_dir.mkdir(parents=True, exist_ok=True)
        path = _flow_snapshot_path(flows_dir, row["name"])
        path.write_text(content)
        return FlowTarget(kind="flow", file=str(path.resolve()), inputs={})
    # kind == "command"
    executable = row.get("action_command")
    if not executable:
        raise ValueError("action_kind='command' row has no action_command")
    args = [str(a) for a in (row.get("action_command_args") or [])]
    return CommandTarget(kind="command", executable=executable, args=args)


def _build_legacy_member(row: dict[str, Any], *, flows_dir: Path) -> ScheduleMember:
    trigger = _convert_legacy_trigger(row)
    target = _convert_legacy_target(row, flows_dir=flows_dir)
    execution = Execution(
        cwd=row.get("action_cwd") or None, project=row.get("action_project") or None
    )
    has_budget = row.get("budget_usd") is not None or row.get("budget_tokens") is not None
    policies = Policies(
        missedFire=row.get("missed_fire_policy") or "skip",
        overlap=row.get("overlap_policy") or "skip",
        maxRuns=row.get("max_runs"),
        budget=Budget(usd=row.get("budget_usd"), tokens=row.get("budget_tokens"))
        if has_budget
        else None,
        rateLimit=row.get("rate_limit"),
    )
    notify = None
    if row.get("notify_on") and row.get("notify_command"):
        notify = Notify(on=list(row["notify_on"]), command=row["notify_command"])
    return ScheduleMember(
        description=row.get("description"),
        enabled=bool(row.get("enabled", 1)),
        trigger=trigger,
        target=target,
        execution=execution,
        policies=policies,
        notify=notify,
    )


def convert_legacy_rows(
    rows: list[dict[str, Any]], *, flows_dir: Path, manifest_dir: Path
) -> tuple[ScheduleSetDocument, list[ExportReportLine]]:
    """Convert every chain-free, expressible legacy row into a member of one
    ``ScheduleSet`` document. Rows with ``on_success``/``on_fail``, an
    unsupported action_kind, or a malformed trigger are reported ``BLOCKED``
    and omitted -- never half-emitted. A row whose own project matches the
    document's chosen project is keyed by its local (prefix-stripped) name,
    so re-applying reconstructs its original qualified name exactly; others
    fall back to the full original name (see ``_member_key``)."""
    project = _pick_project(rows, "legacy-export")
    schedules: dict[str, ScheduleMember] = {}
    lines: list[ExportReportLine] = []
    used_keys: set[str] = set()
    for row in sorted(rows, key=lambda r: r["name"]):
        name = row["name"]
        if row.get("on_success") or row.get("on_fail"):
            lines.append(
                ExportReportLine(
                    name,
                    "BLOCKED",
                    "dependency conversion required (on_success/on_fail present); "
                    f"reusable target fields: {_reusable_target_fields(row)}",
                )
            )
            continue
        try:
            member = _build_legacy_member(row, flows_dir=flows_dir)
        except ValueError as exc:
            lines.append(ExportReportLine(name, "BLOCKED", str(exc)))
            continue
        try:
            resolve_member(name, member, manifest_dir=manifest_dir, project=None, is_global=False)
        except ValueError as exc:
            lines.append(ExportReportLine(name, "BLOCKED", f"static resolution failed: {exc}"))
            continue
        schedules[_member_key(row, project, used_keys)] = member
        lines.append(ExportReportLine(name, "READY"))

    doc = ScheduleSetDocument(
        apiVersion=SPEC_VERSION,
        kind=SPEC_KIND,
        metadata=ScheduleSetMetadata(name="legacy-export", project=project),
        schedules=schedules,
    )
    return doc, lines


def build_managed_export_document(
    rows: list[dict[str, Any]],
) -> tuple[ScheduleSetDocument, list[ExportReportLine]]:
    """Re-serialize every declaration/cli-managed row's persisted
    ``authored_spec`` back into a single ``ScheduleSet`` document. A row
    whose own project matches the document's chosen project is keyed by its
    local (prefix-stripped) name so re-applying reconstructs its original
    qualified name; others fall back to the full original name (see
    ``_member_key``)."""
    project = _pick_project(rows, "export")
    schedules: dict[str, ScheduleMember] = {}
    lines: list[ExportReportLine] = []
    used_keys: set[str] = set()
    for row in sorted(rows, key=lambda r: r["name"]):
        name = row["name"]
        authored = row.get("authored_spec")
        if not isinstance(authored, dict):
            lines.append(ExportReportLine(name, "BLOCKED", "row has no authored_spec to re-export"))
            continue
        authored = dict(authored)
        execution = dict(authored.get("execution") or {})
        if not execution.get("project") and row.get("action_project"):
            execution["project"] = row["action_project"]
        authored["execution"] = execution
        try:
            member = ScheduleMember.model_validate(authored)
        except Exception as exc:  # noqa: BLE001 - reported per-row, never raised
            lines.append(
                ExportReportLine(name, "BLOCKED", f"authored_spec failed validation: {exc}")
            )
            continue
        schedules[_member_key(row, project, used_keys)] = member
        lines.append(ExportReportLine(name, "READY"))

    doc = ScheduleSetDocument(
        apiVersion=SPEC_VERSION,
        kind=SPEC_KIND,
        metadata=ScheduleSetMetadata(name="export", project=project),
        schedules=schedules,
    )
    return doc, lines


class _QuotedStr(str):
    """Marker wrapper: force a double-quoted YAML scalar for this string."""


class _ExportDumper(yaml.SafeDumper):
    pass


def _represent_quoted(dumper: yaml.Dumper, data: _QuotedStr) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


_ExportDumper.add_representer(_QuotedStr, _represent_quoted)


def _quote_notify_on_keys(obj: Any) -> Any:
    """YAML 1.1 parses a bare ``on:`` mapping key as the boolean ``True`` --
    force it quoted wherever it appears as a ``notify`` block's key so the
    document round-trips as the string ``"on"``."""
    if isinstance(obj, dict):
        return {
            (_QuotedStr(k) if k == "on" else k): _quote_notify_on_keys(v) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_quote_notify_on_keys(v) for v in obj]
    return obj


def dump_schedule_set_yaml(doc: ScheduleSetDocument) -> str:
    data = doc.model_dump(mode="json")
    quoted = _quote_notify_on_keys(data)
    return yaml.dump(quoted, Dumper=_ExportDumper, sort_keys=True, default_flow_style=False)


def format_report(lines: list[ExportReportLine]) -> str:
    out = [
        f"{line.status:<8} {line.qualified_name}" + (f"  {line.message}" if line.message else "")
        for line in lines
    ]
    ready = sum(1 for line in lines if line.status == "READY")
    blocked = sum(1 for line in lines if line.status == "BLOCKED")
    out.append(f"{ready} ready, {blocked} blocked")
    return "\n".join(out) + "\n"
