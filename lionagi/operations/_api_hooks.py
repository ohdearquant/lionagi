# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Typed, optional service-to-session observation adapter for API_PRE_CALL / API_POST_CALL /
API_STREAM_CHUNK.

Only fires when the calling Branch is session-bound (``branch._hooks is not
None``); a standalone ``iModel`` is unaffected. Purely observational — wraps
the existing invoke/streaming call sites without touching
``HookRegistry``/``HookedEvent``, so per-``iModel`` pre-invocation control is
unchanged.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

__all__ = ("emit_api_pre_call", "emit_api_post_call", "emit_api_stream_chunk")

# Every EventStatus value plus "error" (this adapter's own label for a raised
# exception). Anything else is redacted to "unknown" rather than forwarded.
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

# lionagi model/provider identifier shape; anything outside it is redacted.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")

# A credential can satisfy the identifier allowlist above, so known secret
# prefixes are redacted too — defense-in-depth against a misconfigured key
# landing in model/provider fields.
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


# Normalized StreamChunk.type values (service/types/stream_chunk.py); chunk.type
# reaches this adapter from a provider stream and could carry arbitrary text,
# so only this closed vocabulary is forwarded — anything else is redacted.
_CHUNK_TYPE_VOCAB = frozenset(
    {"system", "thinking", "text", "tool_use", "tool_result", "result", "error"}
)


def _safe_chunk_type(value: Any) -> str:
    return value if isinstance(value, str) and value in _CHUNK_TYPE_VOCAB else "unknown"


def _model_and_provider(imodel: Any) -> tuple[str, str]:
    model = getattr(imodel, "model_name", None) or ""
    provider = ""
    endpoint = getattr(imodel, "endpoint", None)
    config = getattr(endpoint, "config", None)
    if config is not None:
        provider = getattr(config, "provider", None) or ""
    return _safe_identifier(model), _safe_identifier(provider)


def _extract_tokens(response: Any) -> dict | None:
    """Best-effort provider-usage extraction; ``None`` when the shape is unrecognized."""
    item = response[-1] if isinstance(response, list) and response else response
    if not isinstance(item, dict):
        return None
    usage = item.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


def _typed_usage(tokens: dict | None) -> dict[str, int] | None:
    """Reduce a best-effort usage mapping to a typed numeric summary.

    Only ``input_tokens``/``output_tokens`` (or their ``prompt_tokens``/
    ``completion_tokens`` synonyms) survive, coerced to ``int``; everything
    else is dropped. ``None`` when neither count is present.
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

    Never the raw message — provider exception text routinely carries request
    bodies, URLs, or credential fragments, and this is persisted verbatim to
    observer telemetry.
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
    failure, or a raised exception.

    See docs/internals/providers.md#api-post-call-contract for the pairing
    and field-population contract.
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

    Only a redacted chunk-type discriminator is forwarded, never the raw chunk payload.
    """
    hooks = branch._hooks
    if hooks is None:
        return
    from lionagi.hooks.bus import HookPoint

    model, provider = _model_and_provider(imodel)
    chunk_type = _safe_chunk_type(getattr(chunk, "type", None))
    await hooks.emit(
        HookPoint.API_STREAM_CHUNK,
        session_id=str(branch._owning_session_id or branch.id),
        branch_id=str(branch.id),
        model=model,
        provider=provider,
        chunk_type=chunk_type,
    )
