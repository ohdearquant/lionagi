# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Typed provider error hierarchy for CLI worker failures; all subclasses remain RuntimeError-compatible."""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Base hierarchy
# ---------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Generic error surfaced by a CLI provider subprocess."""

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


class ProviderAuthError(ProviderError):
    """The provider CLI rejected the request due to invalid credentials."""


class ProviderContextError(ProviderError):
    """The provider CLI rejected the request because the context is too long."""


# ---------------------------------------------------------------------------
# Emission error (separate axis — not a provider subprocess failure)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Regex catalogue (case-insensitive patterns → subclass)
# ---------------------------------------------------------------------------

# Each entry: (compiled_pattern, subclass)
_QUOTA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"usage\s+limit\s+reached", re.IGNORECASE),
    re.compile(r"rate[\s._-]?limit[\s._-]?exceeded", re.IGNORECASE),
    re.compile(r"try\s+again\s+at\s+\d", re.IGNORECASE),
]

_AUTH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"invalid[\s._-]?api[\s._-]?key", re.IGNORECASE),
    re.compile(r"not\s+logged\s+in", re.IGNORECASE),
    re.compile(r"401.*unauthorized", re.IGNORECASE),
]

_CONTEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"context[\s._-]?(window|length)[\s._-]?(exceeded|too\s+long)", re.IGNORECASE),
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

    return ProviderError(content, stderr_tail=stderr_tail, raw=content)
