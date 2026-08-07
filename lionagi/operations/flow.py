# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dependency-aware graph execution. New graph-execution surfaces must
delegate through ``Session.flow`` or the existing streaming flow kernel,
not build their own executor."""

import asyncio
import contextlib
import contextvars
import logging
import math
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import anyio
import sniffio

from lionagi._errors import ExecutionError, OperationError
from lionagi.ln import AlcallParams
from lionagi.ln.concurrency import (
    CapacityLimiter,
    ConcurrencyEvent,
    create_task_group,
    get_cancelled_exc_class,
)
from lionagi.models.note import Note
from lionagi.operations.node import Operation, create_operation
from lionagi.protocols.generic.event import Event
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.types import EventStatus
from lionagi.utils import to_dict

if TYPE_CHECKING:
    from lionagi.protocols.graph.graph import Graph
    from lionagi.session.session import Branch, Session
    from lionagi.session.signal import Signal


logger = logging.getLogger(__name__)

UNLIMITED_CONCURRENCY = int(os.environ.get("LIONAGI_MAX_CONCURRENCY", "10000"))

# Gate-reject contract (issue #2860). A playbook-authored node opts into gate
# semantics by setting ``operation.metadata["is_gate"] = True`` (e.g. via
# ``OperationGraphBuilder.add_operation(..., is_gate=True)``). Once that node
# completes, its result is inspected for a top-level ``"gate_verdict"`` key;
# if the value is the string "reject" (case-insensitive), every direct and
# transitive dependent of the gate is short-circuited to SKIPPED instead of
# running against the baseline the gate just rejected. A node that isn't
# marked ``is_gate``, or a gate whose result has no ``gate_verdict`` key (or
# any value other than "reject"), changes nothing -- this keeps flows with no
# gate nodes byte-identical to before.
GATE_VERDICT_KEY = "gate_verdict"
GATE_VERDICT_REJECT = "reject"
SKIP_REASON_UPSTREAM_GATE_REJECT = "upstream_gate_reject"

# Tracks which Operation a reactive task is running (per-task contextvar).
_CURRENT_OP: contextvars.ContextVar = contextvars.ContextVar("reactive_current_op", default=None)

_OPERATOR_STEER_TEMPLATE = """\
[OPERATOR STEER]
A human operator sent these live corrections while this flow is running.
Attend to them before continuing. Most recent last.
{lines}
[/OPERATOR STEER]

"""


def _format_operator_ts(ts: Any) -> str:
    import datetime

    try:
        dt = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _render_operator_messages(operation: "Operation", context: dict[str, Any]) -> None:
    """Lift ``operator_messages`` out of context and render unconsumed
    entries into the instruction. Always pops the key so it never rides
    along as raw JSON; consume-once via the ``rendered_into_op`` breadcrumb."""
    messages = context.pop("operator_messages", None)
    if not messages:
        return

    pending = [m for m in messages if isinstance(m, dict) and not m.get("rendered_into_op")]
    if not pending:
        return

    lines = "\n".join(f"- {_format_operator_ts(m.get('ts'))}: {m.get('text', '')}" for m in pending)
    block = _OPERATOR_STEER_TEMPLATE.format(lines=lines)

    instruction = operation.parameters.get("instruction")
    instruction = "" if instruction is None else str(instruction)
    operation.parameters["instruction"] = block + instruction

    op_id = str(operation.id)
    for m in pending:
        m["rendered_into_op"] = op_id
    operation.metadata["rendered_into_op"] = op_id


@dataclass(slots=True)
class FlowEvent:
    """One operation's completion event."""

    operation_id: str
    name: str
    status: str  # "completed" | "failed" | "skipped"
    result: Any
    spawned: bool = False  # True if this node was injected mid-run

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class DependencyAwareExecutor:
    """Executes operation graphs with dependency management and context inheritance."""

    def __init__(
        self,
        session: "Session",
        graph: "Graph",
        context: dict[str, Any] | None = None,
        max_concurrent: int = 5,
        verbose: bool = False,
        default_branch: "Branch" = None,
        alcall_params: AlcallParams | None = None,
        executor_ref: dict[str, Any] | None = None,
        on_branch_created: Callable[[Any], None] | None = None,
    ):
        self.session = session
        self.graph = graph
        self.context: Note = Note(**(context or {}))
        self.max_concurrent = max_concurrent
        self.verbose = verbose
        self._alcall = alcall_params or AlcallParams()
        self._default_branch = default_branch
        self.on_progress = None
        # Persistence-only seam: invoked with every branch cloned during
        # pre-allocation, so a caller can wire persistence onto branches that
        # didn't exist when it set up the session's initial ones.
        self._on_branch_created = on_branch_created
        self.results = {}
        self.completion_events = {}
        self.operation_branches = {}
        self.skipped_operations = set()
        # Distinct from skipped_operations: an op whose invoke() raised, kept
        # separate from completed_operations (which still includes it, for
        # back-compat -- a FAILED op still produced an (error) result) so a
        # caller can tell a dead node from a genuine completion without
        # inspecting every result value by hand.
        self.failed_operations = set()
        # Gate-reject bookkeeping (see the module-level contract comment).
        # ``_gate_rejections``: op_id of a completed ``is_gate`` node -> the
        # reason payload to attribute to anything downstream of it.
        # ``_skip_reasons``: op_id of any node this executor decided to skip
        # because of a (possibly transitive) upstream gate reject -> the same
        # payload, so a grandchild inherits the original gate's identity
        # rather than just "my parent was skipped".
        self._gate_rejections: dict[Any, dict[str, Any]] = {}
        self._skip_reasons: dict[Any, dict[str, Any]] = {}
        self._op_start_times = {}
        # Identity bookkeeping so every started operation gets exactly one
        # terminal on_progress signal, whichever exit path it takes.
        self._started_ops: set = set()
        self._terminal_emitted: set = set()
        self._pause_event: ConcurrencyEvent | None = None
        # Fire-and-forget flow signal tasks, retained until each finishes so a
        # weakly referenced task can't disappear before it runs.
        self._signal_tasks: set[asyncio.Task[Any]] = set()
        # AnyIO task group backing the current run, if any — lets
        # _emit_best_effort() schedule through anyio (Trio-safe) instead of
        # the asyncio-only loop.create_task() fallback.
        self._tg: Any = None
        # Set synchronously so a control poller can reach pause()/resume() the
        # instant execute() starts.
        if executor_ref is not None:
            executor_ref["executor"] = self
        for node in graph.internal_nodes.values():
            if isinstance(node, Operation):
                self.completion_events[node.id] = ConcurrencyEvent()

                # If operation is already completed, mark it and store results
                if node.execution.status == EventStatus.COMPLETED:
                    self.completion_events[node.id].set()
                    if hasattr(node, "response"):
                        self.results[node.id] = node.response

    async def execute(self) -> dict[str, Any]:
        if not self.graph.is_acyclic():
            raise OperationError("Graph must be acyclic for flow execution")

        self._validate_edge_conditions()
        await self._preallocate_all_branches()

        capacity = self.max_concurrent if self.max_concurrent is not None else UNLIMITED_CONCURRENCY
        limiter = CapacityLimiter(capacity)

        nodes = [n for n in self.graph.internal_nodes.values() if isinstance(n, Operation)]
        if self.on_progress:
            for node in nodes:
                _name = node.metadata.get("reference_id", str(node.id)[:8])
                self.on_progress(str(node.id), _name, "queued", 0.0)
        async with create_task_group() as tg:
            self._tg = tg
            try:
                await self._alcall(nodes, self._execute_operation, limiter=limiter)
            finally:
                self._tg = None

        completed_ops = [
            op_id for op_id in self.results.keys() if op_id not in self.skipped_operations
        ]

        result = {
            "completed_operations": completed_ops,
            "operation_results": self.results,
            "final_context": self.context.content,
            "skipped_operations": list(self.skipped_operations),
            "failed_operations": list(self.failed_operations),
            **self._gate_result_fields(),
        }

        self._validate_execution_results(result)

        return result

    def _gate_result_fields(self) -> dict[str, Any]:
        """``gate_rejected_operations``: ``is_gate`` nodes whose result carried
        a REJECT verdict. ``gate_short_circuited_operations``: nodes skipped
        (directly or transitively) as a consequence -- a subset of
        ``skipped_operations``. Both empty when no gate ever rejected, so a
        caller checking ``bool(result["gate_rejected_operations"])`` sees no
        change for flows with no gate nodes."""
        return {
            "gate_rejected_operations": [str(op_id) for op_id in self._gate_rejections],
            "gate_short_circuited_operations": [
                str(op_id) for op_id in self._skip_reasons if op_id in self.skipped_operations
            ],
        }

    async def _preallocate_all_branches(self):
        """Pre-allocate branches to eliminate runtime locking."""
        operations_needing_branches = []
        for node in self.graph.internal_nodes.values():
            if not isinstance(node, Operation):
                continue

            if node.branch_id:
                try:
                    branch = self.session.branches[node.branch_id]
                    self.operation_branches[node.id] = branch
                except Exception:
                    logger.debug(
                        "Branch %s not found in session for node %s; "
                        "will be assigned during execution.",
                        node.branch_id,
                        node.id,
                    )
                continue

            predecessors = self._get_predecessors(node)
            if predecessors or node.metadata.get("inherit_context"):
                operations_needing_branches.append(node)

        if not operations_needing_branches:
            return

        async with self.session.branches.async_lock:
            for operation in operations_needing_branches:
                branch_clone = self.session.default_branch.clone(sender=self.session.id)
                self.operation_branches[operation.id] = branch_clone
                try:
                    if hasattr(branch_clone, "id"):
                        branch_id = branch_clone.id
                        if isinstance(branch_id, str | UUID) or (
                            hasattr(branch_id, "__str__") and not hasattr(branch_id, "_mock_name")
                        ):
                            # Full session wiring (owner marker, observer,
                            # exchange), not a bare Pile insert that would
                            # leave the clone claimable by another session.
                            self.session.include_branches(branch_clone)
                except Exception:
                    logger.debug("Skipping branch clone registration (likely mock in test).")

                if self._on_branch_created is not None:
                    self._on_branch_created(branch_clone)

                if operation.metadata.get("inherit_context"):
                    branch_clone.metadata = branch_clone.metadata or {}
                    branch_clone.metadata["pending_context_inheritance"] = True
                    branch_clone.metadata["inherit_from_operation"] = operation.metadata.get(
                        "primary_dependency"
                    )

        if self.verbose:
            logger.debug("Pre-allocated %d branches", len(operations_needing_branches))

    def _get_predecessors(self, operation: Operation) -> tuple[Any, ...]:
        """Return a cached, immutable predecessor tuple for executor-internal
        use. Delegates to Graph's own memoized accessor, invalidated by
        Graph's own mutators, so this always reflects current topology."""
        return self.graph.get_predecessors_cached(operation)

    def pause(self) -> None:
        """Install a pause gate at the next operation boundary; idempotent."""
        if self._pause_event is None:
            self._pause_event = ConcurrencyEvent()

    def resume(self) -> None:
        """Release the pause gate; idempotent. A later pause() installs a fresh event."""
        if self._pause_event is not None:
            self._pause_event.set()
            self._pause_event = None

    def _emit_best_effort(self, factory: Callable[[], "Signal"]) -> None:
        """Build and schedule a fire-and-forget flow signal on the session
        bus. `factory` (not a pre-built signal) keeps construction inside
        this failure-isolation boundary; every failure mode is logged and
        never changes the caller's outcome — delivery is best-effort."""
        try:
            sig = factory()
        except Exception as e:  # noqa: BLE001
            logger.warning("flow signal construction failed: %s", e)
            return

        async def _emit() -> None:
            try:
                await self.session.emit(sig)
            except Exception as e:  # noqa: BLE001
                logger.warning("flow signal emission failed for %s: %s", type(sig).__name__, e)

        if self._tg is not None:
            # A flow run is in progress: schedule through its anyio task
            # group, which works under both asyncio and Trio (the raw
            # asyncio loop below only ever runs under asyncio).
            try:
                self._tg.start_soon(_emit)
            except Exception as e:  # noqa: BLE001
                logger.warning("flow signal scheduling failed for %s: %s", type(sig).__name__, e)
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop — tests / sync contexts, not a failure

        coro = _emit()
        try:
            task = loop.create_task(coro)
        except Exception as e:  # noqa: BLE001
            coro.close()
            logger.warning("flow signal scheduling failed for %s: %s", type(sig).__name__, e)
            return

        self._signal_tasks.add(task)
        task.add_done_callback(self._signal_tasks.discard)

    def _emit_paused(self, operation: Operation) -> None:
        """Fire-and-forget NodePaused onto the session bus."""

        def _factory() -> "Signal":
            from lionagi.session.signal import NodePaused  # noqa: PLC0415

            op_id = str(operation.id)
            name = operation.metadata.get("reference_id", op_id[:8])
            return NodePaused(op_id=op_id, name=name)

        self._emit_best_effort(_factory)

    def _emit_terminal_once(
        self, operation: Operation, branch_name: str, status: str, elapsed: float
    ) -> None:
        """Emit a terminal on_progress signal for `operation` at most once.

        Every call site that reaches a terminal outcome (completed, failed,
        skipped, cancelled, or an unexpected flow-level error) goes through
        here, so a race between two exit paths for the same operation can
        never announce two terminal signals for one identity.
        """
        if operation.id in self._terminal_emitted:
            return
        self._terminal_emitted.add(operation.id)
        if self.on_progress:
            self.on_progress(str(operation.id), branch_name, status, elapsed)

    def _emit_abandoned_terminal(self, operation: Operation) -> None:
        """Safety net for cancellation and unexpected flow-level errors.

        If `operation` already emitted "started" but execution left
        `_execute_operation` through the CancelledError or generic-Exception
        handler without going through the normal completed/failed paths, the
        node would otherwise render as perpetually running. Emit a "failed"
        terminal (there is no separate cancelled/abandoned signal kind) so
        every start still gets exactly one terminal outcome. No-op if this
        operation never started, or a terminal was already emitted for it.
        """
        if operation.id not in self._started_ops:
            return
        if operation.id in self._terminal_emitted:
            return
        import time as _time

        ref_id = operation.metadata.get("reference_id", str(operation.id)[:8])
        branch = self.operation_branches.get(operation.id, self.session.default_branch)
        branch_name = getattr(branch, "name", None) or ref_id
        elapsed = _time.monotonic() - self._op_start_times.get(operation.id, _time.monotonic())
        self._emit_terminal_once(operation, branch_name, "failed", elapsed)

    async def _execute_operation(self, operation: Operation, limiter: CapacityLimiter):
        if operation.execution.status in Event._TERMINAL_STATUSES:
            if self.verbose:
                logger.debug(
                    "Skipping %s operation: %s",
                    operation.execution.status.value,
                    str(operation.id)[:8],
                )
            if operation.id not in self.results and operation.response is not None:
                self.results[operation.id] = operation.response
            self.completion_events[operation.id].set()
            return

        try:
            should_execute = await self._check_edge_conditions(operation)

            if not should_execute:
                operation.execution.status = EventStatus.SKIPPED
                self.skipped_operations.add(operation.id)

                gate_reason = self._skip_reasons.get(operation.id)
                if gate_reason is not None:
                    # Visible, not silently absent: the metadata is there for
                    # any caller walking `graph.internal_nodes`, and the same
                    # payload lands in operation_results so it shows up
                    # wherever a completed operation's result would.
                    operation.metadata["skip_reason_code"] = gate_reason["reason_code"]
                    operation.metadata["skip_reason_gate_id"] = gate_reason["gate_id"]
                    operation.metadata["skip_reason_gate_name"] = gate_reason["gate_name"]
                    self.results[operation.id] = {
                        "skipped": True,
                        "reason_code": gate_reason["reason_code"],
                        "gate_id": gate_reason["gate_id"],
                        "gate_name": gate_reason["gate_name"],
                    }

                if self.verbose:
                    logger.debug(
                        "Skipping operation due to edge conditions: %s",
                        str(operation.id)[:8],
                    )

                ref_id = operation.metadata.get("reference_id", str(operation.id)[:8])
                branch = self.operation_branches.get(operation.id, self.session.default_branch)
                branch_name = getattr(branch, "name", None) or ref_id
                self._emit_terminal_once(operation, branch_name, "failed", 0.0)

                self.completion_events[operation.id].set()
                return

            await self._wait_for_dependencies(operation)

            # Soft pause at the operation boundary: ops already past this point
            # (inside the limiter) run to completion; nothing new starts while
            # a gate is installed. Each loop iteration binds a distinct gate
            # instance, so a resume followed by a fresh pause re-emits and
            # re-waits correctly.
            while (gate := self._pause_event) is not None:
                self._emit_paused(operation)
                await gate.wait()

            async with limiter:
                self._prepare_operation(operation)

                ref_id = operation.metadata.get("reference_id", str(operation.id)[:8])
                branch = self.operation_branches.get(operation.id, self.session.default_branch)
                branch_name = getattr(branch, "name", None) or ref_id

                import time as _time

                self._op_start_times[operation.id] = _time.monotonic()
                self._started_ops.add(operation.id)

                if self.on_progress:
                    self.on_progress(str(operation.id), branch_name, "started", 0)
                if self.verbose:
                    logger.debug("Executing operation: %s", ref_id)

                operation._branch = branch
                self._render_pending_operator_steers(operation)
                await operation.invoke()

                elapsed = _time.monotonic() - self._op_start_times.get(
                    operation.id, _time.monotonic()
                )

                if operation.execution.status == EventStatus.COMPLETED:
                    self.results[operation.id] = operation.response

                    # Deep-merge operation context into flow workspace to preserve nested keys.
                    if isinstance(operation.response, dict) and "context" in operation.response:
                        from lionagi.libs.nested import deep_update

                        response_context = operation.response["context"]
                        if not isinstance(response_context, Mapping):
                            error = TypeError(
                                f"Operation {ref_id} response['context'] must be a Mapping, "
                                f"got {type(response_context).__name__}."
                            )
                            operation.execution.status = EventStatus.FAILED
                            operation.execution.error = error
                            self.results[operation.id] = {"error": str(error)}
                            self.failed_operations.add(operation.id)
                            self._emit_terminal_once(operation, branch_name, "failed", elapsed)
                            if self.verbose:
                                logger.error(
                                    "Operation %s failed (%.1fs): %s",
                                    ref_id,
                                    elapsed,
                                    error,
                                )
                            return

                        deep_update(self.context.content, dict(response_context))

                    self._emit_terminal_once(operation, branch_name, "completed", elapsed)
                    if self.verbose:
                        logger.debug("Completed operation: %s (%.1fs)", ref_id, elapsed)

                    if operation.metadata.get("is_gate"):
                        self._record_gate_verdict(operation)

                elif operation.execution.status == EventStatus.FAILED:
                    self.results[operation.id] = {"error": str(operation.execution.error)}
                    self.failed_operations.add(operation.id)
                    self._emit_terminal_once(operation, branch_name, "failed", elapsed)
                    if self.verbose:
                        logger.error(
                            "Operation %s failed (%.1fs): %s",
                            ref_id,
                            elapsed,
                            operation.execution.error,
                        )

        except (get_cancelled_exc_class(), KeyboardInterrupt, SystemExit):
            self.completion_events[operation.id].set()
            # Cancellation (task-group teardown, timeout, abandonment) skips
            # the normal completed/failed paths above; emit the terminal
            # this started operation is still owed so it never renders as
            # perpetually running.
            self._emit_abandoned_terminal(operation)
            raise

        except Exception as e:
            # Defensive net for unexpected flow-level errors; invoke() already handles FAILED status.
            if operation.id not in self.results:
                self.results[operation.id] = {"error": str(e)}
            self.failed_operations.add(operation.id)

            if self.verbose:
                logger.error("Operation %s failed: %s", str(operation.id)[:8], e)

            self._emit_abandoned_terminal(operation)

        finally:
            self.completion_events[operation.id].set()

    def _record_gate_verdict(self, operation: Operation) -> None:
        """Called on a completed ``is_gate`` operation; records a REJECT
        verdict (see the module-level gate-reject contract) so
        ``_check_edge_conditions`` can veto this gate's dependents. A result
        with no ``gate_verdict`` key, or any value other than "reject",
        leaves ``_gate_rejections`` untouched -- the gate completed normally
        and nothing downstream is affected."""
        result = operation.response
        if result is not None and not isinstance(result, str | int | float | bool):
            result = to_dict(result, recursive=True)
        if not isinstance(result, Mapping):
            return

        verdict = result.get(GATE_VERDICT_KEY)
        if not isinstance(verdict, str) or verdict.strip().lower() != GATE_VERDICT_REJECT:
            return

        ref_id = operation.metadata.get("reference_id", str(operation.id)[:8])
        self._gate_rejections[operation.id] = {
            "reason_code": SKIP_REASON_UPSTREAM_GATE_REJECT,
            "gate_id": str(operation.id),
            "gate_name": ref_id,
        }

    async def _check_edge_conditions(self, operation: Operation) -> bool:
        """Return True if at least one valid incoming path exists or no edges; False if all incoming edges failed.

        A gate reject is an absolute veto layered on top of that: if any
        incoming edge traces back (directly or transitively) to a rejecting
        gate, this operation is skipped regardless of any other otherwise
        valid incoming path -- a node must never run against a baseline one
        of its ancestors just rejected. The veto is recorded in
        ``self._skip_reasons`` so a caller skipped for this reason (and, in
        turn, that caller's own dependents) can be tagged and so this
        propagates transitively via the existing `skipped_operations` check
        below without extra bookkeeping.
        """
        # Snapshot before awaiting: iterating the live adjacency dict across
        # an await would raise RuntimeError if reactive injection attaches an
        # edge mid-wait. A dependency added after the snapshot is deferred
        # to the next check.
        incoming_edge_ids = tuple(self.graph.node_edge_mapping[operation.id]["in"])
        if not incoming_edge_ids:
            return True

        has_valid_path = False
        gate_reason: dict[str, Any] | None = None

        # Every incoming edge must be inspected before honoring a valid path:
        # the veto is "any incoming path through a rejected gate", not "the
        # first-listed valid edge wins" -- a node with a valid non-gate edge
        # listed before its rejected-gate edge must still be vetoed, so this
        # never exits early on `has_valid_path=True`.
        for edge_id in incoming_edge_ids:
            edge = self.graph.internal_edges[edge_id]
            if edge.head in self.completion_events:
                await self.completion_events[edge.head].wait()

            upstream_reason = self._skip_reasons.get(edge.head) or self._gate_rejections.get(
                edge.head
            )
            if upstream_reason is not None:
                gate_reason = gate_reason or upstream_reason
                continue

            if edge.head in self.skipped_operations:
                continue

            if has_valid_path:
                # Already know this node has a valid path; still scanning
                # remaining edges for a possible gate veto, so skip the
                # redundant condition evaluation.
                continue

            result_value = self.results.get(edge.head)
            if result_value is not None and not isinstance(result_value, str | int | float | bool):
                result_value = to_dict(result_value, recursive=True)

            # apply() expects a plain dict (dict.get() semantics); pass Note.content not the Note itself.
            ctx = {"result": result_value, "context": self.context.content}

            if await edge.check_condition(ctx):
                has_valid_path = True

        if gate_reason is not None:
            self._skip_reasons[operation.id] = gate_reason
            return False

        return has_valid_path

    async def _wait_for_dependencies(self, operation: Operation):
        """Wait for all dependencies to complete."""
        if operation.metadata.get("aggregation"):
            sources = operation.metadata.get("aggregation_sources", [])
            if self.verbose and sources:
                logger.debug(
                    "Aggregation %s waiting for %d sources",
                    str(operation.id)[:8],
                    len(sources),
                )

            # sources are strings from builder.py — convert back to UUID for completion_events lookup
            for source_id_str in sources:
                for op_id in self.completion_events.keys():
                    if str(op_id) == source_id_str:
                        await self.completion_events[op_id].wait()
                        break

        predecessors = self._get_predecessors(operation)
        for pred in predecessors:
            if self.verbose:
                logger.debug(
                    "Operation %s waiting for %s",
                    str(operation.id)[:8],
                    str(pred.id)[:8],
                )
            await self.completion_events[pred.id].wait()

    def _prepare_operation(self, operation: Operation):
        """Prepare operation with context and branch assignment."""
        predecessors = self._get_predecessors(operation)
        if predecessors:
            pred_ctx = Note()
            for pred in predecessors:
                if pred.id in self.skipped_operations:
                    continue

                if pred.id in self.results:
                    result = self.results[pred.id]
                    if result is not None and not isinstance(result, str | int | float | bool):
                        result = to_dict(result, recursive=True)
                    pred_ctx[f"{str(pred.id)}_result"] = result

            pred_context = pred_ctx.content
            if "context" not in operation.parameters:
                operation.parameters["context"] = pred_context
            else:
                existing_context = operation.parameters["context"]
                if isinstance(existing_context, dict):
                    existing_context.update(pred_context)
                else:
                    operation.parameters["context"] = {
                        "original_context": existing_context,
                        **pred_context,
                    }

        if self.context:
            if "context" not in operation.parameters:
                operation.parameters["context"] = self.context.content.copy()
            else:
                existing_context = operation.parameters["context"]
                if isinstance(existing_context, dict):
                    existing_context.update(self.context.content)
                else:
                    operation.parameters["context"] = {
                        "original_context": existing_context,
                        **self.context.content,
                    }

        context = operation.parameters.get("context")
        if isinstance(context, dict):
            _render_operator_messages(operation, context)

        branch = self._resolve_branch_for_operation(operation)
        self.operation_branches[operation.id] = branch

    def _render_pending_operator_steers(self, operation: Operation) -> None:
        """Last-chance render, called immediately before the provider call —
        catches a steer landing in ``operator_messages`` after this
        operation's own ``_prepare_operation`` already ran (e.g. a
        control-plane poller appending mid-run)."""
        messages = self.context.content.get("operator_messages")
        if not messages:
            return
        if not any(isinstance(m, dict) and not m.get("rendered_into_op") for m in messages):
            return

        context = operation.parameters.get("context")
        if not isinstance(context, dict):
            context = {}
            operation.parameters["context"] = context
        context["operator_messages"] = messages
        _render_operator_messages(operation, context)

    def _resolve_branch_for_operation(self, operation: Operation) -> "Branch":
        """Resolve which branch an operation should use - all branches are pre-allocated."""
        if operation.id in self.operation_branches:
            branch = self.operation_branches[operation.id]

            if (
                hasattr(branch, "metadata")
                and branch.metadata
                and branch.metadata.get("pending_context_inheritance")
            ):
                primary_dep_id = branch.metadata.get("inherit_from_operation")
                if primary_dep_id and primary_dep_id in self.results:
                    primary_branch = self.operation_branches.get(
                        primary_dep_id, self.session.default_branch
                    )

                    # Copy messages without creating a new branch to avoid locking.
                    if hasattr(branch, "_message_manager") and hasattr(
                        primary_branch, "_message_manager"
                    ):
                        branch._message_manager.messages.clear()
                        for msg in primary_branch._message_manager.messages:
                            if hasattr(msg, "clone"):
                                branch._message_manager.messages.append(msg.clone())
                            else:
                                branch._message_manager.messages.append(msg)

                    branch.metadata["pending_context_inheritance"] = False

                    if self.verbose:
                        logger.debug(
                            "Operation %s inherited context from %s",
                            str(operation.id)[:8],
                            str(primary_dep_id)[:8],
                        )

            return branch

        if self.verbose:
            logger.warning(
                "Operation %s using default branch (not pre-allocated)",
                str(operation.id)[:8],
            )

        if hasattr(self, "_default_branch") and self._default_branch:
            return self._default_branch
        return self.session.default_branch

    def _validate_edge_conditions(self):
        """Validate that all edge conditions are properly configured."""
        for edge in self.graph.internal_edges.values():
            if edge.condition is not None:
                from lionagi.protocols._concepts import Condition

                if not isinstance(edge.condition, Condition):
                    raise TypeError(
                        f"Edge {edge.id} has invalid condition type: {type(edge.condition)}. "
                        "Must be a Condition subclass or None."
                    )

                if not hasattr(edge.condition, "apply"):
                    raise AttributeError(f"Edge {edge.id} condition missing 'apply' method.")

    def _validate_execution_results(self, results: dict[str, Any]):
        """Validate execution results for consistency."""
        completed = set(results.get("completed_operations", []))
        skipped = set(results.get("skipped_operations", []))

        overlap = completed & skipped
        if overlap:
            raise ExecutionError(
                f"Operations {overlap} appear in both completed and skipped lists! "
                "This indicates a bug in edge condition handling."
            )

        for node in self.graph.internal_nodes.values():
            if isinstance(node, Operation) and node.id in skipped:
                if node.execution.status != EventStatus.SKIPPED:
                    if self.verbose:
                        logger.warning(
                            "Skipped operation %s has status %s instead of SKIPPED",
                            node.id,
                            node.execution.status,
                        )


def _extract_spawn_requests(response: Any, spawn_type: type) -> list[Any]:
    """Extract SpawnRequest instances from a response (direct, list, or BaseModel/dict field values)."""
    from pydantic import BaseModel

    found: list[Any] = []

    def _visit(x: Any, depth: int = 0) -> None:
        if x is None or depth > 4:
            return
        if isinstance(x, spawn_type):
            found.append(x)
            return
        if isinstance(x, list | tuple):
            for item in x:
                _visit(item, depth + 1)
            return
        if isinstance(x, BaseModel):
            for v in x.__dict__.values():
                _visit(v, depth + 1)
            return
        if isinstance(x, dict):
            for v in x.values():
                _visit(v, depth + 1)

    _visit(response)
    return found


class ReactiveExecutor(DependencyAwareExecutor):
    """Self-expanding DAG executor: running ops may emit SpawnRequests to grow the graph."""

    def __init__(
        self,
        *args: Any,
        spawn_type: type | None = None,
        node_builder: Any = None,
        max_spawn: int = 50,
        on_branch_created: Callable[[Any], None] | None = None,
        spawn_branch_setup: Callable[[Operation, Any], None] | None = None,
        on_op_complete: Callable[[Operation], None] | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, on_branch_created=on_branch_created, **kwargs)
        if spawn_type is None:
            from lionagi.casts.emission import SpawnRequest

            spawn_type = SpawnRequest
        self.spawn_type = spawn_type
        self.node_builder = node_builder
        self.max_spawn = max_spawn
        # CLI-workspace seam: called with (spawned_operation, cloned_branch)
        # right after a spawned node's branch is cloned, so a caller can
        # retarget a CLI-backed chat_model's writable workspace to that
        # spawn's own artifact dir (the clone otherwise inherits the emitter's).
        self.spawn_branch_setup = spawn_branch_setup
        # Fired once per node at the tail of _run_tracked, before that task
        # returns to the task group — the only race-free point for inject().
        self.on_op_complete = on_op_complete
        self._spawn_count = 0
        self._dropped_spawns: list[dict[str, Any]] = []
        self._running = False
        self._graph_lock = threading.Lock()
        self._seen_reqs: set[int] = set()
        self._spawned_ids: set[Any] = set()
        self._result_sink: Any = None
        self._escalated_ids: set[Any] = set()

    async def execute(self) -> dict[str, Any]:
        if not self.graph.is_acyclic():
            raise OperationError("Graph must be acyclic for flow execution")
        self._validate_edge_conditions()
        await self._preallocate_all_branches()

        capacity = self.max_concurrent if self.max_concurrent is not None else UNLIMITED_CONCURRENCY
        self._limiter = CapacityLimiter(capacity)

        initial = [n for n in self.graph.internal_nodes.values() if isinstance(n, Operation)]
        self._running = True
        observer = self.session.observer
        from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

        self.session.observe(self.spawn_type, self._on_bus_spawn)
        self.session.observe(EscalationRequest, self._on_bus_escalation)
        try:
            async with create_task_group() as tg:
                self._tg = tg
                for node in initial:
                    if self.on_progress:
                        _name = node.metadata.get("reference_id", str(node.id)[:8])
                        self.on_progress(str(node.id), _name, "queued", 0.0)
                    tg.start_soon(self._run_tracked, node)
        finally:
            self._running = False
            self._tg = None
            observer.unobserve(self._on_bus_spawn)
            observer.unobserve(self._on_bus_escalation)

        completed_ops = [
            op_id for op_id in self.results.keys() if op_id not in self.skipped_operations
        ]
        result = {
            "completed_operations": completed_ops,
            "operation_results": self.results,
            "final_context": self.context.content,
            "skipped_operations": list(self.skipped_operations),
            "failed_operations": list(self.failed_operations),
            "spawned_operations": self._spawn_count,
            # The roster of every node _accept_node actually accepted into
            # the graph -- distinct from spawned_operations, which is only a
            # running count and can't tell a caller which ids to reconcile
            # against its own outcome sets (a node that reached a terminal
            # status like CANCELLED with no result is still in this roster
            # even though it never lands in results/failed/skipped).
            "spawned_ids": list(self._spawned_ids),
            "escalated_operations": list(self._escalated_ids),
            "dropped_spawns": self._dropped_spawns,
            **self._gate_result_fields(),
        }
        self._validate_execution_results(result)
        return result

    async def execute_stream(self):
        """Yield a FlowEvent the instant each operation completes."""
        if not self.graph.is_acyclic():
            raise OperationError("Graph must be acyclic for flow execution")
        self._validate_edge_conditions()
        await self._preallocate_all_branches()

        capacity = self.max_concurrent if self.max_concurrent is not None else UNLIMITED_CONCURRENCY
        self._limiter = CapacityLimiter(capacity)
        send, recv = anyio.create_memory_object_stream(math.inf)
        self._result_sink = send

        initial = [n for n in self.graph.internal_nodes.values() if isinstance(n, Operation)]
        observer = self.session.observer
        from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

        self.session.observe(self.spawn_type, self._on_bus_spawn)
        self.session.observe(EscalationRequest, self._on_bus_escalation)
        self._running = True

        driver_cancel_scope = anyio.CancelScope()
        driver_done = anyio.Event()
        driver_errors: list[BaseException] = []

        async def _driver():
            # Owns its own task group (entered/exited in THIS task). The
            # generator must not span a task group across `yield` — anyio forbids
            # it — so the driver runs detached and the generator only drains the
            # channel. Closing `send` on completion ends the consumer's loop.
            with driver_cancel_scope:
                try:
                    async with create_task_group() as tg:
                        self._tg = tg
                        for node in initial:
                            if self.on_progress:
                                _name = node.metadata.get("reference_id", str(node.id)[:8])
                                self.on_progress(str(node.id), _name, "queued", 0.0)
                            tg.start_soon(self._run_tracked, node)
                except get_cancelled_exc_class():
                    raise  # let driver_cancel_scope absorb our own cancellation
                except BaseException as e:  # noqa: BLE001
                    driver_errors.append(e)
                finally:
                    await send.aclose()
            driver_done.set()

        # The driver needs a detached task: the generator must outlive any single
        # anyio task-group scope, which is unsafe on Trio once the consumer can
        # close the generator early. See docs/internals/providers.md#flow-stream-driver-task.
        if sniffio.current_async_library() == "trio":
            import trio  # noqa: PLC0415

            trio.lowlevel.spawn_system_task(_driver)
        else:
            asyncio.ensure_future(_driver())
        try:
            async with recv:
                async for event in recv:
                    yield event
            await driver_done.wait()  # normal end: surface any driver exception
        finally:
            self._running = False
            self._tg = None
            self._result_sink = None
            observer.unobserve(self._on_bus_spawn)
            observer.unobserve(self._on_bus_escalation)
            if not driver_done.is_set():  # early break / consumer close: tear down
                driver_cancel_scope.cancel()
                with contextlib.suppress(get_cancelled_exc_class(), Exception):
                    await driver_done.wait()
        if driver_errors:
            raise driver_errors[0]

    async def _run_tracked(self, node: Operation) -> None:
        token = _CURRENT_OP.set(node)
        try:
            await self._execute_operation(node, self._limiter)
        finally:
            _CURRENT_OP.reset(token)
        if self._result_sink is not None:
            self._result_sink.send_nowait(self._make_event(node))
        from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

        for req in _extract_spawn_requests(self.results.get(node.id), self.spawn_type):
            self._inject_request(req, emitter=node)
        for req in _extract_spawn_requests(self.results.get(node.id), EscalationRequest):
            self._schedule_escalation(req, emitter=node)

        if self.on_op_complete is not None:
            # Best-effort, and still inside the task group tracking this
            # coroutine — a caller's inject() here is never rejected.
            try:
                self.on_op_complete(node)
            except Exception as e:  # noqa: BLE001
                logger.warning("on_op_complete callback raised for %s: %s", str(node.id)[:8], e)

    def _make_event(self, node: Operation) -> FlowEvent:
        if node.id in self.skipped_operations:
            status = "skipped"
        elif node.execution.status == EventStatus.FAILED:
            status = "failed"
        else:
            status = "completed"
        return FlowEvent(
            operation_id=str(node.id),
            name=node.metadata.get("reference_id", str(node.id)[:8]),
            status=status,
            result=self.results.get(node.id),
            spawned=node.id in self._spawned_ids,
        )

    async def _on_bus_spawn(self, req: Any, _ctx: Any) -> None:
        if not self._running:
            return
        self._inject_request(req, emitter=_CURRENT_OP.get())

    async def _on_bus_escalation(self, req: Any, _ctx: Any) -> None:
        if not self._running:
            return
        self._schedule_escalation(req, emitter=_CURRENT_OP.get())

    def _schedule_escalation(self, req: Any, *, emitter: Operation | None) -> None:
        """Consume an EscalationRequest/help signal. An explicit
        ``context["route"]`` always wins; otherwise the route follows
        ``urgency`` — "blocked" defaults to "higher_tier" (retry), "fyi"
        defaults to "notify" (no retry; the emitter's completion is untouched)."""
        if id(req) in self._seen_reqs:
            return
        self._seen_reqs.add(id(req))

        context = getattr(req, "context", {}) or {}
        urgency = getattr(req, "urgency", "blocked")
        default_route = "higher_tier" if urgency == "blocked" else "notify"
        route = context.get("route", default_route)

        reason = getattr(req, "reason", "")
        emitter_id = emitter.id if emitter is not None else None
        op_id = str(emitter_id) if emitter_id is not None else ""
        name = emitter.metadata.get("reference_id", op_id[:8]) if emitter is not None else ""

        self._emit_node_escalated(op_id, name, reason, route, req)

        if route == "higher_tier" and emitter is not None and self._tg is not None:
            params = emitter.parameters if isinstance(emitter.parameters, dict) else {}
            original_instr = params.get("instruction", "")
            escalation_instr = f"[escalation] {reason}\nOriginal: {original_instr}"
            child_params = {
                **{k: v for k, v in params.items() if k != "instruction"},
                "instruction": escalation_instr,
            }
            child = create_operation(emitter.operation, parameters=child_params)
            child.metadata["escalated_from"] = op_id
            # Readable label for anything attributing this child's work back to the
            # node it retries (e.g. mirroring a CLI engine's transcript) — cheaper to
            # carry now than to re-derive `name` from a stale emitter reference later.
            child.metadata["escalated_from_name"] = name
            if self._accept_node(child, emitter_id=emitter_id, independent=True):
                self._escalated_ids.add(emitter_id)
        elif route == "notify":
            # Soft help signal: NodeEscalated already fired above for
            # observability; the node is NOT marked escalated (it still
            # completes on its own terms — orthogonal channel, not a
            # give-up/retry decision).
            pass
        else:
            if emitter_id is not None:
                self._escalated_ids.add(emitter_id)

    def _emit_node_escalated(
        self, op_id: str, name: str, reason: str, route: str, req: Any
    ) -> None:
        """Fire-and-forget NodeEscalated onto the session bus."""

        def _factory() -> "Signal":
            from lionagi.session.signal import NodeEscalated  # noqa: PLC0415

            return NodeEscalated(
                op_id=op_id,
                name=name,
                reason=reason,
                route=route,
                escalation_request=req,
            )

        self._emit_best_effort(_factory)

    def _record_dropped_spawn(
        self, reason: str, *, assignee: Any, emitter_id: Any, **extra: Any
    ) -> None:
        entry: dict[str, Any] = {"reason": reason, "assignee": assignee, "emitter_id": emitter_id}
        entry.update(extra)
        self._dropped_spawns.append(entry)

    def _inject_request(self, req: Any, *, emitter: Operation | None) -> bool:
        emitter_id = emitter.id if emitter is not None else None
        assignee = getattr(req, "assignee", None)
        if id(req) in self._seen_reqs:
            # The same req surfaced twice (bus emission + post-completion result
            # scan can both see it) — a de-dup, not a spawn failure; the first
            # sighting already ran.
            self._record_dropped_spawn("duplicate", assignee=assignee, emitter_id=emitter_id)
            return False
        self._seen_reqs.add(id(req))
        builder = self.node_builder or _default_node_builder
        try:
            child = builder(req, emitter)
        except Exception as e:
            logger.warning("spawn node_builder failed: %s", e)
            self._record_dropped_spawn(
                "builder_error", assignee=assignee, emitter_id=emitter_id, error=str(e)[:500]
            )
            return False
        if child is None:
            self._record_dropped_spawn("null_child", assignee=assignee, emitter_id=emitter_id)
            return False
        if self._accept_node(
            child, emitter_id=emitter_id, independent=getattr(req, "independent", False)
        ):
            self._tg.start_soon(self._run_tracked, child)
            return True
        return False

    def inject(
        self,
        operation: Operation,
        *,
        after: Operation | str | None = None,
        independent: bool = False,
    ) -> bool:
        """Schedule a pre-built operation into the running flow."""
        if not self._running or self._tg is None:
            logger.warning("inject() called while flow is not running; dropped")
            return False
        emitter_id = after.id if isinstance(after, Operation) else after
        if self._accept_node(operation, emitter_id=emitter_id, independent=independent):
            self._tg.start_soon(self._run_tracked, operation)
            return True
        return False

    def can_inject(self, count: int = 1) -> bool:
        """Return whether a batch can fit without exceeding the spawn cap."""
        if count < 0:
            raise ValueError("count must be non-negative")
        with self._graph_lock:
            return (
                self._running
                and self._tg is not None
                and self._spawn_count + count <= self.max_spawn
            )

    def _accept_node(
        self,
        child: Operation,
        *,
        emitter_id: Any,
        independent: bool,
    ) -> bool:
        with self._graph_lock:
            if self._spawn_count >= self.max_spawn:
                logger.warning(
                    "spawn cap (%d) reached; dropping injected op %s",
                    self.max_spawn,
                    str(child.id)[:8],
                )
                self._record_dropped_spawn(
                    "max_spawn_exceeded",
                    assignee=child.metadata.get("assignee"),
                    emitter_id=emitter_id,
                    op_id=str(child.id),
                )
                return False

            newly_added = self.graph.internal_nodes.get(child.id, None) is None
            if newly_added:
                self.graph.add_node(child)
                self.completion_events[child.id] = ConcurrencyEvent()

            edge = None
            if not independent and emitter_id is not None:
                edge = Edge(head=emitter_id, tail=child.id, label=["spawn"])
                self.graph.add_edge(edge)

            if not self.graph.is_acyclic():
                if edge is not None:
                    self.graph.remove_edge(edge)
                if newly_added:
                    self.graph.remove_node(child.id)
                    self.completion_events.pop(child.id, None)
                logger.warning("rejected spawn %s: would create a cycle", str(child.id)[:8])
                self._record_dropped_spawn(
                    "cycle",
                    assignee=child.metadata.get("assignee"),
                    emitter_id=emitter_id,
                    op_id=str(child.id),
                )
                return False

            if newly_added:
                self._spawn_count += 1
                self._spawned_ids.add(child.id)

        if newly_added:
            # Store edge info in metadata so on_progress callbacks can attach it
            # to node lifecycle signals.
            if emitter_id is not None and not independent:
                child.metadata["parent_id"] = str(emitter_id)
            self._assign_injected_branch(child, emitter_id, independent)
            self._emit_node_spawned(child, emitter_id, independent)
            if self.on_progress:
                _name = child.metadata.get("reference_id", str(child.id)[:8])
                self.on_progress(str(child.id), _name, "queued", 0.0)
        return True

    def _emit_node_spawned(self, child: Operation, emitter_id: Any, independent: bool) -> None:
        """Fire-and-forget NodeSpawned onto the session bus."""

        def _factory() -> "Signal":
            from lionagi.session.signal import NodeSpawned  # noqa: PLC0415

            instr = None
            params = child.parameters
            if isinstance(params, dict):
                instr = params.get("instruction")
            elif hasattr(params, "instruction"):
                instr = getattr(params, "instruction", None)

            return NodeSpawned(
                op_id=str(child.id),
                parent_id=str(emitter_id) if emitter_id is not None else None,
                independent=independent,
                assignee=child.metadata.get("assignee"),
                instruction=str(instr)[:512] if instr is not None else None,
            )

        self._emit_best_effort(_factory)

    def _assign_injected_branch(self, child: Operation, emitter_id: Any, independent: bool) -> None:
        base = None
        if child.branch_id:
            try:
                base = self.session.branches[child.branch_id]
            except Exception:
                base = None
        if base is None and not independent and emitter_id is not None:
            base = self.operation_branches.get(emitter_id)
        if base is None:
            base = self.session.default_branch

        clone = base.clone(sender=self.session.id)
        self.session.include_branches(clone)
        if self._on_branch_created is not None:
            self._on_branch_created(clone)
        if self.spawn_branch_setup is not None:
            self.spawn_branch_setup(child, clone)
        self.operation_branches[child.id] = clone
        child.branch_id = clone.id


def _default_node_builder(req: Any, emitter: Operation | None) -> Operation:
    return create_operation(
        req.operation or "operate",
        parameters={"instruction": req.instruction},
    )


async def flow(
    session: "Session",
    graph: "Graph",
    *,
    branch: "Branch" = None,
    context: dict[str, Any] | None = None,
    parallel: bool = True,
    max_concurrent: int | None = None,
    verbose: bool = False,
    alcall_params: AlcallParams | None = None,
    on_progress: Any = None,
    reactive: bool = False,
    spawn_type: type | None = None,
    node_builder: Any = None,
    max_spawn: int = 50,
    executor_ref: dict[str, Any] | None = None,
    on_branch_created: Callable[[Any], None] | None = None,
    spawn_branch_setup: Callable[[Operation, Any], None] | None = None,
    on_op_complete: Callable[[Operation], None] | None = None,
) -> dict[str, Any]:
    """Execute a graph with dependency management and optional reactive
    self-expansion. Returns ``{completed_operations, operation_results,
    final_context, skipped_operations}`` always, plus ``spawned_operations``/
    ``escalated_operations``/``dropped_spawns`` when ``reactive=True`` — see
    docs/internals/core.md for the full return-shape and hook contracts."""

    if not parallel:
        max_concurrent = 1

    if reactive:
        executor = ReactiveExecutor(
            session=session,
            graph=graph,
            context=context,
            max_concurrent=max_concurrent,
            verbose=verbose,
            default_branch=branch,
            alcall_params=alcall_params,
            spawn_type=spawn_type,
            node_builder=node_builder,
            max_spawn=max_spawn,
            executor_ref=executor_ref,
            on_branch_created=on_branch_created,
            spawn_branch_setup=spawn_branch_setup,
            on_op_complete=on_op_complete,
        )
    else:
        executor = DependencyAwareExecutor(
            session=session,
            graph=graph,
            context=context,
            max_concurrent=max_concurrent,
            verbose=verbose,
            default_branch=branch,
            alcall_params=alcall_params,
            executor_ref=executor_ref,
            on_branch_created=on_branch_created,
        )
    if on_progress is not None:
        executor.on_progress = on_progress

    return await executor.execute()


async def flow_stream(
    session: "Session",
    graph: "Graph",
    *,
    branch: "Branch" = None,
    context: dict[str, Any] | None = None,
    max_concurrent: int | None = None,
    verbose: bool = False,
    alcall_params: AlcallParams | None = None,
    spawn_type: type | None = None,
    node_builder: Any = None,
    max_spawn: int = 50,
):
    """Yield FlowEvents as each operation completes; self-expanding via SpawnRequests."""
    executor = ReactiveExecutor(
        session=session,
        graph=graph,
        context=context,
        max_concurrent=max_concurrent,
        verbose=verbose,
        default_branch=branch,
        alcall_params=alcall_params,
        spawn_type=spawn_type,
        node_builder=node_builder,
        max_spawn=max_spawn,
    )
    async for event in executor.execute_stream():
        yield event


def cleanup_flow_results(
    result: dict[str, Any], keep_only: list[str] | None = None
) -> dict[str, Any]:
    """Clean up flow results to reduce memory usage."""
    if not isinstance(result, dict) or "operation_results" not in result:
        return result

    if keep_only is not None:
        filtered_results = {
            op_id: res for op_id, res in result["operation_results"].items() if op_id in keep_only
        }
        result["operation_results"] = filtered_results
        result["completed_operations"] = [
            op_id for op_id in result.get("completed_operations", []) if op_id in keep_only
        ]
    else:
        result["operation_results"] = {}
        result["completed_operations"] = []

    return result
