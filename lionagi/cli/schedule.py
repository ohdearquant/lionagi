# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li schedule` — manage lionagi Studio schedules from the CLI.

Talks to the Studio REST API (default http://127.0.0.1:8765).
Set LIONAGI_STUDIO_URL to override.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_DEFAULT_BASE = "http://127.0.0.1:8765"


def _base_url() -> str:
    return os.environ.get("LIONAGI_STUDIO_URL", _DEFAULT_BASE).rstrip("/")


def _api(path: str, method: str = "GET", body: dict | None = None) -> Any:
    """Minimal HTTP helper — no extra deps beyond stdlib urllib."""
    import urllib.error
    import urllib.request

    url = f"{_base_url()}/schedules{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode(errors="replace")
        print(f"Error {exc.code}: {msg}", file=sys.stderr)
        return None
    except OSError as exc:
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
        print(f"  {s['id']}  {s['name']:<30} [{status}]  {s.get('trigger_type', '?')}")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}")
    if result is None:
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "name": args.name,
        "trigger_type": args.trigger_type,
        "action_kind": args.action_kind,
    }
    if args.cron:
        body["cron_expr"] = args.cron
    if args.interval:
        body["interval_sec"] = args.interval
    if args.prompt:
        body["action_prompt"] = args.prompt
    if args.model:
        body["action_model"] = args.model
    if args.agent:
        body["action_agent"] = args.agent
    if args.playbook:
        body["action_playbook"] = args.playbook
    if args.project:
        body["action_project"] = args.project
    if args.description:
        body["description"] = args.description
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
    result = _api(f"/{args.id}/disable", method="POST")
    if result is None:
        return 1
    print(f"Disabled: {args.id}")
    return 0


def _cmd_trigger(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/trigger", method="POST")
    if result is None:
        return 1
    print(f"Triggered: {args.id}")
    if isinstance(result, dict) and result.get("run_id"):
        print(f"Run: {result['run_id']}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}", method="DELETE")
    if result is None:
        return 1
    print(f"Deleted: {args.id}")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/runs")
    if result is None:
        return 1
    runs = result.get("runs", [])
    if not runs:
        print("(no runs)")
        return 0
    for r in runs:
        print(f"  {r['id']}  [{r.get('status', '?')}]  {r.get('started_at', '?')}")
    return 0


def add_schedule_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li schedule` sub-command."""
    sched = subparsers.add_parser(
        "schedule",
        help="Manage lionagi Studio schedules.",
        description=(
            "Create, list, enable, disable, trigger, and delete "
            "schedules via the Studio API (default http://127.0.0.1:8765). "
            "Set LIONAGI_STUDIO_URL to use a different base URL."
        ),
    )
    sched_sub = sched.add_subparsers(dest="schedule_action")
    sched_sub.required = True

    # list
    sched_sub.add_parser("list", help="List all schedules.")

    # get
    get_p = sched_sub.add_parser("get", help="Show schedule details.")
    get_p.add_argument("id", help="Schedule ID.")

    # create
    create_p = sched_sub.add_parser("create", help="Create a new schedule.")
    create_p.add_argument("name", help="Schedule name.")
    create_p.add_argument(
        "--trigger-type",
        dest="trigger_type",
        default="cron",
        choices=("cron", "interval", "github"),
        help="Trigger type (default: cron).",
    )
    create_p.add_argument("--cron", metavar="EXPR", help='Cron expression, e.g. "0 * * * *".')
    create_p.add_argument("--interval", type=int, metavar="SECONDS", help="Interval in seconds.")
    create_p.add_argument(
        "--action-kind",
        dest="action_kind",
        default="agent",
        choices=("agent", "playbook"),
        help="Action kind (default: agent).",
    )
    create_p.add_argument("--prompt", help="Prompt for agent action.")
    create_p.add_argument("--model", help="Model spec for agent action.")
    create_p.add_argument("--agent", help="Agent profile name.")
    create_p.add_argument("--playbook", help="Playbook name (for action-kind=playbook).")
    create_p.add_argument("--project", help="Project name.")
    create_p.add_argument("--description", help="Human-readable description.")

    # enable / disable / trigger / delete
    for sub_name, sub_help in (
        ("enable", "Enable a schedule."),
        ("disable", "Disable a schedule."),
        ("trigger", "Fire a schedule immediately."),
        ("delete", "Delete a schedule."),
    ):
        p = sched_sub.add_parser(sub_name, help=sub_help)
        p.add_argument("id", help="Schedule ID.")

    # runs
    runs_p = sched_sub.add_parser("runs", help="List runs for a schedule.")
    runs_p.add_argument("id", help="Schedule ID.")


_ACTION_MAP = {
    "list": _cmd_list,
    "get": _cmd_get,
    "create": _cmd_create,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "trigger": _cmd_trigger,
    "delete": _cmd_delete,
    "runs": _cmd_runs,
}


def run_schedule(args: argparse.Namespace) -> int:
    action = getattr(args, "schedule_action", None)
    fn = _ACTION_MAP.get(action)
    if fn is None:
        print(
            "Usage: li schedule <subcommand>  (list|get|create|enable|disable|trigger|delete|runs)"
        )
        return 1
    return fn(args)
