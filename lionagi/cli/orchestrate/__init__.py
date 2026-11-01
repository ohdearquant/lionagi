# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li orchestrate` — multi-agent orchestration patterns (fanout, flow)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lionagi._auto import CliDeclaration, auto_register
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi._flow_spec import (
    FLOW_SPEC_FIELDS,
    load_flow_spec,
    validate_flow_args_schema,
    validate_flow_spec_fields,
)
from lionagi._provider_errors import NestedAgentRefusedError
from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.libs.path_safety import validate_path_component as validate_path_component
from lionagi.ln.concurrency import is_cancelled, run_async
from lionagi.service._subagent_admission import refuse_nested_agent

from .._logging import hint, log_error
from .._orchestrate_parsing import _add_mcp_config_args as _add_mcp_config_args
from .._orchestrate_parsing import add_orchestrate_subparser as add_orchestrate_subparser
from .._util import EXIT_CODE_BY_STATUS
from ._checkpoint import FlowResumeError
from .fanout import FanoutPlanError, _run_fanout
from .flow import FlowPlanError, _resume_flow, _run_flow, _validate_flow_invocation

# ── flow-spec helpers ────────────────────────────────────────────────────────

_FLOW_SPEC_FIELDS = FLOW_SPEC_FIELDS
_validate_args_schema = validate_flow_args_schema
_validate_spec_fields = validate_flow_spec_fields


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
        # A playbook that declares no help for its own argument still gets a line worth
        # reading, rather than a blank column in `--help`.
        help_text = field.get("help") or (
            f"Playbook argument {arg_name!r} ({type_str}), declared by this playbook's args: block."
        )
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


def _plugin_playbook_files() -> dict[str, tuple[str, Path]]:
    """``<plugin>/<name>`` -> (plugin name, playbook path) for every ACTIVE
    plugin (trusted+enabled+compatible).

    A plugin's playbooks only ever join the search *after* project and
    global playbooks: local files always win, and an untrusted or disabled
    plugin contributes nothing here at all.
    """
    from lionagi.plugins import PluginRegistry

    return PluginRegistry.active_playbook_files()


def _resolve_plugin_playbook_path(name: str) -> Path | None:
    """Resolve *name* against active plugins: an explicit ``<plugin>/<playbook>``

    token always resolves via direct lookup; a bare name resolves only when
    exactly one active plugin declares it (ambiguous bare names are left
    unresolved — namespacing exists precisely so a caller can disambiguate).
    """
    plugin_playbooks = _plugin_playbook_files()
    if "/" in name:
        entry = plugin_playbooks.get(name)
        return entry[1] if entry is not None else None

    matches = [
        path for token, (_plugin, path) in plugin_playbooks.items() if token.endswith(f"/{name}")
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _warn_if_shadowing_plugin_playbook(name: str) -> None:
    """Log a shadow warning when a local playbook hides a same-named plugin playbook.

    The user's own explicit local file always wins on a bare-name collision;
    this only makes the shadowing visible instead of silent.
    """
    plugin_playbooks = _plugin_playbook_files()
    owners = sorted(
        {token.split("/", 1)[0] for token in plugin_playbooks if token.endswith(f"/{name}")}
    )
    if not owners:
        return
    from .._logging import warn

    warn(
        f"playbook {name!r} is also declared by plugin(s) {', '.join(owners)}; "
        f"using the local file (load the plugin's version with '<plugin>/{name}')."
    )


def _warn_if_shadowing_global_playbook(name: str, matched_dir: Path) -> None:
    """Log a shadow warning when a project-local playbook hides the same-named global one.

    The nearer (project-local) file always wins on a bare-name collision;
    this only makes the shadowing visible instead of silent — an untrusted
    checkout could otherwise supply model instructions the user did not
    intend to run whenever they invoke a familiar (globally-defined) name.
    """
    global_dir = Path.home() / ".lionagi"
    if matched_dir == global_dir:
        return
    global_candidate = global_dir / "playbooks" / f"{name}.playbook.yaml"
    if not global_candidate.is_file():
        return

    from .._logging import warn

    local_candidate = matched_dir / "playbooks" / f"{name}.playbook.yaml"
    warn(
        f"playbook {name!r} at {local_candidate} shadows the global playbook "
        f"at {global_candidate}; using the project-local file."
    )


def list_playbooks() -> list[str]:
    """List available playbook names, merged across ``.lionagi/`` dirs and active plugins.

    Plugin-declared playbooks are namespaced as ``<plugin>/<name>``.
    """
    from lionagi._paths import find_lionagi_dirs

    seen: set[str] = set()
    for d in find_lionagi_dirs():
        playbooks_dir = d / "playbooks"
        if not playbooks_dir.is_dir():
            continue
        for p in sorted(playbooks_dir.glob("*.playbook.yaml")):
            if p.is_file():
                seen.add(p.name.removesuffix(".playbook.yaml"))
    seen.update(_plugin_playbook_files().keys())
    return sorted(seen)


def _resolve_playbook_path(name: str) -> tuple[object, str | None]:
    """Resolve a playbook NAME to (Path, None) or (None, error_message).

    Searches project-local then global ``.lionagi/playbooks/`` (via
    ``find_lionagi_dirs()``), then active plugin bundles: a plugin's
    playbooks join the search only after a project/global miss (bare name),
    or via an explicit ``<plugin>/<name>`` token, and only for a plugin that
    is trusted + enabled + version-compatible (see ``lionagi.plugins``).
    """
    from lionagi._paths import find_lionagi_dirs
    from lionagi.libs.path_safety import validate_path_component

    if not name or not isinstance(name, str):
        return None, "playbook name must be a non-empty string"

    plugin_token = "/" in name
    if plugin_token:
        # A `<plugin>/<name>` token is opaque to the single-component
        # validator (which forbids '/'); validate each half separately
        # instead of rejecting the whole string outright — this is exactly
        # the shape ADR-0088 D6 specifies for a plugin-namespaced playbook.
        plugin_part, _, playbook_part = name.partition("/")
        try:
            validate_path_component(plugin_part, label="plugin NAME")
            validate_path_component(playbook_part, label="playbook NAME")
        except ValueError:
            return (
                None,
                "playbook NAME must be a bare identifier or <plugin>/<name> "
                f"token, got {name!r}. Use -f /abs/path.yaml for ad-hoc specs.",
            )
    else:
        try:
            validate_path_component(name, label="playbook NAME")
        except ValueError:
            return (
                None,
                f"playbook NAME must be a bare identifier, got {name!r}. "
                "Use -f /abs/path.yaml for ad-hoc specs.",
            )
        plugin_part = playbook_part = None

    dirs = find_lionagi_dirs()

    if not plugin_token:
        for d in dirs:
            playbooks_dir = d / "playbooks"
            candidate = playbooks_dir / f"{name}.playbook.yaml"
            if not candidate.is_file():
                continue
            try:
                resolved_root = playbooks_dir.resolve(strict=True)
                resolved_candidate = candidate.resolve(strict=True)
                resolved_candidate.relative_to(resolved_root)
            except (OSError, ValueError):
                return (
                    None,
                    f"playbook {name!r} resolves outside playbooks root (symlink escape blocked)",
                )
            _warn_if_shadowing_plugin_playbook(name)
            _warn_if_shadowing_global_playbook(name, d)
            return candidate, None

    plugin_path = _resolve_plugin_playbook_path(name)
    if plugin_path is not None:
        return plugin_path, None

    if plugin_token:
        return (
            None,
            f"playbook not found: plugin {plugin_part!r} is not active (no "
            "such plugin, or untrusted/disabled/incompatible), or does not "
            f"declare a playbook named {playbook_part!r}",
        )

    suggestions = list_playbooks()
    hint_text = (
        f" Available: {', '.join(suggestions[:10])}" if suggestions else " No playbooks found."
    )
    return None, f"playbook not found: {name!r}.{hint_text}"


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
    try:
        return load_flow_spec(path)
    except ValueError as exc:
        log_error(str(exc))
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


def _check_assembled_prompt(prompt: str) -> str | None:
    """Measure the prompt that will run, not the one it was written from.

    A spec's prompt field is checked at file-read time, but that's a
    template — substituting the caller's positional and the playbook's
    arguments afterward can make the result far longer than the text that
    passed. Uses the same length bound as the spec-field check, deliberately:
    a larger bound here would let an assembled prompt pass while the
    template it came from would not have.
    """
    if len(prompt) > MAX_SPEC_PROMPT_CHARS:
        return (
            f"assembled prompt exceeds maximum length of {MAX_SPEC_PROMPT_CHARS} "
            f"characters (got {len(prompt)})"
        )
    return None


def _resolve_model_and_prompt(query: list[str]) -> tuple[str | None, str | None] | None:
    """Assign a 0-2 token positional bucket to (model, prompt), mirroring the
    `li agent` [MODEL] PROMPT convention: one token is the prompt, two are
    (model, prompt). Returns None (after logging) for >2 positionals.
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

    Returns (result, exit_code). extra_handlers is a tuple of (ExcType,
    exit_code) pairs checked before the shared map, for pattern-specific
    exceptions without repeating the common mapping.
    """
    try:
        result = run_async(coro)
    except (TimeoutError, LionTimeoutError) as e:
        log_error(str(e))
        return None, EXIT_CODE_BY_STATUS["timed_out"]
    except KeyboardInterrupt:
        return None, EXIT_CODE_BY_STATUS["aborted"]
    except NestedAgentRefusedError as exc:
        log_error(str(exc))
        return None, EXIT_CODE_BY_STATUS["failed"]
    except BaseException as exc:
        for exc_type, code in extra_handlers:
            if isinstance(exc, exc_type):
                log_error(str(exc))
                return None, code
        if is_cancelled(exc):
            return None, EXIT_CODE_BY_STATUS["cancelled"]
        raise
    return result, 0


def _warn_resume_on_timeout_is_inert(args: argparse.Namespace) -> None:
    """Tell a caller that passed ``--resume-on-timeout`` that nothing reads it.

    Neither orchestrate command implements the auto-resume contract, so the
    value is discarded. That is invisible from the outside: the run succeeds
    and nothing in its output suggests the option did nothing, which is why
    the only honest moment to say so is before the work starts. The option is
    still accepted, because invocations that pass it parse today and would
    fail outright if it were simply deleted.
    """
    if not getattr(args, "resume_on_timeout", False):
        return

    import warnings

    from .._logging import warn

    message = (
        "--resume-on-timeout has no effect on `li orchestrate "
        f"{args.orch_command}` and will be removed in a future release. "
        "Only `li agent` implements it."
    )
    warnings.warn(message, DeprecationWarning, stacklevel=2)
    warn(message)


@auto_register(
    area="orchestrate",
    cli=CliDeclaration(seed="orchestrate", parser_factory=add_orchestrate_subparser),
)
def run_orchestrate(args: argparse.Namespace) -> int:
    _warn_resume_on_timeout_is_inert(args)
    if args.orch_command == "fanout":
        resolved = _resolve_model_and_prompt(getattr(args, "query", None) or [])
        if resolved is None:
            return 1
        args.model, args.prompt = resolved

        # Naming neither model nor agent defaults to the orchestrator profile
        # (setup_orchestration); a prompt is still required regardless.
        if not args.prompt:
            log_error("prompt is required")
            return 1

        prompt_err = _check_assembled_prompt(args.prompt)
        if prompt_err is not None:
            log_error(prompt_err)
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
                notify=getattr(args, "notify", None),
                mcp_config=getattr(args, "mcp_config", None),
                no_mcp_config=getattr(args, "no_mcp_config", False),
            ),
            verbose=args.verbose,
            # planning produced no usable assignments — fail loud with actionable message
            extra_handlers=((FanoutPlanError, EXIT_CODE_BY_STATUS["failed"]),),
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
                    retry_failed=getattr(args, "retry_failed", False),
                    dry_run=args.dry_run,
                    show_graph=getattr(args, "show_graph", False),
                    notify=getattr(args, "notify", None),
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

        # Naming neither model nor agent defaults to the orchestrator profile
        # (setup_orchestration); a prompt is still required regardless.
        if not args.prompt:
            log_error("prompt is required (positional or via -f spec file)")
            return 1

        prompt_err = _check_assembled_prompt(args.prompt)
        if prompt_err is not None:
            log_error(prompt_err)
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
            # The detached run checks again, but if this process exits first, that run has left
            # the process tree that shows it runs inside an agent (ADR-0133 D7).
            try:
                refuse_nested_agent()
            except NestedAgentRefusedError as exc:
                log_error(str(exc))
                return EXIT_CODE_BY_STATUS["failed"]
            if getattr(args, "invocation", None) is not None:
                _unused, validation_rc = _run_orch_command(
                    _validate_flow_invocation(args.invocation),
                    verbose=args.verbose,
                    extra_handlers=((FlowPlanError, EXIT_CODE_BY_STATUS["failed"]),),
                )
                if validation_rc:
                    return validation_rc
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
            hint(f"Session: {bg_session_id}  →  li monitor {bg_session_id[:8]}")
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
                team_max_rounds=getattr(args, "team_max_rounds", 2),
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
                notify=getattr(args, "notify", None),
                mcp_config=getattr(args, "mcp_config", None),
                no_mcp_config=getattr(args, "no_mcp_config", False),
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
        if args.ctl_command == "resolve":
            from ._control import run_ctl_resolve

            return run_ctl_resolve(args)
        log_error(f"Unknown ctl command: {args.ctl_command}")
        return 1

    log_error(f"Unknown orchestrate command: {args.orch_command}")
    return 1
