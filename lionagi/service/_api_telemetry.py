# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private, scalar-only observation bridge for CLI API usage.

Construction owns a request. A processor's ambient context never supplies a
missing owner, and neither the turn nor the request frame retains its collector.
"""

from __future__ import annotations

import math
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Literal, Protocol


@dataclass(frozen=True)
class InputDecision:
    kind: Literal["checked", "refused", "uncapped"]
    counted: int | None
    scale: float | None
    estimate: int | None
    cap: int | None
    prompt_chars: int | None
    provider: str | None
    model: str | None


class APIUsageSink(Protocol):
    def record_request(
        self, *, turn_index: int, decision: InputDecision, api_call_id: str | None
    ) -> int | None: ...

    def mark_dispatch(self, request_index: int) -> None: ...

    def record_cache_usage(
        self, request_index: int, *, read: int | None, write: int | None
    ) -> None: ...

    def record_served_model(self, request_index: int, model: str | None) -> None: ...

    def record_token_usage(
        self,
        request_index: int,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost_nusd: int | None,
        price_table_version: str | None,
    ) -> None: ...

    def record_selected_attempt(
        self,
        request_index: int,
        *,
        attempt_id: str,
        provider: str,
        account_label: str | None,
    ) -> None: ...

    def record_guard_refusal(self, *, call_id: str) -> None: ...


@dataclass(frozen=True)
class RequestFrame:
    owner: weakref.ReferenceType[APIUsageSink]
    request_index: int


_turn: ContextVar[tuple[weakref.ReferenceType[APIUsageSink], int] | None] = ContextVar(
    "api_usage_turn", default=None
)
_request: ContextVar[RequestFrame | None] = ContextVar("api_usage_request", default=None)
_cache_source: ContextVar[list[bool] | None] = ContextVar("api_usage_cache_source", default=None)


@contextmanager
def observe_cache_source() -> Iterator[list[bool]]:
    """Track whether one endpoint response came from transport execution.

    ``Endpoint.call`` claims this marker before dispatch. A noncached call marks
    it directly, while a cache-enabled call marks it only inside the cached
    function, which runs on a miss and not on a hit. The mutable marker carries
    that result back across the endpoint call; callers must treat overridden
    ``Endpoint.call`` implementations as unattributed because they can change
    these ordering semantics.
    """
    source = [False]
    token = _cache_source.set(source)
    try:
        yield source
    finally:
        _cache_source.reset(token)


def claim_cache_source() -> list[bool] | None:
    source = _cache_source.get()
    if source is not None:
        _cache_source.set(None)
    return source


def mark_uncached_response(source: list[bool] | None) -> None:
    if source is not None:
        source[0] = True


def _live(owner: weakref.ReferenceType[APIUsageSink]) -> APIUsageSink | None:
    collector = owner()
    return collector if collector is not None and not getattr(collector, "closed", False) else None


def is_collecting() -> bool:
    """Whether optional construction measurement has a live, open turn owner."""
    scope = _turn.get()
    return scope is not None and _live(scope[0]) is not None


@contextmanager
def collect_api_turn(collector: APIUsageSink, turn_index: int) -> Iterator[None]:
    token = _turn.set((weakref.ref(collector), turn_index))
    try:
        yield
    finally:
        _turn.reset(token)


def capture_request(decision: InputDecision, *, api_call_id: str | None) -> RequestFrame | None:
    scope = _turn.get()
    if scope is None:
        return None
    try:
        collector = _live(scope[0])
        if collector is None:
            return None
        index = collector.record_request(
            turn_index=scope[1], decision=decision, api_call_id=api_call_id
        )
        if isinstance(index, int) and not isinstance(index, bool) and index > 0:
            return RequestFrame(scope[0], index)
    except Exception:
        # Observation must not replace an SDK result, refusal or cancellation.
        return None
    return None


@contextmanager
def activate_request(frame: RequestFrame | None) -> Iterator[None]:
    # None must shadow an outer frame: an unowned queued call cannot borrow it.
    token = _request.set(frame)
    try:
        yield
    finally:
        _request.reset(token)


def mark_dispatch(frame: RequestFrame | None) -> None:
    if frame is not None:
        try:
            if collector := _live(frame.owner):
                collector.mark_dispatch(frame.request_index)
        except Exception:
            return


def record_cache_usage(frame: RequestFrame | None, *, read: int | None, write: int | None) -> None:
    if frame is not None:
        try:
            if collector := _live(frame.owner):
                collector.record_cache_usage(frame.request_index, read=read, write=write)
        except Exception:
            return


def record_served_model(frame: RequestFrame | None, model: str | None) -> None:
    if frame is not None:
        try:
            if collector := _live(frame.owner):
                collector.record_served_model(frame.request_index, model)
        except Exception:
            return


def _usage_token_counts(usage: Any) -> tuple[int | float | None, int | float | None]:
    """Read the two provider count aliases while preserving missing values."""
    if not isinstance(usage, dict):
        return None, None

    def number(*keys: str) -> int | float | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                return value
        return None

    return number("input_tokens", "prompt_tokens"), number("output_tokens", "completion_tokens")


def _estimate_response_cost(
    served_model: str, usage: dict, payload: dict
) -> tuple[int | None, str | None]:
    from .accounts._pricing import CURRENT_REVISION, price_usage, request_ttls
    from .accounts._types import AccountRoutingError

    if served_model not in CURRENT_REVISION.models:
        return None, None

    # With no reported serving tier, only a standard_only request can be
    # priced at the standard rate.
    if usage.get("service_tier") is None and payload.get("service_tier") != "standard_only":
        return None, None

    # Read the submitted cache markers for any cache write lifetime.
    try:
        ttls = request_ttls({"service_tier": "standard_only", **payload})
    except AccountRoutingError:
        # The response can describe all cache activity even if the request
        # shape cannot be used to infer a write lifetime.
        if any(
            usage.get(key) is None
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
        ):
            return None, None
        ttls = frozenset()
    priced = price_usage(CURRENT_REVISION, ttls, served_model, usage)
    if priced is None:
        return None, None
    return priced.cost_nusd, CURRENT_REVISION.id


def _record_response_usage(
    frame: RequestFrame | None,
    usage: Any,
    *,
    provider: str,
    endpoint: str,
    served_model: str | None,
    payload: dict,
) -> None:
    if frame is None:
        return

    try:
        collector = _live(frame.owner)
        if collector is None:
            return
        provider_input, provider_output = _usage_token_counts(usage)
        input_tokens = (
            provider_input if type(provider_input) is int and 0 <= provider_input < 2**63 else None
        )
        output_tokens = (
            provider_output
            if type(provider_output) is int and 0 <= provider_output < 2**63
            else None
        )
        if input_tokens is None and output_tokens is None:
            return
        cost = version = None
        if (
            provider == "anthropic"
            and endpoint == "messages"
            and served_model is not None
            and input_tokens is not None
            and output_tokens is not None
        ):
            try:
                cost, version = _estimate_response_cost(served_model, usage, payload)
            except Exception:
                # A price estimate is optional even when counts are available.
                cost, version = None, None
        collector.record_token_usage(
            frame.request_index,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_nusd=cost,
            price_table_version=version,
        )
    except Exception:
        # Observation must not replace a result or a cancellation.
        return


def record_selected_attempt(*, attempt_id: str, provider: str, account_label: str | None) -> None:
    frame = _request.get()
    if frame is not None:
        try:
            if collector := _live(frame.owner):
                collector.record_selected_attempt(
                    frame.request_index,
                    attempt_id=attempt_id,
                    provider=provider,
                    account_label=account_label,
                )
        except Exception:
            return


def record_guard_refusal(*, call_id: str) -> None:
    scope = _turn.get()
    if scope is not None:
        try:
            if collector := _live(scope[0]):
                collector.record_guard_refusal(call_id=call_id)
        except Exception:
            return
