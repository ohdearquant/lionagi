# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import copy as _copy
from enum import Enum as _Enum
from typing import Any, ClassVar

from pydantic import Field, PrivateAttr, field_serializer

from lionagi import ln
from lionagi.ln.concurrency._compat import ExceptionGroup  # noqa: A004
from lionagi.ln.types import not_sentinel
from lionagi.utils import Unset, UnsetType, to_dict

from .element import Element

__all__ = (
    "EventStatus",
    "Execution",
    "Event",
)


_SIMPLE_TYPE = (str, bytes, bytearray, int, float, type(None), _Enum)


class EventStatus(str, ln.types.Enum):
    """Status states for tracking action execution progress.

    Attributes:
        PENDING: Initial state before execution starts.
        PROCESSING: Action is currently being executed.
        COMPLETED: Action completed successfully.
        FAILED: Action failed during execution.
        SKIPPED: Action was skipped due to unmet conditions.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ABORTED = "aborted"

    def __as_filter__(self) -> Any:
        """Build a Filter matching any event whose ``status`` equals this member.

        The :func:`~lionagi.ln.types.as_filter` hook that lets a status drive a
        subscription directly — ``session.observe(EventStatus.FAILED)`` reacts to
        every event that reached FAILED, the reactive-bus complement to logging.
        Composes with a type and field predicates via
        ``observe(APICalling, EventStatus.FAILED)``.
        """
        from lionagi.ln.types import FieldRef

        return FieldRef("status") == self


class Execution:
    """Represents the execution state of an event.

    Attributes:
        status (`EventStatus`): The current status of the event execution.
        duration (float | None): Time (in seconds) the execution took,
            if known.
        response (Any): The result or output of the execution, if any.
        error (str | BaseException | None): An error message or exception
            if the execution failed.  May hold an ``ExceptionGroup`` when
            multiple errors are accumulated via :meth:`add_error`.
        retryable (bool | None): Whether a retry is safe after failure.
    """

    __slots__ = ("status", "duration", "response", "error", "retryable")

    def __init__(
        self,
        duration: float | None | UnsetType = Unset,
        response: Any = None,
        status: EventStatus = EventStatus.PENDING,
        error: str | BaseException | None = None,
        retryable: bool | None | UnsetType = Unset,
    ) -> None:
        """Initializes an execution instance.

        Args:
            duration (float | None): The duration of the execution.
                Defaults to ``Unset`` (meaning "not yet measured") to
                distinguish from ``None`` ("explicitly no duration").
            response (Any): The result or output of the execution.
            status (EventStatus): The current status (default is PENDING).
            error (str | BaseException | None): An optional error or message.
            retryable (bool | None): Whether retry is safe.
                Defaults to ``Unset`` (meaning "not yet determined").
        """
        self.status = status
        self.duration = duration
        self.response = response
        self.error = error
        self.retryable = retryable

    def __str__(self) -> str:
        """Returns a string representation of the execution state.

        Returns:
            str: A descriptive string indicating status, duration, response,
            error, and retryable.  Sentinel (Unset) fields are shown as
            ``<unset>`` to distinguish them from ``None``.
        """
        dur = self.duration if not_sentinel(self.duration) else "<unset>"
        retry = self.retryable if not_sentinel(self.retryable) else "<unset>"
        return (
            f"Execution(status={self.status.value}, duration={dur}, "
            f"response={self.response}, error={self.error}, "
            f"retryable={retry})"
        )

    def to_dict(self) -> dict:
        """Converts the execution state to a dictionary.

        Returns:
            dict: A dictionary representation of the execution state.
        """
        res_ = Unset
        json_serializable = True

        if not isinstance(self.response, _SIMPLE_TYPE):
            json_serializable = False
            try:
                # check whether response is JSON serializable
                ln.json_dumps(self.response)
                res_ = self.response
                json_serializable = True
            except Exception:
                with contextlib.suppress(Exception):
                    # attempt to force convert to dict
                    d_ = to_dict(
                        self.response,
                        recursive=True,
                        recursive_python_only=False,
                        use_enum_values=True,
                    )
                    ln.json_dumps(d_)
                    res_ = d_
                    json_serializable = True

        if res_ is Unset and not json_serializable:
            res_ = "<unserializable>"

        error_value = self.error
        if isinstance(self.error, BaseException):
            if ExceptionGroup is not None and isinstance(self.error, ExceptionGroup):
                error_value = self._serialize_exception_group(self.error)
            else:
                error_value = {
                    "error": type(self.error).__name__,
                    "message": str(self.error),
                }

        return {
            "status": self.status.value,
            "duration": self.duration if not_sentinel(self.duration) else None,
            "response": res_ if not_sentinel(res_) else self.response,
            "error": error_value,
            "retryable": self.retryable if not_sentinel(self.retryable) else None,
        }

    def _serialize_exception_group(
        self,
        eg: ExceptionGroup,
        depth: int = 0,
        _seen: set[int] | None = None,
    ) -> dict[str, Any]:
        """Recursively serialize ExceptionGroup with depth limit and cycle detection.

        Args:
            eg: ExceptionGroup to serialize.
            depth: Current recursion depth (internal).
            _seen: Object IDs already visited for cycle detection (internal).

        Returns:
            Dict with error type, message, and nested exceptions.
        """
        max_depth = 100
        if depth > max_depth:
            return {
                "error": "ExceptionGroup",
                "message": f"Max nesting depth ({max_depth}) exceeded",
                "nested_count": len(eg.exceptions) if hasattr(eg, "exceptions") else 0,
            }

        if _seen is None:
            _seen = set()

        eg_id = id(eg)
        if eg_id in _seen:
            return {
                "error": "ExceptionGroup",
                "message": "Circular reference detected",
            }

        _seen.add(eg_id)

        try:
            exceptions = []
            for exc in eg.exceptions:
                if isinstance(exc, ExceptionGroup):
                    exceptions.append(self._serialize_exception_group(exc, depth + 1, _seen))
                else:
                    exceptions.append(
                        {
                            "error": type(exc).__name__,
                            "message": str(exc),
                        }
                    )

            return {
                "error": type(eg).__name__,
                "message": str(eg),
                "exceptions": exceptions,
            }
        finally:
            _seen.discard(eg_id)

    _MAX_ERRORS: int = 100

    def add_error(self, exc: BaseException) -> None:
        """Add error; creates ExceptionGroup if multiple errors accumulated.

        Caps at ``_MAX_ERRORS`` (default 100) to prevent unbounded memory
        growth.  When the cap is reached, subsequent errors are silently
        dropped and a warning is logged via the group message.

        On Python 3.10 without the ``exceptiongroup`` backport, multiple
        errors are stored as a plain list in a wrapper Exception.

        Args:
            exc: The exception to add.
        """
        if self.error is None:
            self.error = exc
        elif ExceptionGroup is not None and isinstance(self.error, ExceptionGroup):
            if len(self.error.exceptions) >= self._MAX_ERRORS:
                return  # cap reached — drop silently
            self.error = ExceptionGroup(
                self.error.message,
                [*self.error.exceptions, exc],  # type: ignore[arg-type]
            )
        elif isinstance(self.error, BaseException):
            if ExceptionGroup is not None:
                self.error = ExceptionGroup(
                    "multiple errors",
                    [self.error, exc],  # type: ignore[arg-type]
                )
            else:
                # Fallback for Python 3.10 without exceptiongroup
                self.error = Exception(f"multiple errors: {self.error}, {exc}")
        else:
            # error is a string or other non-exception type
            self.error = exc


class _EventQuery:
    """Field handles for an event's queryable state, including nested ``execution.*``.

    Reached via ``Event.q`` (a stateless singleton). ``APICalling.q.duration > 3600``
    builds a Filter over ``execution.duration``; ``APICalling.q.status`` over the
    status. Well-known names map to their execution path; anything else is treated
    as a top-level attribute. The complement to the ``EventStatus`` enum filter for
    the non-enum (numeric, value) fields.
    """

    _PATHS: ClassVar[dict[str, str]] = {
        "status": "execution.status",
        "duration": "execution.duration",
        "response": "execution.response",
        "error": "execution.error",
        "retryable": "execution.retryable",
    }

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        from lionagi.ln.types import FieldRef

        return FieldRef(self._PATHS.get(name, name))


class Event(Element):
    """Extends Element with an execution state.

    Attributes:
        execution (Execution): The execution state of this event.
    """

    execution: Execution = Field(default_factory=Execution)
    streaming: bool = Field(False, exclude=True)

    # Class-level filter-DSL handle: ``APICalling.q.duration > 3600`` etc. A
    # stateless singleton; ClassVar keeps Pydantic from treating it as a field.
    q: ClassVar[_EventQuery] = _EventQuery()

    # TODO(#1043 Phase 2): migrate to anyio.Event (needs .clear() audit first)
    # Lazily-created asyncio.Event signalled on terminal status transitions.
    _completion_event: asyncio.Event | None = PrivateAttr(default=None)

    # Terminal statuses that signal completion.
    _TERMINAL_STATUSES: ClassVar[frozenset] = frozenset(
        {
            EventStatus.COMPLETED,
            EventStatus.FAILED,
            EventStatus.CANCELLED,
            EventStatus.ABORTED,
            EventStatus.SKIPPED,
        }
    )

    @property
    def completion_event(self) -> asyncio.Event:
        """Lazily-created ``asyncio.Event`` that is set when this event
        reaches a terminal status (COMPLETED, FAILED, etc.).

        Safe to call from any async context; the underlying
        ``asyncio.Event`` is created on first access.
        """
        if self._completion_event is None:
            self._completion_event = asyncio.Event()
            # If already in a terminal state (e.g., constructed with a
            # terminal status), set it immediately.
            if self.execution.status in self._TERMINAL_STATUSES:
                self._completion_event.set()
        return self._completion_event

    @field_serializer("execution")
    def _serialize_execution(self, val: Execution) -> dict:
        """Serializes the Execution object into a dictionary."""
        return val.to_dict()

    @property
    def response(self) -> Any:
        """Gets or sets the execution response."""
        return self.execution.response

    @response.setter
    def response(self, val: Any) -> None:
        """Sets the execution response."""
        self.execution.response = val

    @property
    def status(self) -> EventStatus:
        """Gets or sets the event status."""
        return self.execution.status

    @status.setter
    def status(self, val: EventStatus | str) -> None:
        """Sets the event status.

        When the status transitions to a terminal state (COMPLETED,
        FAILED, CANCELLED, ABORTED, SKIPPED), the ``completion_event``
        is signalled so that any waiters are woken up immediately.
        """
        if isinstance(val, str):
            if val not in EventStatus.allowed():
                raise ValueError(f"Invalid status: {val}")
            val = EventStatus(val)
        if isinstance(val, EventStatus):
            self.execution.status = val
            # Signal the completion event if we transitioned to a
            # terminal state and the event has already been created.
            if val in self._TERMINAL_STATUSES and self._completion_event is not None:
                self._completion_event.set()
        else:
            raise ValueError(f"Invalid status type: {type(val)}. Expected EventStatus or str.")

    @property
    def request(self) -> dict:
        """Gets the request for this event. Override in subclasses"""
        return {}

    async def invoke(self) -> None:
        """Execute the event, recording the outcome as internal state.

        Idempotent: no-op if status is not PENDING. Handles status transitions,
        timing, and error capture. Override ``_invoke()`` for business logic — do
        NOT override invoke() in subclasses.

        Uses ``self.status`` (the property setter) for terminal transitions so
        that ``completion_event`` is signalled.

        The event IS the outcome channel — a business failure is captured, not
        propagated:

            - success → COMPLETED, ``response`` set.
            - ``Exception`` (a failing tool, a bad response, …) → FAILED, the
              error recorded on ``execution``; **not** re-raised. Callers inspect
              ``status`` / ``execution.error`` (or call ``assert_completed()`` to
              opt into fail-fast), and observers react via
              ``session.observe(EventType, EventStatus.FAILED)``.
            - ``BaseException`` (CancelledError, KeyboardInterrupt, SystemExit) →
              CANCELLED, then **re-raised**: cancellation is a control-flow
              signal that must propagate, never a result to inspect.
        """
        if self.execution.status != EventStatus.PENDING:
            return

        self.execution.status = EventStatus.PROCESSING
        start = ln.now_utc().timestamp()

        try:
            result = await self._invoke()
            self.execution.response = result
            self.status = EventStatus.COMPLETED
        except Exception as e:
            # A business failure is internal state, not a propagated exception.
            self.status = EventStatus.FAILED
            self.execution.add_error(e)
        except BaseException as e:
            # CancelledError, KeyboardInterrupt, SystemExit — must propagate.
            self.execution.add_error(e)
            self.status = EventStatus.CANCELLED
            raise
        finally:
            self.execution.duration = ln.now_utc().timestamp() - start

    async def _invoke(self) -> None:
        """Business logic for this event. Override in subclasses.

        Called by invoke() after status transitions. Raise an exception
        to trigger FAILED status. Set self.execution.response for results.
        """
        raise NotImplementedError("Override _invoke() in subclass.")

    async def stream(self):
        """Execute the event with streaming and lifecycle management.

        Idempotent: no-op if status is already terminal (COMPLETED, FAILED,
        CANCELLED, ABORTED, SKIPPED). Handles status transitions, timing,
        and error capture. Override ``_stream()`` for streaming business
        logic — do NOT override stream() in subclasses.

        Uses ``self.status`` (the property setter) for terminal transitions
        so that ``completion_event`` is signalled.

        Same outcome contract as :meth:`invoke` (the event IS the outcome):
            - all chunks yielded → COMPLETED.
            - ``Exception`` → FAILED, error recorded on ``execution``; **not**
              re-raised. Chunks emitted before the failure are still yielded.
            - ``BaseException`` (cancellation) → CANCELLED, then **re-raised**.
        """
        if self.execution.status in self._TERMINAL_STATUSES:
            return

        self.execution.status = EventStatus.PROCESSING
        start = ln.now_utc().timestamp()

        try:
            async for chunk in self._stream():
                yield chunk
            self.status = EventStatus.COMPLETED
        except Exception as e:
            # A business failure is internal state, not a propagated exception.
            self.status = EventStatus.FAILED
            self.execution.add_error(e)
        except BaseException as e:
            # CancelledError, KeyboardInterrupt, SystemExit — must propagate.
            self.execution.add_error(e)
            self.status = EventStatus.CANCELLED
            raise
        finally:
            self.execution.duration = ln.now_utc().timestamp() - start

    async def _stream(self):
        """Streaming business logic. Override in subclasses."""
        raise NotImplementedError("Override _stream() in subclass.")
        yield  # pragma: no cover -- makes this an async generator

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        """Not implemented. Events cannot be fully recreated once done."""
        raise NotImplementedError("Cannot recreate an event once it's done.")

    def assert_completed(self) -> None:
        """Assert the event completed successfully.

        Raises:
            RuntimeError: If the event status is not COMPLETED, with
                execution details in the message.
        """
        if self.execution.status != EventStatus.COMPLETED:
            exec_dict = self.execution.to_dict()
            exec_dict.pop("response", None)
            raise RuntimeError(f"Event did not complete successfully: {exec_dict}")

    def as_fresh_event(self, copy_meta: bool = False) -> Event:
        """Creates a clone of this event with a fresh execution state.

        - Uses ``model_dump`` rather than ``to_dict`` to avoid
          unconditional key accesses on fields excluded from the dump.
        - Re-attaches fields declared with ``exclude=True`` (e.g.
          ``Operation.parameters``) so a retry clone keeps non-default
          state that is invisible to ``model_dump``.
        - Deep-copies excluded fields and metadata so the retry does not
          share nested mutable state with the original. Falls back to a
          reference copy when the value is not deep-copyable (closures,
          file handles, etc.).
        """
        skip = {"execution", "created_at", "id", "metadata"}
        d_ = self.model_dump(exclude=skip)
        for name, field_info in self.__class__.model_fields.items():
            if name in skip:
                continue
            if field_info.exclude and name not in d_:
                val = getattr(self, name, None)
                if val is not None:
                    try:
                        d_[name] = _copy.deepcopy(val)
                    except Exception:
                        d_[name] = val
        fresh = self.__class__(**d_)
        if copy_meta:
            try:
                fresh.metadata = _copy.deepcopy(self.metadata)
            except Exception:
                fresh.metadata = self.metadata.copy()
        fresh.metadata["original"] = {
            "id": str(self.id),
            "created_at": self.created_at,
        }
        return fresh


# File: lionagi/protocols/generic/event.py
