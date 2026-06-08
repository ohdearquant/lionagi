# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — one-shot or resumed single-agent conversation."""

from __future__ import annotations

import argparse
import json

from lionagi import Branch
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import (
    cache_cancelled_exc_class,
    cancelled_exc_classes,
    run_async,
)
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.state import provenance as _provenance
from lionagi.state.artifact_verifier import resolve_artifact_contract

from ._agents import build_deadline_preamble, load_agent_profile
from ._lifecycle import EXIT_CODE_BY_STATUS, classify_exception
from ._logging import hint, log_error
from ._persist import setup_agent_persist, teardown_agent_persist
from ._providers import (
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    add_common_cli_args,
    build_chat_model,
    parse_model_spec,
    resolve_persisted_effort,
)
from ._runs import allocate_run, find_branch, load_last_branch, save_last_branch_pointer


def _extract_partial_output(branch) -> str:
    """Return the last assistant message text accumulated before a timeout."""
    try:
        progression = branch.msgs.progression
        messages = branch.msgs.messages
        for msg_id in reversed(list(progression)):
            msg = (
                messages.get(msg_id)
                if hasattr(messages, "get")
                else (messages[msg_id] if msg_id in messages else None)
            )
            if msg is None:
                continue
            role = getattr(msg, "role", None)
            if str(role).lower() != "assistant":
                continue
            content = getattr(msg, "content", None)
            if content is None:
                continue
            rendered = getattr(content, "rendered", None)
            if rendered:
                return str(rendered)
            return str(content) if str(content) else ""
    except Exception:  # noqa: S110
        pass
    return ""


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
) -> tuple[str, str, str, str]:
    """Execute one agent turn; returns (result, provider, branch_id, terminal_status)."""
    if resume and continue_last:
        raise ValueError("--resume / -r and --continue-last / -c are mutually exclusive.")

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

    if profile and profile.system_prompt:
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
        res = _extract_partial_output(branch) or None
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

    save_last_branch_pointer(run.run_id, branch_id)

    return res or "", provider, branch_id, _terminal_status


def add_agent_subparser(subparsers: argparse._SubParsersAction) -> None:
    agent = subparsers.add_parser(
        "agent",
        help="Spawn one-shot subagent (blocking); prints final response.",
        description=(
            "Spawn a single subagent and wait for its final response. "
            "Use -r / -c to continue a previous conversation. "
            "Use -a to load a profile from .lionagi/agents/."
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

    add_common_cli_args(agent)


def run_agent(args: argparse.Namespace) -> int:
    """Dispatch agent command."""
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
                args.prompt,
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
