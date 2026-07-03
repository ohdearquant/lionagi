# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dependency-aware flow execution with structured concurrency."""

import asyncio
import contextlib
import contextvars
import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import anyio

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


logger = logging.getLogger(__name__)

UNLIMITED_CONCURRENCY = int(os.environ.get("LIONAGI_MAX_CONCURRENCY", "10000"))

# Tracks which Operation a reactive task is running (per-task contextvar).
_CURRENT_OP: contextvars.ContextVar = contextvars.ContextVar("reactive_current_op", default=None)


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
    ):
        self.session = session
        self.graph = graph
        self.context: Note = Note(**(context or {}))
        self.max_concurrent = max_concurrent
        self.verbose = verbose
        self._alcall = alcall_params or AlcallParams()
        self._default_branch = default_branch
        self.on_progress = None
        self.results = {}
        self.completion_events = {}
        self.operation_branches = {}
        self.skipped_operations = set()
        self._op_start_times = {}
        self._pause_event: ConcurrencyEvent | None = None
        # ADR-0085 part 1: an out-of-band handle the caller can pass so a
        # control poller running alongside this flow (cli/orchestrate/flow.py)
        # can reach pause()/resume()/context/graph on the live executor. Set
        # synchronously here (before any awaiting) so it is available the
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
        await self._alcall(nodes, self._execute_operation, limiter=limiter)

        completed_ops = [
            op_id for op_id in self.results.keys() if op_id not in self.skipped_operations
        ]

        result = {
            "completed_operations": completed_ops,
            "operation_results": self.results,
            "final_context": self.context.content,
            "skipped_operations": list(self.skipped_operations),
        }

        self._validate_execution_results(result)

        return result

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

            predecessors = self.graph.get_predecessors(node)
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
                            self.session.branches.collections[branch_id] = branch_clone
                            self.session.branches.progression.append(branch_id)
                except Exception:
                    logger.debug("Skipping branch clone registration (likely mock in test).")

                if operation.metadata.get("inherit_context"):
                    branch_clone.metadata = branch_clone.metadata or {}
                    branch_clone.metadata["pending_context_inheritance"] = True
                    branch_clone.metadata["inherit_from_operation"] = operation.metadata.get(
                        "primary_dependency"
                    )

        if self.verbose:
            logger.debug("Pre-allocated %d branches", len(operations_needing_branches))

    def pause(self) -> None:
        """Install a pause gate at the next operation boundary; idempotent."""
        if self._pause_event is None:
            self._pause_event = ConcurrencyEvent()

    def resume(self) -> None:
        """Release the pause gate; idempotent. A later pause() installs a fresh event."""
        if self._pause_event is not None:
            self._pause_event.set()
            self._pause_event = None

    def _emit_paused(self, operation: Operation) -> None:
        """Fire-and-forget NodePaused onto the session bus."""
        try:
            from lionagi.session.signal import NodePaused  # noqa: PLC0415

            op_id = str(operation.id)
            name = operation.metadata.get("reference_id", op_id[:8])
            sig = NodePaused(op_id=op_id, name=name)
            loop = asyncio.get_running_loop()
            loop.create_task(self.session.emit(sig))
        except RuntimeError:
            pass  # no running loop — tests / sync contexts
        except Exception:  # noqa: BLE001, S110
            pass  # best-effort; never break the pause path

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

                if self.verbose:
                    logger.debug(
                        "Skipping operation due to edge conditions: %s",
                        str(operation.id)[:8],
                    )

                if self.on_progress:
                    ref_id = operation.metadata.get("reference_id", str(operation.id)[:8])
                    branch = self.operation_branches.get(operation.id, self.session.default_branch)
                    branch_name = getattr(branch, "name", None) or ref_id
                    self.on_progress(str(operation.id), branch_name, "failed", 0.0)

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

                if self.on_progress:
                    self.on_progress(str(operation.id), branch_name, "started", 0)
                if self.verbose:
                    logger.debug("Executing operation: %s", ref_id)

                operation._branch = branch
                await operation.invoke()

                elapsed = _time.monotonic() - self._op_start_times.get(
                    operation.id, _time.monotonic()
                )

                if operation.execution.status == EventStatus.COMPLETED:
                    self.results[operation.id] = operation.response

                    # Deep-merge operation context into flow workspace to preserve nested keys.
                    if isinstance(operation.response, dict) and "context" in operation.response:
                        from lionagi.libs.nested import deep_update

                        deep_update(self.context.content, operation.response["context"])

                    if self.on_progress:
                        self.on_progress(str(operation.id), branch_name, "completed", elapsed)
                    if self.verbose:
                        logger.debug("Completed operation: %s (%.1fs)", ref_id, elapsed)

                elif operation.execution.status == EventStatus.FAILED:
                    self.results[operation.id] = {"error": str(operation.execution.error)}
                    if self.on_progress:
                        self.on_progress(str(operation.id), branch_name, "failed", elapsed)
                    if self.verbose:
                        logger.error(
                            "Operation %s failed (%.1fs): %s",
                            ref_id,
                            elapsed,
                            operation.execution.error,
                        )

        except (get_cancelled_exc_class(), KeyboardInterrupt, SystemExit):
            self.completion_events[operation.id].set()
            raise

        except Exception as e:
            # Defensive net for unexpected flow-level errors; invoke() already handles FAILED status.
            if operation.id not in self.results:
                self.results[operation.id] = {"error": str(e)}

            if self.verbose:
                logger.error("Operation %s failed: %s", str(operation.id)[:8], e)

        finally:
            self.completion_events[operation.id].set()

    async def _check_edge_conditions(self, operation: Operation) -> bool:
        """Return True if at least one valid incoming path exists or no edges; False if all incoming edges failed."""
        incoming_edges = [
            edge for edge in self.graph.internal_edges.values() if edge.tail == operation.id
        ]

        if not incoming_edges:
            return True

        has_valid_path = False

        for edge in incoming_edges:
            if edge.head in self.completion_events:
                await self.completion_events[edge.head].wait()

            if edge.head in self.skipped_operations:
                continue

            result_value = self.results.get(edge.head)
            if result_value is not None and not isinstance(result_value, str | int | float | bool):
                result_value = to_dict(result_value, recursive=True)

            # apply() expects a plain dict (dict.get() semantics); pass Note.content not the Note itself.
            ctx = {"result": result_value, "context": self.context.content}

            if await edge.check_condition(ctx):
                has_valid_path = True
                break

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

        predecessors = self.graph.get_predecessors(operation)
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
        predecessors = self.graph.get_predecessors(operation)
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

        branch = self._resolve_branch_for_operation(operation)
        self.operation_branches[operation.id] = branch

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
                from lionagi.protocols.graph.edge import EdgeCondition

                if not isinstance(edge.condition, EdgeCondition):
                    raise TypeError(
                        f"Edge {edge.id} has invalid condition type: {type(edge.condition)}. "
                        "Must be EdgeCondition or None."
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
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        if spawn_type is None:
            from lionagi.casts.emission import SpawnRequest

            spawn_type = SpawnRequest
        self.spawn_type = spawn_type
        self.node_builder = node_builder
        self.max_spawn = max_spawn
        self._spawn_count = 0
        self._running = False
        self._tg: Any = None
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
            "spawned_operations": self._spawn_count,
            "escalated_operations": list(self._escalated_ids),
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

        async def _driver():
            # Owns its own task group (entered/exited in THIS task). The
            # generator must not span a task group across `yield` — anyio forbids
            # it — so the driver runs detached and the generator only drains the
            # channel. Closing `send` on completion ends the consumer's loop.
            try:
                async with create_task_group() as tg:
                    self._tg = tg
                    for node in initial:
                        if self.on_progress:
                            _name = node.metadata.get("reference_id", str(node.id)[:8])
                            self.on_progress(str(node.id), _name, "queued", 0.0)
                        tg.start_soon(self._run_tracked, node)
            finally:
                await send.aclose()

        # asyncio-only: flow_stream needs a detached task for the driver
        # coroutine so the generator can yield events as they arrive.
        # anyio's create_task_group cannot be used here because the generator
        # must outlive any single task group scope.
        driver = asyncio.ensure_future(_driver())
        try:
            async with recv:
                async for event in recv:
                    yield event
            await driver  # normal end: surface any driver exception
        finally:
            self._running = False
            self._tg = None
            self._result_sink = None
            observer.unobserve(self._on_bus_spawn)
            observer.unobserve(self._on_bus_escalation)
            if not driver.done():  # early break / consumer close: tear down
                driver.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await driver

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
        """Consume an EscalationRequest: higher_tier re-spawns the op; give_up just signals."""
        if id(req) in self._seen_reqs:
            return
        self._seen_reqs.add(id(req))

        context = getattr(req, "context", {}) or {}
        route = context.get("route", "higher_tier")

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
            if self._accept_node(child, emitter_id=emitter_id, independent=True):
                self._escalated_ids.add(emitter_id)
        else:
            if emitter_id is not None:
                self._escalated_ids.add(emitter_id)

    def _emit_node_escalated(
        self, op_id: str, name: str, reason: str, route: str, req: Any
    ) -> None:
        """Fire-and-forget NodeEscalated onto the session bus."""
        try:
            from lionagi.session.signal import NodeEscalated  # noqa: PLC0415

            sig = NodeEscalated(
                op_id=op_id,
                name=name,
                reason=reason,
                route=route,
                escalation_request=req,
            )
            loop = asyncio.get_running_loop()
            loop.create_task(self.session.emit(sig))
        except RuntimeError:
            pass  # no running loop — tests / sync contexts
        except Exception:  # noqa: BLE001, S110
            pass  # best-effort; never break the escalation path

    def _inject_request(self, req: Any, *, emitter: Operation | None) -> bool:
        if id(req) in self._seen_reqs:
            return False
        self._seen_reqs.add(id(req))
        builder = self.node_builder or _default_node_builder
        try:
            child = builder(req, emitter)
        except Exception as e:
            logger.warning("spawn node_builder failed: %s", e)
            return False
        if child is None:
            return False
        emitter_id = emitter.id if emitter is not None else None
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
                return False

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
        try:
            from lionagi.session.signal import NodeSpawned  # noqa: PLC0415

            instr = None
            params = child.parameters
            if isinstance(params, dict):
                instr = params.get("instruction")
            elif hasattr(params, "instruction"):
                instr = getattr(params, "instruction", None)

            sig = NodeSpawned(
                op_id=str(child.id),
                parent_id=str(emitter_id) if emitter_id is not None else None,
                independent=independent,
                assignee=child.metadata.get("assignee"),
                instruction=str(instr)[:512] if instr is not None else None,
            )
            loop = asyncio.get_running_loop()
            loop.create_task(self.session.emit(sig))
        except RuntimeError:
            pass  # no running loop — tests / sync contexts
        except Exception:  # noqa: BLE001, S110
            pass  # best-effort; never break the acceptance path

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
) -> dict[str, Any]:
    """Execute a graph with dependency management and optional reactive self-expansion."""

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


def cleanup_flow_results(result: dict[str, Any], keep_only: list[str] = None) -> dict[str, Any]:
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
