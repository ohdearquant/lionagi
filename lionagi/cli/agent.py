# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — single-agent one-shot or resumed conversation."""

from __future__ import annotations

import argparse
import json

from lionagi import Branch
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import run_async
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.state import provenance as _provenance
from lionagi.state.artifact_verifier import (
    missing_artifact_evidence,
    missing_artifact_summary,
    resolve_artifact_contract,
    verify_artifact_contract,
)

from ._agents import load_agent_profile
from ._logging import hint, log_error
from ._providers import (
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    add_common_cli_args,
    build_chat_model,
    parse_model_spec,
    resolve_persisted_effort,
)
from ._runs import allocate_run, find_branch, load_last_branch, save_last_branch_pointer


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
) -> tuple[str, str, str, str]:
    """Execute one agent turn (new or resumed).

    Returns (result, provider, branch_id, terminal_status). ``terminal_status``
    is one of ``ADR-0025`` ``SESSION_TERMINAL_STATUSES`` and drives the CLI
    exit-code mapping in :func:`run_agent`.
    """
    if resume and continue_last:
        raise ValueError("--resume / -r and --continue-last / -c are mutually exclusive.")

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
        chat_model = build_chat_model(provider, model, yolo, verbose, theme, effort, fast)
        # Resolve the effort value to persist: captures post-clamp values
        # (e.g. "max"→"xhigh" for codex) and forces None for providers that
        # don't accept an effort kwarg at all (e.g. gemini).  The helper is
        # independent of whether build_chat_model() returned iModel or str.
        effort = resolve_persisted_effort(provider, chat_model, effort)
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

    # Set up live SQLite persist (messages stream into DB as they're added)
    # ADR-0022: pass the *resolved* provider + model spec for provenance.
    # ``model_spec`` here is the canonical ``provider/model`` string built
    # right above this block — no aliases, no defaults yet to apply.
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

    # ADR-0025: distinguish timed_out / aborted / cancelled / failed so
    # operators can tell "retry with more time" from "investigate" from
    # "user pressed Ctrl-C" from "orchestrator cascaded a cancel."
    # ADR-0028: also capture the exception so teardown can resolve a
    # structured reason code and summary for the status transition.
    _terminal_status = "completed"
    _terminal_exc: BaseException | None = None
    try:
        res = await branch.operate(
            instruction=prompt,
            stream_persist=True,
            # Streaming chunks land in stream_dir/<id>.buffer.jsonl;
            # the canonical branch snapshot lands in branches_dir/<id>.json
            # so ``find_branch()`` (which only searches branches_dir)
            # can resolve ``li agent -r <branch_id>``.
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
        res = None
    except BaseException as exc:
        from lionagi.ln.concurrency import get_cancelled_exc_class

        if isinstance(exc, get_cancelled_exc_class()):
            _terminal_status = "cancelled"
        else:
            _terminal_status = "failed"
        _terminal_exc = exc
        raise
    finally:
        # Shield teardown from outer cancellation (KeyboardInterrupt, anyio
        # task-group cancel). Without the shield the first await below
        # raises CancelledError, skipping iModel shutdown — leaking the
        # rate-limit replenisher task and hanging anyio.run forever.
        import anyio

        with anyio.CancelScope(shield=True):
            # ADR-0029 §7: teardown may override the proposed terminal
            # status (clean exit + missing required artifact → failed).
            # Use the returned status — NOT the original — for the
            # process exit code so shell scripts, schedules, and chains
            # see the failure.
            effective_status = await _teardown_live_persist(
                live,
                status=_terminal_status,
                exception=_terminal_exc,
            )
            if effective_status != _terminal_status:
                _terminal_status = effective_status
            # Shut down every iModel on the branch (chat_model AND
            # parse_model, plus any other registered) so each
            # RateLimitedAPIExecutor's background replenisher task is
            # cancelled. Without this, anyio.run never returns.
            await branch.mdls.shutdown()

    # Save branch pointer for --continue-last / -r resume
    save_last_branch_pointer(run.run_id, branch_id)

    return res or "", provider, branch_id, _terminal_status


# ADR-0025 exit-code mapping. UNIX conventions: 124 matches GNU coreutils
# ``timeout``; 130 / 143 = 128 + signal number (SIGINT / SIGTERM).
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
    """Open DB, create session/branch rows, register live message hook.

    Returns context dict for _teardown_live_persist, or None if unavailable.

    On any failure, the DB connection is closed before returning None so
    the aiosqlite background thread does not leak. The aiosqlite worker
    is a non-daemon thread; leaking it prevents Python interpreter
    shutdown and manifests as a hanging CLI process.
    """
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

        # Check for existing branch (resume case)
        existing_branch = await db.get_branch(branch_id)
        if existing_branch:
            import uuid as _uuid

            session_id = existing_branch["session_id"]
            existing_session = await db.get_session(session_id)
            session_prog_id = existing_session["progression_id"]
            branch_prog_id = existing_branch["progression_id"]

            # Legacy/pre-PR rows can have NULL progression_id. Without
            # repair, append_to_progression(None, ...) is a silent no-op
            # and the resumed run loses branch (and session) history.
            # The repair helpers return the EFFECTIVE progression id —
            # adopt that, not our local candidate, so a concurrent
            # repair winner cannot leave us writing into an orphan
            # progression while the row points elsewhere.
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
            await db.create_session(
                {
                    "id": session_id,
                    "created_at": session_dict["created_at"],
                    "node_metadata": session_dict.get("node_metadata"),
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
                    # ADR-0020: optional skill-level orchestration parent.
                    "invocation_id": invocation_id,
                    # ADR-0022: resolved provenance — captured at the
                    # session boundary so historical runs disclose the
                    # exact model + provider + effort + agent fingerprint
                    # they used, even if the agent profile changes later.
                    "model": model,
                    "provider": provider,
                    "effort": effort,
                    "agent_hash": _provenance.agent_definition_hash(agent_name),
                    # ADR-0026: project detection.
                    "project": _proj,
                    "project_source": _proj_src,
                }
            )

            # Persist system message if present
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
                    # ADR-0022: branch-level provenance — for `li agent`
                    # the branch shares the session's model/provider,
                    # but write them on the branch too so flow ops
                    # (which may differ per-branch) can be queried
                    # uniformly via the branches table.
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
            # Mirrors the orchestration hook contract: a transient DB
            # write blip (lock contention, busy timeout) must NEVER
            # abort the user-facing turn. Log and continue — the
            # in-memory message is still valid, persistence merely
            # missed a write.
            try:
                msg_dict = msg.to_dict(mode="db")
                msg_id = msg_dict["id"]
                await db.insert_message(msg_dict)
                if msg_id not in ctx["existing_msg_ids"]:
                    await db.append_to_progression(branch_prog_id, msg_id)
                    await db.append_to_progression(session_prog_id, msg_id)
                    ctx["new_msg_ids"].append(msg_id)
                # ADR-0019: activity heartbeat for staleness detection.
                # Done after the progression append so callers see the
                # bump only once the message is committed.
                await db.touch_session_activity(session_id, at=msg_dict.get("created_at"))
                # ADR-0009: branches.system_msg_id must track the CURRENT
                # system message. If the runtime replaces the system mid-run
                # (set_system), update the pointer so Studio's O(1) lookup
                # doesn't return the stale system.
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

        ctx["hook"] = _on_message
        branch.on_message_added.append(_on_message)
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
    """Map terminal session status + exception to ADR-0028 (code, summary, evidence).

    ADR-0029 (artifact contract) extends this when the verifier flips
    a clean-exit run into a contract-failure run; that override happens
    at the call site, not here.
    """
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
        return RunReasons.ABORTED_USER, "User pressed Ctrl-C.", None
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
    """Update session bookmarks, lifecycle columns, and close DB.

    Returns the *effective* terminal status — ADR-0029 §7's verification
    override can flip a clean ``completed`` into ``failed`` when required
    artifacts are missing. Callers MUST use the returned status (not the
    `status` argument they passed in) for any downstream side effect
    like the process exit code.

    The DB close is in its own ``finally`` so it always runs — even if
    the bookmark update or hook removal fails. Failures elsewhere are
    logged (not raised) because teardown runs in a ``finally`` on the
    way out of the agent and must never block clean process exit.

    Leaving the DB unclosed leaks the aiosqlite worker thread, which is
    non-daemon and would prevent the Python interpreter from shutting
    down — the symptom reported as "CLI process hangs after completion".

    ADR-0028: the status transition goes through ``StateDB.update_status()``
    so the denormalized reason columns and ``status_transitions`` history
    are written in the same SQLite transaction. ``exception`` carries
    the terminating exception (if any) so the reason can include the
    exception class and message.
    """
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
        # Bookmark and ended_at go through update_session(); status
        # transition goes through update_status() so the reason model
        # (ADR-0028) is enforced.
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

        # Remove ALL matching registrations of our hook. ``list.remove``
        # only removes the first match; if a caller appended the same
        # callable twice (test / dev), a closed-DB hook would survive
        # teardown and fire on later messages.
        hook = ctx["hook"]
        ctx["branch"].on_message_added[:] = [
            h for h in ctx["branch"].on_message_added if h is not hook
        ]
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
    """Dispatch agent command.

    Exit codes follow ADR-0025: 0 completed, 1 failed, 124 timed_out,
    130 aborted (Ctrl-C), 143 cancelled (system / orchestrator).
    """
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
            )
        )
    except KeyboardInterrupt:
        # Teardown inside ``_run_agent`` already recorded
        # ``status='aborted'`` under a shielded scope before re-raising.
        return _EXIT_CODE_BY_TERMINAL_STATUS["aborted"]
    except BaseException as exc:
        # get_cancelled_exc_class() requires an active event loop; this
        # except-clause runs outside anyio.run(), so we fall back to a
        # name-based check if the loop is gone (fixes #1082).
        try:
            from lionagi.ln.concurrency import get_cancelled_exc_class

            cancelled_cls = get_cancelled_exc_class()
        except Exception:
            cancelled_cls = None

        if cancelled_cls is not None and isinstance(exc, cancelled_cls):
            return _EXIT_CODE_BY_TERMINAL_STATUS["cancelled"]
        if cancelled_cls is None and type(exc).__name__ in (
            "Cancelled",
            "CancelledError",
        ):
            return _EXIT_CODE_BY_TERMINAL_STATUS["cancelled"]
        raise

    if not args.verbose:
        # The final response is user-facing result output — stdout, not a log.
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    return _EXIT_CODE_BY_TERMINAL_STATUS.get(terminal_status, 1)
