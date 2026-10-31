# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private, scalar-only observation bridge for CLI API usage.

Construction owns a request. A processor's ambient context never supplies a
missing owner, and neither the turn nor the request frame retains its collector.
"""

from __future__ import annotations

import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol


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
