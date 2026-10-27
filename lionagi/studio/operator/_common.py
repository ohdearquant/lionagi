# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared operations for durable operator tools."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import OperatorStore

__all__ = ("wait_for_proposal",)

_TERMINAL_PROPOSAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired", "conflict"})


async def wait_for_proposal(
    store: OperatorStore,
    proposal: dict[str, Any],
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    poll_interval: float = 0.1,
    advance_interval: Callable[[float], float] | None = None,
    is_terminal: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Refresh a durable proposal until the caller can return its decision.

    Only pending proposals expire. A confirmation racing expiry is returned
    by the store and followed normally; errors and cancellation propagate.
    The caller owns the clock, sleeper and any polling policy.
    """
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= clock():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in _TERMINAL_PROPOSAL_STATUSES if is_terminal is None else is_terminal(status):
            return proposal
        await sleep(poll_interval)
        if advance_interval is not None:
            poll_interval = advance_interval(poll_interval)


def _identity(store_factory: Callable[[str], OperatorStore]) -> tuple[OperatorStore, str, str]:
    """Load the durable turn identity using the caller's store factory."""
    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        raise RuntimeError("Studio application bridge is missing its durable turn identity")
    return store_factory(db_path), conversation_id, request_id
