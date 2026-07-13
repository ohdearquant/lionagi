# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Typed provider error hierarchy for CLI worker failures; all subclasses remain RuntimeError-compatible."""

from __future__ import annotations

import re
from typing import ClassVar

# --- Base hierarchy ---


class ProviderError(RuntimeError):
    """Generic error surfaced by a CLI provider subprocess.

    ``retryable`` is a class-level classification hint (not a retry
    implementation): True for failures that are transient in nature (rate
    limits, capacity, dropped streams), False for failures that will recur
    on an unmodified retry (bad credentials, unsupported model/tool, context
    overflow, safety rejection). Callers that add retry/backoff logic later
    can key off this attribute instead of re-deriving it from the message.
    """

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        stderr_tail: str = "",
        raw: str = "",
    ) -> None:
        super().__init__(message)
        self.stderr_tail: str = stderr_tail
        self.raw: str = raw or message


class ProviderQuotaError(ProviderError):
    """The provider CLI rejected the request due to usage/rate limits."""

    retryable: ClassVar[bool] = True


class ProviderAuthError(ProviderError):
    """The provider CLI rejected the request due to invalid credentials."""


class ProviderContextError(ProviderError):
    """The provider CLI rejected the request because the context is too long."""


class ProviderCapacityError(ProviderError):
    """The provider CLI rejected the request because the selected model is at capacity."""

    retryable: ClassVar[bool] = True


class ProviderUnsupportedModelError(ProviderError):
    """The provider CLI rejected the request because the model or tool is not
    supported for the current account/configuration (e.g. a model name the
    signed-in account tier cannot use, or a tool the model doesn't support)."""


class ProviderSafetyError(ProviderError):
    """The provider CLI rejected the request because content was flagged by a safety filter."""


class ProviderStreamDisconnectError(ProviderError):
    """The provider CLI's stream disconnected before the turn completed (network drop, idle timeout)."""

    retryable: ClassVar[bool] = True


class ProviderAdapterError(ProviderError):
    """A CLI adapter reported a non-success status with no more specific cause
    identified from the message text (e.g. an `agy` turn ending status=ERROR)."""

    retryable: ClassVar[bool] = True


class WorkerLivenessError(ProviderError):
    """Spawn/hang failure: no first-stream output within the liveness window across every retry, distinct from a classified content error."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "worker.no_first_output",
        stderr_tail: str = "",
        raw: str = "",
    ) -> None:
        super().__init__(message, stderr_tail=stderr_tail, raw=raw)
        self.reason: str = reason


# --- Emission error (separate axis — not a provider subprocess failure) ---


class EmissionError(RuntimeError):
    """An agent in a multi-agent pipeline failed to emit expected structured output."""

    def __init__(
        self,
        message: str,
        *,
        agent: str = "",
        attempts: int = 0,
        stage: str = "",
    ) -> None:
        super().__init__(message)
        self.agent: str = agent
        self.attempts: int = attempts
        self.stage: str = stage


# --- Regex catalogue (case-insensitive patterns → subclass) ---

# Each entry: (compiled_pattern, subclass)
_QUOTA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"usage\s+limit\s+reached", re.IGNORECASE),
    re.compile(r"rate[\s._-]?limit[\s._-]?exceeded", re.IGNORECASE),
    re.compile(r"try\s+again\s+at\s+\d", re.IGNORECASE),
    # Date-formatted retry times ("try again at Jul 6th, 2026 11:08 PM") don't
    # have a digit immediately after "at", so the pattern above misses them —
    # match the quota phrasing itself instead of the retry-time suffix.
    re.compile(r"hit\s+your\s+usage\s+limit", re.IGNORECASE),
    re.compile(r"purchase\s+more\s+credits", re.IGNORECASE),
]

_AUTH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"invalid[\s._-]?api[\s._-]?key", re.IGNORECASE),
    re.compile(r"not\s+logged\s+in", re.IGNORECASE),
    re.compile(r"401.*unauthorized", re.IGNORECASE),
]

_CONTEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"context[\s._-]?(window|length)[\s._-]?(exceeded|too\s+long)", re.IGNORECASE),
    # "Codex ran out of room in the model's context window." never uses the
    # exceeded/too-long wording the pattern above expects.
    re.compile(r"ran\s+out\s+of\s+room.*context\s+window", re.IGNORECASE),
]

_CAPACITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"model\s+is\s+at\s+capacity", re.IGNORECASE),
]

_UNSUPPORTED_MODEL_PATTERNS: list[re.Pattern[str]] = [
    # "the '<model>' model is not supported when using Codex with a ChatGPT
    # account" — anchored on "model" so an incidental "is not supported"
    # inside a transient message (e.g. a dropped-stream notice mentioning
    # reconnect support) is not misread as a permanent model rejection.
    re.compile(r"model\s+is\s+not\s+supported", re.IGNORECASE),
    # "Tool '<name>' is not supported with <model>".
    re.compile(r"tool\s+'[^']+'\s+is\s+not\s+supported", re.IGNORECASE),
]

_SAFETY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"flagged\s+for\s+(possible\s+)?cybersecurity\s+risk", re.IGNORECASE),
]

_STREAM_DISCONNECT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"stream\s+disconnected\s+before\s+completion", re.IGNORECASE),
]

# Catch-all for adapters (e.g. `agy`) that report a bare non-success status
# with no parseable cause in the message. Checked last — a more specific
# pattern above always wins when the adapter's own text names one.
_ADAPTER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"agy\s+returned\s+status=", re.IGNORECASE),
]


def classify_provider_error(
    content: str,
    *,
    stderr_tail: str = "",
) -> ProviderError:
    """Return the most-specific ProviderError subclass matching content; falls back to base ProviderError."""
    combined = f"{content}\n{stderr_tail}" if stderr_tail else content

    for pat in _QUOTA_PATTERNS:
        if pat.search(combined):
            return ProviderQuotaError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _AUTH_PATTERNS:
        if pat.search(combined):
            return ProviderAuthError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _CONTEXT_PATTERNS:
        if pat.search(combined):
            return ProviderContextError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _CAPACITY_PATTERNS:
        if pat.search(combined):
            return ProviderCapacityError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _UNSUPPORTED_MODEL_PATTERNS:
        if pat.search(combined):
            return ProviderUnsupportedModelError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _SAFETY_PATTERNS:
        if pat.search(combined):
            return ProviderSafetyError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _STREAM_DISCONNECT_PATTERNS:
        if pat.search(combined):
            return ProviderStreamDisconnectError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _ADAPTER_PATTERNS:
        if pat.search(combined):
            return ProviderAdapterError(content, stderr_tail=stderr_tail, raw=content)

    return ProviderError(content, stderr_tail=stderr_tail, raw=content)
