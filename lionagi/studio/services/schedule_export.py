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

import os
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

# Mirrors the `schedule.get("poll_interval_sec") or schedule.get("interval_sec")
# or 300` fallback in scheduler/engine.py -- a github_poll row at this value
# is indistinguishable from one with no override, so it stays exportable.
_GITHUB_POLL_DEFAULT_SEC = 300


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

    Strips a leading ``"{doc_project}/"`` from the row's (globally unique)
    ``name`` -- reapplying then reconstructs the identical qualified name
    (``doc_project/local_name``), which is what lets an export round-trip a
    row's original identity. Grouping (``_group_into_documents``) guarantees
    a row lands in the document matching its effective project, so the
    prefix either matches or the name is bare. A stripped local name that
    collides with one already used falls back to the full original name
    (still unique) so no two members are ever silently merged.
    """
    name = row["name"]
    local = name[len(doc_project) + 1 :] if name.startswith(f"{doc_project}/") else name
    if local in used:
        local = name
    used.add(local)
    return local


def _effective_project(row: dict[str, Any]) -> str | None:
    """The project namespace a row's name lives under: the stored project
    column when set, else the qualified name's own prefix. Rows created
    before the project column existed carry qualified names but a NULL
    column; grouping them by name prefix is what lets their identity
    round-trip instead of being re-qualified under a fallback project.
    ``None`` means the name is bare and cannot round-trip untouched (every
    document carries a project) -- callers disclose the resulting rename."""
    project = row.get("action_project")
    if project:
        return project
    name = row["name"]
    return name.split("/", 1)[0] if "/" in name else None


def _rename_note(row: dict[str, Any], base_name: str) -> str | None:
    if _effective_project(row) is not None:
        return None
    return (
        f"row has no project and a bare name; re-applies as "
        f"{base_name}/{row['name']} (the document's project supplies the namespace)"
    )


def _ready_note(row: dict[str, Any], member: ScheduleMember, base_name: str) -> str | None:
    notes = [
        n
        for n in (
            _rename_note(row, base_name),
            _flow_portability_note(member),
            _absolute_cwd_note(member),
        )
        if n
    ]
    return "; ".join(notes) or None


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
        # Mirror the engine's full cadence fallback chain: a NULL
        # poll_interval_sec row with interval_sec set polls at interval_sec
        # today, so it drifts just as much on re-apply as an explicit value.
        effective = (
            row.get("poll_interval_sec") or row.get("interval_sec") or _GITHUB_POLL_DEFAULT_SEC
        )
        if effective != _GITHUB_POLL_DEFAULT_SEC:
            raise ValueError(
                f"trigger_type='github_poll' row polls every {effective}s "
                f"(default {_GITHUB_POLL_DEFAULT_SEC}s) but GithubTriggerSpec has no "
                "poll-interval field to carry a non-default cadence"
            )
        return Trigger(github=GithubTriggerSpec(repo=repo, filter=row.get("github_filter")))
    raise ValueError(f"unsupported trigger_type: {kind!r}")


def _flow_snapshot_path(flows_dir: Path, qualified_name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", qualified_name)
    return flows_dir / f"{safe}.flow.yaml"


def _convert_legacy_target(row: dict[str, Any], *, flows_dir: Path, manifest_dir: Path) -> Target:
    kind = row.get("action_kind")
    if kind not in _CONVERTIBLE_ACTION_KINDS:
        raise ValueError(f"action_kind {kind!r} has no v1 target equivalent -- not exportable")
    extra_args = row.get("action_extra_args")
    if extra_args:
        raise ValueError(
            f"action_kind={kind!r} row has action_extra_args={extra_args!r} but no v1 "
            "target field can carry launch-time extra args -- dropping them would "
            "silently change fire behavior"
        )
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
        if row.get("action_model"):
            raise ValueError(
                f"action_kind='flow_yaml' row has action_model={row['action_model']!r} set "
                f"but FlowTarget has no model field to carry it; reusable fields: "
                f"{_reusable_target_fields(row)}"
            )
        flows_dir.mkdir(parents=True, exist_ok=True)
        path = _flow_snapshot_path(flows_dir, row["name"])
        path.write_text(content)
        # The document is designed to be committed to a repo, so the sidecar
        # reference must not embed this host's filesystem layout. A relative
        # path re-resolves against the manifest dir on apply, exactly like a
        # hand-authored declaration.
        try:
            rel = os.path.relpath(path.resolve(), manifest_dir.resolve())
        except ValueError as exc:
            raise ValueError(
                f"flow snapshot at {path} cannot be expressed relative to the "
                f"output directory {manifest_dir}: {exc}"
            ) from exc
        return FlowTarget(kind="flow", file=rel, inputs={})
    # kind == "command"
    executable = row.get("action_command")
    if not executable:
        raise ValueError("action_kind='command' row has no action_command")
    args = [str(a) for a in (row.get("action_command_args") or [])]
    return CommandTarget(kind="command", executable=executable, args=args)


def _build_legacy_member(
    row: dict[str, Any], *, flows_dir: Path, manifest_dir: Path
) -> ScheduleMember:
    trigger = _convert_legacy_trigger(row)
    target = _convert_legacy_target(row, flows_dir=flows_dir, manifest_dir=manifest_dir)
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


def _flow_portability_note(member: ScheduleMember) -> str | None:
    """Disclose how an exported ``flow`` target's snapshot file travels.
    Legacy conversion writes the sidecar itself and references it relative to
    the manifest dir, so the pair commits and moves together; a re-exported
    authored_spec may still carry an absolute path the author wrote, which
    makes the document host-bound and is called out loudly."""
    if member.target.kind != "flow":
        return None
    file = member.target.file
    if Path(file).is_absolute():
        return (
            f"flow snapshot is an absolute host path ({file}); this "
            "document is host-bound until the sidecar file moves with it"
        )
    return (
        f"flow snapshot sidecar at {file} (relative to this document); "
        "commit and move the sidecar together with the document"
    )


def _absolute_cwd_note(member: ScheduleMember) -> str | None:
    """An absolute ``execution.cwd`` is kept verbatim -- a schedule's working
    directory is machine-local by design, like a cron entry -- but a document
    meant to be committed must not carry host paths silently, so the report
    line flags every one written."""
    cwd = member.execution.cwd if member.execution else None
    if cwd and Path(cwd).is_absolute():
        return (
            f"execution.cwd is an absolute host path ({cwd}); kept verbatim, "
            "review before committing this document"
        )
    return None


def _group_into_documents(
    ready: list[tuple[dict[str, Any], ScheduleMember]],
    all_rows: list[dict[str, Any]],
    *,
    base_name: str,
) -> list[ScheduleSetDocument]:
    """Split *ready* rows into one ``ScheduleSet`` document per distinct
    effective project (``_effective_project``: stored column, else the
    qualified name's prefix; bare-named rows share a single
    *base_name*-keyed group). Grouping *before* computing member keys
    guarantees a row's effective project always matches its document's
    project, so ``_member_key`` never has to fall back for a mismatch --
    this is what fixes
    mixed-project double-qualification: a single document spanning multiple
    projects used to key a mismatched row by its already-qualified name, and
    re-applying then prepended the document's project a second time,
    producing e.g. ``alpha/beta/task`` instead of ``beta/task``."""
    grouped: dict[str, list[tuple[dict[str, Any], ScheduleMember]]] = {}
    for row, member in ready:
        proj = _effective_project(row) or base_name
        grouped.setdefault(proj, []).append((row, member))

    if not grouped:
        return [
            ScheduleSetDocument(
                apiVersion=SPEC_VERSION,
                kind=SPEC_KIND,
                metadata=ScheduleSetMetadata(
                    name=base_name, project=_pick_project(all_rows, base_name)
                ),
                schedules={},
            )
        ]

    multi = len(grouped) > 1
    docs: list[ScheduleSetDocument] = []
    for proj in sorted(grouped):
        used_keys: set[str] = set()
        schedules: dict[str, ScheduleMember] = {}
        for row, member in grouped[proj]:
            schedules[_member_key(row, proj, used_keys)] = member
        doc_name = f"{base_name}-{proj}" if multi else base_name
        docs.append(
            ScheduleSetDocument(
                apiVersion=SPEC_VERSION,
                kind=SPEC_KIND,
                metadata=ScheduleSetMetadata(name=doc_name, project=proj),
                schedules=schedules,
            )
        )
    return docs


def convert_legacy_rows(
    rows: list[dict[str, Any]], *, flows_dir: Path, manifest_dir: Path
) -> tuple[list[ScheduleSetDocument], list[ExportReportLine]]:
    """Convert every chain-free, expressible legacy row into a member of a
    ``ScheduleSet`` document -- one document per distinct project among the
    rows (see ``_group_into_documents``), so a mixed-project export still
    round-trips every row's original qualified name exactly. Rows with
    ``on_success``/``on_fail``, an unsupported action_kind, a legacy-only
    field with no v1 equivalent (a flow_yaml row's action_model, non-empty
    action_extra_args, a non-default github poll_interval_sec), or a
    malformed trigger are reported ``BLOCKED`` and omitted -- never
    half-emitted. Within each document, a row whose own project matches is
    keyed by its local (prefix-stripped) name, so re-applying reconstructs
    its original qualified name exactly (see ``_member_key``)."""
    lines: list[ExportReportLine] = []
    ready: list[tuple[dict[str, Any], ScheduleMember]] = []
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
            member = _build_legacy_member(row, flows_dir=flows_dir, manifest_dir=manifest_dir)
        except ValueError as exc:
            lines.append(ExportReportLine(name, "BLOCKED", str(exc)))
            continue
        try:
            resolve_member(name, member, manifest_dir=manifest_dir, project=None, is_global=False)
        except ValueError as exc:
            lines.append(ExportReportLine(name, "BLOCKED", f"static resolution failed: {exc}"))
            continue
        ready.append((row, member))
        lines.append(ExportReportLine(name, "READY", _ready_note(row, member, "legacy-export")))

    docs = _group_into_documents(ready, rows, base_name="legacy-export")
    return docs, lines


def build_managed_export_document(
    rows: list[dict[str, Any]],
) -> tuple[list[ScheduleSetDocument], list[ExportReportLine]]:
    """Re-serialize every declaration/cli-managed row's persisted
    ``authored_spec`` back into ``ScheduleSet`` documents -- one per distinct
    project among the rows (see ``_group_into_documents``), so a
    mixed-project export still round-trips every row's original qualified
    name exactly. Within each document, a row whose own project matches is
    keyed by its local (prefix-stripped) name so re-applying reconstructs
    its original qualified name (see ``_member_key``)."""
    lines: list[ExportReportLine] = []
    ready: list[tuple[dict[str, Any], ScheduleMember]] = []
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
        ready.append((row, member))
        lines.append(ExportReportLine(name, "READY", _ready_note(row, member, "export")))

    docs = _group_into_documents(ready, rows, base_name="export")
    return docs, lines


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
