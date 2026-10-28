# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Original-derived request-only projections of bounded tool result bodies."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

__all__ = ()

# Bumped when the projection renders the same result and bound differently; a protected first
# round records the version it was decided under (ADR-0130 Amendment 4).
PROJECTION_VERSION = 1


@dataclass(frozen=True)
class _ResultBody:
    key: object
    start: int
    end: int
    is_json: bool = False
    protected: bool = False


def _result_json(value) -> str:
    """Use the signed encoder acceptance, with sorted keys and no fallback."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _result_text(output) -> tuple[str, tuple[_ResultBody, ...]]:
    """Original-derived recovery text and exact global Unicode body spans.

    Outer mapping order is preserved. Raw strings are never JSON-escaped; labels and
    separators count toward offsets. The returned string/spans are immutable snapshots.
    """
    if isinstance(output, str):
        return output, (_ResultBody(None, 0, len(output)),)
    if not isinstance(output, dict):
        text = _result_json(output)
        return text, (_ResultBody(None, 0, len(text), is_json=True),)
    pieces: list[str] = []
    bodies: list[_ResultBody] = []
    position = 0
    for ordinal, (key, value) in enumerate(output.items()):
        is_json = not isinstance(value, str)
        try:
            body = _result_json(value) if is_json else value
        except (TypeError, ValueError):
            # YAML-renderable values without JSON text stay whole, outside the bodies.
            continue
        try:
            label_key = _result_json(key)
        except (TypeError, ValueError):
            # The mapping key is retained verbatim in the output. An ordinal label
            # needs no stringification of a YAML-only key and adds no API selector.
            label_key = _result_json({"entry": ordinal})
        label = f"[body key={label_key} kind={'json' if is_json else 'raw'} chars={len(body)}]\n"
        start = position + len(label)
        bodies.append(
            _ResultBody(key, start, start + len(body), is_json, key in ("success", "error"))
        )
        pieces.extend((label, body, "\n"))
        position = start + len(body) + 1
    return "".join(pieces), tuple(bodies)


class _ProjectionError(ValueError):
    """An original-derived result cannot be represented under the local policy."""


@dataclass(frozen=True)
class _ProjectedBody:
    body: _ResultBody
    eligible: bool
    original_length: int
    retained: int
    k_n: int
    projected_length: int


@dataclass(frozen=True)
class _ProjectedResult:
    output: Any
    bodies: tuple[_ProjectedBody, ...]


@dataclass
class _OriginalResult:
    original: Any
    index: int
    _maxima: dict[tuple[int, int], tuple[int, str | None]] = field(default_factory=dict)

    @property
    def output(self) -> Any:
        return self.original.content.output

    @cached_property
    def _recovery(self) -> tuple[str, tuple[_ResultBody, ...]]:
        return _result_text(self.output)

    @property
    def text(self) -> str:
        return self._recovery[0]

    @property
    def bodies(self) -> tuple[_ResultBody, ...]:
        return self._recovery[1]


def _shortened_body(
    source: _OriginalResult, body: _ResultBody, retained: int, max_chars: int
) -> tuple[str, int, int]:
    text = source.text[body.start : body.end]
    head = 3 * retained // 4
    tail = retained - head
    start = body.start + head
    stop = body.end - tail
    first_limit = min(max_chars, 16000, stop - start)
    marker = (
        f"\n[... {stop - start:,} chars omitted from [{start}, {stop}); recover with "
        f'context(action="read_result", index={source.index}, offset={start}, '
        f"limit={first_limit}); continue at next_offset with "
        f"limit=min({min(max_chars, 16000)}, {stop}-next_offset), stopping at {stop}.]\n"
    )
    value = text[:head] + marker + text[-tail:]
    if body.is_json:
        value = "[JSON preview]\n" + value
    return value, start, stop


def _largest_shortened(
    source: _OriginalResult,
    body: _ResultBody,
    *,
    max_chars: int,
    allowance: int,
    minimum: int = 2,
    protected_ranges: Sequence[tuple[int, int]] = (),
) -> tuple[int, str] | None:
    length = body.end - body.start
    # A candidate retaining more than N source characters cannot fit before its marker.
    upper = min(allowance, length - 1, max_chars)
    for retained in range(upper, max(2, minimum) - 1, -1):
        value, start, stop = _shortened_body(source, body, retained, max_chars)
        if any(begin < end and end > start and begin < stop for begin, end in protected_ranges):
            continue
        if len(value) <= max_chars:
            return retained, value
    return None


def _standing_maximum(
    source: _OriginalResult, number: int, max_chars: int
) -> tuple[int, str | None]:
    key = (number, max_chars)
    if key not in source._maxima:
        body = source.bodies[number]
        length = body.end - body.start
        if length <= max_chars:
            source._maxima[key] = (length, None)
        else:
            fit = _largest_shortened(source, body, max_chars=max_chars, allowance=length)
            if fit is None:
                raise _ProjectionError(
                    f"Tool-result body {number} at original index {source.index} cannot fit "
                    f"tool_result_max_chars={max_chars} with complete recovery instructions."
                )
            source._maxima[key] = fit
    return source._maxima[key]


def _retained_counts(values, lengths: list[int], *, recorded: bool) -> list[int]:
    counts = list(values)
    if len(counts) != len(lengths):
        if recorded:
            return lengths
        raise _ProjectionError("Result projection constraints do not match original bodies.")
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
        raise _ProjectionError("Result projection counts must be nonnegative integers.")
    return counts


def _project_original(
    source: _OriginalResult,
    *,
    max_chars: int,
    allowances: Sequence[int] | None = None,
    minimums: Sequence[int] | None = None,
    protected_ranges: Sequence[tuple[int, int]] = (),
) -> _ProjectedResult:
    output = source.output
    if not isinstance(output, (str, dict)):
        return _ProjectedResult(output, ())
    bodies = source.bodies
    lengths = [body.end - body.start for body in bodies]
    kept = lengths if allowances is None else _retained_counts(allowances, lengths, recorded=False)
    floors = (
        [0] * len(bodies)
        if minimums is None
        else _retained_counts(minimums, lengths, recorded=False)
    )
    for interval in protected_ranges:
        if (
            len(interval) != 2
            or any(isinstance(n, bool) or not isinstance(n, int) for n in interval)
            or interval[0] < 0
            or interval[1] < interval[0]
            or interval[1] > len(source.text)
        ):
            raise _ProjectionError("Protected result ranges must address original recovery text.")
    projected = output
    metrics = []
    for number, body in enumerate(bodies):
        length = lengths[number]
        if body.protected:
            metrics.append(_ProjectedBody(body, False, length, length, length, length))
            continue
        k_n, _standing = _standing_maximum(source, number, max_chars)
        if length <= max_chars and kept[number] >= length and floors[number] <= length:
            metrics.append(_ProjectedBody(body, True, length, length, k_n, length))
            continue
        fit = _largest_shortened(
            source,
            body,
            max_chars=max_chars,
            allowance=min(kept[number], k_n),
            minimum=floors[number],
            protected_ranges=protected_ranges,
        )
        if fit is None:
            raise _ProjectionError(
                f"Tool-result body {number} at original index {source.index} cannot fit "
                f"tool_result_max_chars={max_chars} with its retained-source constraints."
            )
        retained, value = fit
        if isinstance(output, str):
            projected = value
        else:
            if projected is output:
                projected = dict(output)
            projected[body.key] = value
        metrics.append(_ProjectedBody(body, True, length, retained, k_n, len(value)))
    return _ProjectedResult(projected, tuple(metrics))


class _ProjectionSession:
    def __init__(self, branch: Any, max_chars: int, *, authority=None):
        self.max_chars = max_chars
        if authority is None:
            self._pile = branch.msgs.messages
            self._positions = {str(uid): i for i, uid in enumerate(branch.msgs.progression)}
            self._sources: dict[str, _OriginalResult] = {}
        else:
            self._pile = authority._pile
            self._positions = authority._positions
            self._sources = authority._sources

    def source(self, message: Any) -> _OriginalResult:
        original_id = message.metadata.get("clipped_from")
        if original_id is None:
            original_id = message.id
        key = str(original_id)
        if key not in self._positions:
            raise _ProjectionError("Tool-result projection has no authoritative original index.")
        if key not in self._sources:
            try:
                original = self._pile[original_id]
            except (KeyError, TypeError, ValueError) as exc:
                raise _ProjectionError(
                    "Tool-result projection has no authoritative original."
                ) from exc
            if original.metadata.get("clipped_from") is not None:
                raise _ProjectionError("Tool-result projection provenance must name the original.")
            self._sources[key] = _OriginalResult(original, self._positions[key])
        return self._sources[key]

    def project(
        self,
        message: Any,
        *,
        allowances: Sequence[int] | None = None,
        minimums: Sequence[int] | None = None,
        protected_ranges: Sequence[tuple[int, int]] = (),
    ) -> _ProjectedResult:
        source = self.source(message)
        if not isinstance(source.output, (str, dict)):
            return _ProjectedResult(message.content.output, ())
        if allowances is None:
            lengths = [body.end - body.start for body in source.bodies]
            allowances = _retained_counts(
                message.metadata.get("context_clip_kept", lengths), lengths, recorded=True
            )
        return _project_original(
            source,
            max_chars=self.max_chars,
            allowances=allowances,
            minimums=minimums,
            protected_ranges=protected_ranges,
        )


_authority: ContextVar[tuple[Any, _ProjectionSession] | None] = ContextVar(
    "result_projection_authority", default=None
)


@contextmanager
def _projection_authority(candidate: Any, authority: _ProjectionSession):
    token = _authority.set((candidate, authority))
    try:
        yield
    finally:
        _authority.reset(token)


def _projection_session(branch: Any, imodel: Any) -> _ProjectionSession | None:
    bound = getattr(imodel, "tool_result_max_chars", None)
    if bound is None:
        return None
    if isinstance(bound, bool) or not isinstance(bound, int) or bound < 1024:
        raise _ProjectionError("tool_result_max_chars must be an integer >= 1024 or None.")
    current = _authority.get()
    authority = current[1] if current is not None and current[0] is branch else None
    return _ProjectionSession(branch, bound, authority=authority)
