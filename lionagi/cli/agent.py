# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — single-agent one-shot or resumed conversation."""

from __future__ import annotations

import argparse
import json

from lionagi import Branch
from lionagi.ln.concurrency import run_async
from lionagi.protocols.generic.log import DataLoggerConfig

from ._agents import load_agent_profile
from ._logging import hint, log_error
from ._providers import (
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    add_common_cli_args,
    build_chat_model,
    parse_model_spec,
)
from ._runs import (
    RunDir,
    allocate_run,
    find_branch,
    load_last_branch,
    save_last_branch_pointer,
)


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
) -> tuple[str, str, str]:
    """Execute one agent turn (new or resumed).

    Returns (result, provider, branch_id).
    """
    if resume and continue_last:
        raise ValueError(
            "--resume / -r and --continue-last / -c are mutually exclusive."
        )

    # Load agent profile if specified — provides defaults for model/effort/yolo/fast_mode
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
        chat_model = build_chat_model(
            provider, model, yolo, verbose, theme, effort, fast
        )
        branch = Branch(
            chat_model=chat_model,
            log_config=DataLoggerConfig(auto_save_on_exit=False),
        )
    else:
        # Update existing endpoint config — no new iModel needed
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
        if yolo:
            cfg.update(PROVIDER_YOLO_KWARGS.get(provider, {}))
        if fast:
            cfg.update(PROVIDER_FAST_KWARGS.get(provider, {}))

    # Inject agent system prompt
    if profile and profile.system_prompt:
        branch.msgs.add_message(system=profile.system_prompt)

    run = allocate_run()
    branch_id = str(branch.id)

    res = await branch.operate(
        instruction=prompt,
        stream_persist=True,
        persist_dir=str(run.stream_dir),
        timeout=timeout,
        **({"repo": cwd} if cwd else {}),
    )

    # Final branch snapshot + run manifest (filesystem — legacy)
    run.branch_path(branch_id).write_text(json.dumps(branch.to_dict()))
    run.write_manifest(
        {
            "kind": "agent",
            "model": model,
            "provider": provider,
            "prompt": prompt,
            "branches": [{"id": branch_id, "provider": provider, "model": model}],
        }
    )
    save_last_branch_pointer(run.run_id, branch_id)

    # Persist to SQLite state layer (if aiosqlite available)
    await _persist_to_state_db(branch, prompt)

    return res or "", provider, branch_id


async def _persist_to_state_db(branch: Branch, task: str) -> None:
    """Best-effort persist to ~/.lionagi/state.db."""
    try:
        from lionagi.session.session import Session

        from lionagi.state.db import StateDB
        from lionagi.state.persist import persist_session

        session = Session(name="agent")
        session.include_branches(branch)

        async with StateDB() as db:
            await persist_session(db, session)
    except ImportError:
        pass
    except Exception:
        pass


def add_agent_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li agent` sub-command."""
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

    result, provider, branch_id = run_async(
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
        )
    )
    if not args.verbose:
        # The final response is user-facing result output — stdout, not a log.
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    return 0
