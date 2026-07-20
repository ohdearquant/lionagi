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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

__all__ = ("emit_api_pre_call", "emit_api_post_call", "emit_api_stream_chunk")


def _model_and_provider(imodel: Any) -> tuple[str, str]:
    model = getattr(imodel, "model_name", None) or ""
    provider = ""
    endpoint = getattr(imodel, "endpoint", None)
    config = getattr(endpoint, "config", None)
    if config is not None:
        provider = getattr(config, "provider", None) or ""
    return model, provider


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
    failure (``api_call.status``), or a raised exception (``error``)."""
    hooks = branch._hooks
    if hooks is None:
        return
    from lionagi.hooks.bus import HookPoint

    model, provider = _model_and_provider(imodel)
    duration = getattr(getattr(api_call, "execution", None), "duration", None)
    latency_ms = duration * 1000.0 if isinstance(duration, int | float) else None

    if error is not None:
        status = "error"
    else:
        status_obj = getattr(api_call, "status", None)
        status = getattr(status_obj, "value", None)

    if tokens is None and api_call is not None:
        tokens = _extract_tokens(getattr(api_call, "response", None))

    await hooks.emit(
        HookPoint.API_POST_CALL,
        session_id=str(branch._owning_session_id or branch.id),
        branch_id=str(branch.id),
        model=model,
        provider=provider,
        status=status,
        latency_ms=latency_ms,
        tokens=tokens,
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
    chunk_type = getattr(chunk, "type", None) or type(chunk).__name__
    await hooks.emit(
        HookPoint.API_STREAM_CHUNK,
        session_id=str(branch._owning_session_id or branch.id),
        branch_id=str(branch.id),
        model=model,
        provider=provider,
        chunk_type=chunk_type,
    )
