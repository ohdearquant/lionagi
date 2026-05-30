# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from lionagi.runtime.control import (
    ControlRequest,
    ControlVerb,
    RunnerHandle,
    RunnerState,
    validate_transition,
)
from lionagi.runtime.runner import LocalRunner, PlayRunner
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunnerReasons


class ControlService:
    """Coordinate persistent control request bookkeeping with runner execution."""

    def __init__(
        self,
        *,
        db: StateDB,
        runners: dict[str, PlayRunner] | None = None,
    ) -> None:
        local_runner = LocalRunner()
        self._runners: dict[str, PlayRunner] = {
            "local": local_runner,
            "local_worktree": local_runner,
        }
        if runners:
            self._runners.update(runners)
        self._db = db

    async def _resolve_target_session(self, target_type: str, target_id: str) -> str:
        if target_type == "session":
            return target_id
        if target_type == "play":
            play = await self._db.get_play(target_id)
            if play is None:
                raise LookupError(f"play not found: {target_id!r}")
            session_id = play.get("session_id")
            if not session_id:
                raise LookupError(f"play {target_id!r} is not linked to a session")
            return session_id

        raise ValueError(f"unsupported target_type: {target_type!r}")

    def _runner_for_handle(self, runner_type: str) -> PlayRunner:
        if runner_type not in self._runners:
            raise ValueError(f"unsupported runner type: {runner_type!r}")
        return self._runners[runner_type]

    @staticmethod
    def _target_state(verb: ControlVerb) -> RunnerState:
        if verb == ControlVerb.PAUSE:
            return RunnerState.PAUSED
        if verb == ControlVerb.RESUME:
            return RunnerState.RUNNING
        if verb == ControlVerb.CANCEL:
            return RunnerState.CANCELLING
        if verb == ControlVerb.KILL:
            return RunnerState.KILLED
        raise ValueError(f"verb {verb.value!r} is not supported yet")

    @staticmethod
    def _reason_code_for_state(
        state: RunnerState,
        verb: ControlVerb,
        *,
        fallback: str = RunnerReasons.FAILED,
    ) -> str:
        if state == RunnerState.PAUSED:
            return RunnerReasons.PAUSED
        if state == RunnerState.RUNNING:
            return RunnerReasons.RESUMED
        if state == RunnerState.CANCELLING:
            return RunnerReasons.CANCELLED
        if state == RunnerState.KILLED:
            return RunnerReasons.KILLED
        if state == RunnerState.FAILED:
            return RunnerReasons.FAILED
        if state == RunnerState.TIMED_OUT:
            return fallback
        if verb == ControlVerb.PAUSE:
            return RunnerReasons.PAUSED
        if verb == ControlVerb.RESUME:
            return RunnerReasons.RESUMED
        if verb == ControlVerb.CANCEL:
            return RunnerReasons.CANCELLED
        if verb == ControlVerb.KILL:
            return RunnerReasons.KILLED
        return fallback

    async def dispatch(
        self,
        target_type: str,
        target_id: str,
        request: ControlRequest,
        *,
        idempotency_key: str | None = None,
        grace_seconds: float = 5.0,
        cascade: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> RunnerHandle:
        session_id = await self._resolve_target_session(target_type, target_id)
        handle = await self._db.get_runner_handle(session_id)
        if handle is None:
            raise LookupError(f"runner handle not found for session: {session_id!r}")
        if handle.state.is_terminal:
            raise ValueError(f"cannot control terminal runner state {handle.state.value!r}")

        requested_state = self._target_state(request.verb)
        if not validate_transition(handle.state, requested_state):
            raise ValueError(
                f"invalid transition {handle.state.value!r} -> {requested_state.value!r}"
            )

        cr = await self._db.create_control_request(
            target_type=target_type,
            target_id=target_id,
            resolved_session_id=session_id,
            action=request.verb,
            requested_by=request.actor_id,
            reason=request.reason,
            idempotency_key=idempotency_key,
            expected_state=requested_state,
            grace_seconds=grace_seconds,
            cascade=cascade,
            metadata=metadata,
        )
        await self._db.claim_control_request(cr["id"])

        runner = self._runner_for_handle(handle.runner_type)
        pre_state: RunnerState = handle.state
        updated_handle = handle
        try:
            handle = await self._db.transition_runner_state(
                session_id,
                new_state=requested_state,
                reason_code=self._reason_code_for_state(requested_state, request.verb),
                reason_summary=f"{request.verb.value} requested",
                actor=request.actor_id,
                source="admin",
                control_request_id=cr["id"],
            )

            updated_handle = await runner.control(
                handle.session_id,
                request.verb,
                request.reason,
            )
            await self._db.upsert_runner_handle(updated_handle)
            if updated_handle.state != requested_state:
                handle = await self._db.transition_runner_state(
                    session_id,
                    new_state=updated_handle.state,
                    reason_code=self._reason_code_for_state(
                        updated_handle.state,
                        request.verb,
                    ),
                    reason_summary=f"{request.verb.value} applied",
                    actor=request.actor_id,
                    source="admin",
                    control_request_id=cr["id"],
                )
            else:
                handle = updated_handle

            await self._db.complete_control_request(
                cr["id"],
                request_status="completed",
            )
            return handle
        except Exception:
            if updated_handle.state != pre_state:
                await self._db.upsert_runner_handle(updated_handle)
            await self._db.complete_control_request(
                cr["id"],
                request_status="failed",
                error="runner control failed",
            )
            raise
