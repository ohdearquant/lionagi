# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read the cache-write fields of an Anthropic usage report; no prices are involved."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ("CACHE_WRITE_LIFETIMES", "cache_write_split")


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# The two cache-write lifetimes a provider report may split by, in the order
# the split is returned as ``(five_minute, one_hour)``.
CACHE_WRITE_LIFETIMES = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
_SPLIT_KEYS = frozenset(CACHE_WRITE_LIFETIMES)


def cache_write_split(usage: Mapping[str, Any]) -> tuple[int, int] | None:
    """Read one provider report's cache-write lifetime split.

    Returns ``(five_minute, one_hour)`` as ints, or None when the report
    states a split that cannot be certified. Keys must be a subset of
    ``CACHE_WRITE_LIFETIMES``, each present value must be a non-negative int,
    an absent lifetime reads 0, and the two must sum exactly to the report's
    ``cache_creation_input_tokens``. A report with no ``cache_creation`` at all
    claims no split and returns ``(0, 0)``. See the cache-write lifetime rule
    in docs/internals/core.md (``session/``) for the consumer-facing meaning.
    """
    total = usage.get("cache_creation_input_tokens")
    split = usage.get("cache_creation")
    if split is None:
        return 0, 0
    if not isinstance(split, Mapping) or not set(split) <= _SPLIT_KEYS:
        return None
    if not _is_count(total):
        return None
    five_minute = split.get(CACHE_WRITE_LIFETIMES[0], 0)
    one_hour = split.get(CACHE_WRITE_LIFETIMES[1], 0)
    if not (_is_count(five_minute) and _is_count(one_hour)):
        return None
    if five_minute + one_hour != total:
        return None
    return five_minute, one_hour
