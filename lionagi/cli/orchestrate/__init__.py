# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li orchestrate` — multi-agent orchestration patterns (fanout, flow)."""

from __future__ import annotations

import argparse
import sys

from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.libs.path_safety import validate_path_component as validate_path_component
from lionagi.ln.concurrency import is_cancelled, run_async

from .._logging import hint, log_error
from .._providers import add_common_cli_args
from .._util import EXIT_CODE_BY_STATUS
from ._checkpoint import FlowResumeError
from .fanout import _run_fanout
from .flow import FlowPlanError, _resume_flow, _run_flow

# ── flow-spec helpers ────────────────────────────────────────────────────────


def _scan_argv_for_playbook_name(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-p", "--playbook"):
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("--playbook="):
            return tok.split("=", 1)[1]
        i += 1
    return None


def _derive_args_schema_from_spec(spec: dict) -> dict:
    if isinstance(spec.get("args"), dict):
        schema: dict = {}
        for name, field in spec["args"].items():
            if not isinstance(field, dict):
                continue
            schema[name] = {
                "type": field.get("type", "str"),
                "default": field.get("default"),
                "help": field.get("help", ""),
            }
        return schema
    if spec.get("argument-hint"):
        return _parse_argument_hint(spec["argument-hint"])
    return {}


def inject_playbook_schema_into_parser(
    flow_parser: argparse.ArgumentParser, argv: list[str]
) -> dict:
    """Pre-scan argv for playbook; inject declared args as parser flags."""
    name = _scan_argv_for_playbook_name(argv)
    if not name:
        return {}
    path, err = _resolve_playbook_path(name)
    if err is not None:
        return {}  # Defer error reporting to run_orchestrate
    spec = _load_flow_spec(str(path))
    if not isinstance(spec, dict):
        return {}
    schema = _derive_args_schema_from_spec(spec)
    if not schema:
        return {}
    reserved: set[str] = set()
    for action in flow_parser._actions:
        for opt in getattr(action, "option_strings", ()):
            reserved.add(opt)
    resolved_schema: dict = {}
    for arg_name, field in schema.items():
        cli_flag = "--" + arg_name.replace("_", "-")
        if cli_flag in reserved:
            import logging as _logging

            _logging.getLogger("lionagi.cli").warning(
                "playbook arg %r (%s) collides with built-in flag; "
                "rename it in the playbook to use it",
                arg_name,
                cli_flag,
            )
            continue
        type_str = field.get("type", "str")
        help_text = field.get("help", "")
        if type_str == "bool":
            flow_parser.add_argument(
                cli_flag,
                dest=arg_name,
                action="store_true",
                default=None,
                help=help_text,
            )
        else:
            flow_parser.add_argument(
                cli_flag,
                dest=arg_name,
                default=None,
                help=help_text,
                metavar=type_str.upper(),
            )
        resolved_schema[arg_name] = field
    flow_parser.set_defaults(_playbook_args_schema=resolved_schema)
    return resolved_schema


def _resolve_playbook_path(name: str) -> tuple[object, str | None]:
    """Resolve a playbook NAME to (Path, None) or (None, error_message)."""
    from pathlib import Path

    from lionagi.libs.path_safety import validate_path_component

    if not name or not isinstance(name, str):
        return None, "playbook name must be a non-empty string"
    try:
        validate_path_component(name, label="playbook NAME")
    except ValueError:
        return (
            None,
            f"playbook NAME must be a bare identifier, got {name!r}. "
            "Use -f /abs/path.yaml for ad-hoc specs.",
        )
    root = Path("~/.lionagi/playbooks").expanduser()
    candidate = root / f"{name}.playbook.yaml"
    if not candidate.is_file():
        # Look for near-matches to suggest.
        suggestions = []
        if root.is_dir():
            for p in sorted(root.glob("*.playbook.yaml")):
                suggestions.append(p.stem.removesuffix(".playbook"))
        hint_text = (
            f" Available: {', '.join(suggestions[:10])}"
            if suggestions
            else " No playbooks found in ~/.lionagi/playbooks/"
        )
        return None, f"playbook not found: {candidate}.{hint_text}"
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return (
            None,
            f"playbook {name!r} resolves outside playbooks root (symlink escape blocked)",
        )
    return candidate, None


def _parse_argument_hint(hint: str) -> dict:
    """Parse CC-style argument-hint string into an args schema."""
    import re

    schema: dict = {}
    pattern = re.compile(r"\[--([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+([A-Z_][A-Z0-9_]*))?\]")
    for match in pattern.finditer(hint or ""):
        flag_name = match.group(1).replace("-", "_")
        value_placeholder = match.group(2)
        if value_placeholder is None:
            schema[flag_name] = {"type": "bool", "default": False}
        else:
            schema[flag_name] = {"type": "str", "default": None}
    return schema


def _validate_args_schema(args_schema) -> str | None:
    if not isinstance(args_schema, dict):
        return f"spec field 'args' must be a dict, got {type(args_schema).__name__}"
    valid_types = {"str", "int", "float", "bool"}
    for name, spec in args_schema.items():
        if not isinstance(name, str) or not name.replace("_", "").isalnum():
            return f"args key {name!r} must be an alphanumeric identifier"
        if not isinstance(spec, dict):
            return f"args[{name!r}] must be a dict, got {type(spec).__name__}"
        type_str = spec.get("type", "str")
        if type_str not in valid_types:
            return f"args[{name!r}].type must be one of {sorted(valid_types)}, got {type_str!r}"
    return None


def _coerce_arg_value(name: str, value, type_str: str):
    if value is None:
        return None, None
    if type_str == "bool":
        return bool(value), None
    if type_str == "str":
        return str(value), None
    try:
        if type_str == "int":
            return int(value), None
        if type_str == "float":
            return float(value), None
    except (TypeError, ValueError):
        return (
            None,
            f"arg --{name.replace('_', '-')} expected {type_str}, got {value!r}",
        )
    return value, None


def _load_flow_spec(path: str) -> dict | None:
    from pathlib import Path

    p = Path(path).expanduser()
    if not p.is_file():
        log_error(f"spec file not found: {p}")
        return None
    text = p.read_text()
    suffix = p.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text) or {}
        elif suffix == ".json":
            import json

            data = json.loads(text)
        else:
            import yaml

            try:
                data = yaml.safe_load(text) or {}
            except Exception:
                import json

                data = json.loads(text)
    except Exception as e:
        log_error(f"failed to parse spec file {p}: {e}")
        return None

    if not isinstance(data, dict):
        log_error("spec file must contain a YAML/JSON object")
        return None
    preserve_dashed = {"argument-hint"}
    normalized: dict = {}
    for key, value in data.items():
        if key in preserve_dashed or "-" not in key:
            normalized[key] = value
        else:
            normalized[key.replace("-", "_")] = value
    return normalized


def _validate_spec_fields(spec: dict) -> str | None:
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
        from .._providers import EFFORT_LEVELS

        if not isinstance(effort, str):
            return f"spec field 'effort' must be a string, got {type(effort).__name__}"
        if effort not in EFFORT_LEVELS:
            allowed = sorted(EFFORT_LEVELS)
            return f"spec field 'effort' must be one of {allowed}, got {effort!r}"

    if "with_synthesis" in spec:
        val = spec["with_synthesis"]
        if not isinstance(val, bool | str):
            return (
                f"spec field 'with_synthesis' must be bool or str (model spec), "
                f"got {type(val).__name__}"
            )

    for bool_field in ("bare", "dry_run", "show_graph"):
        if bool_field in spec:
            val = spec[bool_field]
            if not isinstance(val, bool):
                return f"spec field {bool_field!r} must be a bool, got {type(val).__name__}"

    if "prompt" in spec:
        prompt = spec["prompt"]
        if not isinstance(prompt, str):
            return f"spec field 'prompt' must be a string, got {type(prompt).__name__}"
        if len(prompt) > 8192:
            return "spec field 'prompt' exceeds maximum length of 8192 characters"

    if "save" in spec:
        save = spec["save"]
        if not isinstance(save, str):
            return f"spec field 'save' must be a string, got {type(save).__name__}"

    for str_field in ("model", "agent", "team_mode", "team_attach", "reactive"):
        if str_field in spec:
            val = spec[str_field]
            if not isinstance(val, str):
                return f"spec field {str_field!r} must be a string, got {type(val).__name__}"

    if "artifacts" in spec:
        artifacts = spec["artifacts"]
        if artifacts is None:
            return "spec field 'artifacts' must be a dict, got NoneType"
        try:
            from lionagi.state.artifact_verifier import (
                validate_artifact_contract,
                warn_unknown_artifact_keys,
            )

            validate_artifact_contract(artifacts)
            import logging as _logging

            _cli_log = _logging.getLogger("lionagi.cli")
            warn_unknown_artifact_keys(
                artifacts,
                source="playbook",
                emit=_cli_log.warning,
            )
        except Exception as exc:
            return f"spec field 'artifacts' is invalid: {exc}"

    return None


def _interpolate_prompt(template: str, positional: str | None, playbook_args: dict) -> str:
    """Interpolate {input} + playbook args into the prompt template."""
    if not template:
        return positional or ""

    ctx: dict = dict(playbook_args)
    if positional is not None:
        ctx["input"] = positional

    import re

    placeholders = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template))
    if not placeholders and positional is not None:
        return template + "\n\n" + positional

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in ctx:
            return str(ctx[key])
        return match.group(0)

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _sub, template)


def add_orchestrate_subparser(
    subparsers: argparse._SubParsersAction,
) -> dict[str, argparse.ArgumentParser]:
    orch = subparsers.add_parser(
        "orchestrate",
        aliases=["o"],
        help="Multi-agent orchestration patterns.",
        description="Orchestrate multiple agents in structured patterns.",
    )
    orch_sub = orch.add_subparsers(dest="orch_command", required=True)

    fo = orch_sub.add_parser(
        "fanout",
        help="Fan-out N workers in parallel, optionally synthesize.",
        description=(
            "Orchestrator decomposes task into N agent requests, "
            "fans out to workers, optionally synthesizes. "
            "Effort can be embedded in model spec: claude/opus-4-7-high. "
            "Flags may appear anywhere relative to the positionals."
        ),
    )
    fo.add_argument(
        "query",
        nargs="*",
        metavar="[MODEL] PROMPT",
        help=(
            "Orchestrator model spec (provider/model-effort) followed by the "
            "task prompt. Model is also used as the default worker model "
            "unless --workers is set; omit it when -a/--agent provides one. "
            "A single positional is treated as the prompt."
        ),
    )
    fo.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load orchestrator profile by name. Resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md. "
            "Profile provides system prompt, default model, effort, yolo. "
            "CLI flags and positional model override profile settings."
        ),
    )

    fo.add_argument(
        "-n",
        "--num-workers",
        type=int,
        default=3,
        help="Number of workers (default: 3). Ignored if --workers set.",
    )
    fo.add_argument(
        "--workers",
        metavar="M1,M2,...",
        default=None,
        help="Comma-separated worker model specs (each can include effort).",
    )
    fo.add_argument(
        "--pack",
        metavar="PATH",
        default=None,
        help=(
            "Path to a YAML routing pack. Provides per-role model/effort when "
            "--workers is absent. --workers overrides pack routing."
        ),
    )
    fo.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help="Max concurrent workers (default: all).",
    )
    fo.add_argument(
        "--with-synthesis",
        nargs="?",
        const=True,
        default=False,
        metavar="MODEL",
        help="Enable synthesis. Bare flag uses orchestrator model; with arg uses that model.",
    )
    fo.add_argument(
        "--synthesis-prompt",
        default=None,
        help="Custom synthesis instruction.",
    )
    fo.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    fo.add_argument(
        "--save",
        metavar="DIR",
        default=None,
        help="Save outputs to directory.",
    )

    fo.add_argument(
        "--team-mode",
        nargs="?",
        const="fanout",
        default=None,
        metavar="NAME",
        help=(
            "Create a persistent team for this fanout. Workers get team context "
            "and results are posted as team messages. Bare flag uses 'fanout' as "
            "team name; with arg uses that name."
        ),
    )

    add_common_cli_args(fo)

    fl = orch_sub.add_parser(
        "flow",
        help="Auto-DAG pipeline: orchestrator plans DAG, engine executes.",
        description=(
            "Orchestrator analyzes the task, composes a DAG of agents "
            "with dependency edges, and executes with automatic "
            "parallelism where dependencies allow. Flags may appear "
            "anywhere relative to the positionals."
        ),
    )
    fl.add_argument(
        "query",
        nargs="*",
        metavar="[MODEL] PROMPT",
        help=(
            "Orchestrator model spec followed by the task prompt. Model is "
            "optional when -a/--agent provides one; a single positional is "
            "treated as the prompt. The prompt itself may instead come from "
            "-f/--file or -p/--playbook."
        ),
    )
    fl.add_argument(
        "-f",
        "--file",
        metavar="PATH",
        default=None,
        help=(
            "Load flow spec from YAML or JSON file. File values serve as "
            "defaults; CLI flags override them. Prompt can come from the "
            "file (prompt: key) or as a positional argument."
        ),
    )
    fl.add_argument(
        "-p",
        "--playbook",
        metavar="NAME",
        default=None,
        help=(
            "Load playbook from ~/.lionagi/playbooks/<NAME>.playbook.yaml. "
            "Playbooks may declare args: schema, artifacts: contracts, "
            "or argument-hint: placeholders for prompt template values."
        ),
    )
    fl.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load orchestrator profile by name — resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md."
        ),
    )
    fl.add_argument(
        "--with-synthesis",
        nargs="?",
        const=True,
        default=False,
        metavar="MODEL",
        help="Enable final synthesis. Bare flag uses orchestrator model.",
    )
    fl.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help="Max concurrent agents within a phase (default: all).",
    )
    fl.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    fl.add_argument(
        "--save",
        metavar="DIR",
        default=None,
        help="Save outputs to directory.",
    )
    fl.add_argument(
        "--team-mode",
        nargs="?",
        const="flow",
        default=None,
        metavar="NAME",
        help=(
            "Create a FRESH team for this flow (new UUID every invocation). "
            "Bare flag uses 'flow' as the name."
        ),
    )
    fl.add_argument(
        "--team-attach",
        metavar="NAME",
        default=None,
        help=(
            "Attach to a team by NAME — upsert semantics: load existing team "
            "if found (preserving message history), else create fresh. "
            "Mutually exclusive with --team-mode."
        ),
    )
    fl.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the DAG but don't execute. Shows agents, deps, and model resolution.",
    )
    fl.add_argument(
        "--show-graph",
        action="store_true",
        help="Render DAG as matplotlib visualization. With --save, saves PNG to save dir.",
    )
    fl.add_argument(
        "--background",
        action="store_true",
        help="Run flow in background. Requires --save. Check output in save dir.",
    )
    fl.add_argument(
        "--bare",
        action="store_true",
        help=(
            "Ignore agent profiles — all workers use the CLI model spec. "
            "Roles define behavioral focus only, no profile system prompts."
        ),
    )
    fl.add_argument(
        "--workers",
        metavar="M1,M2,...",
        default=None,
        help=(
            "Comma-separated worker model specs (assignment i uses pool[i %% len]). "
            "Overrides the per-role model while KEEPING each role's profile/system "
            "prompt — unlike --bare, which also drops profiles. Enables mixed-model "
            "flows (cheap roles + expensive roles)."
        ),
    )
    fl.add_argument(
        "--pack",
        metavar="PATH",
        default=None,
        help=(
            "Path to a YAML routing pack. Provides per-role model/effort when "
            "--workers is absent. --workers overrides pack routing."
        ),
    )
    fl.add_argument(
        "--max-ops",
        "--max-agents",
        dest="max_ops",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Cap total ops (nodes in the planned DAG). 0 = unlimited. "
            "`--max-agents` is a deprecated alias — prefer `--max-ops`."
        ),
    )
    fl.add_argument(
        "--reactive",
        metavar="MODE",
        default=None,
        help=(
            "Who may grow the live DAG by emitting a SpawnRequest: "
            "'all' (default — every worker), 'off' (flat batch DAG, no spawning), "
            "or a comma-separated list of roles (e.g. 'critic,evaluator') that "
            "alone may spawn. Caps still apply via --max-ops."
        ),
    )
    fl.add_argument(
        "--resume",
        metavar="RUN_OR_SESSION_ID",
        default=None,
        help=(
            "Resume a checkpointed flow from a prior process (by run id, or "
            "any session/invocation/play id backed by one). Replays the "
            "persisted plan verbatim — no planner call, no other flow flags "
            "read (model/prompt/playbook/etc. all come from the checkpoint). "
            "Distinct from `li o ctl resume`, which un-pauses a still-running "
            "session."
        ),
    )
    fl.add_argument(
        "--allow-degraded-context",
        action="store_true",
        help=(
            "With --resume: proceed even when a pending op declared "
            "inherit_context — it runs against an empty branch instead of "
            "its predecessor's conversation history, which resume does not "
            "restore. Without this flag such ops refuse loudly, naming them."
        ),
    )
    add_common_cli_args(fl)

    # `li o ctl status <id>` — generic alias into the same status renderer as
    # `li agent status` / `li play status` (ADR-0085 section 6). `pause` /
    # `resume` / `msg` queue session_controls rows consumed by the control
    # poller running alongside a live flow's heartbeat loop (ADR-0085 part 1).
    # `stop` is out of scope for this slice — it depends on the checkpoint
    # writer, which lands separately.
    ctl = orch_sub.add_parser(
        "ctl",
        help="Control-plane surfaces for a run (status, pause, resume, msg).",
        description="Read-only and control operations addressed by run id.",
    )
    ctl_sub = ctl.add_subparsers(dest="ctl_command", required=True)
    ctl_status = ctl_sub.add_parser(
        "status",
        help="Show lifecycle status for a session, invocation, or play by id.",
        description=(
            "Generic id-addressed status lookup — no agent/play kind scoping, so "
            "<id> is required (no 'latest run' default). Prefer `li agent status` "
            "/ `li play status` when the kind is known."
        ),
    )
    ctl_status.add_argument("id", help="Session, invocation, or play ID (or short prefix).")
    ctl_status.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit a stable JSON object."
    )

    ctl_pause = ctl_sub.add_parser(
        "pause",
        help="Queue a pause for a running flow.",
        description=(
            "Queues a pause control row; the target flow's control poller applies "
            "it at the next op boundary (idempotent — safe to queue more than once)."
        ),
    )
    ctl_pause.add_argument("id", help="Session, invocation, or play ID (or short prefix).")

    ctl_resume = ctl_sub.add_parser(
        "resume",
        help="Queue a resume for a paused flow.",
        description="Queues a resume control row, releasing a pending pause gate.",
    )
    ctl_resume.add_argument("id", help="Session, invocation, or play ID (or short prefix).")

    ctl_msg = ctl_sub.add_parser(
        "msg",
        help="Queue an operator message for a running flow (context mode only).",
        description=(
            "Queues a message control row; the control poller deep-merges it into "
            "the flow's workspace context, visible to any op not yet started. "
            "Op-mode injection (--as-op) is not supported by this command yet."
        ),
    )
    ctl_msg.add_argument("id", help="Session, invocation, or play ID (or short prefix).")
    ctl_msg.add_argument("text", help="Message text to inject into the flow context.")

    return {"fanout": fo, "flow": fl, "ctl": ctl}


def _resolve_model_and_prompt(query: list[str]) -> tuple[str | None, str | None] | None:
    """Assign a 0-2 token positional bucket to (model, prompt).

    Mirrors the `li agent` [MODEL] PROMPT convention: a single token is the
    prompt (model comes from -a/--agent, a spec file, or a playbook); two
    tokens are (model, prompt) in order. Returns None after logging a clear
    error when more than two positionals are given (e.g. an unquoted prompt).
    """
    if len(query) > 2:
        log_error(
            "too many positional arguments — expected [MODEL] PROMPT. "
            "Did you forget to quote the prompt?"
        )
        return None
    if len(query) == 2:
        return query[0], query[1]
    if len(query) == 1:
        return None, query[0]
    return None, None


def _run_orch_command(coro, *, verbose: bool, extra_handlers: tuple = ()) -> tuple[object, int]:
    """Run an orchestration coroutine, map shared exceptions to exit codes.

    Returns (result, exit_code).  extra_handlers is a tuple of (ExcType, exit_code)
    pairs checked before the shared map, allowing callers to handle pattern-specific
    exceptions without repeating the common mapping.
    """
    try:
        result = run_async(coro)
    except (TimeoutError, LionTimeoutError) as e:
        log_error(str(e))
        return None, EXIT_CODE_BY_STATUS["timed_out"]
    except KeyboardInterrupt:
        return None, EXIT_CODE_BY_STATUS["aborted"]
    except BaseException as exc:
        for exc_type, code in extra_handlers:
            if isinstance(exc, exc_type):
                log_error(str(exc))
                return None, code
        if is_cancelled(exc):
            return None, EXIT_CODE_BY_STATUS["cancelled"]
        raise
    return result, 0


def run_orchestrate(args: argparse.Namespace) -> int:
    if args.orch_command == "fanout":
        resolved = _resolve_model_and_prompt(getattr(args, "query", None) or [])
        if resolved is None:
            return 1
        args.model, args.prompt = resolved

        has_model = args.model is not None or args.agent is not None
        if not has_model:
            log_error("model or --agent is required")
            return 1
        if not args.prompt:
            log_error("prompt is required")
            return 1

        synth = args.with_synthesis
        with_synthesis = synth is not False
        synthesis_model = synth if isinstance(synth, str) else None

        output, rc = _run_orch_command(
            _run_fanout(
                model_spec=args.model or "",
                prompt=args.prompt,
                num_workers=args.num_workers,
                workers_str=args.workers,
                with_synthesis=with_synthesis,
                synthesis_model=synthesis_model,
                synthesis_prompt=args.synthesis_prompt,
                max_concurrent=args.max_concurrent,
                yolo=args.yolo,
                bypass=getattr(args, "bypass", False),
                verbose=args.verbose,
                effort=args.effort,
                theme=args.theme,
                output_format=args.output,
                save_dir=args.save,
                team_name=args.team_mode,
                cwd=args.cwd,
                timeout=args.timeout,
                agent_name=args.agent,
                fast=getattr(args, "fast", False),
                playbook_name=getattr(args, "playbook", None),
                invocation_id=getattr(args, "invocation", None),
                project=getattr(args, "project", None),
                pack=getattr(args, "pack", None),
            ),
            verbose=args.verbose,
        )
        if rc != 0:
            return rc
        fanout_result, fanout_terminal_status = output
        if not args.verbose:
            print(fanout_result)
        return EXIT_CODE_BY_STATUS.get(fanout_terminal_status, 0)

    if args.orch_command == "flow":
        resume_target = getattr(args, "resume", None)
        if resume_target:
            flow_result, rc = _run_orch_command(
                _resume_flow(
                    resume_target,
                    allow_degraded_context=getattr(args, "allow_degraded_context", False),
                    dry_run=args.dry_run,
                    show_graph=getattr(args, "show_graph", False),
                ),
                verbose=args.verbose,
                extra_handlers=((FlowResumeError, EXIT_CODE_BY_STATUS["failed"]),),
            )
            if rc != 0:
                return rc
            output, terminal_status = flow_result
            if not args.verbose:
                print(output)
            return EXIT_CODE_BY_STATUS.get(terminal_status, 0)

        resolved = _resolve_model_and_prompt(getattr(args, "query", None) or [])
        if resolved is None:
            return 1
        args.model, args.prompt = resolved

        playbook_name = getattr(args, "playbook", None)
        playbook_artifacts: dict | None = None
        file_spec = getattr(args, "file", None)
        if playbook_name and file_spec:
            log_error("pass either -p/--playbook or -f/--file, not both")
            return 1
        if playbook_name:
            resolved_path, resolve_err = _resolve_playbook_path(playbook_name)
            if resolve_err is not None:
                log_error(resolve_err)
                return 1
            file_spec = str(resolved_path)

        if file_spec:
            spec = _load_flow_spec(file_spec)
            if spec is None:
                return 1
            spec_err = _validate_spec_fields(spec)
            if spec_err is not None:
                log_error(spec_err)
                return 1

            playbook_artifacts = spec.get("artifacts")

            if "args" in spec:
                schema_err = _validate_args_schema(spec["args"])
                if schema_err is not None:
                    log_error(schema_err)
                    return 1
            args_schema = getattr(args, "_playbook_args_schema", None)
            if args_schema is None:
                args_schema = _derive_args_schema_from_spec(spec)

            playbook_ctx: dict = {}
            for name, field in args_schema.items():
                if field.get("default") is not None:
                    playbook_ctx[name] = field["default"]
                raw = getattr(args, name, None)
                if raw is None:
                    continue
                coerced, coerce_err = _coerce_arg_value(name, raw, field.get("type", "str"))
                if coerce_err is not None:
                    log_error(coerce_err)
                    return 1
                playbook_ctx[name] = coerced

            if args.model and args.prompt is None and (spec.get("model") or spec.get("agent")):
                args.prompt = args.model
                args.model = None
            if args.model is None and "model" in spec:
                args.model = spec["model"]
            if args.agent is None and spec.get("agent"):
                args.agent = spec["agent"]
            if spec.get("prompt"):
                args.prompt = _interpolate_prompt(spec["prompt"], args.prompt, playbook_ctx)
            if args.max_concurrent == 0 and spec.get("workers"):
                args.max_concurrent = spec["workers"]
            if args.effort is None and spec.get("effort"):
                args.effort = spec["effort"]
            if args.with_synthesis is False and spec.get("with_synthesis"):
                args.with_synthesis = spec["with_synthesis"]
            if args.team_mode is None and spec.get("team_mode"):
                args.team_mode = spec["team_mode"]
            if getattr(args, "team_attach", None) is None and spec.get("team_attach"):
                args.team_attach = spec["team_attach"]
            if args.max_ops == 0:
                spec_cap = spec.get("max_ops") or spec.get("max_agents")
                if spec_cap:
                    args.max_ops = spec_cap
            if not args.bare and spec.get("bare"):
                args.bare = True
            if not args.dry_run and spec.get("dry_run"):
                args.dry_run = True
            if not getattr(args, "show_graph", False) and spec.get("show_graph"):
                args.show_graph = True
            if getattr(args, "reactive", None) is None and spec.get("reactive") is not None:
                args.reactive = spec["reactive"]
            if getattr(args, "pack", None) is None and spec.get("pack"):
                args.pack = spec["pack"]
            if args.save is None and spec.get("save"):
                args.save = spec["save"]
            if spec.get("critic_model"):
                pass  # reserved for future use

        if args.model and not args.prompt and args.agent:
            args.prompt = args.model
            args.model = None

        has_model = args.model is not None or args.agent is not None
        if not has_model:
            log_error("model or --agent is required")
            return 1

        if not args.prompt:
            log_error("prompt is required (positional or via -f spec file)")
            return 1

        if args.team_mode is not None and getattr(args, "team_attach", None) is not None:
            log_error("--team-mode and --team-attach are mutually exclusive")
            return 1

        if args.save is not None:
            from pathlib import Path as _Path

            _resolved_save = _Path(args.save).expanduser().resolve()
            _safe_save = False
            for _root in (_Path.cwd().resolve(), _Path.home().resolve()):
                try:
                    _resolved_save.relative_to(_root)
                    _safe_save = True
                    break
                except ValueError:
                    pass
            if not _safe_save:
                log_error(
                    f"save path {str(_resolved_save)!r} escapes allowed roots "
                    f"(must be under cwd or home)"
                )
                return 1

        background = getattr(args, "background", False)
        if background and not args.save:
            log_error("--background requires --save")
            return 1

        if background:
            import os as _os
            import subprocess
            import uuid as _uuid
            from pathlib import Path as _Path

            bg_session_id = str(_uuid.uuid4())
            bg_args = [a for a in sys.argv[1:] if a != "--background"]
            log_root = _Path(args.save).expanduser()
            log_root.mkdir(parents=True, exist_ok=True)
            log_path = log_root / "flow.log"
            bg_env = {**_os.environ, "LIONAGI_SESSION_ID": bg_session_id}
            with open(log_path, "w") as log_f:
                proc = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "lionagi.cli", *bg_args],
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=bg_env,
                )
            hint(f"Flow running in background (PID {proc.pid})")
            hint(f"Session: {bg_session_id[:16]}  →  li monitor {bg_session_id[:16]}")
            hint(f"Output: {log_path}")
            return 0

        synth = args.with_synthesis
        with_synthesis = synth is not False
        synthesis_model = synth if isinstance(synth, str) else None

        flow_result, rc = _run_orch_command(
            _run_flow(
                model_spec=args.model or "",
                prompt=args.prompt,
                with_synthesis=with_synthesis,
                synthesis_model=synthesis_model,
                max_concurrent=args.max_concurrent,
                yolo=args.yolo,
                bypass=getattr(args, "bypass", False),
                verbose=args.verbose,
                effort=args.effort,
                theme=args.theme,
                output_format=args.output,
                save_dir=args.save,
                team_name=args.team_mode,
                team_attach=getattr(args, "team_attach", None),
                cwd=args.cwd,
                timeout=args.timeout,
                agent_name=args.agent,
                bare=args.bare,
                workers_str=args.workers,
                max_ops=args.max_ops,
                dry_run=args.dry_run,
                show_graph=getattr(args, "show_graph", False),
                reactive_spec=getattr(args, "reactive", None) or "all",
                fast=getattr(args, "fast", False),
                playbook_name=playbook_name,
                playbook_artifacts=playbook_artifacts,
                invocation_id=getattr(args, "invocation", None),
                project=getattr(args, "project", None),
                pack=getattr(args, "pack", None),
            ),
            verbose=args.verbose,
            # planning produced no usable DAG — fail loud with actionable message
            extra_handlers=((FlowPlanError, EXIT_CODE_BY_STATUS["failed"]),),
        )
        if rc != 0:
            return rc
        output, terminal_status = flow_result
        if not args.verbose:
            print(output)
        return EXIT_CODE_BY_STATUS.get(terminal_status, 0)

    if args.orch_command == "ctl":
        if args.ctl_command == "status":
            from lionagi.cli.status import run_ctl_status

            return run_ctl_status(args)
        if args.ctl_command == "pause":
            from ._control import run_ctl_pause

            return run_ctl_pause(args)
        if args.ctl_command == "resume":
            from ._control import run_ctl_resume

            return run_ctl_resume(args)
        if args.ctl_command == "msg":
            from ._control import run_ctl_msg

            return run_ctl_msg(args)
        log_error(f"Unknown ctl command: {args.ctl_command}")
        return 1

    log_error(f"Unknown orchestrate command: {args.orch_command}")
    return 1
