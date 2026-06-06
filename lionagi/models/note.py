# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import ItemsView, Iterator, KeysView, ValuesView
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from typing_extensions import override

from lionagi.libs.nested import deep_update, flatten, nget, npop, nset, unflatten
from lionagi.ln import is_sentinel, to_dict
from lionagi.utils import UNDEFINED, copy

IndicesType = str | int | tuple[str | int, ...]


def _strip_sentinels(obj: Any, none_as_sentinel=False, empty_as_sentinel=False) -> Any:
    _is_sential = partial(
        is_sentinel,
        none_as_sentinel=none_as_sentinel,
        empty_as_sentinel=empty_as_sentinel,
    )

    def _inner(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _inner(v) for k, v in obj.items() if not _is_sential(v)}
        if isinstance(obj, list):
            return [_inner(v) for v in obj if not _is_sential(v)]
        return obj

    return _inner(obj)


def _to_indices(key: IndicesType) -> list[str | int]:
    """Normalize a single key or tuple of keys to a list of indices."""
    if isinstance(key, (str, int)):
        return [key]
    return list(key)


class Note(BaseModel):
    """Container for nested dict data with path-indexing sugar."""

    content: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        if len(kwargs) == 1 and "content" in kwargs and isinstance(kwargs["content"], dict):
            self.content = kwargs["content"]
        else:
            self.content = kwargs

    @field_serializer("content")
    def _serialize_content(self, value: Any) -> dict[str, Any]:
        return copy(value, deep=True)

    # --- core path operations ---

    def get(self, indices: list[str | int] | str | int, /, default: Any = UNDEFINED) -> Any:
        """Get value at path; return default if missing (raise KeyError if no default)."""
        return nget(self.content, _to_indices(indices), default)

    def set(self, indices: list[str | int] | str | int, value: Any, /) -> None:
        """Deep set value at path; auto-creates intermediate containers."""
        nset(self.content, _to_indices(indices), value)

    def pop(self, indices: list[str | int] | str | int, /, default: Any = UNDEFINED) -> Any:
        """Remove and return value at path."""
        return npop(self.content, _to_indices(indices), default)

    def update(self, indices: list[str | int] | str | int, value: Any, /) -> None:
        """Update nested structure at path (list: extend/append; dict: merge)."""
        existing = self.get(indices, None)
        if existing is None:
            if not isinstance(value, (list, dict)):
                value = [value]
            self.set(indices, value)
        elif isinstance(existing, list):
            if isinstance(value, list):
                existing.extend(value)
            else:
                existing.append(value)
        elif isinstance(existing, dict):
            if isinstance(value, Note):
                value = value.content
            if isinstance(value, dict):
                deep_update(existing, value)
            else:
                raise ValueError("Cannot update a dict with a non-dict value.")
        else:
            raise TypeError(
                f"Cannot update {type(existing).__name__} at {indices!r}; use set() to overwrite"
            )

    # --- flatten / unflatten ---

    def flatten(self, sep: str = "|", max_depth: int | None = None) -> dict[str, Any]:
        """Return flat dict with sep-joined path keys."""
        return flatten(self.content, sep=sep, max_depth=max_depth)

    @classmethod
    def unflatten(cls, data: dict[str, Any], sep: str = "|") -> "Note":
        """Reconstruct Note from a flattened dict."""
        nested = unflatten(data, sep=sep)
        if isinstance(nested, dict):
            return cls(**nested)
        return cls(**{"0": nested})

    # --- serialization ---

    def to_dict(
        self,
        mode: Literal["python", "json"] = "python",
        exclude_none: bool = False,
        exclude_empty: bool = False,
    ) -> dict[str, Any]:
        """Deep copy of content, sentinel values removed at all levels."""
        out = _strip_sentinels(
            copy(self.content, deep=True),
            none_as_sentinel=exclude_none,
            empty_as_sentinel=exclude_empty,
        )
        if mode == "json":
            return to_dict(
                out,
                recursive=True,
                recursive_python_only=False,
                use_enum_values=True,
                use_model_dump=True,
            )
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Note":
        """Create Note from a plain dict."""
        return cls(**data)

    # --- dict-like interface ---

    def keys(self, /, flat: bool = False, sep: str = "|", **kwargs: Any) -> KeysView:
        """Return top-level keys, or flattened keys when flat=True."""
        if flat:
            return flatten(self.content, sep=sep, **kwargs).keys()
        return self.content.keys()

    def values(self, /, flat: bool = False, sep: str = "|", **kwargs: Any) -> ValuesView:
        """Return top-level values, or flattened values when flat=True."""
        if flat:
            return flatten(self.content, sep=sep, **kwargs).values()
        return self.content.values()

    def items(self, /, flat: bool = False, sep: str = "|", **kwargs: Any) -> ItemsView:
        """Return top-level items, or flattened items when flat=True."""
        if flat:
            return flatten(self.content, sep=sep, **kwargs).items()
        return self.content.items()

    def clear(self) -> None:
        """Clear all content."""
        self.content.clear()

    # --- operators ---

    def __contains__(self, key: Any) -> bool:
        """Check top-level key membership."""
        return key in self.content

    def __len__(self) -> int:
        return len(self.content)

    def __iter__(self) -> Iterator[str]:
        return iter(self.content)

    def __getitem__(self, indices: IndicesType) -> Any:
        """Single key: direct dict lookup. Tuple: deep path walk."""
        if isinstance(indices, tuple):
            return nget(self.content, list(indices))
        return self.content[indices]

    def __setitem__(self, indices: IndicesType, value: Any) -> None:
        """Single key: direct dict set. Tuple: deep path set."""
        if isinstance(indices, tuple):
            nset(self.content, list(indices), value)
        else:
            self.content[indices] = value

    @override
    def __str__(self) -> str:
        return str(self.content)

    @override
    def __repr__(self) -> str:
        return repr(self.content)
