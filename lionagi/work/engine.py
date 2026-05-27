# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""WorkEngine: orchestration layer that dispatches WorkForms to workers.

The engine is the runtime coordinator:

- Workers register themselves (by definition_id) along with their handler
  callables.
- Callers submit a WorkForm; the engine picks a worker, creates a WorkTask,
  executes the handler, and stores the result.
- All state mutations are protected by a ``threading.Lock`` so the engine is
  safe to use from multiple threads.

Execution model
---------------
Handlers are called *synchronously* in the current thread by default.  For
async handlers, use :meth:`WorkEngine.submit_async` which awaits the handler
in the current event loop.  Timeout enforcement for async handlers is done via
``asyncio.wait_for``.

Concurrency (max_concurrent) is enforced as a simple gate: if a worker is
already at its concurrency limit, ``submit`` raises ``RuntimeError`` rather
than queuing (queueing is the caller's responsibility).

Usage::

    engine = WorkEngine()
    engine.register_worker(
        definition=defn,
        handler=my_callable,  # (form: WorkForm) -> Any
    )
    task_id = engine.submit(form, worker_id="summarise")
    result  = engine.get_result(task_id)
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from .definition import WorkerDefinition
from .form import WorkForm

__all__ = (
    "WorkEngine",
    "WorkResult",
    "WorkTask",
)

TaskStatus = Literal["queued", "running", "completed", "failed"]


class WorkResult(BaseModel):
    """Outcome of a completed (or failed) work task.

    Attributes:
        task_id: Links back to the originating :class:`WorkTask`.
        value: Return value from the handler, or ``None`` on failure.
        error: Error message string on failure, ``None`` on success.
    """

    task_id: str = Field(..., description="ID of the task that produced this result.")
    value: Any = Field(None, description="Handler return value on success.")
    error: str | None = Field(None, description="Error message on failure.")

    model_config = {"arbitrary_types_allowed": True}

    @property
    def success(self) -> bool:
        """Return True when no error is recorded."""
        return self.error is None


class WorkTask(BaseModel):
    """Runtime record for a single submitted work item.

    Attributes:
        task_id: Unique identifier for this task.
        form_id: ID of the WorkForm that was submitted.
        worker_id: definition_id of the worker assigned to this task.
        status: Current lifecycle phase.
        result: Populated after the task completes or fails.
        error: Short error message (also stored in result.error).
        submitted_at: Unix timestamp when the task was created.
        completed_at: Unix timestamp when the task finished (or None).
    """

    task_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique task identifier.",
    )
    form_id: str = Field(..., description="ID of the submitted WorkForm.")
    worker_id: str = Field(..., description="definition_id of the assigned worker.")
    status: TaskStatus = Field("queued", description="Current task status.")
    result: Any = Field(None, description="Handler return value after completion.")
    error: str | None = Field(None, description="Error message if the task failed.")
    submitted_at: float = Field(
        default_factory=time.time,
        description="Unix timestamp of submission.",
    )
    completed_at: float | None = Field(
        None,
        description="Unix timestamp of completion (None while in-flight).",
    )

    model_config = {"arbitrary_types_allowed": True}

    @property
    def is_terminal(self) -> bool:
        """Return True when the task has reached a final state."""
        return self.status in {"completed", "failed"}

    @property
    def duration(self) -> float | None:
        """Elapsed seconds from submission to completion, or None."""
        if self.completed_at is None:
            return None
        return self.completed_at - self.submitted_at


class _WorkerSlot:
    """Internal: tracks a registered worker and its in-flight count."""

    def __init__(self, definition: WorkerDefinition, handler: Any) -> None:
        self.definition = definition
        self.handler = handler
        self.in_flight: int = 0

    @property
    def at_capacity(self) -> bool:
        limit = self.definition.max_concurrent
        return limit > 0 and self.in_flight >= limit


class WorkEngine:
    """Synchronous orchestration engine for dispatching WorkForms.

    Thread-safe via an internal ``threading.Lock``.

    Attributes:
        name: Optional display name for this engine instance.
    """

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._workers: dict[str, _WorkerSlot] = {}
        self._tasks: dict[str, WorkTask] = {}

    # ------------------------------------------------------------------
    # Worker registration
    # ------------------------------------------------------------------

    def register_worker(
        self,
        definition: WorkerDefinition,
        handler: Any | None = None,
    ) -> None:
        """Register a worker with the engine.

        Args:
            definition: Static descriptor for this worker type.
            handler: The callable that processes a WorkForm.  If None, the
                engine will try ``definition.resolve_handler()`` at submit time.
        """
        if handler is None:
            handler = definition.resolve_handler()
        with self._lock:
            self._workers[definition.definition_id] = _WorkerSlot(definition, handler)

    def unregister_worker(self, worker_id: str) -> bool:
        """Remove a worker registration.  Returns True if it existed."""
        with self._lock:
            if worker_id in self._workers:
                del self._workers[worker_id]
                return True
            return False

    def worker_ids(self) -> list[str]:
        """Return a list of all registered worker IDs."""
        with self._lock:
            return list(self._workers.keys())

    # ------------------------------------------------------------------
    # Task submission
    # ------------------------------------------------------------------

    def submit(self, form: WorkForm, worker_id: str | None = None) -> str:
        """Submit *form* for processing and return its task_id.

        Args:
            form: The WorkForm to process.  Must be in ``draft``, ``validated``,
                or ``filled`` status.  Forms in ``submitted``, ``completed``, or
                ``error`` status are rejected to enforce the lifecycle.
            worker_id: Which worker to use.  If None, the first registered
                worker is used (useful when there is only one).

        Returns:
            The ``task_id`` string that can be passed to :meth:`get_result`.

        Raises:
            ValueError: No workers registered, *worker_id* not found, or *form*
                is in a status that cannot be submitted.
            RuntimeError: Worker is at its concurrency limit.
        """
        _submittable = {"draft", "filled", "validated"}
        if form.status not in _submittable:
            raise ValueError(
                f"Cannot submit a form in {form.status!r} status.  "
                f"Only {sorted(_submittable)} forms are accepted."
            )
        with self._lock:
            slot = self._resolve_slot(worker_id)
            if slot.at_capacity:
                raise RuntimeError(
                    f"Worker {slot.definition.definition_id!r} is at its concurrency "
                    f"limit ({slot.definition.max_concurrent})."
                )

            task = WorkTask(form_id=form.form_id, worker_id=slot.definition.definition_id)
            self._tasks[task.task_id] = task
            slot.in_flight += 1

        # Execute outside the lock so other threads aren't blocked.
        self._run_task(task, slot, form)
        return task.task_id

    async def submit_async(self, form: WorkForm, worker_id: str | None = None) -> str:
        """Async variant of :meth:`submit` for coroutine handlers.

        If the handler is a coroutine function, it is awaited with the
        configured timeout (if any).  If the handler is a plain callable,
        it is called directly in the event loop (use an executor for blocking
        work).

        Returns the task_id.
        """
        import asyncio
        import inspect

        with self._lock:
            slot = self._resolve_slot(worker_id)
            if slot.at_capacity:
                raise RuntimeError(
                    f"Worker {slot.definition.definition_id!r} is at its concurrency "
                    f"limit ({slot.definition.max_concurrent})."
                )
            task = WorkTask(form_id=form.form_id, worker_id=slot.definition.definition_id)
            self._tasks[task.task_id] = task
            slot.in_flight += 1

        try:
            with self._lock:
                task.status = "running"  # type: ignore[assignment]

            timeout = slot.definition.timeout_seconds or None
            if inspect.iscoroutinefunction(slot.handler):
                coro = slot.handler(form)
                value = await (asyncio.wait_for(coro, timeout=timeout) if timeout else coro)
            else:
                value = slot.handler(form)

            with self._lock:
                task.status = "completed"  # type: ignore[assignment]
                task.result = value
                task.completed_at = time.time()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                task.status = "failed"  # type: ignore[assignment]
                task.error = f"{type(exc).__name__}: {exc}"
                task.completed_at = time.time()
        finally:
            with self._lock:
                slot.in_flight = max(0, slot.in_flight - 1)

        return task.task_id

    # ------------------------------------------------------------------
    # Task queries
    # ------------------------------------------------------------------

    def get_result(self, task_id: str) -> WorkResult | None:
        """Return a :class:`WorkResult` for a completed/failed task.

        Returns ``None`` if the task is still in-flight or does not exist.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or not task.is_terminal:
                return None
            return WorkResult(task_id=task_id, value=task.result, error=task.error)

    def get_task(self, task_id: str) -> WorkTask | None:
        """Return the raw WorkTask for *task_id*, or None."""
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, status: TaskStatus | None = None) -> list[WorkTask]:
        """Return all tasks, optionally filtered by *status*.

        Args:
            status: When provided, only tasks matching this status are returned.

        Returns:
            List of :class:`WorkTask` instances, ordered by submission time.
        """
        with self._lock:
            tasks = list(self._tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: t.submitted_at)

    def clear_completed(self) -> int:
        """Remove all completed and failed tasks from memory.  Returns count removed."""
        with self._lock:
            to_remove = [tid for tid, t in self._tasks.items() if t.is_terminal]
            for tid in to_remove:
                del self._tasks[tid]
            return len(to_remove)

    # ------------------------------------------------------------------
    # Public ID-resolution helpers (used by CLI schedule.py)
    # ------------------------------------------------------------------

    def get_item(self, item_id: str) -> WorkTask | None:
        """Return the WorkTask for *item_id*, or None.

        Alias for :meth:`get_task` that presents a stable public surface
        for callers that don't want to assume the internal storage name.
        """
        return self.get_task(item_id)

    def find_by_prefix(self, prefix: str) -> list[str]:
        """Return task IDs that start with *prefix*.

        Args:
            prefix: ID prefix to match (at least 4 characters recommended).

        Returns:
            List of matching task IDs (may be empty, one, or many).
        """
        with self._lock:
            return [tid for tid in self._tasks if tid.startswith(prefix)]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_slot(self, worker_id: str | None) -> _WorkerSlot:
        """Return the slot for *worker_id* (or the sole registered worker)."""
        if not self._workers:
            raise ValueError("No workers registered with this engine.")
        if worker_id is None:
            if len(self._workers) != 1:
                raise ValueError("worker_id is required when more than one worker is registered.")
            return next(iter(self._workers.values()))
        if worker_id not in self._workers:
            raise ValueError(
                f"No worker registered with id {worker_id!r}.  Available: {list(self._workers)}."
            )
        return self._workers[worker_id]

    def _run_task(self, task: WorkTask, slot: _WorkerSlot, form: WorkForm) -> None:
        """Execute handler synchronously, updating task state.

        Timeout enforcement uses ``concurrent.futures.ThreadPoolExecutor`` so
        it is safe to call from any thread (unlike ``signal.SIGALRM``, which
        crashes with ``ValueError`` when called outside the main thread and is
        process-global, meaning concurrent timeouts clobber each other).
        """
        timeout = slot.definition.timeout_seconds or None

        with self._lock:
            task.status = "running"  # type: ignore[assignment]

        try:
            if timeout:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _executor:
                    future = _executor.submit(slot.handler, form)
                    try:
                        value = future.result(timeout=timeout)
                    except concurrent.futures.TimeoutError as exc:
                        future.cancel()
                        raise TimeoutError(
                            f"Task {task.task_id} timed out after {timeout}s."
                        ) from exc
            else:
                value = slot.handler(form)

            with self._lock:
                task.status = "completed"  # type: ignore[assignment]
                task.result = value
                task.completed_at = time.time()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                task.status = "failed"  # type: ignore[assignment]
                task.error = f"{type(exc).__name__}: {exc}"
                task.completed_at = time.time()
        finally:
            with self._lock:
                slot.in_flight = max(0, slot.in_flight - 1)
