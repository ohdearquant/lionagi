# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Coordinator for durable Operator turns, cancellation, and permissions."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from typing import Any

from .catalog import OperatorSelectionError, resolve_selection
from .engine import (
    BranchOperatorEngine,
    OperatorProviderUnavailableError,
    build_operator_branch,
    compile_operator_history,
    resolve_operator_provider_model,
    write_resumable_operator_snapshot,
)
from .store import (
    OperatorAuditUnavailableError,
    OperatorConflictError,
    OperatorStore,
    OperatorValidationError,
)
from .types import (
    CommandExecutor,
    OperatorEngineEvent,
    OperatorEngineFactory,
    OperatorEngineTurn,
    PermissionDecision,
)

_log = logging.getLogger(__name__)


class ApplicationTargetConflictError(RuntimeError):
    """The exact target approved by a human is no longer current."""


async def _verify_application_target(proposal: dict[str, Any]) -> None:
    target_version = proposal.get("targetVersion")
    if target_version is None:
        return
    command = proposal["command"]
    if (
        proposal["commandType"] != "launch"
        or command.get("action_kind") != "play"
        or not isinstance(command.get("action_playbook"), str)
    ):
        raise ApplicationTargetConflictError("Unsupported versioned application target")
    from .application_mcp import resolve_playbook_version

    try:
        current_version = await resolve_playbook_version(command["action_playbook"])
    except Exception as exc:  # noqa: BLE001
        raise ApplicationTargetConflictError(
            "The approved playbook target is no longer available"
        ) from exc
    if current_version != target_version:
        raise ApplicationTargetConflictError("The approved playbook changed before execution")


async def _execute_application_command(
    command_type: str, command: dict[str, Any]
) -> dict[str, Any]:
    if command_type == "cancel":
        from .cancel_run import execute_cancel_command

        return await execute_cancel_command(command)
    if command_type != "launch":
        raise ValueError(f"Unsupported Operator application command: {command_type!r}")
    from lionagi.studio.services.launches import launch

    result = await launch(command)
    invocation_id = result.get("invocation_id")
    if not invocation_id:
        return result

    # A launch's canonical Run row is created by the child process. Give it a
    # short opportunity to appear so the confirmation can carry a direct run
    # link; retain the invocation link if startup takes longer.
    from lionagi.studio.services.invocations import get_invocation

    run_id = None
    for _ in range(20):
        detail = await get_invocation(str(invocation_id))
        sessions = detail.get("sessions", []) if detail else []
        if sessions:
            run_id = sessions[0].get("id")
            break
        await asyncio.sleep(0.1)
    return {
        **result,
        "run_id": run_id,
        "href": f"/runs/{run_id}" if run_id else f"/invocations/{invocation_id}",
    }


class OperatorCoordinator:
    def __init__(
        self,
        *,
        store: OperatorStore | None = None,
        engine_factory: OperatorEngineFactory | None = None,
        command_executor: CommandExecutor | None = None,
    ) -> None:
        self.store = store or OperatorStore()
        self.engine_factory = engine_factory or BranchOperatorEngine
        self.command_executor = command_executor or _execute_application_command
        self._tasks: dict[str, asyncio.Task] = {}
        self._started = False

    async def startup(self) -> list[str]:
        await self.store.ensure_schema()
        recovered = await self.store.recover_interrupted_turns()
        self._started = True
        return recovered

    async def ensure_started(self) -> None:
        if not self._started:
            # Route-level fallback for direct ASGI/service tests. Normal daemon
            # startup calls startup() before accepting requests.
            await self.startup()

    async def shutdown(self) -> None:
        tasks = list(self._tasks.items())
        for request_id, task in tasks:
            if task.done():
                continue
            try:
                turn = await self.store.get_turn(request_id)
            except Exception:  # noqa: BLE001
                turn = None
            # A done frame makes the turn terminal before canonical Run
            # teardown closes its StateDB handle and writes its final
            # snapshot. Let that bounded cleanup finish; cancelling it here
            # can strand the aiosqlite worker/connection.
            if turn is None or turn["status"] not in {
                "completed",
                "failed",
                "cancelled",
            }:
                task.cancel()
        if tasks:
            await asyncio.gather(
                *(task for _request_id, task in tasks),
                return_exceptions=True,
            )
        self._tasks.clear()
        self._started = False

    async def create_conversation(
        self, *, project: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        await self.ensure_started()
        conversation = await self.store.create_conversation(project=project, title=title)
        return {"conversation": conversation, "frames": []}

    async def snapshot(
        self,
        conversation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        await self.ensure_started()
        frames = await self.store.list_frames(
            conversation_id, after_sequence=after_sequence, limit=limit
        )
        # Read the tail after the page so metadata cannot predate a frame the
        # page itself already contains.
        conversation = await self.store.get_conversation(conversation_id)
        page_tail = frames[-1]["sequence"] if frames else after_sequence
        latest = int(conversation["nextSequence"]) - 1
        return {
            "conversation": conversation,
            "frames": frames,
            "hasMore": page_tail < latest,
            "nextAfterSequence": page_tail,
            "latestSequence": latest,
        }

    async def submit(
        self,
        conversation_id: str,
        *,
        instruction: str,
        context: dict[str, Any],
        expected_last_sequence: int,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        clear_selection: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_started()
        try:
            resolved_provider, resolved_model, resolved_effort = resolve_selection(
                provider=provider, model=model, effort=effort
            )
        except OperatorSelectionError as exc:
            raise OperatorValidationError(str(exc)) from exc
        # The selection travels with the turn rather than being written first:
        # a turn that is refused for an active turn or a stale cursor must
        # leave the conversation exactly as it found it. It still applies to
        # this turn, because the store commits it before the turn is readable.
        accepted = await self.store.submit_turn(
            conversation_id,
            instruction=instruction,
            context=context,
            expected_last_sequence=expected_last_sequence,
            effort=resolved_effort,
            select_provider=resolved_provider,
            select_model=resolved_model,
            clear_selection=clear_selection,
        )
        request_id = accepted["requestId"]
        ready = asyncio.Event()
        task = asyncio.create_task(
            self._run_turn(request_id, ready), name=f"operator-turn-{request_id}"
        )
        self._tasks[request_id] = task

        def discard(done: asyncio.Task) -> None:
            ready.set()
            self._tasks.pop(request_id, None)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                _log.error("Operator turn task escaped", exc_info=exc)

        task.add_done_callback(discard)
        # A successful 202 identifies a canonical Run, not merely a future
        # intention to create one. The event is set after the durable run-link
        # frames land (or when setup terminally fails).
        await ready.wait()
        return accepted

    async def _run_turn(self, request_id: str, ready: asyncio.Event) -> None:
        turn_row = await self.store.get_turn(request_id)
        if not await self.store.mark_running(request_id):
            return
        conversation_id = turn_row["conversationId"]
        live = None
        run_branch = None
        run_dir = None
        started_at: float | None = None
        terminal_status = "completed"
        terminal_exc: BaseException | None = None
        try:
            complete_turns = await self.store.list_complete_turn_frame_groups(
                conversation_id,
                exclude_request_id=request_id,
                limit=64,
            )
            compiled = compile_operator_history(complete_turns)
            compiled_context = await self.store.record_context_compilation(
                request_id, compiled.metadata
            )

            async def request_permission(
                command_type: str,
                command: dict[str, Any],
                risk: str,
                summary: str,
            ) -> PermissionDecision:
                proposal = await self.store.create_proposal(
                    conversation_id,
                    request_id,
                    command_type=command_type,
                    command=command,
                    risk=risk,
                    summary=summary,
                )
                while True:
                    current = await self.store.get_proposal(proposal["id"])
                    if current["status"] == "pending" and current["expiresAt"] <= time.time():
                        current = await self.store.expire_proposal(current["id"])
                    if current["status"] in {"confirmed", "succeeded"}:
                        return PermissionDecision(True, current["id"], result=current.get("result"))
                    if current["status"] in {
                        "cancelled",
                        "expired",
                        "failed",
                        "conflict",
                    }:
                        return PermissionDecision(False, current["id"])
                    await asyncio.sleep(0.05)

            conversation_row = await self.store.get_conversation(conversation_id)
            selected_provider = conversation_row.get("provider")
            selected_model = conversation_row.get("providerModel")
            selected_effort = turn_row.get("effort")
            engine_turn = OperatorEngineTurn(
                conversation_id=conversation_id,
                request_id=request_id,
                instruction=turn_row["instruction"],
                context=compiled_context,
                history=compiled.frames,
                request_permission=request_permission,
                store_path=str(self.store.path()),
                # Filled in below, once the store has said whether the session
                # still belongs to what this turn is about to run on.
                provider_session_id=None,
                provider=selected_provider if isinstance(selected_provider, str) else None,
                model=selected_model if isinstance(selected_model, str) else None,
                effort=selected_effort if isinstance(selected_effort, str) else None,
            )
            # Resolve once, here, and hand the same pair to the store, the
            # branch and the manifest. The environment is re-read on every
            # resolution, so two calls a few lines apart are two chances to
            # disagree about what ran.
            resolved_provider, resolved_model = resolve_operator_provider_model(engine_turn)
            resumed_session_id = await self.store.claim_resolved_pair(
                conversation_id,
                provider=resolved_provider,
                model=resolved_model,
            )
            engine_turn = replace(
                engine_turn,
                provider_session_id=(
                    resumed_session_id if isinstance(resumed_session_id, str) else None
                ),
            )
            run_branch = build_operator_branch(engine_turn)
            engine_turn = replace(engine_turn, runtime_branch=run_branch)
            from lionagi.cli import _runs as cli_runs

            file_run_id = f"operator-{uuid.uuid4().hex[:12]}"
            run_dir = cli_runs.RunDir(
                run_id=file_run_id,
                state_root=cli_runs.RUNS_ROOT / file_run_id,
                artifact_root=cli_runs.RUNS_ROOT / file_run_id / "artifacts",
            )
            run_dir.ensure_state_dirs()
            run_dir.ensure_artifact_root()
            started_at = time.time()
            run_dir.write_manifest(
                {
                    "kind": "agent",
                    "agent_name": "Operator",
                    "branch_id": str(run_branch.id),
                    "provider": resolved_provider,
                    "model": resolved_model,
                    "status": "running",
                    "started_at": started_at,
                    "ended_at": None,
                }
            )
            # Scripted/test engines may not call Branch.run themselves; the
            # canonical snapshot still exists before the run link is emitted.
            await write_resumable_operator_snapshot(run_branch, run_dir.branches_dir)

            live = await cli_runs.setup_agent_persist(
                run_branch,
                agent_name="Operator",
                artifacts_path=str(run_dir.artifact_root),
                model=resolved_model,
                provider=resolved_provider,
                project=turn_row["context"].get("project"),
                run_id=run_dir.run_id,
                share_db=False,
            )
            if live is None:
                raise RuntimeError("Could not create the canonical Operator run")
            run_id = live["session_id"]
            await self.store.append_frame(
                conversation_id,
                request_id,
                "tool_result",
                {
                    "callId": f"run:{request_id}",
                    "ok": True,
                    "result": {
                        "runId": run_id,
                        "branchId": str(run_branch.id),
                        "href": f"/runs/{run_id}",
                    },
                },
            )
            await self.store.append_frame(
                conversation_id,
                request_id,
                "text",
                {
                    # Trailing break: consecutive assistant text frames are
                    # concatenated for display, so without it this link runs
                    # straight into the first word of the model's reply.
                    "content": f"[Open this Operator run](/runs/{run_id})\n\n",
                    "format": "markdown",
                    "role": "assistant",
                },
            )
            ready.set()
            engine = self.engine_factory()
            # Only run_dir changes here. Copying field by field means every
            # field added later has to be remembered at this call site, and one
            # already was not: provider and effort were being dropped.
            engine_turn = replace(engine_turn, run_dir=run_dir)
            async for event in engine.stream(engine_turn):
                if not isinstance(event, OperatorEngineEvent):
                    raise TypeError("Operator engine yielded an invalid event")
                if event.type == "done":
                    continue
                if event.type == "session":
                    # Continuity state, not conversation content: remembered on
                    # the conversation so the next turn resumes, never appended
                    # as a frame the human has to read.
                    session_id = event.payload.get("providerSessionId")
                    if isinstance(session_id, str) and session_id:
                        await self.store.set_provider_session_id(conversation_id, session_id)
                    continue
                if event.type == "ui_command":
                    effect = event.payload.get("effect")
                    if not isinstance(effect, dict):
                        raise TypeError("Operator engine yielded an invalid UI effect")
                    await self.store.append_effect(conversation_id, request_id, effect)
                else:
                    if event.type == "tool_result":
                        call_id = event.payload.get("callId")
                        if isinstance(call_id, str):
                            completed = await self.store.complete_provider_permission(
                                request_id,
                                call_id,
                                ok=bool(event.payload.get("ok")),
                            )
                            if completed is not None:
                                await self.store.append_frame(
                                    conversation_id,
                                    request_id,
                                    "confirmation",
                                    {
                                        "proposalId": completed["id"],
                                        "state": "executed",
                                    },
                                )
                    await self.store.append_frame(
                        conversation_id, request_id, event.type, event.payload
                    )
            await self.store.finish_turn(request_id, outcome="completed")
        except asyncio.CancelledError:
            terminal_status = "cancelled"
            await self.store.finish_turn(
                request_id,
                outcome="cancelled",
                error={
                    "code": "cancelled",
                    "message": "The operator cancelled this turn",
                    "retryable": False,
                },
            )
            raise
        except OperatorProviderUnavailableError as exc:
            terminal_status = "failed"
            terminal_exc = exc
            await self.store.finish_turn(
                request_id,
                outcome="failed",
                error={
                    "code": "provider_unavailable",
                    "message": str(exc),
                    "retryable": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            terminal_status = "failed"
            terminal_exc = exc
            _log.exception("Operator turn failed: %s", request_id)
            await self.store.finish_turn(
                request_id,
                outcome="failed",
                error={
                    "code": "model_failure",
                    "message": (
                        f"The Operator engine failed ({type(exc).__name__}); "
                        "see daemon logs for diagnostics"
                    ),
                    "retryable": True,
                },
            )
        finally:
            # Publish the final checkpoint before marking the canonical
            # session terminal. Queued resume workers use that transition as
            # the hand-off boundary.
            if run_branch is not None and run_dir is not None:
                with suppress(Exception):
                    await write_resumable_operator_snapshot(run_branch, run_dir.branches_dir)
            if live is not None:
                from lionagi.cli._runs import teardown_agent_persist

                with suppress(Exception):
                    await teardown_agent_persist(
                        live,
                        status=terminal_status,
                        exception=terminal_exc,
                    )
            if run_branch is not None and run_dir is not None:
                with suppress(Exception):
                    final_provider, final_model = resolve_operator_provider_model(engine_turn)
                    run_dir.write_manifest(
                        {
                            "kind": "agent",
                            "agent_name": "Operator",
                            "branch_id": str(run_branch.id),
                            "session_id": live["session_id"] if live is not None else None,
                            "provider": final_provider,
                            "model": final_model,
                            "status": terminal_status,
                            "started_at": started_at or time.time(),
                            "ended_at": time.time(),
                        }
                    )
            ready.set()

    async def cancel(self, conversation_id: str, request_id: str) -> dict[str, Any]:
        await self.ensure_started()
        result = await self.store.request_cancel(conversation_id, request_id)
        task = self._tasks.get(request_id)
        if task is not None and not task.done():
            task.cancel()
        if result["cancelRequested"]:
            # Do not rely on the task's coroutine body having started: a task
            # cancelled immediately after create_task() never enters its
            # try/except. finish_turn is idempotent with the running task's
            # cancellation cleanup.
            await self.store.finish_turn(
                request_id,
                outcome="cancelled",
                error={
                    "code": "cancelled",
                    "message": "The operator cancelled this turn",
                    "retryable": False,
                },
            )
        return result

    async def decide(
        self,
        conversation_id: str,
        proposal_id: str,
        *,
        allow: bool,
        expected_command_hash: str | None,
        expected_target_version: str | None,
    ) -> dict[str, Any]:
        await self.ensure_started()
        before = await self.store.get_proposal(proposal_id)
        if before["conversationId"] != conversation_id:
            raise OperatorConflictError("Proposal does not belong to this conversation")

        try:
            # Validation, audit insertion, and the pending -> executing claim
            # share one SQLite transaction. A concurrent allow therefore sees
            # executing and cannot invoke the application command again.
            proposal = await self.store.decide_proposal(
                conversation_id,
                proposal_id,
                allow=allow,
                expected_command_hash=expected_command_hash,
                expected_target_version=expected_target_version,
                audit=True,
                claim_execution=True,
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, OperatorConflictError):
                raise
            raise OperatorAuditUnavailableError("Mutation audit is unavailable") from exc

        if not allow:
            return self._proposal_result(proposal)

        if proposal["commandType"] == "provider_permission":
            return self._proposal_result(proposal)
        if not proposal.get("_claimedExecution"):
            return self._proposal_result(proposal)

        try:
            # Re-fingerprint after the durable execution claim and immediately
            # before invoking the application service. A changed or removed
            # playbook fails closed without executing the approved command.
            await _verify_application_target(proposal)
        except ApplicationTargetConflictError as exc:
            _log.warning("Operator application target conflict: %s", proposal_id)
            try:
                proposal = await self.store.complete_proposal(
                    proposal_id,
                    status="conflict",
                    error_code="stale_context",
                    result={"message": str(exc)},
                    audit=True,
                )
            except Exception as persist_exc:  # noqa: BLE001
                raise OperatorAuditUnavailableError(
                    "Target conflict outcome is indeterminate; reconciliation is required"
                ) from persist_exc
            await self.store.append_frame(
                conversation_id,
                proposal["requestId"],
                "tool_result",
                {
                    "callId": proposal_id,
                    "ok": False,
                    "error": {
                        "code": "stale_context",
                        "message": str(exc),
                        "retryable": False,
                    },
                },
            )
            return self._proposal_result(proposal)

        try:
            result = await self.command_executor(proposal["commandType"], proposal["command"])
        except Exception as exc:  # noqa: BLE001
            public_message = (
                f"Application command failed ({type(exc).__name__}); "
                "see daemon logs for diagnostics"
            )
            _log.exception("Operator application command failed: %s", proposal_id)
            try:
                proposal = await self.store.complete_proposal(
                    proposal_id,
                    status="failed",
                    error_code="service_failure",
                    result={"message": public_message},
                    audit=True,
                )
            except Exception as persist_exc:  # noqa: BLE001
                # The application was attempted but its terminal audit could
                # not be committed. Leave the durable claim executing so an
                # automatic retry cannot duplicate an indeterminate command.
                _log.exception(
                    "Operator command failure outcome could not be audited: %s",
                    proposal_id,
                )
                raise OperatorAuditUnavailableError(
                    "Command outcome is indeterminate; reconciliation is required"
                ) from persist_exc
            await self.store.append_frame(
                conversation_id,
                proposal["requestId"],
                "tool_result",
                {
                    "callId": proposal_id,
                    "ok": False,
                    "error": {
                        "code": "service_failure",
                        "message": public_message,
                        "retryable": False,
                    },
                },
            )
            return self._proposal_result(proposal)

        try:
            proposal = await self.store.complete_proposal(
                proposal_id, status="succeeded", result=result, audit=True
            )
        except Exception as exc:  # noqa: BLE001
            # Execution happened. Never rewrite this as a safe failure or run
            # it again: the executing claim and idempotency key intentionally
            # remain for manual reconciliation.
            _log.exception("Operator command result audit failed: %s", proposal_id)
            raise OperatorAuditUnavailableError(
                "Command outcome is indeterminate; reconciliation is required"
            ) from exc
        await self.store.append_frame(
            conversation_id,
            proposal["requestId"],
            "confirmation",
            {"proposalId": proposal_id, "state": "executed"},
        )
        await self.store.append_frame(
            conversation_id,
            proposal["requestId"],
            "tool_result",
            {
                "callId": proposal_id,
                "ok": True,
                "result": result,
            },
        )
        href = result.get("href")
        if isinstance(href, str):
            label = "Open run" if result.get("run_id") else "Open launch invocation"
            await self.store.append_frame(
                conversation_id,
                proposal["requestId"],
                "text",
                {
                    "content": f"[{label}]({href})",
                    "format": "markdown",
                    "role": "assistant",
                },
            )
        return self._proposal_result(proposal)

    @staticmethod
    def _proposal_result(proposal: dict[str, Any]) -> dict[str, Any]:
        status = proposal["status"]
        wire_status = {
            "confirmed": "executing",
            "cancelled": "failed",
        }.get(status, status)
        return {
            "proposalId": proposal["id"],
            "status": wire_status,
            "result": proposal.get("result"),
            "error": (
                {
                    "code": proposal.get("errorCode") or "denied",
                    "message": (
                        "The approved target changed before execution"
                        if proposal.get("errorCode") == "stale_context"
                        else "Permission was not granted"
                    ),
                    "retryable": False,
                }
                if status in {"cancelled", "failed", "expired", "conflict"}
                else None
            ),
        }


_COORDINATOR: OperatorCoordinator | None = None


def get_operator_coordinator() -> OperatorCoordinator:
    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = OperatorCoordinator()
    return _COORDINATOR


async def reset_operator_coordinator_for_testing(
    coordinator: OperatorCoordinator | None = None,
) -> OperatorCoordinator:
    global _COORDINATOR
    if _COORDINATOR is not None:
        with suppress(Exception):
            await _COORDINATOR.shutdown()
    _COORDINATOR = coordinator or OperatorCoordinator()
    return _COORDINATOR
