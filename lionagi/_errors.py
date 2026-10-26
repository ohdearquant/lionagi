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
    "ProviderStreamError",
    "ConfigurationError",
    "CredentialMissingError",
    "ContainmentRefusedError",
    "TimeoutError",
    "EmptyOutgoingContentError",
    "CLIInputRefusedError",
    "InputTokenCapError",
    "FailedRoundError",
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
    __slots__ = ("retry_after",)
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


class ProviderStreamError(ExecutionError):
    """A provider accepted a streamed call and then reported, inside the stream, that the
    call failed (an ``error`` event, ``response.failed``, a chunk carrying an error)."""

    default_message = "The provider reported a failed call inside its stream"
    default_status_code = 502
    __slots__ = ()


class ConfigurationError(LionError, ValueError):
    """Exception raised when configuration is invalid."""

    default_message = "Invalid configuration"
    default_status_code = 500
    __slots__ = ()


class CredentialMissingError(ConfigurationError):
    """A declared credential variable has no configured value at dispatch."""

    reason: ClassVar[str] = "credential_missing"

    def __init__(self, variable_name: str):
        super().__init__(
            f"Credential variable '{variable_name}' is not set.", details={"reason": self.reason}
        )


class ContainmentRefusedError(ConfigurationError):
    """A request under ``contain_account_integrations`` was refused before it ran: its
    configuration could turn account integrations back on, or could not be read to rule
    that out."""

    default_message = "Account-integration containment refused the request"
    __slots__ = ()


class TimeoutError(LionError):  # noqa: A001 — intentional shadowing of builtin
    """Exception raised when an operation times out."""

    default_message = "Operation timed out"
    default_status_code = 408
    __slots__ = ()


class EmptyOutgoingContentError(LionError, ValueError):
    """Raised when a model call is about to go out with no user content
    despite a non-empty instruction having been supplied."""

    default_message = "Assembled outgoing message content is empty"
    default_status_code = 500
    __slots__ = ()


class CLIInputRefusedError(LionError):
    """Raised when a request's input cannot be passed to a CLI, refused before any
    process starts.

    Deliberately not a ValueError: pydantic wraps a ValueError raised inside a model
    validator in a ValidationError, and that wrapper carries the whole input it was
    validating, so a refusal that named one part of the input would expose every other
    part of it. An exception that is not a ValueError passes through the validator
    unwrapped.
    """

    default_message = "CLI input cannot be passed"
    default_status_code = 400
    __slots__ = ()


class InputTokenCapError(LionError):
    """Raised when an API request's estimated input exceeds the model's
    ``max_input_tokens``, refused before the request is sent.

    Not a ValueError, for the reason given on CLIInputRefusedError: it is raised
    inside APICalling's validator and must reach the caller unwrapped. ``details``
    carries ``estimate`` and ``cap``.
    """

    default_message = "Request input exceeds max_input_tokens"
    default_status_code = 400
    __slots__ = ()


class FailedRoundError(LionError):
    """Raised when an API-model leg's tool loop keeps ending on a failed round: a response that
    stopped at its output token limit with no text and no tool call, a reply that did not parse,
    or an answer with no text. Raised once the loop has been continued past it as many times as it
    may be, so the run fails instead of ending as finished work. ``details`` carries ``continued``.
    """

    default_message = "The agent loop ended on a failed round"
    default_status_code = 502
    __slots__ = ()


ItemNotFoundError = NotFoundError
ItemExistsError = ExistsError
