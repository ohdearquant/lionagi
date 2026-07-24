# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import Field, PrivateAttr, model_validator
from typing_extensions import Self  # noqa: UP035

from lionagi._errors import ItemExistsError, ItemNotFoundError

from .element import ID, Element
from .pile import Pile
from .progression import Progression

__all__ = ("Flow",)

E = TypeVar("E", bound=Element)
P = TypeVar("P", bound=Progression)


class Flow(Element, Generic[E, P]):
    """Workflow state: item Pile + named Progressions with referential integrity and RLock."""

    name: str | None = Field(
        default=None,
        description="Optional name for this flow.",
    )
    items: Pile[E] = Field(
        default_factory=Pile,
        description="Items that progressions reference.",
    )
    progressions: Pile[P] = Field(
        default_factory=Pile,
        description="Workflow stages as named progressions.",
    )
    _progression_names: dict[str, UUID] = PrivateAttr(default_factory=dict)
    # Reverse of `_progression_names` (uuid -> indexed name). Progression.name
    # is a plain mutable field, so a progression's live `.name` can drift from
    # whatever name it was indexed under; this lets removal/rename find the
    # actually-indexed name instead of trusting the (possibly stale) live one.
    _progression_ids: dict[UUID, str] = PrivateAttr(default_factory=dict)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    @model_validator(mode="after")
    def _validate_referential_integrity(self) -> Self:
        """Validate all progression UUIDs exist in items pile."""
        item_ids = set(self.items.keys())
        for prog in self.progressions:
            missing = set(prog) - item_ids
            if missing:
                raise ItemNotFoundError(
                    f"Progression '{prog.name}' references missing items: {missing}"
                )
        return self

    def model_post_init(self, __context: Any) -> None:
        """Rebuild _progression_names index from progressions."""
        super().model_post_init(__context)
        for progression in self.progressions:
            if progression.name:
                self._progression_names[progression.name] = progression.id
                self._progression_ids[progression.id] = progression.name

    # ==================== Serialization ====================

    def to_dict(self, mode="python", **kw):
        """Serialize with nested Pile.to_dict() to preserve lion_class for polymorphic round-trips."""
        if mode == "python":
            dict_ = self._to_dict(**kw)
            dict_["items"] = self.items.to_dict()
            dict_["progressions"] = self.progressions.to_dict()
            return dict_
        return super().to_dict(mode=mode, **kw)

    @classmethod
    def _coerce_pile(cls, value: Any) -> Pile:
        """Coerce dict, list, or Pile to a Pile instance."""
        if isinstance(value, Pile):
            return value
        if isinstance(value, dict):
            return Pile.from_dict(value)
        if isinstance(value, list):
            return Pile(collections=value)
        return value

    @classmethod
    def from_dict(cls, data: dict) -> Flow:
        """Deserialize from dict; reconstructs Pile fields explicitly (model_validate can't round-trip them)."""
        data = data.copy()
        # Copy the nested metadata dict: data.copy() is shallow, so popping
        # lion_class off the original would mutate the caller's snapshot.
        metadata = dict(data.pop("metadata", None) or {})
        metadata.pop("lion_class", None)

        # Coerce scalar fields that model_construct won't validate
        if "id" in data and not isinstance(data["id"], UUID):
            data["id"] = UUID(str(data["id"]))

        items = cls._coerce_pile(data.pop("items", None)) or Pile()
        progressions = cls._coerce_pile(data.pop("progressions", None)) or Pile()

        item_ids = set(items.keys())
        for prog in progressions:
            missing = set(prog) - item_ids
            if missing:
                raise ItemNotFoundError(
                    f"Progression '{prog.name}' references missing items: {missing}"
                )

        flow = cls.model_construct(
            items=items,
            progressions=progressions,
            metadata=metadata,
            **data,
        )
        # Rebuild private attrs that model_construct skips
        flow._progression_names = {}
        flow._progression_ids = {}
        flow._lock = threading.RLock()
        for prog in flow.progressions:
            if prog.name:
                flow._progression_names[prog.name] = prog.id
                flow._progression_ids[prog.id] = prog.name
        return flow

    # ==================== Progression Management ====================

    def add_progression(self, progression: P) -> None:
        """Add progression; raises ItemExistsError on duplicate name, ItemNotFoundError on missing items."""
        with self._lock:
            if progression.name and progression.name in self._progression_names:
                raise ItemExistsError(f"Progression with name '{progression.name}' already exists.")

            item_ids = set(self.items.keys())
            missing = set(progression) - item_ids
            if missing:
                raise ItemNotFoundError(f"Progression references missing items: {missing}")

            self.progressions.include(progression)
            if progression.name:
                self._progression_names[progression.name] = progression.id
                self._progression_ids[progression.id] = progression.name

    def remove_progression(self, key: UUID | str | P) -> None:
        """Remove progression by UUID, name, or instance; raises ItemNotFoundError if absent."""
        with self._lock:
            if isinstance(key, str) and key in self._progression_names:
                uid = self._progression_names.pop(key)
                self._progression_ids.pop(uid, None)
                self.progressions.pop(uid)
                return

            uid = ID.get_id(key)
            # Look up the name this uuid was actually indexed under, rather
            # than trusting the progression's current (possibly renamed)
            # `.name` - the two can disagree if it was renamed directly.
            indexed_name = self._progression_ids.pop(uid, None)
            if indexed_name is not None:
                self._progression_names.pop(indexed_name, None)
            self.progressions.pop(uid)

    def rename_progression(self, key: UUID | str | P, new_name: str | None) -> None:
        """Rename an owned progression, keeping the Flow's name index in sync.

        Raises ItemExistsError if `new_name` already names a different
        progression, ItemNotFoundError if `key` does not resolve.
        """
        with self._lock:
            progression = self.get_progression(key)
            if (
                new_name
                and new_name in self._progression_names
                and self._progression_names[new_name] != progression.id
            ):
                raise ItemExistsError(f"Progression with name '{new_name}' already exists.")

            old_name = self._progression_ids.pop(progression.id, None)
            if old_name is not None:
                self._progression_names.pop(old_name, None)

            progression.name = new_name
            if new_name:
                self._progression_names[new_name] = progression.id
                self._progression_ids[progression.id] = new_name

    def get_progression(self, key: UUID | str | P) -> P:
        """Return progression by UUID, name, or instance; raises ItemNotFoundError if absent."""
        with self._lock:
            if isinstance(key, str) and key in self._progression_names:
                uid = self._progression_names[key]
                return self.progressions[uid]

            if isinstance(key, str):
                try:
                    uid = ID.get_id(key)
                    return self.progressions[uid]
                except Exception as exc:
                    raise ItemNotFoundError(f"Progression '{key}' not found in flow") from exc

            uid = key.id if isinstance(key, Progression) else key
            return self.progressions[uid]

    # ==================== Item Management ====================

    def add_item(
        self,
        item: E,
        progressions: list[UUID | str | P] | UUID | str | P | None = None,
    ) -> None:
        """Add item to the pile and optionally append it to named progressions."""
        with self._lock:
            resolved: list[P] = []
            if progressions is not None:
                if isinstance(progressions, str | UUID | Progression):
                    progs_list = [progressions]
                else:
                    progs_list = list(progressions)

                # Resolve every reference through the owned progression pile,
                # including bare Progression instances: appending to an unowned
                # progression would mutate an ordering the flow does not track.
                for p in progs_list:
                    resolved.append(self.get_progression(p))

            self.items.include(item)

            for prog in resolved:
                prog.append(item)

    def remove_item(self, item_id: UUID | str | Element) -> None:
        """Remove item from the pile and all progressions; raises ItemNotFoundError if absent."""
        with self._lock:
            uid = ID.get_id(item_id)

            for progression in self.progressions:
                if uid in progression:
                    progression.exclude(uid)

            self.items.pop(uid)

    def clear(self) -> None:
        """Clear all items and progressions."""
        with self._lock:
            self.items.clear()
            self.progressions.clear()
            self._progression_names.clear()
            self._progression_ids.clear()

    def __repr__(self) -> str:
        name_str = f", name='{self.name}'" if self.name else ""
        return f"Flow(items={len(self.items)}, progressions={len(self.progressions)}{name_str})"

    def __len__(self) -> int:
        return len(self.items)
