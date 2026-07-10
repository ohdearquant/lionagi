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
    """Status states for event execution lifecycle."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ABORTED = "aborted"

    def __as_filter__(self) -> Any:
        from lionagi.ln.types import FieldRef

        return FieldRef("status") == self


class Execution:
    """Mutable execution state for an event (status, duration, response, error)."""

    __slots__ = ("status", "duration", "response", "error", "retryable")

    def __init__(
        self,
        duration: float | None | UnsetType = Unset,
        response: Any = None,
        status: EventStatus = EventStatus.PENDING,
        error: str | BaseException | None = None,
        retryable: bool | None | UnsetType = Unset,
    ) -> None:
        self.status = status
        self.duration = duration
        self.response = response
        self.error = error
        self.retryable = retryable

    def __str__(self) -> str:
        dur = self.duration if not_sentinel(self.duration) else "<unset>"
        retry = self.retryable if not_sentinel(self.retryable) else "<unset>"
        return (
            f"Execution(status={self.status.value}, duration={dur}, "
            f"response={self.response}, error={self.error}, "
            f"retryable={retry})"
        )

    def to_dict(self) -> dict:
        res_ = Unset
        json_serializable = True

        if not isinstance(self.response, _SIMPLE_TYPE):
            json_serializable = False
            try:
                ln.json_dumps(self.response)
                res_ = self.response
                json_serializable = True
            except Exception:
                with contextlib.suppress(Exception):
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
        """Accumulate errors, grouping into ExceptionGroup. Capped at _MAX_ERRORS."""
        if self.error is None:
            self.error = exc
        elif ExceptionGroup is not None and isinstance(self.error, ExceptionGroup):
            if len(self.error.exceptions) >= self._MAX_ERRORS:
                return
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
    """Field handles for Event.q.{field} filter expressions."""

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
    """Element with an execution lifecycle (status, duration, response, error)."""

    execution: Execution = Field(default_factory=Execution)
    streaming: bool = Field(False, exclude=True)

    q: ClassVar[_EventQuery] = _EventQuery()

    # Lazily-created asyncio.Event signalled on terminal status transitions.
    # TODO: migrate to anyio.Event (needs .clear() audit first).
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
        if self._completion_event is None:
            self._completion_event = asyncio.Event()
            if self.execution.status in self._TERMINAL_STATUSES:
                self._completion_event.set()
        return self._completion_event

    @field_serializer("execution")
    def _serialize_execution(self, val: Execution) -> dict:
        return val.to_dict()

    @property
    def response(self) -> Any:
        return self.execution.response

    @response.setter
    def response(self, val: Any) -> None:
        self.execution.response = val

    @property
    def status(self) -> EventStatus:
        return self.execution.status

    @status.setter
    def status(self, val: EventStatus | str) -> None:
        if isinstance(val, str):
            if val not in EventStatus.allowed():
                raise ValueError(f"Invalid status: {val}")
            val = EventStatus(val)
        if isinstance(val, EventStatus):
            self.execution.status = val
            if val in self._TERMINAL_STATUSES and self._completion_event is not None:
                self._completion_event.set()
        else:
            raise ValueError(f"Invalid status type: {type(val)}. Expected EventStatus or str.")

    @property
    def request(self) -> dict:
        return {}

    async def invoke(self) -> None:
        """Execute the event. Exception -> FAILED (captured), BaseException -> CANCELLED (re-raised)."""
        if self.execution.status != EventStatus.PENDING:
            return

        self.execution.status = EventStatus.PROCESSING
        start = ln.now_utc().timestamp()

        try:
            result = await self._invoke()
            self.execution.response = result
            self.status = EventStatus.COMPLETED
        except Exception as e:
            self.status = EventStatus.FAILED
            self.execution.add_error(e)
        except BaseException as e:
            self.execution.add_error(e)
            self.status = EventStatus.CANCELLED
            raise
        finally:
            self.execution.duration = ln.now_utc().timestamp() - start

    async def _invoke(self) -> None:
        raise NotImplementedError("Override _invoke() in subclass.")

    async def stream(self):
        """Streaming variant of invoke(). Same outcome contract."""
        if self.execution.status in self._TERMINAL_STATUSES:
            return

        self.execution.status = EventStatus.PROCESSING
        start = ln.now_utc().timestamp()

        try:
            async for chunk in self._stream():
                yield chunk
            self.status = EventStatus.COMPLETED
        except Exception as e:
            self.status = EventStatus.FAILED
            self.execution.add_error(e)
        except BaseException as e:
            self.execution.add_error(e)
            self.status = EventStatus.CANCELLED
            raise
        finally:
            self.execution.duration = ln.now_utc().timestamp() - start

    async def _stream(self):
        raise NotImplementedError("Override _stream() in subclass.")
        yield  # pragma: no cover -- makes this an async generator

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        raise NotImplementedError("Cannot recreate an event once it's done.")

    def assert_completed(self) -> None:
        if self.execution.status != EventStatus.COMPLETED:
            exec_dict = self.execution.to_dict()
            exec_dict.pop("response", None)
            raise RuntimeError(f"Event did not complete successfully: {exec_dict}")

    def as_fresh_event(self, copy_meta: bool = False) -> Event:
        """Clone with fresh execution state, re-attaching exclude=True fields."""
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
