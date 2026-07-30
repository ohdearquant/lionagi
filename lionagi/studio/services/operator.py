# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""HTTP surface for the durable ADR-0083 Studio Operator protocol."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import HTTPException, Query, Request

from ..operator.coordinator import get_operator_coordinator
from ..operator.store import (
    OperatorConflictError,
    OperatorNotFoundError,
    OperatorStoreError,
)
from ..operator.types import (
    AcknowledgeEffectRequest,
    ConfirmProposalRequest,
    CreateConversationRequest,
    DecideProposalRequest,
    OperatorTurnRequest,
    OperatorViewReport,
)
from ..registry import studio_route
from ._sse import sse_response


def _http_error(exc: OperatorStoreError) -> HTTPException:
    if isinstance(exc, OperatorNotFoundError):
        status = 404
    elif isinstance(exc, OperatorConflictError):
        status = 409
    else:
        status = 503
    detail: dict[str, Any] = {
        "code": exc.code,
        "message": str(exc),
        "retryable": False,
    }
    details = getattr(exc, "details", None)
    if details:
        if details.get("code") == "stale_context":
            detail["code"] = "stale_context"
        detail["details"] = details
    return HTTPException(status_code=status, detail=detail)


async def operator_startup() -> list[str]:
    return await get_operator_coordinator().startup()


async def operator_shutdown() -> None:
    await get_operator_coordinator().shutdown()


@studio_route("/operator/conversations", method="GET", area="operator")
async def list_operator_conversations(
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    coordinator = get_operator_coordinator()
    try:
        await coordinator.ensure_started()
        rows = await coordinator.store.list_conversations(limit=limit)
        return {"conversations": rows}
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route("/operator/conversations", method="POST", area="operator")
async def create_operator_conversation(
    body: CreateConversationRequest | None = None,
) -> dict[str, Any]:
    body = body or CreateConversationRequest()
    try:
        return await get_operator_coordinator().create_conversation(
            project=body.project, title=body.title
        )
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route("/operator/conversations/{conversation_id}", method="GET", area="operator")
async def get_operator_conversation(
    conversation_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=1000),
) -> dict[str, Any]:
    try:
        return await get_operator_coordinator().snapshot(
            conversation_id, after_sequence=after_sequence, limit=limit
        )
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route("/operator/conversations/{conversation_id}", method="DELETE", area="operator")
async def delete_operator_conversation(conversation_id: str) -> dict[str, Any]:
    coordinator = get_operator_coordinator()
    try:
        await coordinator.ensure_started()
        await coordinator.store.archive_or_delete(conversation_id)
        return {"ok": True, "conversationId": conversation_id}
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route(
    "/operator/conversations/{conversation_id}/turns",
    method="POST",
    area="operator",
    status_code=202,
)
async def submit_operator_turn(conversation_id: str, body: OperatorTurnRequest) -> dict[str, Any]:
    try:
        return await get_operator_coordinator().submit(
            conversation_id,
            instruction=body.instruction,
            context=body.context.model_dump(by_alias=True),
            expected_last_sequence=body.expected_last_sequence,
            model=body.model,
        )
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route(
    "/operator/conversations/{conversation_id}/view",
    method="POST",
    area="operator",
)
async def report_operator_view(conversation_id: str, body: OperatorViewReport) -> dict[str, Any]:
    """Record where the human is now, so the Operator can read it mid-turn.

    A turn's context is frozen at submit. Without this the Operator answers
    "where am I" with wherever the human was when they hit send, which is
    wrong precisely when they have moved since.

    A report the browser observed before the one already stored is discarded,
    since reports race and the loser of that race is the stale view.
    """
    coordinator = get_operator_coordinator()
    view = body.model_dump(by_alias=True)
    observed_at = view.pop("observedAt")
    try:
        await coordinator.ensure_started()
        received_at, applied = await coordinator.store.record_view(
            conversation_id, view, observed_at
        )
        return {"ok": True, "receivedAt": received_at, "applied": applied}
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route(
    "/operator/conversations/{conversation_id}/requests/{request_id}/cancel",
    method="POST",
    area="operator",
    status_code=202,
)
async def cancel_operator_turn(conversation_id: str, request_id: str) -> dict[str, Any]:
    try:
        return await get_operator_coordinator().cancel(conversation_id, request_id)
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route(
    "/operator/conversations/{conversation_id}/stream",
    method="GET",
    area="operator",
    include_in_schema=True,
)
async def stream_operator_conversation(
    conversation_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
):
    coordinator = get_operator_coordinator()
    try:
        await coordinator.ensure_started()
        await coordinator.store.get_conversation(conversation_id)
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc

    async def events():
        cursor = after_sequence
        heartbeat_at = asyncio.get_running_loop().time() + 5
        while True:
            if await request.is_disconnected():
                return
            frames = await coordinator.store.list_frames(
                conversation_id, after_sequence=cursor, limit=250
            )
            if frames:
                for frame in frames:
                    # The row was committed before list_frames returned.
                    cursor = frame["sequence"]
                    yield (
                        "data:" + json.dumps(frame, sort_keys=True, separators=(",", ":")) + "\n\n"
                    )
                heartbeat_at = asyncio.get_running_loop().time() + 5
                continue
            now = asyncio.get_running_loop().time()
            if now >= heartbeat_at:
                yield ": heartbeat\n\n"
                heartbeat_at = now + 5
            await asyncio.sleep(0.05)

    return sse_response(events())


@studio_route(
    "/operator/conversations/{conversation_id}/proposals/{proposal_id}/confirm",
    method="POST",
    area="operator",
)
async def confirm_operator_proposal(
    conversation_id: str,
    proposal_id: str,
    body: ConfirmProposalRequest,
) -> dict[str, Any]:
    try:
        return await get_operator_coordinator().decide(
            conversation_id,
            proposal_id,
            allow=True,
            expected_command_hash=body.expected_command_hash,
            expected_target_version=body.expected_target_version,
        )
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route(
    "/operator/conversations/{conversation_id}/proposals/{proposal_id}/decision",
    method="POST",
    area="operator",
)
async def decide_operator_proposal(
    conversation_id: str,
    proposal_id: str,
    body: DecideProposalRequest,
) -> dict[str, Any]:
    try:
        return await get_operator_coordinator().decide(
            conversation_id,
            proposal_id,
            allow=body.decision == "allow",
            expected_command_hash=body.expected_command_hash,
            expected_target_version=body.expected_target_version,
        )
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc


@studio_route(
    "/operator/conversations/{conversation_id}/effects/{effect_id}/ack",
    method="POST",
    area="operator",
)
async def acknowledge_operator_effect(
    conversation_id: str,
    effect_id: str,
    body: AcknowledgeEffectRequest,
) -> dict[str, Any]:
    coordinator = get_operator_coordinator()
    try:
        await coordinator.ensure_started()
        return await coordinator.store.acknowledge_effect(
            conversation_id,
            effect_id,
            status=body.status,
            rejection_code=body.rejection_code,
        )
    except OperatorStoreError as exc:
        raise _http_error(exc) from exc
