# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Minimal memory contract: a typed item, a typed query, a Protocol, and a
zero-dependency in-process default backend built on `Pile`."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field

from .generic.element import Element
from .generic.pile import Pile

__all__ = (
    "MemoryItem",
    "MemoryQuery",
    "MemoryStore",
    "InMemoryStore",
)


class MemoryItem(Element):
    """A single stored memory record."""

    content: Any = None
    tags: list[str] = Field(default_factory=list)
    # `metadata` (inherited from Element) carries provenance: branch_id, source, etc.


class MemoryQuery(BaseModel):
    """Search parameters, kept as data rather than a query-string DSL."""

    text: str | None = None
    tags: list[str] | None = None
    filters: dict[str, Any] | None = None
    limit: int = 20


@runtime_checkable
class MemoryStore(Protocol):
    """Structural contract every memory backend (default or pluggable) satisfies."""

    async def store(self, item: MemoryItem) -> UUID: ...

    async def retrieve(self, item_id: UUID) -> MemoryItem | None: ...

    async def search(self, query: MemoryQuery) -> list[MemoryItem]: ...


class InMemoryStore:
    """Default MemoryStore: a Pile[MemoryItem] with substring/tag search."""

    def __init__(self) -> None:
        self._items: Pile[MemoryItem] = Pile(item_type={MemoryItem})

    async def store(self, item: MemoryItem) -> UUID:
        await self._items.ainclude(item)
        return item.id

    async def retrieve(self, item_id: UUID) -> MemoryItem | None:
        return await self._items.aget(item_id, None)

    async def search(self, query: MemoryQuery) -> list[MemoryItem]:
        results = list(self._items.values())

        if query.text:
            needle = query.text.lower()
            results = [r for r in results if needle in str(r.content).lower()]

        if query.tags:
            wanted = set(query.tags)
            results = [r for r in results if wanted & set(r.tags)]

        if query.filters:
            results = [
                r for r in results if all(r.metadata.get(k) == v for k, v in query.filters.items())
            ]

        return results[: query.limit]
