# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li orchestrate` — multi-agent orchestration patterns (fanout, flow)."""

from __future__ import annotations

import argparse
import sys

from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.libs.path_safety import validate_path_component as validate_path_component
from lionagi.ln.concurrency import is_cancelled, run_async

from .._lifecycle import EXIT_CODE_BY_STATUS
from .._logging import hint, log_error
from .._providers import add_common_cli_args
from ._spec import (
    _coerce_arg_value,
    _derive_args_schema_from_spec,
    _interpolate_prompt,
    _load_flow_spec,
    _resolve_playbook_path,
    _validate_args_schema,
    _validate_spec_fields,
)
from ._spec import (
    _parse_argument_hint as _parse_argument_hint,
)
from ._spec import (
    _scan_argv_for_playbook_name as _scan_argv_for_playbook_name,
)
from ._spec import (
    inject_playbook_schema_into_parser as inject_playbook_schema_into_parser,
)
from .fanout import _run_fanout
from .flow import FlowPlanError, _run_flow


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
            "Effort can be embedded in model spec: claude/opus-4-7-high."
        ),
    )
    fo.add_argument(
        "model",
        nargs="?",
        default=None,
        help=(
            "Orchestrator model spec (provider/model-effort). "
            "Also used as default worker model unless --workers specified. "
            "Optional when -a/--agent provides a model."
        ),
    )
    fo.add_argument("prompt", help="Task prompt for the orchestrator to decompose.")
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
            "parallelism where dependencies allow."
        ),
    )
    fl.add_argument(
        "model",
        nargs="?",
        default=None,
        help="Orchestrator model spec. Optional when -a/--agent provides one.",
    )
    fl.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Task for the orchestrator to plan and execute.",
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
    add_common_cli_args(fl)

    return {"fanout": fo, "flow": fl}


def run_orchestrate(args: argparse.Namespace) -> int:
    if args.orch_command == "fanout":
        has_model = args.model is not None or args.agent is not None
        if not has_model:
            log_error("model or --agent is required")
            return 1

        synth = args.with_synthesis
        with_synthesis = synth is not False
        synthesis_model = synth if isinstance(synth, str) else None

        try:
            output = run_async(
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
                )
            )
        except (TimeoutError, LionTimeoutError) as e:
            log_error(str(e))
            return EXIT_CODE_BY_STATUS["timed_out"]
        except KeyboardInterrupt:
            return EXIT_CODE_BY_STATUS["aborted"]
        except BaseException as exc:
            if is_cancelled(exc):
                return EXIT_CODE_BY_STATUS["cancelled"]
            raise
        if not args.verbose:
            print(output)
        return 0

    if args.orch_command == "flow":
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

        try:
            output, terminal_status = run_async(
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
                )
            )
        except (TimeoutError, LionTimeoutError) as e:
            log_error(str(e))
            return EXIT_CODE_BY_STATUS["timed_out"]
        except KeyboardInterrupt:
            return EXIT_CODE_BY_STATUS["aborted"]
        except FlowPlanError as e:
            # planning produced no usable DAG — fail loud with actionable message
            log_error(str(e))
            return EXIT_CODE_BY_STATUS["failed"]
        except BaseException as exc:
            if is_cancelled(exc):
                return EXIT_CODE_BY_STATUS["cancelled"]
            raise
        if not args.verbose:
            print(output)
        return EXIT_CODE_BY_STATUS.get(terminal_status, 0)

    log_error(f"Unknown orchestrate command: {args.orch_command}")
    return 1
