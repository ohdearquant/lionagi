# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Implement the `li schedule` command."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from lionagi._auto import CliDeclaration, auto_register
from lionagi._log_channels import log_error, warn
from lionagi.state.db import SCHEDULE_RUN_TERMINAL_STATUSES
from lionhub.orchestration.console import _refused_inside_an_agent
from lionhub.orchestration.schedule.parsing import (
    _base_url,
    _is_schedule_request_timeout,
    _normalize_schedule_reason,
    _schedule_request_timeout_message,
    _validate_identifier,
    add_schedule_subparser,
    build_create_body,
    build_quick_create_parser,
)

# --- `li schedule` — manage lionagi Studio schedules from the CLI ---


_SCHEDULE_API_TIMEOUT_SECONDS = 10.0


def _api(path: str, method: str = "GET", body: dict | None = None) -> Any:
    """Minimal HTTP helper — no extra deps beyond stdlib urllib."""
    import urllib.error
    import urllib.request

    from lionagi.ln._ssrf import open_url

    url = f"{_base_url()}/api/schedules{path}"
    data = json.dumps(body).encode() if body is not None else None
    declares_json = data is not None or method.upper() not in {"GET", "HEAD", "OPTIONS"}
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if declares_json else {},
    )
    started_at = time.monotonic()
    try:
        with open_url(req, timeout=_SCHEDULE_API_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode(errors="replace")
        print(f"Error {exc.code}: {msg}", file=sys.stderr)
        return None
    except OSError as exc:
        if _is_schedule_request_timeout(exc):
            elapsed_seconds = max(0.0, time.monotonic() - started_at)
            print(
                _schedule_request_timeout_message(
                    method=method,
                    url=url,
                    elapsed_seconds=elapsed_seconds,
                    limit_seconds=_SCHEDULE_API_TIMEOUT_SECONDS,
                ),
                file=sys.stderr,
            )
            return None
        print(
            f"Cannot reach Studio at {_base_url()} — is `li studio` running? ({exc})",
            file=sys.stderr,
        )
        return None


def _cmd_list(args: argparse.Namespace) -> int:
    result = _api("/")
    if result is None:
        return 1
    schedules = result.get("schedules", [])
    if not schedules:
        print("(no schedules)")
        return 0
    for s in schedules:
        status = "enabled" if s.get("enabled") else "disabled"
        line = f"  {s['id']}  {s['name']:<30} [{status}]  {s.get('trigger_type', '?')}"
        if s.get("max_runs"):
            line += f"  (runs left: {s.get('remaining_runs')}/{s['max_runs']})"
        print(line)
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}")
    if result is None:
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_limits(args: argparse.Namespace) -> int:
    result = _api("/limits")
    if result is None:
        return 1
    cap = result.get("max_scheduled_concurrent")
    cap_display = "unlimited" if not cap else str(cap)
    print(f"Max concurrent fires:      {cap_display}")
    print(f"Current in-flight:         {result.get('current_inflight', 0)}")
    holders = result.get("current_holders") or []
    print(f"Current holders:          {len(holders)}")
    for holder in holders:
        print(
            f"  {holder.get('schedule_id', '?')}  {holder.get('path', '?')}"
            f"  acquired: {_fmt_rfc3339(holder.get('acquired_at'))}"
        )
    adhoc_cap = result.get("max_adhoc_concurrent")
    adhoc_cap_display = "unlimited" if not adhoc_cap else str(adhoc_cap)
    print(f"Max concurrent ad-hoc:     {adhoc_cap_display}")
    print(f"Current ad-hoc in-flight:  {result.get('current_adhoc_inflight', 0)}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    body, err = build_create_body(args)
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    result = _api("/", method="POST", body=body)
    if result is None:
        return 1
    print(f"Created: {result.get('id')}  {result.get('name')}")
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/enable", method="POST")
    if result is None:
        return 1
    print(f"Enabled: {args.id}")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    body = None
    if args.reason is not None:
        try:
            body = {"reason": _normalize_schedule_reason(args.reason)}
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    result = _api(f"/{args.id}/disable", method="POST", body=body)
    if result is None:
        return 1
    print(f"Disabled: {args.id}")
    return 0


# fire_now() hands the run_id back to the HTTP caller before its occurrence
# row is durably written (the fire runs as a background task) -- a lookup
# immediately after trigger can race that insert. Retry within this bounded
# grace period instead of a single up-front lookup.
_TRIGGER_WAIT_GRACE_SECONDS = 5.0
_TRIGGER_WAIT_GRACE_POLL_SECONDS = 0.2
_TRIGGER_WAIT_POLL_SECONDS = 2.0
_TRIGGER_WAIT_MAX_SECONDS = 600.0


def _fmt_rfc3339(ts: float | None) -> str:
    if ts is None:
        return "-"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _fmt_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    secs = ms // 1000
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h{mins:02d}m"


def _fmt_outcome(outcome: dict[str, Any] | None) -> str:
    if not outcome:
        return "-"
    code, summary = outcome.get("code", "?"), outcome.get("summary")
    return f"{code}: {summary}" if summary and summary != code else code


def _fmt_artifacts(artifacts: list[str] | None) -> str:
    if not artifacts:
        return "-"
    return artifacts[0] if len(artifacts) == 1 else f"{artifacts[0]} (+{len(artifacts) - 1} more)"


def _print_run_table(runs: list[dict[str, Any]]) -> None:
    header = f"{'RUN':<14}{'STATUS':<11}{'FIRED':<26}{'DURATION':<10}{'OUTCOME':<32}{'INVOCATION':<14}ARTIFACTS"
    print(header)
    for r in runs:
        outcome = _fmt_outcome(r.get("outcome"))
        print(
            f"{r.get('id', '?'):<14}{r.get('status', '?'):<11}{_fmt_rfc3339(r.get('fired_at')):<26}"
            f"{_fmt_duration_ms(r.get('duration_ms')):<10}{outcome[:30]:<32}"
            f"{r.get('invocation_id') or '-':<14}{_fmt_artifacts(r.get('artifacts'))}"
        )
        if r.get("reason_code") or r.get("reason_summary"):
            print(f"  reason: {r.get('reason_code') or '-'} — {r.get('reason_summary') or '-'}")


def _cmd_trigger(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/trigger", method="POST")
    if result is None:
        return 1
    print(f"Triggered: {args.id}")
    run_id = result.get("run_id") if isinstance(result, dict) else None
    if not run_id:
        return 0
    print(f"Run: {run_id}")
    if not getattr(args, "wait", False):
        return 0
    return _wait_for_run(run_id)


def _wait_for_run(run_id: str) -> int:
    """Poll `/schedules/runs/{run_id}` for the occurrence, tolerating the
    grace-period race, then for a terminal status."""
    import time as _time

    deadline_grace = _time.monotonic() + _TRIGGER_WAIT_GRACE_SECONDS
    run = _api(f"/runs/{run_id}")
    while run is None and _time.monotonic() < deadline_grace:
        _time.sleep(_TRIGGER_WAIT_GRACE_POLL_SECONDS)
        run = _api(f"/runs/{run_id}")
    if run is None:
        print(f"Error: run {run_id!r} never appeared", file=sys.stderr)
        return 1

    deadline = _time.monotonic() + _TRIGGER_WAIT_MAX_SECONDS
    while run.get("status") not in SCHEDULE_RUN_TERMINAL_STATUSES and _time.monotonic() < deadline:
        _time.sleep(_TRIGGER_WAIT_POLL_SECONDS)
        run = _api(f"/runs/{run_id}")
        if run is None:
            print(f"Error: run {run_id!r} disappeared while waiting", file=sys.stderr)
            return 1

    print(f"status: {run.get('status', '?')}  outcome: {_fmt_outcome(run.get('outcome'))}")
    return 0 if run.get("status") == "completed" else 1


def _cmd_delete(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}", method="DELETE")
    if result is None:
        return 1
    print(f"Deleted: {args.id}")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    path = f"/{args.id}/runs?limit={args.limit}"
    for status in getattr(args, "status", None) or ():
        path += f"&status={status}"
    result = _api(path)
    if result is None:
        return 1
    runs = result.get("runs", [])
    if getattr(args, "as_json", False):
        print(json.dumps(result))
        return 0
    if not runs:
        print("(no runs)")
        return 0
    _print_run_table(runs)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    result = _api(f"/runs/{args.id}")
    if result is None:
        return 1
    if getattr(args, "as_json", False):
        print(json.dumps(result))
    else:
        _print_run_table([result])
    return 0


def _status_still_running(result: dict[str, Any] | None) -> bool:
    latest = (result or {}).get("latest_run") or {}
    status = latest.get("status")
    return status is not None and status not in SCHEDULE_RUN_TERMINAL_STATUSES


def _cmd_status(args: argparse.Namespace) -> int:
    import time as _time

    result = _api(f"/{args.id}/status")
    if getattr(args, "wait", False):
        deadline = _time.monotonic() + _TRIGGER_WAIT_MAX_SECONDS
        while result is not None and _status_still_running(result) and _time.monotonic() < deadline:
            _time.sleep(_TRIGGER_WAIT_POLL_SECONDS)
            result = _api(f"/{args.id}/status")
    if result is None:
        return 1

    if getattr(args, "as_json", False):
        print(json.dumps(result))
        return int(result.get("exit_code", 2))

    schedule = result.get("schedule") or {}
    latest = result.get("latest_run")
    trigger = (
        f'cron "{schedule["cron_expr"]}"'
        if schedule.get("cron_expr")
        else (f"every {schedule['interval_sec']}s" if schedule.get("interval_sec") else "-")
    )
    print(
        f"{schedule.get('id', args.id)}  {'enabled' if schedule.get('enabled') else 'disabled'}  {trigger}"
    )
    print(f"next:        {_fmt_rfc3339(schedule.get('next_fire_at'))}")
    if latest is None:
        print("last run:    (none)")
        return int(result.get("exit_code", 2))
    print(f"last run:    {latest.get('id')}")
    exit_code_str = f" (exit {latest['exit_code']})" if latest.get("exit_code") is not None else ""
    print(f"status:      {latest.get('status', '?')}{exit_code_str}")
    print(f"fired:       {_fmt_rfc3339(latest.get('fired_at'))}")
    if latest.get("ended_at") is not None:
        print(
            f"ended:       {_fmt_rfc3339(latest.get('ended_at'))}  ({_fmt_duration_ms(latest.get('duration_ms'))})"
        )
    outcome = latest.get("outcome") or {}
    print(f"outcome:     {outcome.get('code', '-')} — {outcome.get('summary', '-')}")
    if latest.get("invocation_id"):
        print(f"invocation:  {latest['invocation_id']}")
    for sid in latest.get("session_ids") or ():
        print(f"session:     {sid}")
    for path in latest.get("artifacts") or ():
        print(f"artifacts:   {path}")
    if latest.get("invocation_id"):
        print(f"inspect:     li monitor {latest['invocation_id']}")
    return int(result.get("exit_code", 2))


def _load_schedule_set_doc(path_str: str):
    """Read + parse a ScheduleSet file. Prints an error and returns
    ``(None, None)`` on any failure -- callers just check for None."""
    from lionhub.orchestration.schedule.declaration import parse_schedule_set

    path = Path(path_str)
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return None, None
    try:
        doc = parse_schedule_set(path.read_text(), source=str(path))
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
        print(f"Error: {exc}", file=sys.stderr)
        return None, None
    return doc, path.resolve().parent


def _cmd_validate_set(args: argparse.Namespace) -> int:
    from lionhub.orchestration.schedule.declaration import ScheduleSetError, resolve_schedule_set

    doc, manifest_dir = _load_schedule_set_doc(args.file)
    if doc is None:
        return 1
    owner_key = f"{doc.metadata.project}/{doc.metadata.name}"
    try:
        resolved = resolve_schedule_set(doc, manifest_dir)
    except ScheduleSetError as exc:
        if args.as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "owner_key": owner_key,
                        "errors": [{"name": n, "message": m} for n, m in exc.errors],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for name, message in exc.errors:
                # Every raise site carries the qualified name, so it prints as it stands.
                warn(f"INVALID  {name}  {message}")
        return 1

    if args.as_json:
        print(
            json.dumps(
                {
                    "valid": True,
                    "owner_key": owner_key,
                    "schedules": {name: r.resolved for name, r in resolved.items()},
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    else:
        print(f"VALID  {owner_key}  {len(resolved)} schedule(s)")
    return 0


def _fmt_plan_line(entry, set_ref: str) -> str:
    from lionhub.orchestration.schedule.declaration import PlanEntry

    e: PlanEntry = entry
    if e.action == "CREATE":
        target = e.resolved.resolved["target"]
        model = target.get("model")
        cwd = e.resolved.resolved["execution"]["cwd"]
        extra = f"  model={model}" if model else ""
        return f"CREATE     {e.qualified_name}{extra}  cwd={cwd}"
    if e.action == "UPDATE":
        if e.model_change is not None:
            stored, declared = e.model_change
            return f"UPDATE     {e.qualified_name}  model={stored} -> {declared}"
        return f"UPDATE     {e.qualified_name}"
    if e.action == "ADOPT":
        origin = (
            f"  adopts existing row {e.adopted_from}" if e.adopted_from else "  adopts existing row"
        )
        return f"ADOPT      {e.qualified_name}{origin}"
    if e.action == "UNCHANGED":
        return f"UNCHANGED  {e.qualified_name}"
    if e.action == "DISABLE":
        return f"DISABLE    {e.qualified_name}     omitted from set {set_ref}"
    return f"ERROR      {e.qualified_name}  {e.detail}"


def _cmd_apply_set(args: argparse.Namespace) -> int:
    from lionagi.state.db import StateDB
    from lionhub.orchestration.schedule.declaration import (
        ScheduleSetError,
        apply_schedule_set,
        build_plan,
    )

    doc, manifest_dir = _load_schedule_set_doc(args.file)
    if doc is None:
        return 1
    set_ref = f"{doc.metadata.project}/{doc.metadata.name}"

    async def _run():
        async with StateDB() as db:
            if args.dry_run:
                plan, _resolved = await build_plan(db, doc, manifest_dir, adopt=args.adopt)
                return plan
            result = await apply_schedule_set(db, doc, manifest_dir, adopt=args.adopt)
            return result.plan

    try:
        plan = asyncio.run(_run())
    except ScheduleSetError as exc:
        if args.as_json:
            print(
                json.dumps(
                    {"valid": False, "errors": [{"name": n, "message": m} for n, m in exc.errors]},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for name, message in exc.errors:
                # Every raise site carries the qualified name, so it prints as it stands.
                warn(f"INVALID  {name}  {message}")
        return 1

    has_errors = any(e.action == "ERROR" for e in plan)
    if args.as_json:
        print(
            json.dumps(
                {
                    "dry_run": bool(args.dry_run),
                    "set": set_ref,
                    "plan": [
                        {
                            "name": e.qualified_name,
                            "action": e.action,
                            "detail": e.detail,
                            **({"adopted_from": e.adopted_from} if e.adopted_from else {}),
                            **(
                                {
                                    "model_change": {
                                        "from": e.model_change[0],
                                        "to": e.model_change[1],
                                    }
                                }
                                if e.model_change is not None
                                else {}
                            ),
                        }
                        for e in plan
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for entry in plan:
            # A refused entry goes through the CLI's warning channel, as every
            # other refusal here does; the rest of the plan is ordinary output.
            if entry.action == "ERROR":
                warn(_fmt_plan_line(entry, set_ref))
            else:
                print(_fmt_plan_line(entry, set_ref))
        created = sum(1 for e in plan if e.action == "CREATE")
        adopted = sum(1 for e in plan if e.action == "ADOPT")
        updated = sum(1 for e in plan if e.action == "UPDATE")
        unchanged = sum(1 for e in plan if e.action == "UNCHANGED")
        disabled = sum(1 for e in plan if e.action == "DISABLE")
        # The adopt count appears only when there is one, so output without
        # --adopt reads as it did before adoption existed.
        if args.dry_run:
            adopt_part = f"{adopted} adopt, " if adopted else ""
            print(
                f"Plan: {created} create, {adopt_part}{updated} update, "
                f"{unchanged} unchanged, {disabled} disable"
            )
        elif not has_errors:
            adopt_part = f"{adopted} adopted, " if adopted else ""
            print(
                f"Applied atomically: {created} created, {adopt_part}{updated} updated, "
                f"{unchanged} unchanged, {disabled} disabled"
            )
    return 1 if has_errors else 0


# Distinct from 0 (clean success) and 1 (hard failure, e.g. an unreadable
# input file elsewhere in this CLI); mirrors the EXIT_UNKNOWN=2 "ambiguous,
# needs attention" convention used by `li status`/`li monitor`/`li wait` --
# the document and report are still emitted exactly as on a clean export.
EXIT_EXPORT_PARTIAL = 2


def _cmd_export(args: argparse.Namespace) -> int:
    """`li schedule export` — convert rows into ScheduleSet document(s), one
    per distinct project when the export spans more than one. Read-only:
    never opens a write transaction against the database. Exit code 2 (not
    0/1) when any row was BLOCKED -- see EXIT_EXPORT_PARTIAL."""
    from lionagi.state.db import StateDB
    from lionhub.orchestration.schedule.export import (
        build_managed_export_document,
        convert_legacy_rows,
        dump_schedule_set_yaml,
        format_report,
        is_legacy_row,
        is_managed_row,
    )

    output_path = Path(args.output).resolve() if args.output else None
    manifest_dir = output_path.parent if output_path else Path.cwd()
    flows_dir = (
        manifest_dir / f"{output_path.stem}.flows"
        if output_path
        else manifest_dir / "exported-flows"
    )

    async def _run():
        async with StateDB() as db:
            rows = await db.list_schedules(limit=1_000_000)
            if args.legacy:
                return convert_legacy_rows(
                    [r for r in rows if is_legacy_row(r)],
                    flows_dir=flows_dir,
                    manifest_dir=manifest_dir,
                )
            return build_managed_export_document([r for r in rows if is_managed_row(r)])

    docs, lines = asyncio.run(_run())

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if len(docs) == 1:
            output_path.write_text(dump_schedule_set_yaml(docs[0]))
        else:
            # Mixed-project export: one file per project, suffixed with its
            # project so none silently overwrite `--output`. The sanitizer is
            # not injective (foo/bar and foo:bar both become foo_bar), so
            # collisions are rejected before ANY sibling is written rather
            # than letting the last document win.
            tokens: dict[str, str] = {}
            for doc in docs:
                token = re.sub(r"[^a-zA-Z0-9_.-]", "_", doc.metadata.project)
                if token in tokens:
                    log_error(
                        f"cannot export: projects {tokens[token]!r} and "
                        f"{doc.metadata.project!r} both sanitize to sibling file "
                        f"token {token!r}; export to stdout or rename a project"
                    )
                    return 1
                tokens[token] = doc.metadata.project
            for doc, token in zip(docs, tokens, strict=True):
                sibling = output_path.with_name(f"{output_path.stem}.{token}{output_path.suffix}")
                sibling.write_text(dump_schedule_set_yaml(doc))
    else:
        print("\n---\n".join(dump_schedule_set_yaml(doc) for doc in docs), end="")

    report_text = format_report(lines)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text)
    else:
        print(report_text, file=sys.stderr, end="")

    has_blocked = any(line.status == "BLOCKED" for line in lines)
    return EXIT_EXPORT_PARTIAL if has_blocked else 0


def _quick_create_trigger(args: argparse.Namespace) -> Any:
    from lionhub.orchestration.schedule.declaration import CronTrigger, GithubTriggerSpec, Trigger

    if args.at:
        return Trigger(at=args.at)
    if args.cron:
        if not args.timezone:
            print("Error: --cron requires --timezone.", file=sys.stderr)
            return None
        return Trigger(cron=CronTrigger(expression=args.cron, timezone=args.timezone))
    if args.every:
        return Trigger(every=args.every)
    # args.github — the only remaining branch, guaranteed by the required
    # mutually-exclusive trigger group.
    github_filter = None
    if getattr(args, "github_filter", None):
        try:
            github_filter = json.loads(args.github_filter)
        except (ValueError, TypeError) as exc:
            print(f"Error: --github-filter must be valid JSON: {exc}", file=sys.stderr)
            return None
        if not isinstance(github_filter, dict):
            print("Error: --github-filter must be a JSON object.", file=sys.stderr)
            return None
    return Trigger(github=GithubTriggerSpec(repo=args.github, filter=github_filter))


def _quick_create_policies(args: argparse.Namespace) -> Any:
    from pydantic import ValidationError

    from lionhub.orchestration.schedule.declaration import Budget, Policies

    if args.once and args.max_runs is not None:
        print("Error: --once and --max-runs are mutually exclusive.", file=sys.stderr)
        return None
    max_runs = 1 if args.once else args.max_runs

    budget = None
    if args.budget_usd is not None or args.budget_tokens is not None:
        try:
            budget = Budget(usd=args.budget_usd, tokens=args.budget_tokens)
        except ValidationError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return None

    rate_limit = None
    if getattr(args, "rate_limit", None):
        try:
            rate_limit = json.loads(args.rate_limit)
        except (ValueError, TypeError) as exc:
            print(f"Error: --rate-limit must be valid JSON: {exc}", file=sys.stderr)
            return None

    try:
        return Policies(
            missedFire=args.missed_fire,
            overlap=args.overlap,
            maxRuns=max_runs,
            budget=budget,
            rateLimit=rate_limit,
        )
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _quick_create_target(kind: str, args: argparse.Namespace) -> Any:
    from lionhub.orchestration.schedule.declaration import (
        AgentTarget,
        CommandTarget,
        FlowTarget,
        PlaybookTarget,
    )

    if kind == "agent":
        prompt = args.prompt
        if args.prompt_file:
            if prompt is not None:
                print("Error: --prompt and --prompt-file are mutually exclusive.", file=sys.stderr)
                return None
            if args.prompt_file == "-":
                prompt = sys.stdin.read()
            else:
                try:
                    prompt = Path(args.prompt_file).read_text()
                except OSError as exc:
                    print(f"Error: could not read --prompt-file: {exc}", file=sys.stderr)
                    return None
        if not prompt or not prompt.strip():
            print("Error: agent target requires --prompt or --prompt-file.", file=sys.stderr)
            return None
        return AgentTarget(kind="agent", profile=args.profile, prompt=prompt, model=args.model)

    if kind == "flow":
        return FlowTarget(kind="flow", file=args.file)

    if kind == "playbook":
        arg_dict: dict[str, str] = {}
        for item in args.args or []:
            if "=" not in item:
                print(f"Error: --arg must be key=value, got {item!r}.", file=sys.stderr)
                return None
            key, _, value = item.partition("=")
            arg_dict[key] = value
        return PlaybookTarget(kind="playbook", name=args.playbook, args=arg_dict)

    # kind == "command": trailing `-- argv...`, never a shell string. The
    # executable/args tokens were already split off before argparse ran (see
    # run_schedule_quick_create) and attached as args.command_argv.
    rest = getattr(args, "command_argv", None)
    if not rest:
        print(
            "Error: command target requires a trailing '--' before the "
            "executable, e.g. `li schedule create command NAME --every 15m "
            "-- refresh-index --incremental`.",
            file=sys.stderr,
        )
        return None
    return CommandTarget(kind="command", executable=rest[0], args=rest[1:])


def run_schedule_quick_create(kind: str, argv: list[str]) -> int:
    """`li schedule create <kind> <name> ...` entry point."""
    if _refused_inside_an_agent():
        return 1
    from pydantic import ValidationError

    from lionagi.state.db import StateDB
    from lionhub.orchestration.schedule.declaration import (
        Execution,
        ScheduleMember,
        ScheduleSetError,
        create_quick_schedule,
    )

    parser = build_quick_create_parser(kind)
    command_argv: list[str] = []
    if kind == "command":
        # Split off the executable/argv at a literal '--' *before* argparse
        # runs: nargs=REMAINDER on a positional would otherwise greedily
        # swallow the trigger/policy flags that come after `name` too.
        if "--" not in argv:
            print(
                "Error: command target requires a trailing '--' before the "
                "executable, e.g. `li schedule create command NAME --every 15m "
                "-- refresh-index --incremental`.",
                file=sys.stderr,
            )
            return 1
        i = argv.index("--")
        argv, command_argv = argv[:i], argv[i + 1 :]
    args = parser.parse_args(argv)
    if kind == "command":
        args.command_argv = command_argv

    target = _quick_create_target(kind, args)
    if target is None:
        return 1
    trigger = _quick_create_trigger(args)
    if trigger is None:
        return 1
    policies = _quick_create_policies(args)
    if policies is None:
        return 1

    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd()

    # Best-effort project auto-detection, same cascade as the legacy create
    # path; any failure here must never block schedule creation.
    project: str | None = None
    with contextlib.suppress(Exception):
        from lionhub.orchestration.project import detect_project
        from lionhub.orchestration.schedule.parsing import _validate_identifier

        detected, _source = detect_project(cwd)
        if detected:
            _validate_identifier(detected, "action_project")
            project = detected

    try:
        member = ScheduleMember(
            description=args.description,
            enabled=not args.disabled,
            trigger=trigger,
            target=target,
            execution=Execution(cwd=str(cwd)),
            policies=policies,
        )
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    async def _run():
        async with StateDB() as db:
            return await create_quick_schedule(db, args.name, member, cwd=cwd, project=project)

    try:
        schedule_id, resolved = asyncio.run(_run())
    except ScheduleSetError as exc:
        for _name, message in exc.errors:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    print(f"Created: {schedule_id}  {resolved.qualified_name}")
    return 0


# The schedule subcommands that can have the Studio daemon start a run. The daemon runs
# outside the asking process's tree, so no check at spawn can see that the request came
# from inside an agent; the refusal happens here, before the request leaves.
_STARTS_RUNS = frozenset({"create", "enable", "trigger", "apply"})


_ACTION_MAP = {
    "list": _cmd_list,
    "get": _cmd_get,
    "limits": _cmd_limits,
    "create": _cmd_create,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "trigger": _cmd_trigger,
    "delete": _cmd_delete,
    "runs": _cmd_runs,
    "run": _cmd_run,
    "status": _cmd_status,
    "validate": _cmd_validate_set,
    "apply": _cmd_apply_set,
    "export": _cmd_export,
}


@auto_register(
    area="schedule", cli=CliDeclaration(seed="schedule", parser_factory=add_schedule_subparser)
)
def run_schedule(args: argparse.Namespace) -> int:
    action = getattr(args, "schedule_action", None)
    fn = _ACTION_MAP.get(action)
    if fn is None:
        print(
            "Usage: li schedule <subcommand>  (list|get|limits|create|enable|disable|"
            "trigger|delete|runs|run|status|validate|apply|export)"
        )
        return 1
    if action == "runs" and not (1 <= args.limit <= 200):
        print(f"Error: --limit must be between 1 and 200, got {args.limit}.", file=sys.stderr)
        return 1
    if action in _STARTS_RUNS and _refused_inside_an_agent():
        return 1
    return fn(args)
