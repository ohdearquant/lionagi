# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — one-shot or resumed single-agent conversation."""

from __future__ import annotations

import argparse
import json

from lionagi import Branch
from lionagi._errors import ConfigurationError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import (
    cache_cancelled_exc_class,
    cancelled_exc_classes,
    run_async,
)
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.state import provenance as _provenance
from lionagi.state.artifact_verifier import resolve_artifact_contract

from ._logging import hint, log_error
from ._providers import (
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    add_common_cli_args,
    build_chat_model,
    build_deadline_preamble,
    load_agent_profile,
    parse_model_spec,
    resolve_persisted_effort,
)
from ._runs import (
    allocate_run,
    find_branch,
    load_last_branch,
    save_last_branch_pointer,
    setup_agent_persist,
    teardown_agent_persist,
)
from ._util import EXIT_CODE_BY_STATUS, classify_exception

# ---------------------------------------------------------------------------
# Preset names supported by --preset
# ---------------------------------------------------------------------------

_PRESET_CHOICES = ("coding",)


def _make_coding_preset(
    cwd: str | None = None,
    effort: str | None = "high",
    system_prompt: str | None = None,
):
    """Construct an AgentSpec.coding() instance; isolated for test monkeypatching."""
    from lionagi.agent.spec import AgentSpec

    return AgentSpec.coding(cwd=cwd, effort=effort, system_prompt=system_prompt)


# ---------------------------------------------------------------------------
# WorkForm loading helpers (for --form)
# ---------------------------------------------------------------------------


_FORM_SPEC_ALLOWED_KEYS = frozenset({"title", "fields", "values"})


def _load_form_spec(path: str) -> dict:
    """Load a YAML or JSON work-form spec file; raises ValueError/FileNotFoundError on failure."""
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists():
        raise FileNotFoundError(f"form spec file not found: {path!r}")
    if not p.is_file():
        raise ValueError(f"form spec path is not a regular file: {path!r}")

    with open(path) as fh:
        raw = fh.read()

    # Try YAML first (superset of JSON), then fall back to plain JSON.
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(raw)
    except Exception as yaml_err:
        try:
            data = json.loads(raw)
        except Exception:
            raise ValueError(f"could not parse form spec {path!r}: {yaml_err}") from yaml_err

    if not isinstance(data, dict):
        raise ValueError(
            f"form spec {path!r} must be a YAML/JSON mapping, got {type(data).__name__}"
        )
    return data


def _build_work_form(spec: dict, spec_path: str):
    """Construct a WorkForm from a parsed spec dict (keys: title, fields, values)."""
    from lionagi.work import FieldSpec, WorkForm, fill_form

    # Enforce closed top-level schema.
    unknown_keys = set(spec) - _FORM_SPEC_ALLOWED_KEYS
    if unknown_keys:
        bad = ", ".join(sorted(f"{k!r}" for k in unknown_keys))
        raise ValueError(
            f"form spec {spec_path!r}: unknown top-level key(s) {bad}; "
            f"allowed: {sorted(_FORM_SPEC_ALLOWED_KEYS)}"
        )

    title = spec.get("title", spec_path)
    raw_fields_raw = spec.get("fields")
    raw_values_raw = spec.get("values")

    # Validate types: 'fields' and 'values' must be mappings when present.
    if raw_fields_raw is not None and not isinstance(raw_fields_raw, dict):
        raise ValueError(
            f"form spec {spec_path!r}: 'fields' must be a mapping, "
            f"got {type(raw_fields_raw).__name__!r}"
        )
    if raw_values_raw is not None and not isinstance(raw_values_raw, dict):
        raise ValueError(
            f"form spec {spec_path!r}: 'values' must be a mapping, "
            f"got {type(raw_values_raw).__name__!r}"
        )

    raw_fields: dict = raw_fields_raw or {}
    raw_values: dict = raw_values_raw or {}

    # Enforce: values without declared fields is not a valid use of --form.
    # --form is a validation gate; forwarding unvalidated values silently
    # defeats its purpose.  Use the prompt directly for unstructured context.
    if raw_values and not raw_fields:
        raise ValueError(
            f"form spec {spec_path!r}: 'values' are declared but 'fields' is "
            "absent or empty; declare fields to validate values against"
        )

    # When fields are declared, reject undeclared value keys.
    if raw_fields:
        undeclared = set(raw_values) - set(raw_fields)
        if undeclared:
            bad = ", ".join(sorted(f"{k!r}" for k in undeclared))
            raise ValueError(
                f"form spec {spec_path!r}: values contain undeclared key(s) {bad}; "
                f"declared fields: {sorted(raw_fields)}"
            )

    fields: dict[str, FieldSpec] = {}
    for name, fspec in raw_fields.items():
        if not isinstance(fspec, dict):
            raise ValueError(
                f"form spec {spec_path!r}: field {name!r} must be a mapping, "
                f"got {type(fspec).__name__}"
            )
        try:
            fields[name] = FieldSpec(name=name, **fspec)
        except Exception as exc:
            raise ValueError(f"form spec {spec_path!r}: invalid field {name!r}: {exc}") from exc

    form = WorkForm(title=title, fields=fields)
    if raw_values or fields:
        form = fill_form(form, raw_values)
    return form


def _form_to_context_block(form) -> str:
    """Render a validated WorkForm's values as a structured context preamble.

    Returns a string that can be prepended to the user's prompt so the LLM
    receives the form values as structured inputs.
    """
    lines = [f"[Work Form: {form.title}]"]
    for key, value in form.values.items():
        lines.append(f"  {key}: {value!r}")
    return "\n".join(lines)


async def _run_agent(
    model_str: str | None,
    prompt: str,
    yolo: bool = False,
    verbose: bool = False,
    theme: str | None = None,
    resume: str | None = None,
    continue_last: bool = False,
    effort: str | None = None,
    agent_name: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    fast: bool = False,
    invocation_id: str | None = None,
    project: str | None = None,
    bypass: bool = False,
    preset: str | None = None,
) -> tuple[str, str, str, str]:
    """Execute one agent turn; returns (result, provider, branch_id, terminal_status)."""
    if resume and continue_last:
        raise ConfigurationError("--resume / -r and --continue-last / -c are mutually exclusive.")
    if preset and (resume or continue_last):
        raise ConfigurationError(
            "--preset only applies to new branches; cannot combine with --resume / --continue-last."
        )

    # Cache cancellation exception class while event loop is running;
    # cancelled_exc_classes() in the error path needs it after loop exit.
    try:
        cache_cancelled_exc_class()
    except Exception as _cache_err:
        import logging as _logging

        _logging.getLogger("lionagi.cli").debug(
            "cache_cancelled_exc_class() failed (non-fatal): %s", _cache_err
        )

    profile = None
    if agent_name:
        profile = load_agent_profile(agent_name)
        if profile.model and model_str is None:
            model_str = profile.model
        if profile.effort and effort is None:
            effort = profile.effort
        if profile.yolo and not yolo:
            yolo = True
        if profile.fast_mode and not fast:
            fast = True

    branch: Branch | None = None
    if continue_last:
        _, branch_id = load_last_branch()
        _, branch_path = find_branch(branch_id)
        branch = Branch.from_dict(json.loads(branch_path.read_text()))
    elif resume:
        _, branch_path = find_branch(resume)
        branch = Branch.from_dict(json.loads(branch_path.read_text()))

    if model_str is not None:
        ms = parse_model_spec(model_str)
        if "/" in ms.model:
            provider, model = ms.model.split("/", 1)
        else:
            provider, model = ms.model, ms.model
        if ms.effort and not effort:
            effort = ms.effort
    elif branch is not None:
        ep_cfg = branch.chat_model.endpoint.config
        provider = ep_cfg.provider
        model = ep_cfg.kwargs.get("model")
    else:
        raise ValueError(
            "Provide a model spec (e.g. 'claude') for a new branch, "
            "or use --resume / --continue-last to reopen an existing one."
        )

    if branch is None:
        # codex sandbox blocks tool calls without bypass — warn early.
        if provider == "codex" and not yolo and not bypass:
            from lionagi.cli._logging import warn

            warn(
                "codex models require --bypass or --yolo for local file access. "
                "Without one of these flags the agent may hang silently. "
                "Re-run with --bypass or use an agent profile (-a)."
            )
        chat_model = build_chat_model(provider, model, yolo, verbose, theme, effort, fast, bypass)
        effort = resolve_persisted_effort(provider, chat_model, effort)

        if preset == "coding":
            # 3a: use create_agent so CodingToolkit tools and path-guards are
            # fully wired (guard_destructive on bash, guard_paths on
            # reader/editor).  The factory installs the full system message via
            # set_system() — compose the profile extension into the spec BEFORE
            # calling create_agent so both preset role/policy AND the profile
            # prompt land in a single system message.
            #
            # AgentSpec.coding(system_prompt=...) maps to spec.extra_prompt,
            # which build_system_message() appends AFTER the role header and
            # policy block — no duplication of the LION system text.
            # The post-factory add_message on the preset path is skipped to
            # avoid set_system replacing the composed message.
            from lionagi.agent.factory import create_agent

            # Use profile.raw_body (not profile.system_prompt) to avoid
            # duplicating LION_SYSTEM_MESSAGE: _parse_profile prepends it into
            # system_prompt when lion_system=True, and factory.py:117-125 also
            # prepends it because spec.lion_system remains True.  raw_body is
            # the profile body before that expansion; the factory adds the
            # header exactly once.  When lion_system=False, raw_body==system_prompt
            # so both paths are consistent.
            profile_extra = (getattr(profile, "raw_body", None) or "") if profile else ""
            spec = _make_coding_preset(
                cwd=cwd,
                effort=effort or "high",
                system_prompt=profile_extra or None,
            )
            branch = await create_agent(
                spec,
                chat_model=chat_model,
                log_config=DataLoggerConfig(auto_save_on_exit=False),
                load_settings=False,
            )
        else:
            branch = Branch(
                chat_model=chat_model,
                log_config=DataLoggerConfig(auto_save_on_exit=False),
            )
    else:
        cfg = branch.chat_model.endpoint.config.kwargs
        if model_str is not None:
            cfg["model"] = model
        if verbose:
            cfg["verbose_output"] = True
        if theme is not None:
            cfg["cli_display_theme"] = theme
        if effort is not None:
            kwarg = PROVIDER_EFFORT_KWARG.get(provider)
            if kwarg:
                cfg[kwarg] = effort
        if bypass:
            cfg.update(PROVIDER_BYPASS_KWARGS.get(provider, {}))
        elif yolo:
            cfg.update(PROVIDER_YOLO_KWARGS.get(provider, {}))
        if fast:
            cfg.update(PROVIDER_FAST_KWARGS.get(provider, {}))

    # Profile system prompt for the non-preset path only.
    # On the preset path the profile extension was already composed into the
    # spec before create_agent ran (add_message would call set_system and
    # replace the preset system message — see protocols/messages/manager.py:385).
    if profile and profile.system_prompt and preset is None:
        branch.msgs.add_message(system=profile.system_prompt)

    if timeout is not None:
        preamble = build_deadline_preamble(timeout)
        prompt = preamble + prompt

    run = allocate_run()
    branch_id = str(branch.id)

    resolved_model_spec = _provenance.resolve_model_spec(provider, model)
    artifact_contract = resolve_artifact_contract(
        playbook_artifacts=None,
        agent_defaults=profile.artifact_defaults if profile else None,
    )
    live = await setup_agent_persist(
        branch,
        agent_name=agent_name,
        artifacts_path=str(run.artifact_root),
        artifact_contract=artifact_contract,
        invocation_id=invocation_id,
        model=resolved_model_spec,
        provider=provider,
        effort=effort,
        project=project,
    )

    _terminal_status = "completed"
    _terminal_exc: BaseException | None = None

    _heartbeat_task = None
    if timeout is not None:
        import asyncio as _asyncio
        import time as _hb_time

        _hb_start = _hb_time.monotonic()

        async def _heartbeat_loop():
            while True:
                await _asyncio.sleep(60)
                elapsed = int(_hb_time.monotonic() - _hb_start)
                from lionagi.cli._logging import hint as _hint

                _hint(f"[progress] {elapsed}s elapsed — agent still running…")

        try:
            _heartbeat_task = _asyncio.ensure_future(_heartbeat_loop())
        except RuntimeError:
            _heartbeat_task = None

    try:
        res = await branch.operate(
            instruction=prompt,
            stream_persist=True,
            persist_dir=str(run.stream_dir),
            snapshot_dir=str(run.branches_dir),
            timeout=timeout,
            **({"repo": cwd} if cwd else {}),
        )
    except (TimeoutError, LionTimeoutError) as exc:
        _terminal_status = "timed_out"
        _terminal_exc = exc
        from lionagi.cli._logging import warn

        warn(f"agent timed out after {timeout}s")
        last = branch.msgs.last_response
        res = (last.response if last else "") or None
    except BaseException as exc:
        _terminal_status = classify_exception(exc)
        _terminal_exc = exc
        raise
    finally:
        if _heartbeat_task is not None:
            _heartbeat_task.cancel()
            import asyncio as _asyncio2
            import contextlib as _contextlib

            with _contextlib.suppress(_asyncio2.CancelledError, Exception):
                await _heartbeat_task

        # Shield teardown so iModel shutdown always runs (avoids leaked
        # rate-limit replenisher tasks that hang anyio.run forever).
        import anyio

        with anyio.CancelScope(shield=True):
            effective_status = await teardown_agent_persist(
                live,
                status=_terminal_status,
                exception=_terminal_exc,
            )
            if effective_status != _terminal_status:
                _terminal_status = effective_status
            await branch.mdls.shutdown()

    is_resume = bool(resume or continue_last)
    if is_resume and _terminal_status == "completed" and not (res or "").strip():
        log_error(
            f"resume produced empty stream — session may be expired; "
            f"re-run without -r (resume target: {resume or 'last'})"
        )
        _terminal_status = "failed"

    save_last_branch_pointer(run.run_id, branch_id)

    return res or "", provider, branch_id, _terminal_status


def add_agent_subparser(subparsers: argparse._SubParsersAction) -> None:
    agent = subparsers.add_parser(
        "agent",
        help="Spawn one-shot subagent (blocking); prints final response.",
        description=(
            "Spawn a single subagent and wait for its final response. "
            "Use -r / -c to continue a previous conversation. "
            "Use -a to load a profile from .lionagi/agents/. "
            "Use --preset to apply a built-in agent configuration. "
            "Use --form to load and validate structured inputs before invoking."
        ),
    )
    agent.add_argument(
        "model",
        nargs="?",
        default=None,
        help=(
            "One of 'claude', 'codex', 'gemini-code' (defaults), or a full spec "
            "like 'claude/opus'. Optional when -a (agent profile) provides a model, "
            "or when --resume / --continue-last is set."
        ),
    )
    agent.add_argument("prompt", help="Prompt to send to the subagent.")
    agent.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load agent profile by name. Resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md. "
            "Profile provides system prompt, default model, effort, yolo. "
            "CLI flags override profile settings."
        ),
    )
    agent.add_argument(
        "-r",
        "--resume",
        metavar="BRANCH_ID",
        default=None,
        help="Resume a previous branch by ID.",
    )
    agent.add_argument(
        "-c",
        "--continue-last",
        action="store_true",
        help="Continue the most recently used branch.",
    )
    agent.add_argument(
        "--preset",
        choices=_PRESET_CHOICES,
        default=None,
        metavar="NAME",
        help=(
            "Apply a built-in agent configuration preset. "
            f"Supported values: {', '.join(_PRESET_CHOICES)}. "
            "'coding' wires CodingToolkit with path-guard hooks "
            "and a coding system prompt; cwd defaults to the invocation directory."
        ),
    )
    agent.add_argument(
        "--form",
        metavar="SPEC",
        default=None,
        help=(
            "Path to a YAML or JSON work-form spec file. "
            "The spec declares typed fields and values; validation runs "
            "BEFORE any LLM call. Exits rc=1 on validation error. "
            "Validated values are injected into the prompt as structured context."
        ),
    )

    add_common_cli_args(agent)


def run_agent(args: argparse.Namespace) -> int:
    """Dispatch agent command."""
    # --form: load, build, and validate BEFORE any LLM call.
    form_prompt_prefix: str = ""
    if getattr(args, "form", None):
        try:
            spec = _load_form_spec(args.form)
        except FileNotFoundError as exc:
            log_error(str(exc))
            return 1
        except ValueError as exc:
            log_error(str(exc))
            return 1

        try:
            work_form = _build_work_form(spec, args.form)
        except ValueError as exc:
            log_error(str(exc))
            return 1

        if work_form.status == "error":
            errs = "; ".join(work_form.validation_errors)
            log_error(f"form validation failed ({args.form}): {errs}")
            return 1

        # Validated — build a context block to prepend to the prompt.
        if work_form.values:
            form_prompt_prefix = _form_to_context_block(work_form) + "\n\n"

    prompt = form_prompt_prefix + args.prompt

    has_model = args.model is not None or args.agent is not None
    if not has_model and not (args.resume or args.continue_last):
        log_error(
            "model or --agent is required unless --resume / -r or --continue-last / -c is set"
        )
        return 1

    try:
        result, provider, branch_id, terminal_status = run_async(
            _run_agent(
                args.model,
                prompt,
                yolo=args.yolo,
                verbose=args.verbose,
                theme=args.theme,
                resume=args.resume,
                continue_last=args.continue_last,
                effort=args.effort,
                agent_name=args.agent,
                cwd=args.cwd,
                timeout=args.timeout,
                fast=args.fast,
                invocation_id=getattr(args, "invocation", None),
                project=getattr(args, "project", None),
                bypass=getattr(args, "bypass", False),
                preset=getattr(args, "preset", None),
            )
        )
    except KeyboardInterrupt:
        return EXIT_CODE_BY_STATUS["aborted"]
    except BaseException as exc:
        if isinstance(exc, cancelled_exc_classes()):
            return EXIT_CODE_BY_STATUS["cancelled"]
        raise

    if not args.verbose:
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    return EXIT_CODE_BY_STATUS.get(terminal_status, 1)
