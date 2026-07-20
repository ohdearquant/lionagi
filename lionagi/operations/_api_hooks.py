# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Typed, optional service-to-session observation adapter for API_PRE_CALL / API_POST_CALL /
API_STREAM_CHUNK (ADR-0047 delta row 2).

These helpers only fire when the calling Branch is session-bound
(``branch._hooks is not None``); a standalone ``iModel`` never reaches
``operations/chat/chat.py`` or ``operations/run/run.py``, so its behavior is
unaffected. Emission is purely observational: it wraps the existing
``imodel.invoke()`` / streaming call sites from the outside and never touches
``HookRegistry``/``HookedEvent``, so per-``iModel`` pre-invocation control
(replace/abort/exit) is unchanged.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

__all__ = ("emit_api_pre_call", "emit_api_post_call", "emit_api_stream_chunk")

# Closed status vocabulary for the emitted payload — every EventStatus value
# (the terminal status an APICalling can settle into) plus "error" (this
# adapter's own label for a raised exception, never provider-reported).
# Anything outside this set — a raw provider status string, or a status
# object whose ``.value`` was never validated against EventStatus — is
# redacted to "unknown" rather than forwarded.
_STATUS_VOCAB = frozenset(
    {
        "pending",
        "processing",
        "completed",
        "failed",
        "skipped",
        "cancelled",
        "aborted",
        "error",
    }
)

# Expected shape for a model/provider identifier: lionagi's own naming
# convention (letters, digits, ``. _ - : /``), capped well above any real
# identifier in use. A value outside this shape is redacted rather than
# forwarded verbatim, even though these fields are normally sourced from
# local iModel/endpoint configuration rather than provider response text.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")

# A credential can satisfy the identifier allowlist above (API keys are
# typically ``[A-Za-z0-9_-]``), so a well-formed value carrying a known secret
# prefix is redacted anyway. Defense-in-depth: model/provider come from local
# config, but a misconfiguration that lands a key here must not reach telemetry
# verbatim. No real model/provider identifier starts with these prefixes.
_CREDENTIAL_RE = re.compile(
    r"(?i)^(?:bearer[\s_-]|basic[\s_-]|sk-|sk_|pk-|pk_|rk_|ak_|api[_-]?key|"
    r"token[_-]|secret[_-]|ghp_|gho_|ghs_|ghr_|github_pat_|xox[baprs]-)"
)


def _safe_status(value: Any) -> str:
    return value if isinstance(value, str) and value in _STATUS_VOCAB else "unknown"


def _safe_identifier(value: Any) -> str:
    if not (isinstance(value, str) and _IDENTIFIER_RE.match(value)):
        return "unknown"
    if _CREDENTIAL_RE.search(value):
        return "unknown"
    return value


def _model_and_provider(imodel: Any) -> tuple[str, str]:
    model = getattr(imodel, "model_name", None) or ""
    provider = ""
    endpoint = getattr(imodel, "endpoint", None)
    config = getattr(endpoint, "config", None)
    if config is not None:
        provider = getattr(config, "provider", None) or ""
    return _safe_identifier(model), _safe_identifier(provider)


def _extract_tokens(response: Any) -> dict | None:
    """Best-effort provider-usage extraction; ``None`` when the shape is unrecognized.

    Mirrors the normalization already used by
    ``lionagi.session.signal._collect_branch_usage``: a provider's raw
    response dict (or the last item of a list of them) carries an optional
    ``usage`` mapping.
    """
    item = response[-1] if isinstance(response, list) and response else response
    if not isinstance(item, dict):
        return None
    usage = item.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


def _typed_usage(tokens: dict | None) -> dict[str, int] | None:
    """Reduce a best-effort usage mapping to a typed numeric summary.

    The raw ``tokens`` dict (a provider's own response shape, forwarded
    verbatim before this fix) can carry non-numeric fields alongside the
    counts. Only ``input_tokens``/``output_tokens`` (or their
    ``prompt_tokens``/``completion_tokens`` synonyms — same normalization as
    ``_collect_branch_usage``) survive, coerced to ``int``; every other key,
    and any non-numeric value under a recognized key, is dropped. ``None``
    when neither count is present, matching the prior no-usage contract.
    """
    if not isinstance(tokens, dict):
        return None

    def _num(*keys: str) -> int | None:
        for key in keys:
            val = tokens.get(key)
            if isinstance(val, int | float) and not isinstance(val, bool):
                if isinstance(val, float) and not math.isfinite(val):
                    continue  # NaN/inf can't coerce to int; treat as absent
                return int(val)
        return None

    input_tokens = _num("input_tokens", "prompt_tokens")
    output_tokens = _num("output_tokens", "completion_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return {"input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0}


def _error_summary(error: str | BaseException | None) -> str | None:
    """Exception-class-name-only summary of a call failure.

    Matches the ``TOOL_ERROR`` hook convention (``operations/act/act.py``
    forwards the exception object itself, never a stringified message) --
    the raw text of a provider exception routinely carries request bodies,
    full URLs with query parameters, or header/credential fragments, and
    this payload is persisted verbatim to observer telemetry. A
    non-exception failure reason (``APICalling.execution.error`` can be a
    plain ``str``) is equally capable of embedding that text, so it gets the
    same generic, content-free label rather than a per-type name.
    """
    if error is None:
        return None
    if isinstance(error, BaseException):
        return type(error).__name__
    return "ProviderError"


async def emit_api_pre_call(branch: Branch, imodel: Any) -> None:
    """Fire API_PRE_CALL immediately before a session-bound iModel is invoked."""
    hooks = branch._hooks
    if hooks is None:
        return
    from lionagi.hooks.bus import HookPoint

    model, provider = _model_and_provider(imodel)
    await hooks.emit(
        HookPoint.API_PRE_CALL,
        session_id=str(branch._owning_session_id or branch.id),
        branch_id=str(branch.id),
        model=model,
        provider=provider,
    )


async def emit_api_post_call(
    branch: Branch,
    imodel: Any,
    api_call: Any = None,
    *,
    error: str | BaseException | None = None,
    tokens: dict | None = None,
) -> None:
    """Fire API_POST_CALL once the call has settled — success, provider-reported
    failure (``api_call.status``), or a raised exception (``error``).

    Every ``API_PRE_CALL`` this adapter's caller emits is paired with exactly
    one ``API_POST_CALL`` carrying whatever is actually known about how the
    call ended:

    - ``status``: ``"error"`` when an exception was raised (``error`` is
      set), otherwise ``api_call.status`` mapped onto the closed status
      vocabulary (``"completed"``/``"failed"``/... — anything else becomes
      ``"unknown"``, never a raw provider string).
    - ``error``: populated whenever *either* an exception was raised *or*
      the call settled with a provider-reported failure and nothing was
      raised (``api_call.execution.error``) -- a FAILED ``APICalling`` that
      never raises must not leave this field null just because raising
      wasn't how it failed. Always reduced to a class-name-only summary
      (see ``_error_summary``), never the raw message.
    - ``tokens``: typed numeric usage summary (``input_tokens``/
      ``output_tokens`` ints); ``None`` when the shape is unrecognized or
      the call never produced one. Never the raw provider usage mapping.
    """
    hooks = branch._hooks
    if hooks is None:
        return
    from lionagi.hooks.bus import HookPoint

    model, provider = _model_and_provider(imodel)
    duration = getattr(getattr(api_call, "execution", None), "duration", None)
    latency_ms = duration * 1000.0 if isinstance(duration, int | float) else None

    status_obj = getattr(api_call, "status", None)
    provider_status = getattr(status_obj, "value", None)

    if error is not None:
        status = "error"
    else:
        status = provider_status
        if provider_status == "failed":
            error = getattr(getattr(api_call, "execution", None), "error", None)

    if tokens is None and api_call is not None:
        tokens = _extract_tokens(getattr(api_call, "response", None))

    await hooks.emit(
        HookPoint.API_POST_CALL,
        session_id=str(branch._owning_session_id or branch.id),
        branch_id=str(branch.id),
        model=model,
        provider=provider,
        status=_safe_status(status),
        latency_ms=latency_ms,
        tokens=_typed_usage(tokens),
        error=_error_summary(error),
    )


async def emit_api_stream_chunk(branch: Branch, imodel: Any, chunk: Any) -> None:
    """Fire API_STREAM_CHUNK for one chunk of a session-bound streaming response.

    Only a redacted chunk-type discriminator is forwarded — matching the
    TOOL_PRE/TOOL_POST convention of summary-only telemetry (see
    ``operations/act/act.py``) rather than the raw chunk payload.
    """
    hooks = branch._hooks
    if hooks is None:
        return
    from lionagi.hooks.bus import HookPoint

    model, provider = _model_and_provider(imodel)
    chunk_type = _safe_identifier(getattr(chunk, "type", None) or type(chunk).__name__)
    await hooks.emit(
        HookPoint.API_STREAM_CHUNK,
        session_id=str(branch._owning_session_id or branch.id),
        branch_id=str(branch.id),
        model=model,
        provider=provider,
        chunk_type=chunk_type,
    )
