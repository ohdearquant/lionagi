"""Test-only Studio ASGI app for deterministic Operator browser contracts.

This module is imported only by the isolated seeded-daemon harness.  It
installs a coordinator before the production app module creates its FastAPI
application, so browser tests exercise the real HTTP, persistence, SSE, and
permission-decision paths without invoking a real model or application
command.
"""

from __future__ import annotations

import asyncio

from lionagi.studio.operator import coordinator as coordinator_module
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.types import OperatorEngineEvent, OperatorEngineTurn

_EXECUTED_MARKERS: set[str] = set()


async def _execute_harmless_command(
    command_type: str, command: dict[str, object]
) -> dict[str, object]:
    """Record an in-memory marker; never touch the filesystem or launch work."""
    if command_type != "e2e_harmless_action":
        raise ValueError(f"unsupported e2e command type: {command_type}")
    marker = command.get("marker")
    if not isinstance(marker, str) or not marker:
        raise ValueError("e2e command marker is required")
    _EXECUTED_MARKERS.add(marker)
    return {"executed": True, "marker": marker}


class DeterministicOperatorEngine:
    """Stable test engine covering streaming, cancellation, and permissions."""

    def stream(self, turn: OperatorEngineTurn):
        return self._stream(turn)

    async def _stream(self, turn: OperatorEngineTurn):
        instruction = turn.instruction.casefold()

        if "wait until i stop you" in instruction:
            await asyncio.sleep(10)
            return

        if "gated demo action" in instruction:
            marker = f"permission-{turn.request_id}"
            decision = await turn.request_permission(
                "e2e_harmless_action",
                {
                    "operation": "record_permission_decision",
                    "marker": marker,
                    "scope": "isolated_e2e_memory",
                },
                "execute",
                "Record a harmless marker in the isolated browser-test process",
            )
            executed = marker in _EXECUTED_MARKERS
            if decision.allowed and executed:
                content = "Demo action allowed and completed safely."
            elif not decision.allowed and not executed:
                content = "Demo action denied. Nothing was changed."
            else:  # Make an execution/decision mismatch fail the browser contract loudly.
                raise AssertionError("permission decision did not match harmless command execution")
            yield OperatorEngineEvent(
                "text",
                {"content": content, "format": "plain", "role": "assistant"},
            )
            return

        yield OperatorEngineEvent(
            "text",
            {"content": "Fleet ", "format": "plain", "role": "assistant"},
        )
        await asyncio.sleep(0)
        yield OperatorEngineEvent(
            "text",
            {"content": "ready.", "format": "plain", "role": "assistant"},
        )


# Install the test coordinator before importing lionagi.studio.app: that
# module creates its FastAPI application at import time.
coordinator_module._COORDINATOR = OperatorCoordinator(  # noqa: SLF001
    engine_factory=DeterministicOperatorEngine,
    command_executor=_execute_harmless_command,
)

from lionagi.studio.app import app  # noqa: E402  (intentional import ordering)
