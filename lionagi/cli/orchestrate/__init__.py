# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li orchestrate` — multi-agent orchestration patterns (fanout, flow)."""

from __future__ import annotations

import argparse
import sys

from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import run_async

from .._logging import hint, log_error
from .._providers import add_common_cli_args
from .fanout import _run_fanout
from .flow import _run_flow


def add_orchestrate_subparser(
    subparsers: argparse._SubParsersAction,
) -> dict[str, argparse.ArgumentParser]:
    """Register `li orchestrate` (alias `li o`) with its sub-commands.

    Returns a mapping of sub-command name → ArgumentParser so callers that
    need to post-hoc extend a sub-parser (e.g. to inject playbook-declared
    flags) can do so without re-navigating argparse internals.
    """
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

    # ── flow sub-command ─────────────────────────────────────────────
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
            "Playbooks may declare args: schema or argument-hint: for "
            "CLI flags that fill template placeholders {name} in the prompt."
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
    add_common_cli_args(fl)

    return {"fanout": fo, "flow": fl}


def _scan_argv_for_playbook_name(argv: list[str]) -> str | None:
    """Scan argv for -p NAME / --playbook NAME / --playbook=NAME."""
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
    """Extract args schema from a loaded spec dict.

    Priority: explicit args: block > argument-hint: fallback.
    Returns {} on malformed input (caller validates separately).
    """
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
    """Pre-scan argv for -p/--playbook; if found, load the playbook and
    add its declared args as flags on the flow sub-parser.

    Must be called BEFORE parser.parse_args so argparse can recognize
    playbook-declared flags and consume their values correctly. No-op if
    argv doesn't reference a playbook, or if resolution/loading fails
    (errors surface at dispatch time via run_orchestrate).

    Returns the extracted args schema (empty dict if none).
    """
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
    # Collect reserved option strings already defined on the flow parser.
    # Playbook flags that collide are skipped with a warning — the base
    # parser's flag wins, and the playbook author should rename.
    reserved: set[str] = set()
    for action in flow_parser._actions:
        for opt in getattr(action, "option_strings", ()):
            reserved.add(opt)
    # Add each schema-declared flag to the flow parser. Use default=None so
    # we can distinguish "user didn't pass" from "user passed false".
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
    # Stash the collision-filtered schema on the parser as a default so
    # run_orchestrate can interpolate against the exact same set of args
    # (without re-deriving and re-introducing collisions).
    flow_parser.set_defaults(_playbook_args_schema=resolved_schema)
    return resolved_schema


def _resolve_playbook_path(name: str) -> tuple[object, str | None]:
    """Resolve a playbook NAME to its file path.

    Returns (Path, None) on success, or (None, error_message) on failure.
    """
    from pathlib import Path

    if not name or not isinstance(name, str):
        return None, "playbook name must be a non-empty string"
    # Reject path separators — NAME is a bare identifier.
    if "/" in name or "\\" in name or name.startswith("."):
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
    # Symlink containment — the playbooks root may itself be a symlink
    # (users can point `~/.lionagi/playbooks/` at any directory they
    # manage); comparing resolved paths accepts that while rejecting a
    # malicious per-playbook symlink pointing at an arbitrary file on disk.
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
    """Parse CC-style argument-hint string into an args schema.

    Examples:
        '[--tabs N]'     → {"tabs": {"type": "str", "default": None}}
        '[--poll]'       → {"poll": {"type": "bool", "default": False}}
        '[--tabs N] [--poll]' → combination of both

    Unparseable tokens are skipped silently. For strict typing, use args: block.
    """
    import re

    schema: dict = {}
    # Match [--flag] or [--flag VALUE] or [--flag N] etc.
    # Group 1 = flag name, Group 2 = optional value placeholder
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
    """Validate the args: block. Returns error message or None."""
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
    """Coerce a raw string/bool from argparse into the schema-declared type.

    Returns (coerced_value, None) on success or (None, error_message).
    """
    if value is None:
        return None, None
    if type_str == "bool":
        # argparse store_true gives us a bool directly
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
    """Load a YAML or JSON flow spec file.

    Returns a dict on success, or None after logging a CLI-facing error.
    Empty specs are treated as an empty object.
    """
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
    # Normalize top-level keys: accept both dashed (`max-agents`) and
    # underscored (`max_agents`) forms so authors can mirror CLI flag names.
    # `argument-hint` stays dashed (CC convention, parsed specially). `args`
    # holds user-defined arg names; don't touch its children.
    preserve_dashed = {"argument-hint"}
    normalized: dict = {}
    for key, value in data.items():
        if key in preserve_dashed or "-" not in key:
            normalized[key] = value
        else:
            normalized[key.replace("-", "_")] = value
    return normalized


def _validate_spec_fields(spec: dict) -> str | None:
    """Validate spec field types and ranges. Returns an error message or None.

    Uses ``in`` checks (not ``.get()``) so that YAML ``null`` (Python ``None``)
    is treated as an invalid present value for all fields except ``effort``,
    which explicitly allows ``None`` (meaning "use the profile default effort").
    """
    if "workers" in spec:
        workers = spec["workers"]
        if not isinstance(workers, int) or isinstance(workers, bool):
            return (
                f"spec field 'workers' must be an integer, got {type(workers).__name__}"
            )
        if not (1 <= workers <= 32):
            return f"spec field 'workers' must be in [1, 32], got {workers}"

    # `max_ops` is canonical; `max_agents` is a deprecated alias. Validate
    # whichever key is present (rejecting a present-None value) and preserve
    # the user-supplied key name in error messages for discoverability.
    for key in ("max_ops", "max_agents"):
        if key not in spec:
            continue
        value = spec[key]
        if not isinstance(value, int) or isinstance(value, bool):
            return f"spec field {key!r} must be an integer, got {type(value).__name__}"
        # CLI docs `--max-ops`/`--max-agents` as "0 = unlimited" (default).
        # Mirror that in spec: accept 0 (means unlimited) and 1-50 as a cap.
        if not (0 <= value <= 50):
            return f"spec field {key!r} must be in [0, 50] (0 = unlimited), got {value}"

    # effort: None is explicitly allowed (means "use profile default").
    # Allowed values mirror provider EFFORT_LEVELS in cli/_providers.py so
    # the spec validator can't reject values the CLI itself accepts.
    effort = spec.get("effort")
    if effort is not None:
        from .._providers import EFFORT_LEVELS

        if not isinstance(effort, str):
            return f"spec field 'effort' must be a string, got {type(effort).__name__}"
        if effort not in EFFORT_LEVELS:
            allowed = sorted(EFFORT_LEVELS)
            return f"spec field 'effort' must be one of {allowed}, got {effort!r}"

    # with_synthesis mirrors the CLI `--with-synthesis [MODEL]` surface:
    #   bool  → use orchestrator model (bare flag)
    #   str   → synthesis model spec (flag with explicit value)
    if "with_synthesis" in spec:
        val = spec["with_synthesis"]
        if not isinstance(val, (bool, str)):
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

    for str_field in ("model", "agent", "team_mode", "team_attach"):
        if str_field in spec:
            val = spec[str_field]
            if not isinstance(val, str):
                return f"spec field {str_field!r} must be a string, got {type(val).__name__}"

    return None


def _interpolate_prompt(
    template: str, positional: str | None, playbook_args: dict
) -> str:
    """Interpolate {input} + all playbook args into the prompt template.

    - {input} substitution uses the positional prompt if present
    - {arg_name} substitutions use playbook_args (CLI-overridden values + defaults)
    - If no placeholders present AND a positional prompt exists, append the
      positional to the template (CC-skill-style)
    """
    if not template:
        return positional or ""

    # Build substitution context: {input} + all named args
    ctx: dict = dict(playbook_args)
    if positional is not None:
        ctx["input"] = positional

    # Detect any placeholder using format-style {name} tokens
    import re

    placeholders = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template))
    if not placeholders and positional is not None:
        # No placeholders — append positional like a CC skill
        return template + "\n\n" + positional

    # Render: missing keys remain as literal {name} tokens
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in ctx:
            return str(ctx[key])
        return match.group(0)

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _sub, template)


def run_orchestrate(args: argparse.Namespace) -> int:
    """Dispatch orchestrate sub-commands."""
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
                )
            )
        except (TimeoutError, LionTimeoutError) as e:
            log_error(str(e))
            return 124  # ADR-0025: timed_out exits with GNU `timeout` code
        except KeyboardInterrupt:
            return 130  # ADR-0025: aborted (SIGINT)
        except BaseException as exc:
            from lionagi.ln.concurrency import get_cancelled_exc_class

            if isinstance(exc, get_cancelled_exc_class()):
                return 143  # ADR-0025: cancelled (SIGTERM)
            raise
        if not args.verbose:
            print(output)
        return 0

    if args.orch_command == "flow":
        # ── Resolve -p/--playbook NAME into a concrete file path ─────
        playbook_name = getattr(args, "playbook", None)
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

        # ── Load spec file if -f/--file or -p/--playbook was given ──
        if file_spec:
            spec = _load_flow_spec(file_spec)
            if spec is None:
                return 1
            spec_err = _validate_spec_fields(spec)
            if spec_err is not None:
                log_error(spec_err)
                return 1

            # ── Derive args schema: explicit args: block OR argument-hint fallback
            if "args" in spec:
                schema_err = _validate_args_schema(spec["args"])
                if schema_err is not None:
                    log_error(schema_err)
                    return 1
            # Prefer the collision-filtered schema stashed by
            # inject_playbook_schema_into_parser — guarantees we don't
            # interpolate against args whose flags were shadowed by base CLI.
            # Use `is None` (not truthiness) so an empty filtered schema —
            # e.g. every declared arg collided — still wins over re-derivation.
            args_schema = getattr(args, "_playbook_args_schema", None)
            if args_schema is None:
                args_schema = _derive_args_schema_from_spec(spec)

            # Playbook-declared flags were injected into the argparse parser
            # before parse_args ran (see inject_playbook_schema_into_parser).
            # Read the parsed values straight off the namespace.
            playbook_ctx: dict = {}
            for name, field in args_schema.items():
                if field.get("default") is not None:
                    playbook_ctx[name] = field["default"]
                raw = getattr(args, name, None)
                if raw is None:
                    continue
                coerced, coerce_err = _coerce_arg_value(
                    name, raw, field.get("type", "str")
                )
                if coerce_err is not None:
                    log_error(coerce_err)
                    return 1
                playbook_ctx[name] = coerced

            # If the file supplies the model/agent, argparse's lone positional
            # is a prompt override, not a model override.
            if (
                args.model
                and args.prompt is None
                and (spec.get("model") or spec.get("agent"))
            ):
                args.prompt = args.model
                args.model = None
            # File values are defaults; CLI flags override.
            if args.model is None and "model" in spec:
                args.model = spec["model"]
            if args.agent is None and spec.get("agent"):
                args.agent = spec["agent"]
            if spec.get("prompt"):
                args.prompt = _interpolate_prompt(
                    spec["prompt"], args.prompt, playbook_ctx
                )
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
            # Prefer max_ops; fall back to deprecated max_agents spec field.
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
            if args.save is None and spec.get("save"):
                args.save = spec["save"]
            if spec.get("critic_model"):
                pass  # reserved for future use

        # Argparse assigns a lone positional to `model`, leaving prompt
        # None. When --agent supplies the model and the user passed a
        # single positional, that positional is actually the prompt.
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

        if (
            args.team_mode is not None
            and getattr(args, "team_attach", None) is not None
        ):
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
            import subprocess
            from pathlib import Path as _Path

            bg_args = [a for a in sys.argv[1:] if a != "--background"]
            log_root = _Path(args.save).expanduser()
            log_root.mkdir(parents=True, exist_ok=True)
            log_path = log_root / "flow.log"
            with open(log_path, "w") as log_f:
                proc = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "lionagi.cli", *bg_args],
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            hint(f"Flow running in background (PID {proc.pid})")
            hint(f"Output: {log_path}")
            hint(f"Monitor: tail -f {log_path}")
            return 0

        synth = args.with_synthesis
        with_synthesis = synth is not False
        synthesis_model = synth if isinstance(synth, str) else None

        try:
            output = run_async(
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
                    max_ops=args.max_ops,
                    dry_run=args.dry_run,
                    show_graph=getattr(args, "show_graph", False),
                    fast=getattr(args, "fast", False),
                    playbook_name=playbook_name,
                    invocation_id=getattr(args, "invocation", None),
                )
            )
        except (TimeoutError, LionTimeoutError) as e:
            log_error(str(e))
            return 124  # ADR-0025: timed_out exits with GNU `timeout` code
        except KeyboardInterrupt:
            return 130  # ADR-0025: aborted (SIGINT)
        except BaseException as exc:
            from lionagi.ln.concurrency import get_cancelled_exc_class

            if isinstance(exc, get_cancelled_exc_class()):
                return 143  # ADR-0025: cancelled (SIGTERM)
            raise
        if not args.verbose:
            print(output)
        return 0

    log_error(f"Unknown orchestrate command: {args.orch_command}")
    return 1
