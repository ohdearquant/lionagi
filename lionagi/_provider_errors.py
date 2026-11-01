# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Typed provider error hierarchy for CLI worker failures; all subclasses remain RuntimeError-compatible."""

from __future__ import annotations

import re
from typing import Any, ClassVar

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


class _WorkerSpawnAdmissionError(ProviderError):
    """A durable worker owner refused admission or registration."""

    retryable: ClassVar[bool] = False


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


class ProviderTeardownError(ProviderError):
    """A provider transport failed while releasing its stream or session."""

    retryable: ClassVar[bool] = True


class ProviderPermissionError(ProviderError):
    """The turn produced nothing because the CLI could not be granted a tool
    permission. Not a provider fault — a headless CLI can't prompt for a
    permission, so a tool call it wasn't pre-granted is denied and the turn
    returns empty. Not retryable: an unmodified retry reproduces this
    exactly, forever, unlike an actual provider outage.
    """


class ProviderAdapterError(ProviderError):
    """A CLI adapter reported a non-success status with no more specific cause
    identified from the message text (e.g. an `agy` turn ending status=ERROR)."""

    retryable: ClassVar[bool] = True


class WorkerLivenessError(ProviderError):
    """Worker missed a first-output or between-chunk liveness window."""

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


class SubagentAdmissionRefusedError(ProviderError):
    """No machine-wide slot for a CLI agent: a pool stayed at its cap, or admission could
    not be read. Raised before the child exists, or just after it when its start time
    could not be read twice, in which case the child is killed before this is raised;
    with no start time to prove its process group, the group is not signalled. Also
    raised just after the spawn when a slot cannot record the child, once ending the
    child's group has been attempted (signalled, then waited for a bounded time).

    Retryable: at a cap the same request admits once a slot frees or the cap is raised,
    and a slot that could not be written can be written on a later attempt, once the
    disk has room.
    """

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        pool: str = "",
        cap: int = 0,
        holders: tuple[int | None, ...] = (),
    ) -> None:
        super().__init__(message)
        self.pool: str = pool
        self.cap: int = cap
        self.holders: tuple[int | None, ...] = holders


class NestedAgentRefusedError(ProviderError):
    """A process running inside a CLI agent lionagi started tried to start another CLI
    agent through lionagi. Raised before any admission, so nothing ran and no slot was
    waited for.

    Not retryable: the same command run from inside the same agent is refused the same way.
    """

    retryable: ClassVar[bool] = False


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
    # Claude Code's refusal of a signed-in account ("You've hit your session
    # limit"). Read only when no status came with the text: Claude Code reports
    # 429 beside it, and a reported status decides (classify_provider_error).
    re.compile(r"hit\s+your\s+session\s+limit", re.IGNORECASE),
    re.compile(r"purchase\s+more\s+credits", re.IGNORECASE),
]

_AUTH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"invalid[\s._-]?api[\s._-]?key", re.IGNORECASE),
    re.compile(r"not\s+logged\s+in", re.IGNORECASE),
    re.compile(r"401.*unauthorized", re.IGNORECASE),
]

_CONTEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"context[\s._-]?(window|length)[\s._-]?(exceeded|too\s+long)", re.IGNORECASE),
    re.compile(
        r"prompt\s+is\s+too\s+long[\s\S]*?"
        r"request\s+is\s+~?[\d,]+\s+tokens?\s+\(limit\s+~?[\d,]+\)",
        re.IGNORECASE,
    ),
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

_TEARDOWN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"agy\s+teardown\s+failed:\s*event\s+loop\s+is\s+closed", re.IGNORECASE),
]

# A turn that ended with nothing because a tool call could not be permitted.
# Matched on what the adapter says about the cause rather than on the word
# "permission" alone, which appears in plenty of unrelated provider text.
_PERMISSION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"auto[\s._-]?denied", re.IGNORECASE),
    re.compile(r"cannot\s+prompt\s+for\s+a?\s*tool\s+permission", re.IGNORECASE),
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
    status: Any = None,
) -> ProviderError:
    """Return the most-specific ProviderError subclass matching content; falls back to base ProviderError.

    ``status`` is the HTTP status a provider reported beside the error, if it reported one. It
    decides quota alone: 429 is a quota refusal whatever the text says, and under any other status
    no text makes it one, since text naming a limit may only be quoting it.
    """
    combined = f"{content}\n{stderr_tail}" if stderr_tail else content

    if status == 429:
        return ProviderQuotaError(content, stderr_tail=stderr_tail, raw=content)
    if status is None:
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

    for pat in _TEARDOWN_PATTERNS:
        if pat.search(combined):
            return ProviderTeardownError(content, stderr_tail=stderr_tail, raw=content)

    # Ahead of the adapter catch-all deliberately, and the order is the whole
    # change rather than a tidiness preference. A permission failure is reported
    # BY an adapter, so its text can satisfy both patterns; whichever branch runs
    # first wins, and the catch-all would swallow the specific cause while every
    # pattern-level test still passed. The arm that pins this feeds a string
    # matching both.
    for pat in _PERMISSION_PATTERNS:
        if pat.search(combined):
            return ProviderPermissionError(content, stderr_tail=stderr_tail, raw=content)

    for pat in _ADAPTER_PATTERNS:
        if pat.search(combined):
            return ProviderAdapterError(content, stderr_tail=stderr_tail, raw=content)

    return ProviderError(content, stderr_tail=stderr_tail, raw=content)


# --- Provider contract errors (deliberately not ProviderError subclasses) ---
#
# These describe how a provider catalog is composed or activated, not how a provider
# call failed. Code that catches ProviderError retries or falls back, which an
# unsupported contract or a failed activation must never trigger. Each message is
# built from the error's fields alone and never carries the text of a chained exception.


def _rebuild_contract_error(cls: type[Any], fields: dict[str, Any]) -> Any:
    """Unpickle helper: these constructors are keyword-only, so the fields are replayed by name."""
    return cls(**fields)


class ProviderContractVersionError(ValueError):
    """A provider declaration names a contract version that is not supported.

    ``observed`` is the raw declared value (it may be a bool, a str or ``None``) and is
    kept as given; the message shows it with ``repr``. Raised before any factory runs.
    """

    def __init__(self, *, source: str, expected: tuple[int, ...], observed: object) -> None:
        expected = tuple(expected)
        versions = ", ".join(str(version) for version in expected)
        super().__init__(
            f"Unsupported provider contract version in {source}: "
            f"expected one of {versions}; observed {observed!r}"
        )
        self.source: str = source
        self.expected: tuple[int, ...] = expected
        self.observed: object = observed

    def __reduce__(self):
        fields = {"source": self.source, "expected": self.expected, "observed": self.observed}
        return (_rebuild_contract_error, (type(self), fields))


class ProviderActivationError(RuntimeError):
    """A provider endpoint could not be activated.

    ``reason`` is ``"activation_failed"`` for an internal import failure or a factory
    defect, and ``"cycle"`` for a recursive activation of the same endpoint; any other
    value raises ``ValueError``. The raiser chains the original exception with
    ``raise ... from exc``; its text is not part of the message.
    """

    _REASONS: ClassVar[tuple[str, ...]] = ("activation_failed", "cycle")

    def __init__(
        self,
        *,
        provider: str,
        endpoint: str | None,
        source: str,
        reason: str,
    ) -> None:
        if reason not in self._REASONS:
            raise ValueError(f"reason must be one of {', '.join(self._REASONS)}; got {reason!r}")
        scope = f"Provider {provider!r}"
        if endpoint is not None:
            scope += f" endpoint {endpoint!r}"
        super().__init__(f"{scope} could not be activated from {source}: reason={reason}")
        self.provider: str = provider
        self.endpoint: str | None = endpoint
        self.source: str = source
        self.reason: str = reason

    def __reduce__(self):
        fields = {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "source": self.source,
            "reason": self.reason,
        }
        return (_rebuild_contract_error, (type(self), fields))


class ProviderCompositionError(ValueError):
    """No provider catalog was supplied, and the caller must compose one explicitly.

    The message names the missing catalog and never mentions a missing distribution:
    whether a hub distribution is installed is a separate question.
    """

    def __init__(self) -> None:
        super().__init__(
            "No provider catalog was supplied; explicit composition of a provider "
            "catalog is required."
        )

    def __reduce__(self):
        return (type(self), ())


class ProviderCollisionError(ValueError):
    """Two declarations in one provider catalog claim the same identity.

    ``kind`` names what collided: ``"provider"`` (two fragments with one provider key),
    ``"provider_alias"`` (a provider alias claimed twice, or equal to another provider's
    key), ``"endpoint"`` (an endpoint key or alias claimed twice inside one provider) or
    ``"duplicate_declaration"`` (the same declaration object repeated inside one input);
    any other value raises ``ValueError``. ``key`` is the contested name and
    ``first_owner`` / ``second_owner`` identify the two claimants.

    No catalog was composed. This is neither a ``ProviderCompositionError`` (which means
    no catalog was supplied, and a handler for it must not catch a collision) nor a
    ``ProviderError``.
    """

    _KINDS: ClassVar[tuple[str, ...]] = (
        "provider",
        "provider_alias",
        "endpoint",
        "duplicate_declaration",
    )

    def __init__(self, *, key: str, kind: str, first_owner: str, second_owner: str) -> None:
        if kind not in self._KINDS:
            raise ValueError(f"kind must be one of {', '.join(self._KINDS)}; got {kind!r}")
        super().__init__(
            f"Provider catalog collision ({kind}) on {key!r}: "
            f"{first_owner} conflicts with {second_owner}"
        )
        self.key: str = key
        self.kind: str = kind
        self.first_owner: str = first_owner
        self.second_owner: str = second_owner

    def __reduce__(self):
        fields = {
            "key": self.key,
            "kind": self.kind,
            "first_owner": self.first_owner,
            "second_owner": self.second_owner,
        }
        return (_rebuild_contract_error, (type(self), fields))
