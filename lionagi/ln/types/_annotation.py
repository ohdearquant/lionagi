# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared, identity-safe annotation materialization for neutral field specs."""

from __future__ import annotations

import typing
from collections.abc import Callable
from typing import Any

from .._cache import BoundedLRUCache
from .._structural import _try_stable_cache_key
from .base import Meta

# Weakly: the materialized annotation holds the declaration's metadata, and the
# metadata holds whatever it carries, a validator included. An entry holding that
# strongly outlives every name the validator had.
_annotation_cache: BoundedLRUCache[Any, Any] = BoundedLRUCache(
    "LIONAGI_FIELD_CACHE_SIZE",
    10000,
    weak_values=True,
)


def _uncached_annotated(origin: Any, metadata: tuple[Meta, ...]) -> Any:
    """Build Annotated without typing's equality-based process-global cache."""
    alias_type = getattr(typing, "_AnnotatedAlias", None)
    if alias_type is None:
        raise RuntimeError("This Python runtime cannot build an uncached Annotated alias")
    return alias_type(origin, metadata)


def _materialize_annotation(
    *,
    owner: object,
    base_type: Any,
    metadata: Any,
    sentinel_predicate: Callable[[Any], bool],
) -> Any:
    """Materialize one annotation; mutable inputs deliberately bypass sharing."""
    # The whole declaration includes its concrete owner type, base type, and
    # metadata. A stable declaration therefore owns the effective policy result
    # without rebuilding a second, equivalent tuple key on every cache hit.
    cache_key = _try_stable_cache_key(owner)

    def build() -> Any:
        actual_type = Any if sentinel_predicate(base_type) else base_type
        current_metadata: tuple[Meta, ...] = () if sentinel_predicate(metadata) else metadata
        if any(meta.key == "nullable" and meta.value for meta in current_metadata):
            actual_type = actual_type | None
        return (
            _uncached_annotated(actual_type, current_metadata) if current_metadata else actual_type
        )

    if cache_key is None:
        return build()
    return _annotation_cache.get_or_create(cache_key, build)
