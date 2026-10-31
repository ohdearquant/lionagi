# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li schedule` argument parsing and request-body building.

Shared by the human command path and the machine one. The chain-depth cap, the
action-kind vocabulary and the identifier check are defined here; the scheduler
imports these names from this module, and this module imports nothing from
lionagi.studio, so importing it does not load the Studio scheduler.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
from pathlib import Path
from typing import Any

from lionagi._log_channels import warn
from lionagi.cli._argtypes import JsonArgument
from lionagi.ln import now_utc

_MAX_CHAIN_DEPTH = 10

# ADR-0070 defines the closed set of action kinds.  The CLI parser accepts
# "playbook" as an alias for "play" for backward compatibility.
# The "command" kind is an allow-listed executable spawned directly
# (not through `li`), with templated argv rendered from trigger_context.
_VALID_ACTION_KINDS = frozenset(
    {"agent", "flow", "fanout", "play", "flow_yaml", "engine", "command"}
)
_ALIAS_ACTION_KINDS: dict[str, str] = {"playbook": "play"}

# action_model must be a safe model-spec token: alphanumerics, dots, slashes,
# colons, hyphens and underscores only.  Values starting with '-' are rejected
# unconditionally to block flag injection into the spawned li process (CWE-88).
_MODEL_RE = re.compile(r"^[a-zA-Z0-9_./:@-]+\Z")

# Identifier fields (action_agent, action_project, action_playbook) share the
# same character set as model specs: they name agents, projects, and playbooks
# and have no legitimate use for leading '-'.
_IDENT_RE = _MODEL_RE


def _validate_identifier(value: str, field_name: str) -> None:
    """Raise ValueError if *value* starts with '-' — argparse would misinterpret it as a flag (CWE-88)."""
    if not value:
        return
    if value.startswith("-"):
        raise ValueError(
            f"{field_name} {value!r} starts with '-' and is not a valid identifier. "
            "Identifier fields must not begin with '-'."
        )
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"{field_name} {value!r} contains characters not allowed in an identifier. "
            "Allowed: letters, digits, '_', '.', '/', ':', '@', '-'."
        )


def _normalize_schedule_reason(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("--reason must not be blank")
    if len(value) > 500:
        raise ValueError("--reason must be at most 500 characters")
    return value


# Keys the scheduler engine's chain-fire merge actually understands;
# anything else would shallow-merge into the fired child row and clobber columns.
_CHAIN_ACTION_ALLOWED_KEYS = frozenset(
    {"kind", "action_kind", "model", "prompt", "agent", "playbook", "on_success", "on_fail"}
)


def _is_schedule_request_timeout(exc: OSError) -> bool:
    """Return whether urllib stopped because the request exceeded its deadline."""
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def _schedule_request_timeout_message(
    *, method: str, url: str, elapsed_seconds: float, limit_seconds: float
) -> str:
    """Describe a timeout without claiming the mutation did not land."""
    return (
        f"Studio request {method} {url} timed out "
        f"(elapsed {elapsed_seconds:.1f}s; limit {limit_seconds:g}s). "
        "The request may still have completed; verify schedule state before retrying."
    )


def _validate_chain_action_node(
    action: Any,
    label: str,
    self_field: str,
    chain_depth: int,
    max_chain_depth: int,
) -> str | None:
    """Validate one chain_action node, recursing into its own nested
    on_success/on_fail the same way the engine's chain-fire would reach them."""
    if not isinstance(action, dict):
        return f"{label}: must be a JSON object, got {type(action).__name__}"

    unknown = set(action) - _CHAIN_ACTION_ALLOWED_KEYS
    if unknown:
        allowed = ", ".join(sorted(_CHAIN_ACTION_ALLOWED_KEYS))
        return f"{label}: unknown key(s) {sorted(unknown)}; allowed: {allowed}"

    if self_field not in action:
        warn(
            f'{label} does not set its own "{self_field}" key — under the '
            f"engine's shallow merge, the chained run will inherit its "
            f"parent's {self_field} and may re-fire again at the next chain "
            f'depth. Add "{self_field}": null to the JSON to stop the chain '
            "here."
        )

    if chain_depth >= max_chain_depth:
        return None

    for nested_field in ("on_success", "on_fail"):
        if nested_field in action and action[nested_field] is not None:
            err = _validate_chain_action_node(
                action[nested_field],
                f"{label}.{nested_field}",
                nested_field,
                chain_depth + 1,
                max_chain_depth,
            )
            if err:
                return err
    return None


def _parse_chain_action(raw: str, flag: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse+validate a --on-success/--on-fail JSON blob, recursively.

    Returns (parsed_dict, error_message); error_message is None on success.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{flag}: invalid JSON ({exc})"

    field = "on_success" if flag == "--on-success" else "on_fail"
    err = _validate_chain_action_node(
        parsed, flag, field, chain_depth=1, max_chain_depth=_MAX_CHAIN_DEPTH
    )
    if err:
        return None, err
    return parsed, None


def _warn_if_cron_far_out(cron_expr: str) -> None:
    """Best-effort heads-up when a cron expression's next fire is far out
    (e.g. a date-pinned one-shot created after this year's moment already passed)."""
    try:
        from croniter import croniter
    except ImportError:
        return
    import time as _time

    now = now_utc()
    try:
        next_fire = croniter(cron_expr, start_time=now).get_next(float)
    except Exception:
        return
    days_out = (next_fire - _time.time()) / 86400
    if days_out > 360:
        warn(
            f"cron {cron_expr!r} next fires in about {days_out:.0f} days. "
            "If you meant a one-shot for a specific date this year, the "
            "schedule may have been created after that date's moment has "
            "already passed (cron resolves in UTC) and silently waits a "
            "full year. Consider --max-runs / --once plus a nearer date."
        )


def build_create_body(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    """The POST body `li schedule create` sends, or the reason it cannot be built.

    Shared by the human path and the machine one so a rule only lives here: a
    second copy would drift, and the two paths would then disagree about which
    arguments are legal.
    """
    if args.once and args.max_runs is not None:
        return None, "--once and --max-runs are mutually exclusive."
    max_runs = 1 if args.once else args.max_runs
    if max_runs is not None and max_runs < 1:
        return None, f"--max-runs must be a positive integer, got {max_runs}."

    # 'github' is a friendly alias; the DB CHECK and scheduler engine only
    # recognize the canonical 'github_poll' token.
    trigger_type = "github_poll" if args.trigger_type == "github" else args.trigger_type

    action_kind = _ALIAS_ACTION_KINDS.get(args.action_kind, args.action_kind)
    body: dict[str, Any] = {
        "name": args.name,
        "trigger_type": trigger_type,
        "action_kind": action_kind,
    }
    if args.cron:
        body["cron_expr"] = args.cron
        _warn_if_cron_far_out(args.cron)
    if args.interval:
        body["interval_sec"] = args.interval
    if getattr(args, "github_repo", None):
        body["github_repo"] = args.github_repo
    if getattr(args, "github_filter", None):
        try:
            parsed_filter = json.loads(args.github_filter)
        except (ValueError, TypeError) as exc:
            return None, f"--github-filter must be valid JSON: {exc}"
        if not isinstance(parsed_filter, dict):
            return None, "--github-filter must be a JSON object."
        body["github_filter"] = parsed_filter
    if getattr(args, "threshold_config", None):
        try:
            parsed_threshold = json.loads(args.threshold_config)
        except (ValueError, TypeError) as exc:
            return None, f"--threshold-config must be valid JSON: {exc}"
        if not isinstance(parsed_threshold, dict):
            return None, "--threshold-config must be a JSON object."
        # Full value validation happens server-side; this is just a shape check.
        body["threshold_config"] = parsed_threshold
    if getattr(args, "poll_interval", None) is not None:
        if args.poll_interval < 1:
            return None, "--poll-interval must be a positive integer."
        body["poll_interval_sec"] = args.poll_interval
    if max_runs is not None:
        body["max_runs"] = max_runs
    if getattr(args, "max_cost_usd", None) is not None:
        if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
            return (
                None,
                f"--max-cost-usd must be a finite positive number, got {args.max_cost_usd}.",
            )
        body["budget_usd"] = args.max_cost_usd
    if getattr(args, "max_tokens", None) is not None:
        if args.max_tokens <= 0:
            return None, f"--max-tokens must be a positive integer, got {args.max_tokens}."
        body["budget_tokens"] = args.max_tokens
    if args.prompt:
        body["action_prompt"] = args.prompt
    if args.model:
        body["action_model"] = args.model
    if args.agent:
        body["action_agent"] = args.agent
    if args.playbook:
        body["action_playbook"] = args.playbook
    if getattr(args, "flow_yaml", None):
        p = Path(args.flow_yaml).expanduser()
        if not p.is_file():
            return None, f"flow-yaml file not found: {p}"
        body["action_flow_yaml"] = p.read_text()
    if getattr(args, "action_command", None):
        body["action_command"] = args.action_command
    if getattr(args, "action_command_args", None):
        # Already a list: the argument's own type decoded and checked it.
        body["action_command_args"] = args.action_command_args
    # ADR-0070 delta 1: persist a stable execution root instead of depending
    # on the daemon's cwd when it fires. An explicit --cwd always wins.
    if getattr(args, "cwd", None):
        resolved_cwd = Path(args.cwd).expanduser().resolve()
        if not resolved_cwd.is_dir():
            return None, f"--cwd path does not exist or is not a directory: {resolved_cwd}"
        body["action_cwd"] = str(resolved_cwd)

    if args.project:
        body["action_project"] = args.project
    else:
        # Best-effort: auto-capture the project from cwd (ADR-0063 detection
        # cascade). Any failure here must never block schedule creation.
        with contextlib.suppress(Exception):
            from lionagi.cli._project import detect_project

            detected, _source = detect_project(Path.cwd())
            if detected:
                _validate_identifier(detected, "action_project")
                body["action_project"] = detected

    if "action_cwd" not in body and "action_project" not in body:
        # Neither resolved: fall back to the CLI's own invocation directory
        # so the schedule still carries a stable execution root.
        body["action_cwd"] = str(Path.cwd())

    if args.description:
        body["description"] = args.description
    if args.on_success:
        parsed, err = _parse_chain_action(args.on_success, "--on-success")
        if err:
            return None, err
        body["on_success"] = parsed
    if args.on_fail:
        parsed, err = _parse_chain_action(args.on_fail, "--on-fail")
        if err:
            return None, err
        body["on_fail"] = parsed
    return body, None


# Common wrong spellings mapped to the real flag, checked before the fuzzy
# match below since some (e.g. --every) aren't close enough for difflib.
_SCHEDULE_FLAG_SYNONYMS: dict[str, str] = {
    "--every": "--interval",
    "--at": "--cron",
    "--action": "--action-kind",
    "--on_success": "--on-success",
    "--on_fail": "--on-fail",
    "--max_runs": "--max-runs",
}

# Populated by add_schedule_subparser() with every long option string across
# all `li schedule` subcommands, for fuzzy did-you-mean matching.
_ALL_SCHEDULE_FLAGS: set[str] = set()


def suggest_schedule_flag(token: str) -> str | None:
    """Return a suggested correction for an unrecognized `li schedule` flag."""
    if token in _SCHEDULE_FLAG_SYNONYMS:
        return _SCHEDULE_FLAG_SYNONYMS[token]
    import difflib

    matches = difflib.get_close_matches(token, _ALL_SCHEDULE_FLAGS, n=1, cutoff=0.6)
    return matches[0] if matches else None


def add_schedule_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register `li schedule` sub-command. Returns the `schedule` parser."""
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
    sched_sub.add_parser(
        "list",
        help="List all schedules.",
        epilog="Example: li schedule list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # get
    get_p = sched_sub.add_parser(
        "get",
        help="Show schedule details.",
        epilog="Example: li schedule get sched-abc123",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    get_p.add_argument(
        "id", help="Id of the schedule, as returned by `li schedule list` in its `id` field."
    )

    # limits
    sched_sub.add_parser(
        "limits",
        help="Show the global concurrent-fire cap and current in-flight count.",
        epilog="Example: li schedule limits",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # create
    create_p = sched_sub.add_parser(
        "create",
        help="Create a new schedule.",
        epilog=(
            "Examples:\n"
            '  li schedule create daily-digest --cron "0 9 * * *" \\\n'
            '      --prompt "summarize overnight activity"\n'
            "  li schedule create hourly-poll --interval 3600 --agent researcher\n"
            '  li schedule create one-shot-backfill --cron "0 18 2 7 *" --once\n'
            '  li schedule create nightly-chain --cron "0 2 * * *" --prompt build \\\n'
            '      --on-success \'{"prompt": "notify done", "on_success": null}\'\n'
            "      # WARNING: --on-success/--on-fail shallow-merge into the chained\n"
            "      # run — any key you omit is INHERITED from this schedule,\n"
            '      # including on_success/on_fail themselves; set "on_success": null\n'
            "      # explicitly at each level to stop the chain there."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    create_p.add_argument(
        "name",
        help=(
            "Unique name identifying this schedule. Reused as the display label in "
            "listings; a name already in use is rejected rather than overwritten."
        ),
    )
    create_p.add_argument(
        "--trigger-type",
        dest="trigger_type",
        default="cron",
        choices=("cron", "interval", "github", "github_poll"),
        help="Trigger type (default: cron). 'github' is an alias for 'github_poll'.",
    )
    create_p.add_argument(
        "--cron",
        metavar="EXPR",
        help=(
            'Cron expression, e.g. "0 * * * *". Required when --trigger-type is cron, '
            "which is the default; ignored for other trigger types."
        ),
    )
    create_p.add_argument(
        "--interval",
        type=int,
        metavar="SECONDS",
        help=(
            "Seconds between fires. Required when --trigger-type is interval; "
            "ignored for other trigger types."
        ),
    )
    create_p.add_argument(
        "--github-repo",
        dest="github_repo",
        metavar="OWNER/NAME",
        help="GitHub repository to poll (required for --trigger-type github/github_poll).",
    )
    create_p.add_argument(
        "--github-filter",
        dest="github_filter",
        metavar="JSON",
        help=(
            "JSON object filtering which PRs fire the trigger, e.g. "
            '\'{"state": "open", "base": "main"}\'.'
        ),
    )
    create_p.add_argument(
        "--threshold-config",
        dest="threshold_config",
        metavar="JSON",
        help=(
            "Metric threshold alert config as a JSON object: "
            '{"metric": "failed_sessions|total_cost_usd|p95_latency_ms|'
            'github_poll_healthy_age_minutes|github_poll_consecutive_401", '
            '"op": "gt|gte", "value": N, "window_minutes": N}. When set, '
            "this schedule's own cron/interval cadence only evaluates the "
            "metric on each tick and fires the action only when the "
            "threshold is breached (cooldown = window_minutes). Full "
            'validation happens server-side, e.g. \'{"metric": '
            '"failed_sessions", "op": "gt", "value": 5, "window_minutes": 60}\'.'
        ),
    )
    create_p.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=int,
        metavar="SECONDS",
        help="How often to poll GitHub, in seconds (github_poll only).",
    )
    create_p.add_argument(
        "--action-kind",
        dest="action_kind",
        default="agent",
        choices=tuple(sorted(_VALID_ACTION_KINDS | set(_ALIAS_ACTION_KINDS))),
        help="Stored action kind or accepted alias (default: agent).",
    )
    create_p.add_argument(
        "--prompt",
        help=(
            "Instruction the scheduled agent runs at each fire. Required when "
            "--action-kind is agent, which is the default. A profile named by "
            "--agent supplies the model and settings, never this instruction, so "
            "a schedule created without it fires and fails."
        ),
    )
    create_p.add_argument("--model", help="Model spec for agent action.")
    create_p.add_argument(
        "--agent",
        help=(
            "Agent profile to run, resolved the same way `li agent --agent` resolves "
            "it. Supplies the system prompt, model and effort the fire runs with."
        ),
    )
    create_p.add_argument("--playbook", help="Playbook name (for action-kind=play/playbook).")
    create_p.add_argument(
        "--flow-yaml",
        dest="flow_yaml",
        metavar="FILE",
        help="Path to a YAML flow spec file (for action-kind=flow_yaml).",
    )
    create_p.add_argument(
        "--action-command",
        dest="action_command",
        metavar="NAME",
        help=(
            "Executable name to spawn directly, bypassing `li` (for "
            "action-kind=command). Must be a bare name (no path separators) "
            "and be a member of LIONAGI_SCHEDULER_COMMAND_ALLOWLIST; refused "
            "loudly at create time and re-checked at fire time."
        ),
    )
    create_p.add_argument(
        "--action-command-args",
        dest="action_command_args",
        metavar="JSON",
        type=JsonArgument({"type": "array", "items": {"type": "string"}}),
        help=(
            "action-kind=command argv, as a JSON array of {{var}} templates "
            "rendered from trigger_context at fire time, e.g. "
            '\'["review-pr", "--pr", "{{pr_number}}"]\'.'
        ),
    )
    create_p.add_argument(
        "--project",
        help=(
            "Project this schedule's runs are recorded under. Defaults to the project "
            "detected from the execution root."
        ),
    )
    create_p.add_argument(
        "--cwd",
        metavar="PATH",
        help=(
            "Explicit execution root for this schedule's spawned process "
            "(must be an existing directory). Persisted at creation time so "
            "the schedule's spawn cwd never depends on where the Studio "
            "daemon is running from (default: --project's registered path, "
            "or this CLI's own working directory)."
        ),
    )
    create_p.add_argument("--description", help="Human-readable description.")
    create_p.add_argument(
        "--max-runs",
        dest="max_runs",
        type=int,
        metavar="N",
        help=(
            "Auto-disable this schedule once N total runs have fired "
            "(default: unlimited). Chained on_success/on_fail fires do not "
            "count toward N. Mutually exclusive with --once."
        ),
    )
    create_p.add_argument(
        "--once",
        dest="once",
        action="store_true",
        help="Sugar for --max-runs 1 — fire once, then auto-disable.",
    )
    create_p.add_argument(
        "--max-cost-usd",
        dest="max_cost_usd",
        type=float,
        metavar="USD",
        help=(
            "Auto-disable this schedule once its cumulative session spend "
            "reaches USD (default: unlimited). Pre-fire cumulative gate: an "
            "in-flight run is not interrupted, so the schedule may overshoot "
            "by up to one run's cost before the next fire is refused."
        ),
    )
    create_p.add_argument(
        "--max-tokens",
        dest="max_tokens",
        type=int,
        metavar="N",
        help=(
            "Auto-disable this schedule once its cumulative session token "
            "usage (input+output) reaches N (default: unlimited). Same "
            "pre-fire cumulative semantics as --max-cost-usd."
        ),
    )
    create_p.add_argument(
        "--on-success",
        dest="on_success",
        metavar="JSON",
        help=(
            "Chain action to fire when this run exits 0, as a JSON object "
            "(allowed keys: kind/action_kind, model, prompt, agent, playbook, "
            "on_success, on_fail). WARNING — shallow merge: the chain child is "
            "built as {**this_schedule, **on_success}, so any key you omit is "
            "INHERITED from this schedule, including on_success/on_fail "
            "themselves. A 2-level chain must set the inner level's own "
            '"on_success": null explicitly, or the chain keeps re-firing at '
            "each depth (capped, but rarely what you want). Example value: "
            '{"prompt": "notify done", "on_success": null}'
        ),
    )
    create_p.add_argument(
        "--on-fail",
        dest="on_fail",
        metavar="JSON",
        help=(
            "Chain action to fire when this run exits non-zero, as a JSON "
            "object (same allowed keys and shallow-merge caveat as "
            "--on-success — see above). Example value: "
            '{"prompt": "alert on-call", "on_fail": null}'
        ),
    )

    # validate
    validate_p = sched_sub.add_parser(
        "validate",
        help="Validate + statically resolve a ScheduleSet file (no database writes).",
        epilog="Example: li schedule validate .lionagi/schedules.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    validate_p.add_argument("file", help="Path to a ScheduleSet YAML file.")
    validate_p.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON.")

    # apply
    apply_p = sched_sub.add_parser(
        "apply",
        help="Reconcile a ScheduleSet file into the database, atomically.",
        epilog=(
            "Examples:\n"
            "  li schedule apply .lionagi/schedules.yaml --dry-run\n"
            "  li schedule apply .lionagi/schedules.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply_p.add_argument("file", help="Path to a ScheduleSet YAML file.")
    apply_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Show the reconciliation plan with resolved values; no database writes.",
    )
    apply_p.add_argument(
        "--adopt",
        action="store_true",
        help=(
            "Take an existing unowned row into this set in place, keeping its id and history. "
            "Refuses unless the member declares the row's behavior, compared by how the "
            "scheduler acts on each column, enablement included."
        ),
    )
    apply_p.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON.")

    # export
    export_p = sched_sub.add_parser(
        "export",
        help="Convert schedules into a ScheduleSet document (never writes the database).",
        epilog=(
            "Examples:\n"
            "  li schedule export --legacy --output schedules.yaml\n"
            "  li schedule export --output schedules.yaml\n"
            "\n"
            "Exit codes: 0 all rows READY, 2 some rows BLOCKED (document and "
            "report are still emitted -- see stderr/--report), 1 hard failure.\n"
            "A row spanning multiple projects is split into one document per "
            "project so every original qualified name round-trips exactly; "
            "with --output, extra projects are written to sibling "
            "<name>.<project>.yaml files.\n"
            "An exported flow target's snapshot file is an absolute host "
            "path -- the document only re-applies on this host, or after "
            "the sidecar file moves with it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    export_p.add_argument(
        "--legacy",
        action="store_true",
        help=(
            "Convert chain-free legacy rows (managed_by is null, i.e. rows "
            "predating the declaration layer) instead of declaration/cli-"
            "managed rows. A row with on_success/on_fail is reported BLOCKED "
            "and omitted."
        ),
    )
    export_p.add_argument(
        "--output",
        metavar="PATH",
        help="Write the ScheduleSet YAML here (default: stdout).",
    )
    export_p.add_argument(
        "--report",
        metavar="PATH",
        help="Write the human-readable conversion report here (default: stderr).",
    )

    # enable / disable / delete
    for sub_name, sub_help, example in (
        ("enable", "Enable a schedule.", "li schedule enable sched-abc123"),
        (
            "disable",
            "Disable a schedule.",
            'li schedule disable sched-abc123 --reason "planned maintenance"',
        ),
        ("delete", "Delete a schedule.", "li schedule delete sched-abc123"),
    ):
        p = sched_sub.add_parser(
            sub_name,
            help=sub_help,
            epilog=f"Example: {example}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument(
            "id", help="Id of the schedule, as returned by `li schedule list` in its `id` field."
        )
        if sub_name == "disable":
            p.add_argument(
                "--reason",
                default=None,
                help=(
                    "Optional explanation recorded when an enabled schedule is disabled. "
                    "Refused if already disabled; no reason or history entry is added."
                ),
            )

    # trigger
    trigger_p = sched_sub.add_parser(
        "trigger",
        help="Fire a schedule immediately.",
        epilog="Example: li schedule trigger sched-abc123 --wait",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    trigger_p.add_argument(
        "id", help="Id of the schedule, as returned by `li schedule list` in its `id` field."
    )
    trigger_p.add_argument(
        "--wait",
        action="store_true",
        help=(
            "Block until the fired occurrence reaches a terminal status "
            "(retries a short grace period first, since the occurrence row "
            "isn't durably written until just after this returns a run id), "
            "then print its outcome and exit non-zero on failure."
        ),
    )

    # runs
    runs_p = sched_sub.add_parser(
        "runs",
        help="List runs for a schedule.",
        epilog="Example: li schedule runs sched-abc123 --status failed --limit 5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    runs_p.add_argument(
        "id", help="Id of the schedule, as returned by `li schedule list` in its `id` field."
    )
    runs_p.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Max runs to return, 1-200 (default: 20).",
    )
    runs_p.add_argument(
        "--status",
        action="append",
        metavar="STATUS",
        help="Filter by run status; repeatable (e.g. failed, timed_out).",
    )
    runs_p.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON.")

    # run (singular)
    run_p = sched_sub.add_parser(
        "run",
        help="Show one schedule run.",
        epilog="Example: li schedule run 9c8f4d5a2b10 --json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_p.add_argument("id", help="Schedule run ID.")
    run_p.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON.")

    # status
    status_p = sched_sub.add_parser(
        "status",
        help='"Did it work?" summary for a schedule.',
        epilog="Example: li schedule status sched-abc123 --wait",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status_p.add_argument(
        "id", help="Id of the schedule, as returned by `li schedule list` in its `id` field."
    )
    status_p.add_argument(
        "--wait", action="store_true", help="Wait for the latest in-flight run to finish first."
    )
    status_p.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON.")

    _ALL_SCHEDULE_FLAGS.clear()
    for action_parser in sched_sub.choices.values():
        _ALL_SCHEDULE_FLAGS.update(
            opt
            for opt in action_parser._option_string_actions
            if opt.startswith("--") and opt != "--help"
        )

    return sched
