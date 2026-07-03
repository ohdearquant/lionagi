"""Runner — execute one (provider, arm) cell of the steering fixture once."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .arms import Arm, make_steer_entry, suppress_operator_render
from .fixture import is_steer_adherent
from .graph import build_two_op_flow
from .providers import build_imodel

logger = logging.getLogger("orchbench.steering")


@dataclass(slots=True)
class SteerRunResult:
    """One trial's outcome for one (provider, arm) cell."""

    provider: str
    arm: str
    trial: int
    adherent: bool = False
    rendered_into_op: str | None = None
    op2_text: str = ""
    error: str | None = None
    wall_seconds: float = 0.0


async def run_steering_once(provider_key: str, arm: Arm, trial: int) -> SteerRunResult:
    """Run one trial. Catches errors into ``SteerRunResult.error`` (never raises)."""
    t0 = time.monotonic()
    try:
        imodel = build_imodel(provider_key)
        session, graph, op1, op2 = build_two_op_flow(imodel)

        executor_ref: dict = {}
        injected = False

        def on_progress(op_id, name, status, elapsed):
            # op1's "completed" callback fires before its completion_event is
            # set, so this always lands before op2's _prepare_operation runs.
            nonlocal injected
            if arm is Arm.NO_STEER or injected:
                return
            if status == "completed" and op_id == str(op1.id):
                executor = executor_ref.get("executor")
                if executor is not None:
                    existing = executor.context.content.get("operator_messages", [])
                    executor.context.content["operator_messages"] = [
                        *existing,
                        make_steer_entry(),
                    ]
                    injected = True

        if arm is Arm.STEER_BURIED:
            with suppress_operator_render():
                result = await session.flow(
                    graph, on_progress=on_progress, executor_ref=executor_ref, max_concurrent=2
                )
        else:
            result = await session.flow(
                graph, on_progress=on_progress, executor_ref=executor_ref, max_concurrent=2
            )

        op_results = result.get("operation_results", {})
        op2_text = str(op_results.get(op2.id, ""))
        return SteerRunResult(
            provider=provider_key,
            arm=arm.value,
            trial=trial,
            adherent=is_steer_adherent(op2_text),
            rendered_into_op=op2.metadata.get("rendered_into_op"),
            op2_text=op2_text[:4000],
            wall_seconds=time.monotonic() - t0,
        )
    except Exception as e:  # noqa: BLE001 — a failed run is data, not a crash
        logger.exception("run_steering_once failed: %s / %s / trial %d", provider_key, arm, trial)
        return SteerRunResult(
            provider=provider_key,
            arm=arm.value,
            trial=trial,
            error=f"{type(e).__name__}: {e}",
            wall_seconds=time.monotonic() - t0,
        )
