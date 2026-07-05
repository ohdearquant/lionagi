# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — one-shot or resumed single-agent conversation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lionagi import Branch
from lionagi._errors import ConfigurationError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import (
    SigtermInterrupt,
    cache_cancelled_exc_class,
    cancelled_exc_classes,
    run_async,
)
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.state import provenance as _provenance
from lionagi.state.artifact_verifier import resolve_artifact_contract

from ._context_from import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextFromError,
    resolve_and_build_context_block,
)
from ._logging import hint, log_error
from ._providers import (
    BACKENDS,
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    PROVIDERS_EFFORT_VIA_MODEL_NAME,
    add_common_cli_args,
    build_chat_model,
    build_deadline_preamble,
    load_agent_profile,
    normalize_effort,
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
    resume_on_timeout: bool = False,
    context_from: list[str] | None = None,
    context_budget: int | None = None,
    _auto_resumed: bool = False,
) -> tuple[str, str, str, str, str | None]:
    """Execute one agent turn; returns (result, provider, branch_id, terminal_status, session_id).

    session_id is None whenever live persistence never started (e.g. the
    mangled resume-model-override guard fires before setup_agent_persist is
    called, or setup itself failed and disabled persistence for this run).
    """
    effort = normalize_effort(effort)
    if resume and continue_last:
        raise ConfigurationError("--resume / -r and --continue-last / -c are mutually exclusive.")
    if preset and (resume or continue_last):
        raise ConfigurationError(
            "--preset only applies to new branches; cannot combine with --resume / --continue-last."
        )
    if context_from and (resume or continue_last):
        raise ContextFromError(
            "--context-from cannot be combined with --resume / -r or --continue-last / -c "
            "(resume already carries the source context)."
        )
    if context_from:
        effective_context_budget = (
            context_budget if context_budget is not None else DEFAULT_CONTEXT_BUDGET_TOKENS
        )
        context_block = await resolve_and_build_context_block(
            context_from, effective_context_budget
        )
        if context_block:
            prompt = f"{context_block}\n\n{prompt}"

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
            effort = normalize_effort(profile.effort)
        if profile.yolo and not yolo:
            yolo = True
        if profile.fast_mode and not fast:
            fast = True
        if profile.timeout and timeout is None:
            timeout = profile.timeout
        if profile.resume_on_timeout and not resume_on_timeout:
            resume_on_timeout = True

    branch: Branch | None = None
    if continue_last:
        _, branch_id = load_last_branch()
        _, branch_path = find_branch(branch_id)
        branch = Branch.from_dict(json.loads(branch_path.read_text()))
    elif resume:
        _, branch_path = find_branch(resume)
        resolved_branch_id = branch_path.stem
        if resolved_branch_id != resume:
            hint(f"[resume] prefix-matched {resume} → {resolved_branch_id}")
        branch = Branch.from_dict(json.loads(branch_path.read_text()))

    if model_str is not None:
        ms = parse_model_spec(model_str)
        if branch is not None and "/" not in ms.model and ms.model not in BACKENDS:
            # A resume-path override is grafted into the existing branch's
            # config with no further validation (unlike a new branch, whose
            # provider must resolve via build_chat_model/match_endpoint or
            # fail naturally). A bare token that isn't a known backend name
            # is almost always a mangled command — e.g. a --resume id split
            # across two argv tokens leaves [fragment, prompt] as positionals
            # and the fragment is read as MODEL.
            log_error(
                f"resume model override {model_str!r} does not look like a "
                "model spec (expected 'provider/model', or a known name "
                "like 'claude', 'codex', 'gemini-code'). Positionals are "
                "[MODEL] PROMPT — this looks like a mangled command, e.g. "
                "a --resume id accidentally split across two arguments."
            )
            return "", "", str(branch.id), "failed", None
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
            old_model = cfg.get("model")
            if model != old_model:
                from lionagi.cli._logging import warn

                warn(f"resume model override: {old_model} → {model}")
            cfg["model"] = model
        if verbose:
            cfg["verbose_output"] = True
        if theme is not None:
            cfg["cli_display_theme"] = theme
        if effort is not None:
            kwarg = PROVIDER_EFFORT_KWARG.get(provider)
            if kwarg:
                cfg[kwarg] = effort
            elif provider in PROVIDERS_EFFORT_VIA_MODEL_NAME:
                # agy (Antigravity CLI) has no effort kwarg — fold effort into
                # the resolved --model name instead (see resolve_agy_model).
                # cfg["model"] here is the *persisted* prior resolution
                # (already a concrete agy display name) unless the caller
                # also passed a new model_str this turn — reapply_effort
                # lets a fresh --effort replace that persisted suffix,
                # while an explicit model_str given on this call still wins.
                from lionagi.providers.google.gemini_code import resolve_agy_model

                cfg["model"] = resolve_agy_model(
                    cfg.get("model"),
                    effort=effort,
                    reapply_effort=model_str is None,
                )
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
    if context_from:
        run.write_manifest({"branch_id": branch_id, "context_from": list(context_from)})

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
        if _terminal_status == "failed":
            # Default traceback printing is unreliable under SIGTERM/process
            # death — leave a one-line diagnostic before it propagates.
            log_error(f"{type(exc).__name__}: {exc}")
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
            # The CLI provider's real engine session id (e.g. a Claude Code
            # session uuid), captured from a "system" stream chunk — the
            # link teardown uses to tell a genuine failure from a wrapper
            # exception racing an engine session that is still alive.
            _engine_session_uid = getattr(branch.chat_model.endpoint, "session_id", None)
            effective_status = await teardown_agent_persist(
                live,
                status=_terminal_status,
                exception=_terminal_exc,
                cwd=cwd,
                engine_session_uid=_engine_session_uid,
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

    session_id = live.get("session_id") if live else None

    if _terminal_status == "timed_out" and resume_on_timeout and not _auto_resumed:
        from lionagi.cli._logging import warn

        warn(
            f"[auto-resume] session {session_id or branch_id} timed out after "
            f"{timeout}s — resuming once with 'continue and conclude the task'"
        )
        # Carry the model that actually ran this leg (explicit --model, profile
        # default, or whatever a prior resume already grafted) forward as an
        # explicit override. Passing model_str=None here would let the
        # agent_name profile's model re-apply on the resumed leg (see the
        # profile-precedence block above), silently switching models mid-run.
        _effective_cfg = branch.chat_model.endpoint.config
        _effective_model_str = f"{_effective_cfg.provider}/{_effective_cfg.kwargs.get('model')}"
        return await _run_agent(
            _effective_model_str,
            "continue and conclude the task",
            yolo=yolo,
            verbose=verbose,
            theme=theme,
            resume=branch_id,
            effort=effort,
            agent_name=agent_name,
            cwd=cwd,
            timeout=timeout,
            fast=fast,
            invocation_id=invocation_id,
            project=project,
            bypass=bypass,
            resume_on_timeout=resume_on_timeout,
            _auto_resumed=True,
        )

    return res or "", provider, branch_id, _terminal_status, session_id


def add_agent_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    agent = subparsers.add_parser(
        "agent",
        help="Spawn one-shot subagent (blocking); prints final response.",
        description=(
            "Spawn a single subagent and wait for its final response. "
            "Flags may appear anywhere relative to the positionals. "
            "Use -r / -c to continue a previous conversation. "
            "Use -a to load a profile from .lionagi/agents/. "
            "Use --preset to apply a built-in agent configuration. "
            "Use --form to load and validate structured inputs before invoking. "
            "Use --context-from to hand a new agent distilled context from a "
            "prior session/branch/run/file."
        ),
    )
    agent.add_argument(
        "query",
        nargs="*",
        metavar="[MODEL] PROMPT",
        help=(
            "Optional model spec followed by the prompt. Model is one of "
            "'claude', 'codex', 'gemini-code' (defaults), or a full spec like "
            "'claude/opus'; omit it when -a / --resume / -c provides one. "
            "The prompt may instead be passed via --prompt or --prompt-file."
        ),
    )
    agent.add_argument(
        "--prompt",
        dest="prompt_flag",
        metavar="TEXT",
        default=None,
        help="Prompt text (alternative to the positional PROMPT).",
    )
    agent.add_argument(
        "--prompt-file",
        metavar="PATH",
        default=None,
        help="Read the prompt from a file; '-' reads stdin (heredoc-friendly).",
    )
    agent.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load agent profile by name. Resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md. "
            "Profile provides system prompt, default model, effort, yolo, "
            "timeout, resume_on_timeout. CLI flags override profile settings."
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

    agent.add_argument(
        "--context-from",
        dest="context_from",
        metavar="REF",
        action="append",
        default=None,
        help=(
            "Inject distilled context from a prior session id, branch id, run id, "
            "or file path into this new branch's first instruction, above the "
            "prompt. Repeatable (concatenated in argv order); the total budget "
            "(see --context-budget) is shared across all refs. Rejected in "
            "combination with -r / --resume or -c / --continue-last."
        ),
    )
    agent.add_argument(
        "--context-budget",
        dest="context_budget",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"Total token budget for --context-from content "
            f"(default {DEFAULT_CONTEXT_BUDGET_TOKENS}; ~4 chars/token)."
        ),
    )

    add_common_cli_args(agent)
    return agent


def _resolve_model_and_prompt(args: argparse.Namespace) -> tuple[str | None, str] | None:
    """Assign the positional bucket + --prompt/--prompt-file to (model, prompt).

    Returns None after logging a clear error."""
    query: list[str] = getattr(args, "query", None) or []
    flag_prompt = args.prompt_flag
    if args.prompt_file:
        if flag_prompt is not None:
            log_error("pass --prompt or --prompt-file, not both")
            return None
        if args.prompt_file == "-":
            flag_prompt = sys.stdin.read()
        else:
            try:
                flag_prompt = Path(args.prompt_file).read_text()
            except OSError as exc:
                log_error(f"could not read --prompt-file: {exc}")
                return None
        if not flag_prompt.strip():
            log_error(f"--prompt-file {args.prompt_file!r} is empty")
            return None
    if len(query) > 2:
        log_error(
            "too many positional arguments — expected [MODEL] PROMPT. "
            "Did you forget to quote the prompt?"
        )
        return None
    if flag_prompt is not None:
        if len(query) == 2:
            log_error("prompt given twice (positionally and via --prompt/--prompt-file)")
            return None
        return (query[0] if query else None), flag_prompt
    if len(query) == 2:
        return query[0], query[1]
    if len(query) == 1:
        return None, query[0]
    log_error("no prompt given — pass it positionally, or via --prompt / --prompt-file")
    return None


def run_agent(args: argparse.Namespace) -> int:
    """Dispatch agent command."""
    resolved = _resolve_model_and_prompt(args)
    if resolved is None:
        return 1
    model, prompt_text = resolved
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

    prompt = form_prompt_prefix + prompt_text

    has_model = model is not None or args.agent is not None
    if not has_model and not (args.resume or args.continue_last):
        log_error(
            "model or --agent is required unless --resume / -r or --continue-last / -c is set"
        )
        return 1

    try:
        result, provider, branch_id, terminal_status, session_id = run_async(
            _run_agent(
                model,
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
                resume_on_timeout=getattr(args, "resume_on_timeout", False),
                context_from=getattr(args, "context_from", None),
                context_budget=getattr(args, "context_budget", None),
            )
        )
    except ContextFromError as exc:
        log_error(str(exc))
        return 2
    except KeyboardInterrupt:
        return EXIT_CODE_BY_STATUS["aborted"]
    except SigtermInterrupt as exc:
        from lionagi.cli._logging import warn

        warn(f"agent terminated by SIGTERM: {exc}")
        return EXIT_CODE_BY_STATUS["cancelled"]
    except BaseException as exc:
        if isinstance(exc, cancelled_exc_classes()):
            return EXIT_CODE_BY_STATUS["cancelled"]
        log_error(f"{type(exc).__name__}: {exc}")
        raise

    if not args.verbose:
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    if session_id:
        hint(f"[status]    li agent status {session_id}")
    return EXIT_CODE_BY_STATUS.get(terminal_status, 1)
