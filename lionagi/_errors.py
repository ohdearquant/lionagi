# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, ClassVar

__all__ = (
    "LionError",
    "ValidationError",
    "NotFoundError",
    "ExistsError",
    "ObservationError",
    "ResourceError",
    "RateLimitError",
    "RelationError",
    "OperationError",
    "ExecutionError",
    "ConfigurationError",
    "TimeoutError",
    "ItemNotFoundError",
    "ItemExistsError",
)


class LionError(Exception):
    default_message: ClassVar[str] = "LionAGI error"
    default_status_code: ClassVar[int] = 500
    __slots__ = ("message", "details", "status_code")

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        cause: Exception | None = None,
    ):
        super().__init__(message or self.default_message)
        if cause:
            self.__cause__ = cause  # preserves traceback
        self.message = message or self.default_message
        self.details = details or {}
        self.status_code = status_code or type(self).default_status_code

    def to_dict(self, *, include_cause: bool = False) -> dict[str, Any]:
        data = {
            "error": self.__class__.__name__,
            "message": self.message,
            "status_code": self.status_code,
            **({"details": self.details} if self.details else {}),
        }
        if include_cause and (cause := self.get_cause()):
            data["cause"] = repr(cause)
        return data

    def get_cause(self) -> Exception | None:
        """Get the cause of this error, if any."""
        return self.__cause__ if hasattr(self, "__cause__") else None

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        expected: str | None = None,
        message: str | None = None,
        cause: Exception | None = None,
        **extra: Any,
    ):
        """Create a ValidationError from a value with optional expected type and message."""
        details = {
            "value": value,
            "type": type(value).__name__,
            **({"expected": expected} if expected else {}),
            **extra,
        }
        return cls(message=message, details=details, cause=cause)


class ValidationError(LionError, ValueError):
    """Exception raised when validation fails."""

    default_message = "Validation failed"
    default_status_code = 422
    __slots__ = ()


class NotFoundError(LionError):
    """Exception raised when an item is not found."""

    default_message = "Item not found"
    default_status_code = 404
    __slots__ = ()


class ExistsError(LionError):
    """Exception raised when an item already exists."""

    default_message = "Item already exists"
    default_status_code = 409
    __slots__ = ()


class ObservationError(LionError):
    """Exception raised when an observation fails."""

    default_message = "Observation failed"
    default_status_code = 500
    __slots__ = ()


class ResourceError(LionError):
    """Exception raised when resource access fails."""

    default_message = "Resource error"
    default_status_code = 429
    __slots__ = ()


class RateLimitError(LionError):
    __slots__ = ("retry_after",)  # one extra attr
    default_message = "Rate limit exceeded"
    default_status_code = 429

    def __init__(self, retry_after: float, **kw):
        super().__init__(**kw)
        object.__setattr__(self, "retry_after", retry_after)


class RelationError(LionError):
    pass


class OperationError(LionError, ValueError):
    pass


class ExecutionError(LionError, RuntimeError):
    pass


class ConfigurationError(LionError, ValueError):
    """Exception raised when configuration is invalid."""

    default_message = "Invalid configuration"
    default_status_code = 500
    __slots__ = ()


class TimeoutError(LionError):  # noqa: A001 — intentional shadowing of builtin
    """Exception raised when an operation times out."""

    default_message = "Operation timed out"
    default_status_code = 408
    __slots__ = ()


ItemNotFoundError = NotFoundError
ItemExistsError = ExistsError
