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
from lionagi.state.artifact_verifier import (
    missing_artifact_evidence,
    missing_artifact_summary,
    resolve_artifact_contract,
    verify_artifact_contract,
)

from ._agents import build_deadline_preamble, load_agent_profile
from ._logging import hint, log_error
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
    live = await _setup_live_persist(
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
    except KeyboardInterrupt as exc:
        _terminal_status = "aborted"
        _terminal_exc = exc
        raise
    except (TimeoutError, LionTimeoutError) as exc:
        _terminal_status = "timed_out"
        _terminal_exc = exc
        from lionagi.cli._logging import warn

        warn(f"agent timed out after {timeout}s")
        res = _extract_partial_output(branch) or None
    except BaseException as exc:
        from lionagi.ln.concurrency import get_cancelled_exc_class

        if isinstance(exc, get_cancelled_exc_class()):
            _terminal_status = "cancelled"
        else:
            _terminal_status = "failed"
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
            effective_status = await _teardown_live_persist(
                live,
                status=_terminal_status,
                exception=_terminal_exc,
            )
            if effective_status != _terminal_status:
                _terminal_status = effective_status
            await branch.mdls.shutdown()

    save_last_branch_pointer(run.run_id, branch_id)

    return res or "", provider, branch_id, _terminal_status


_EXIT_CODE_BY_TERMINAL_STATUS: dict[str, int] = {
    "completed": 0,
    "failed": 1,
    "timed_out": 124,
    "aborted": 130,
    "cancelled": 143,
}


async def _setup_live_persist(
    branch: Branch,
    *,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
) -> dict | None:
    """Open DB, create session/branch rows, register live message hook."""
    import logging

    from lionagi.session.session import Session
    from lionagi.state.db import StateDB

    db: StateDB | None = None
    try:
        db = StateDB()
        await db.open()

        session = Session(name="agent", default_branch=branch)
        session_id = str(session.id)
        branch_id = str(branch.id)

        existing_branch = await db.get_branch(branch_id)
        if existing_branch:
            import uuid as _uuid

            session_id = existing_branch["session_id"]
            existing_session = await db.get_session(session_id)
            session_prog_id = existing_session["progression_id"]
            branch_prog_id = existing_branch["progression_id"]

            # Repair NULL progression_id from legacy rows to avoid silent
            # no-ops in append_to_progression.
            if session_prog_id is None:
                candidate = str(_uuid.uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_session_progression(
                    session_id,
                    candidate,
                )
                session_prog_id = effective or candidate
            if branch_prog_id is None:
                candidate = str(_uuid.uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_branch_progression(
                    branch_id,
                    candidate,
                )
                branch_prog_id = effective or candidate

            existing_msg_ids = set(await db.get_progression(branch_prog_id))
        else:
            import time
            import uuid

            session_prog_id = str(uuid.uuid4())
            branch_prog_id = str(uuid.uuid4())
            existing_msg_ids = set()

            await db.create_progression(session_prog_id)
            await db.create_progression(branch_prog_id)

            session_dict = session.to_dict(mode="db")
            if project:
                _proj, _proj_src = project, "explicit"
            else:
                from lionagi.cli._project import detect_project

                _proj, _proj_src = detect_project()
            from lionagi.cli.kill import current_pid_markers

            _node_meta = {**(session_dict.get("node_metadata") or {}), **current_pid_markers()}
            await db.create_session(
                {
                    "id": session_id,
                    "created_at": session_dict["created_at"],
                    "node_metadata": _node_meta,
                    "name": session_dict.get("name"),
                    "user": session_dict.get("user"),
                    "progression_id": session_prog_id,
                    "first_msg_id": None,
                    "last_msg_id": None,
                    "invocation_kind": "agent",
                    "agent_name": agent_name,
                    "artifacts_path": artifacts_path,
                    "artifact_contract_json": artifact_contract,
                    "status": "running",
                    "started_at": time.time(),
                    "invocation_id": invocation_id,
                    "model": model,
                    "provider": provider,
                    "effort": effort,
                    "agent_hash": _provenance.agent_definition_hash(agent_name),
                    "project": _proj,
                    "project_source": _proj_src,
                }
            )

            system_msg_id = None
            if branch.system:
                sys_dict = branch.system.to_dict(mode="db")
                system_msg_id = sys_dict["id"]
                await db.insert_message(sys_dict)

            branch_dict = branch.to_dict(mode="db")
            node_meta = branch_dict.get("node_metadata") or {}
            if isinstance(node_meta, str):
                node_meta = json.loads(node_meta)
            if "chat_model" in branch_dict:
                node_meta["chat_model"] = branch_dict["chat_model"]

            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": branch_dict["created_at"],
                    "node_metadata": node_meta,
                    "user": branch_dict.get("user"),
                    "name": branch_dict.get("name"),
                    "session_id": session_id,
                    "progression_id": branch_prog_id,
                    "system_msg_id": system_msg_id,
                    "model": model,
                    "provider": provider,
                    "agent_name": agent_name,
                }
            )

        ctx = {
            "db": db,
            "branch": branch,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_id": branch_prog_id,
            "existing_msg_ids": existing_msg_ids,
            "new_msg_ids": [],
            "artifacts_path": artifacts_path,
            "artifact_contract": artifact_contract,
        }

        async def _on_message(msg):
            try:
                msg_dict = msg.to_dict(mode="db")
                msg_id = msg_dict["id"]
                await db.insert_message(msg_dict)
                if msg_id not in ctx["existing_msg_ids"]:
                    await db.append_to_progression(branch_prog_id, msg_id)
                    await db.append_to_progression(session_prog_id, msg_id)
                    ctx["new_msg_ids"].append(msg_id)
                await db.touch_session_activity(session_id, at=msg_dict.get("created_at"))
                if msg_dict.get("role") == "system":
                    await db.update_branch(branch_id, system_msg_id=msg_id)
            except Exception as exc:
                import logging

                logging.getLogger("lionagi.cli").warning(
                    "live persist write failed for branch %s: %s",
                    branch_id,
                    exc,
                    exc_info=True,
                )

        from lionagi.hooks import route_message_persistence

        ctx["hook"] = route_message_persistence(session, branch, _on_message)
        return ctx
    except Exception as exc:
        logging.getLogger("lionagi.cli").warning(
            "live persist setup failed (%s) — disabling persistence for this run",
            exc,
            exc_info=True,
        )
        if db is not None:
            try:
                await db.close()
            except Exception as close_exc:  # noqa: BLE001
                logging.getLogger("lionagi.cli").warning(
                    "fallback db.close after setup failure also failed: %s",
                    close_exc,
                )
        return None


def _resolve_run_reason(
    *,
    status: str,
    exception: BaseException | None,
) -> tuple[str, str, list[dict] | None]:
    """Map terminal status + exception to (reason_code, summary, evidence)."""
    from lionagi.state.reasons import RunReasons

    if status == "completed":
        return RunReasons.COMPLETED_OK, "Run completed successfully.", None
    if status == "timed_out":
        return (
            RunReasons.TIMED_OUT_DEADLINE,
            "Run exceeded the configured timeout.",
            None,
        )
    if status == "aborted":
        return RunReasons.CANCELLED_SIGINT, "User pressed Ctrl-C (SIGINT).", None
    if status == "cancelled":
        return (
            RunReasons.CANCELLED_SYSTEM,
            "Task cancelled by the runtime (anyio CancelledError).",
            None,
        )
    # status == "failed"
    if exception is not None:
        return (
            RunReasons.FAILED_EXCEPTION,
            f"{type(exception).__name__}: {exception}",
            None,
        )
    return RunReasons.FAILED_EXCEPTION, "Run failed.", None


async def _teardown_live_persist(
    ctx: dict | None,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
) -> str:
    """Update session bookmarks, lifecycle columns, close DB; returns effective status."""
    if ctx is None:
        return status
    import logging

    log = logging.getLogger("lionagi.cli")
    db = ctx["db"]
    try:
        import time as _time

        session_id = ctx["session_id"]
        session_prog_id = ctx["session_prog_id"]

        all_msgs = await db.get_progression(session_prog_id)
        bookmark_kwargs: dict = {"ended_at": _time.time()}
        if all_msgs:
            bookmark_kwargs["first_msg_id"] = all_msgs[0]
            bookmark_kwargs["last_msg_id"] = all_msgs[-1]
        await db.update_session(session_id, **bookmark_kwargs)

        reason_code, reason_summary, evidence_refs = _resolve_run_reason(
            status=status,
            exception=exception,
        )
        metadata: dict | None = None
        if exception is not None:
            metadata = {"exception_class": type(exception).__name__}

        session_row = await db.get_session(session_id) or {}
        contract = session_row.get("artifact_contract_json")
        artifacts_root = session_row.get("artifacts_path")
        verification = verify_artifact_contract(
            contract,
            artifacts_root=artifacts_root,
        )
        await db.update_artifact_verification(session_id, verification)

        final_status = status
        final_reason_code = reason_code
        final_reason_summary = reason_summary
        final_evidence_refs = evidence_refs
        if verification and verification["status"] == "failed":
            missing = verification["missing_required"]
            if status == "completed":
                from lionagi.state.reasons import RunReasons

                final_status = "failed"
                final_reason_code = RunReasons.FAILED_MISSING_ARTIFACT
                final_reason_summary = missing_artifact_summary(missing)
                final_evidence_refs = missing_artifact_evidence(missing)
            else:
                metadata = dict(metadata or {})
                metadata["artifact_verification_status"] = verification["status"]
                metadata["missing_required_artifact_ids"] = [
                    str(entry.get("id", "")) for entry in missing
                ]

        await db.update_status(
            "session",
            session_id,
            new_status=final_status,
            reason_code=final_reason_code,
            reason_summary=final_reason_summary,
            evidence_refs=final_evidence_refs,
            source="executor",
            actor=session_id,
            metadata=metadata,
        )

        from lionagi.hooks import unroute_message_persistence

        unroute_message_persistence(ctx["branch"], ctx["hook"])
    except Exception as exc:
        log.warning("live persist teardown failed: %s", exc, exc_info=True)
        # Surface the original status on best-effort failure so the
        # caller's exit code reflects what we tried to commit.
        return status
    finally:
        try:
            await db.close()
        except Exception as exc:
            log.warning("live persist db.close failed: %s", exc, exc_info=True)
    return final_status


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
        return _EXIT_CODE_BY_TERMINAL_STATUS["aborted"]
    except BaseException as exc:
        if isinstance(exc, cancelled_exc_classes()):
            return _EXIT_CODE_BY_TERMINAL_STATUS["cancelled"]
        raise

    if not args.verbose:
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    return _EXIT_CODE_BY_TERMINAL_STATUS.get(terminal_status, 1)
