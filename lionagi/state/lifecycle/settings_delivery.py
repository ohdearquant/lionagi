# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Build explicit settings-notification outcomes for durable delivery."""

from __future__ import annotations

import inspect
from typing import Any

from .callbacks import RunTerminalEnvelope
from .notify_settings import ResolvedNotifyHandler, build_handler
from .reconciler import DeliveryOutcome, PreparedDelivery


def prepare_resolved_delivery(
    resolved: ResolvedNotifyHandler,
    envelope: RunTerminalEnvelope,
) -> PreparedDelivery | DeliveryOutcome:
    """Resolve a selected adapter without treating registry dispatch as success."""
    if resolved.filter_kinds is not None and envelope.entity.kind not in resolved.filter_kinds:
        return DeliveryOutcome.NOT_APPLICABLE
    if resolved.filter_ids is not None and envelope.entity.id not in resolved.filter_ids:
        return DeliveryOutcome.NOT_APPLICABLE

    if resolved.python_ref is not None:

        def invoke_python() -> Any:
            # Resolving a configured module can execute arbitrary import-time code;
            # the reconciler invokes this inside its tracked, bounded worker.
            handler = build_handler(resolved)
            if handler is None:
                return DeliveryOutcome.FAILURE
            value = handler(envelope)
            if inspect.isawaitable(value):

                async def finish() -> DeliveryOutcome:
                    result = await value
                    return DeliveryOutcome.FAILURE if result is False else DeliveryOutcome.SUCCESS

                return finish()
            return DeliveryOutcome.FAILURE if value is False else DeliveryOutcome.SUCCESS

        return PreparedDelivery(invoke_python)

    outcomes: list[bool] = []

    def record_exec(*, ok: bool, exit_code: int | None, stderr_text: str | None) -> None:
        outcomes.append(ok)
        return None

    handler = build_handler(resolved, outcome_fn=record_exec)
    if handler is None:
        return DeliveryOutcome.FAILURE

    async def invoke_exec() -> DeliveryOutcome:
        await handler(envelope)
        if outcomes and outcomes[-1] is True:
            return DeliveryOutcome.SUCCESS
        return DeliveryOutcome.FAILURE

    return PreparedDelivery(invoke_exec)
