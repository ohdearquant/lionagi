# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Session.flow() governance integration (P18).

Provides ``governed_flow()`` — a drop-in wrapper around ``Session.flow()``
that applies a CharterDocument to every FlowOp in the DAG.  When no charter
is supplied the function delegates directly to ``Session.flow()`` with zero
overhead (backward compatible).

Typical usage::

    result, cert = await governed_flow(
        session,
        graph,
        charter="path/to/charter.yaml",
        on_deny="raise",   # or "skip"
    )
    # cert is None when no charter was supplied
"""

from __future__ import annotations

import hashlib
import json
import time as _time
from typing import TYPE_CHECKING, Any

from lionagi.ln import AlcallParams
from lionagi.operations.flow import DependencyAwareExecutor
from lionagi.operations.flow import flow as _ungoverned_flow
from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import Event
from lionagi.protocols.governance.certificate import TaskCertificate
from lionagi.protocols.governance.flow_integration import GovernedFlowController
from lionagi.protocols.governance.gates import GateVerdict, GovernanceViolationError
from lionagi.protocols.types import EventStatus

if TYPE_CHECKING:
    from lionagi.protocols.graph.graph import Graph
    from lionagi.session.session import Branch, Session

__all__ = ["governed_flow"]

_SKIP = "skip"
_RAISE = "raise"


def _hash_value(value: Any) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        payload = repr(value)
    return hashlib.sha256(payload.encode()).hexdigest()


class _GovernedExecutor(DependencyAwareExecutor):
    """DependencyAwareExecutor with per-operation governance hooks."""

    def __init__(
        self,
        controller: GovernedFlowController,
        on_deny: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._controller = controller
        self._on_deny = on_deny

    async def _execute_operation(self, operation: Operation, limiter: Any) -> None:
        if operation.execution.status in Event._TERMINAL_STATUSES:
            await super()._execute_operation(operation, limiter)
            return

        op_name = operation.metadata.get("reference_id") or str(operation.operation)

        gate_result = self._controller.pre_op_check(op_name, ctx=None)

        if gate_result.verdict == GateVerdict.DENY:
            if self._on_deny == _RAISE:
                raise GovernanceViolationError(gate_result)
            # skip mode
            operation.execution.status = EventStatus.SKIPPED
            self.skipped_operations.add(operation.id)
            self.completion_events[operation.id].set()
            self._controller.post_op_record(
                op_name,
                args_hash="",
                result_hash="",
                gate_result=gate_result,
                elapsed_ms=0.0,
            )
            return

        args_hash = _hash_value(operation.parameters)
        t0 = _time.monotonic()

        await super()._execute_operation(operation, limiter)

        elapsed_ms = (_time.monotonic() - t0) * 1000.0
        result_hash = _hash_value(self.results.get(operation.id))

        self._controller.post_op_record(
            op_name,
            args_hash=args_hash,
            result_hash=result_hash,
            gate_result=gate_result,
            elapsed_ms=elapsed_ms,
        )


async def governed_flow(
    session: Session,
    graph: Graph,
    *,
    charter: Any = None,
    on_deny: str = _RAISE,
    branch: Branch = None,
    context: dict[str, Any] | None = None,
    parallel: bool = True,
    max_concurrent: int = None,
    verbose: bool = False,
    alcall_params: AlcallParams | None = None,
    on_progress: Any = None,
) -> tuple[dict[str, Any], TaskCertificate | None]:
    """Execute a flow graph with optional charter governance.

    Parameters
    ----------
    session:
        The Session that owns the graph branches.
    graph:
        Operation graph to execute.
    charter:
        CharterDocument, YAML string, or path to YAML file.
        ``None`` means no governance — the function behaves identically to
        ``Session.flow()`` and returns ``(result, None)``.
    on_deny:
        ``"raise"`` (default) — raises :exc:`GovernanceViolationError`.
        ``"skip"`` — marks the operation as skipped and continues.
    branch:
        Default branch for single-branch operations.
    context:
        Initial flow-level context dict.
    parallel:
        Run independent operations in parallel (default True).
    max_concurrent:
        Maximum concurrent operations.
    verbose:
        Enable verbose logging.
    alcall_params:
        Parameters for the async parallel call harness.
    on_progress:
        Optional ``(op_id, branch_name, status, elapsed_s)`` callback.

    Returns
    -------
    tuple[dict, TaskCertificate | None]
        ``(flow_result, certificate)`` where ``certificate`` is ``None``
        when *charter* is ``None``.
    """
    if charter is None:
        result = await _ungoverned_flow(
            session=session,
            graph=graph,
            branch=branch,
            context=context,
            parallel=parallel,
            max_concurrent=max_concurrent,
            verbose=verbose,
            alcall_params=alcall_params,
            on_progress=on_progress,
        )
        return result, None

    session_id = str(session.id)
    controller = GovernedFlowController(charter=charter, session_id=session_id)

    if not parallel:
        max_concurrent = 1

    executor = _GovernedExecutor(
        controller=controller,
        on_deny=on_deny,
        session=session,
        graph=graph,
        context=context,
        max_concurrent=max_concurrent,
        verbose=verbose,
        default_branch=branch,
        alcall_params=alcall_params or AlcallParams(),
    )
    if on_progress is not None:
        executor.on_progress = on_progress

    result = await executor.execute()
    certificate = controller.mint_certificate()
    return result, certificate
