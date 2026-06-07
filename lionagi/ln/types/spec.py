"""Spec - Universal type specification for framework-agnostic field definitions.

This module provides the Spec class for defining field specifications that can be
adapted to any framework (Pydantic, attrs, dataclasses, etc.) via adapters.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from lionagi.ln.concurrency.utils import is_coro_func

from ._sentinel import MaybeUndefined, Undefined, is_sentinel, not_sentinel
from .base import Meta

# Global cache for annotated types with bounded size
_MAX_CACHE_SIZE = int(os.environ.get("LIONAGI_FIELD_CACHE_SIZE", "10000"))
_annotated_cache: OrderedDict[tuple[type, tuple[Meta, ...]], type] = OrderedDict()
_cache_lock = threading.RLock()  # Thread-safe access to cache


__all__ = ("Spec", "CommonMeta")


class CommonMeta(Enum):
    """Common metadata keys used across field specifications."""

    NAME = "name"
    NULLABLE = "nullable"
    LISTABLE = "listable"
    VALIDATOR = "validator"
    DEFAULT = "default"
    DEFAULT_FACTORY = "default_factory"

    @classmethod
    def allowed(cls) -> set[str]:
        """Return all allowed common metadata keys."""
        return {i.value for i in cls}

    @classmethod
    def _validate_common_metas(cls, **kw):
        """Validate common metadata constraints.

        Uses key-presence checks rather than truthiness so that falsy-but-valid
        values (e.g. ``default=0``, ``default=False``) are handled correctly.
        """
        if "default" in kw and "default_factory" in kw:
            raise ValueError("Cannot provide both 'default' and 'default_factory'")
        if "default_factory" in kw:
            if not callable(kw["default_factory"]):
                raise ValueError("'default_factory' must be callable")
        if "validator" in kw:
            _val = kw["validator"]
            _val = [_val] if not isinstance(_val, list) else _val
            if not all(callable(v) for v in _val):
                raise ValueError("Validators must be a list of functions or a function")

    @classmethod
    def prepare(cls, *args: Meta, metadata: tuple[Meta, ...] = None, **kw: Any) -> tuple[Meta, ...]:
        """Prepare metadata tuple from various inputs, checking for duplicates.

        Args:
            *args: Individual Meta objects
            metadata: Existing metadata tuple
            **kw: Keyword arguments to convert to Meta objects

        Returns:
            Tuple of Meta objects

        Raises:
            ValueError: If duplicate keys are found
        """
        # Lazy import to avoid circular dependency
        from .._to_list import to_list

        seen_keys = set()
        metas = []

        # Process existing metadata
        if metadata:
            for meta in metadata:
                if meta.key in seen_keys:
                    raise ValueError(f"Duplicate metadata key: {meta.key}")
                seen_keys.add(meta.key)
                metas.append(meta)

        # Process args
        if args:
            _args = to_list(args, flatten=True, flatten_tuple_set=True, dropna=True)
            for meta in _args:
                if meta.key in seen_keys:
                    raise ValueError(f"Duplicate metadata key: {meta.key}")
                seen_keys.add(meta.key)
                metas.append(meta)

        # Process kwargs
        for k, v in kw.items():
            if k in seen_keys:
                raise ValueError(f"Duplicate metadata key: {k}")
            seen_keys.add(k)
            metas.append(Meta(k, v))

        # Validate common metadata constraints
        meta_dict = {m.key: m.value for m in metas}
        cls._validate_common_metas(**meta_dict)

        return tuple(metas)


@dataclass(frozen=True, slots=True, init=False)
class Spec:
    """Framework-agnostic field specification.

    A Spec defines the type and metadata for a field without coupling to any
    specific framework. Use adapters to convert Spec to framework-specific
    field definitions (e.g., Pydantic Field, attrs attribute).

    Attributes:
        base_type: The base Python type for this field
        metadata: Tuple of metadata objects attached to this spec

    Example:
        >>> spec = Spec(str, name="username", nullable=False)
        >>> spec.name
        'username'
        >>> spec.annotation
        str
    """

    base_type: type
    metadata: tuple[Meta, ...]

    def __init__(
        self,
        base_type: type = None,
        *args,
        metadata: tuple[Meta, ...] = None,
        **kw,
    ) -> None:
        """Initialize Spec with type and metadata.

        Args:
            base_type: Base Python type
            *args: Additional Meta objects
            metadata: Existing metadata tuple
            **kw: Keyword arguments converted to Meta objects
        """
        metas = CommonMeta.prepare(*args, metadata=metadata, **kw)

        if not_sentinel(base_type, True):
            import types

            is_valid_type = (
                isinstance(base_type, type)
                or hasattr(base_type, "__origin__")
                or isinstance(base_type, types.UnionType)
                or str(type(base_type)) == "<class 'types.UnionType'>"
            )
            if not is_valid_type:
                raise ValueError(f"base_type must be a type or type annotation, got {base_type}")

        # Check for async default factory and warn
        if kw.get("default_factory") and is_coro_func(kw["default_factory"]):
            import warnings

            warnings.warn(
                "Async default factories are not yet fully supported by all adapters. "
                "Consider using sync factories for compatibility.",
                UserWarning,
                stacklevel=2,
            )

        object.__setattr__(self, "base_type", base_type)
        object.__setattr__(self, "metadata", metas)

    def __getitem__(self, key: str) -> Any:
        """Get metadata value by key.

        Args:
            key: Metadata key

        Returns:
            Metadata value

        Raises:
            KeyError: If key not found
        """
        for meta in self.metadata:
            if meta.key == key:
                return meta.value
        raise KeyError(f"Metadata key '{key}' undefined in Spec.")

    def get(self, key: str, default: Any = Undefined) -> Any:
        """Get metadata value by key with default.

        Args:
            key: Metadata key
            default: Default value if key not found

        Returns:
            Metadata value or default
        """
        with contextlib.suppress(KeyError):
            return self[key]
        return default

    @property
    def name(self) -> MaybeUndefined[str]:
        """Get the field name from metadata."""
        return self.get(CommonMeta.NAME.value)

    @property
    def q(self):
        """A :class:`FieldRef` for this spec's name — the entry point to the
        filter DSL (``spec.q == value``, ``spec.q > value``, ...).

        Lives here rather than on ``Spec`` directly because ``Spec.__eq__`` is
        load-bearing (sets, dedup, caches); ``q`` hands out a fresh FieldRef
        whose operators build :class:`SpecFilter`s instead.
        """
        from .filters import FieldRef

        return FieldRef(self.name)

    @property
    def is_nullable(self) -> bool:
        """Check if field is nullable."""
        return self.get(CommonMeta.NULLABLE.value) is True

    @property
    def is_listable(self) -> bool:
        """Check if field is listable."""
        return self.get(CommonMeta.LISTABLE.value) is True

    @property
    def default(self) -> MaybeUndefined[Any]:
        """Get default value or factory."""
        return self.get(
            CommonMeta.DEFAULT.value,
            self.get(CommonMeta.DEFAULT_FACTORY.value),
        )

    @property
    def has_default_factory(self) -> bool:
        """Check if this spec has a default factory."""
        return _is_factory(self.get(CommonMeta.DEFAULT_FACTORY.value))[0]

    @property
    def has_async_default_factory(self) -> bool:
        """Check if this spec has an async default factory."""
        return _is_factory(self.get(CommonMeta.DEFAULT_FACTORY.value))[1]

    def create_default_value(self) -> Any:
        """Create default value synchronously.

        Returns:
            Default value

        Raises:
            ValueError: If no default or factory defined, or if factory is async
        """
        if self.default is Undefined:
            raise ValueError("No default value or factory defined in Spec.")
        if self.has_async_default_factory:
            raise ValueError(
                "Default factory is asynchronous; cannot create default synchronously. "
                "Use 'await spec.acreate_default_value()' instead."
            )
        if self.has_default_factory:
            return self.default()
        return self.default

    async def acreate_default_value(self) -> Any:
        """Create default value asynchronously.

        Returns:
            Default value
        """
        if self.has_async_default_factory:
            return await self.default()
        return self.create_default_value()

    def with_updates(self, **kw):
        """Create new Spec with updated metadata.

        Args:
            **kw: Metadata updates

        Returns:
            New Spec instance with updates
        """
        _filtered = [meta for meta in self.metadata if meta.key not in kw]
        for k, v in kw.items():
            if not_sentinel(v):
                _filtered.append(Meta(k, v))
        _metas = tuple(_filtered)
        return type(self)(self.base_type, metadata=_metas)

    def as_nullable(self) -> Spec:
        """Create nullable version of this spec."""
        return self.with_updates(nullable=True)

    def as_listable(self) -> Spec:
        """Create listable version of this spec."""
        return self.with_updates(listable=True)

    def with_default(self, default: Any) -> Spec:
        """Create spec with default value or factory.

        Args:
            default: Default value or factory function

        Returns:
            New Spec with default
        """
        if callable(default):
            return self.with_updates(default_factory=default)
        return self.with_updates(default=default)

    def with_validator(self, validator: Callable[..., Any] | list[Callable[..., Any]]) -> Spec:
        """Create spec with validator(s).

        Args:
            validator: Single validator or list of validators

        Returns:
            New Spec with validator(s)
        """
        return self.with_updates(validator=validator)

    @property
    def annotation(self) -> type[Any]:
        """Plain type annotation representing base type, nullable, and listable.

        Returns:
            Type annotation
        """
        if is_sentinel(self.base_type, none_as_sentinel=True):
            return Any
        t_ = self.base_type
        if self.is_listable:
            t_ = list[t_]
        if self.is_nullable:
            return t_ | None
        return t_

    def annotated(self) -> type[Any]:
        """Materialize this spec into an Annotated type.

        This method is cached to ensure repeated calls return the same
        type object for performance and identity checks. The cache is bounded
        using LRU eviction to prevent unbounded memory growth.

        Returns:
            Annotated type with all metadata attached
        """
        # Check cache first with thread safety
        cache_key = (self.base_type, self.metadata)

        with _cache_lock:
            if cache_key in _annotated_cache:
                # Move to end to mark as recently used
                _annotated_cache.move_to_end(cache_key)
                return _annotated_cache[cache_key]

            # Handle nullable case - wrap in Optional-like union
            actual_type = (
                Any if is_sentinel(self.base_type, none_as_sentinel=True) else self.base_type
            )
            current_metadata = (
                () if is_sentinel(self.metadata, none_as_sentinel=True) else self.metadata
            )

            if any(m.key == "nullable" and m.value for m in current_metadata):
                # Use union syntax for nullable
                actual_type = actual_type | None  # type: ignore

            if current_metadata:
                args = [actual_type] + list(current_metadata)
                result = Annotated.__class_getitem__(tuple(args))  # type: ignore
            else:
                result = actual_type  # type: ignore[misc]

            # Cache the result with LRU eviction
            _annotated_cache[cache_key] = result  # type: ignore[assignment]

            # Evict oldest if cache is too large (guard against empty cache)
            while len(_annotated_cache) > _MAX_CACHE_SIZE:
                try:
                    _annotated_cache.popitem(last=False)  # Remove oldest
                except KeyError:
                    # Cache became empty during race, safe to continue
                    break

        return result  # type: ignore[return-value]

    def metadict(
        self, exclude: set[str] | None = None, exclude_common: bool = False
    ) -> dict[str, Any]:
        """Get metadata as dictionary.

        Args:
            exclude: Keys to exclude
            exclude_common: Exclude all common metadata keys

        Returns:
            Dictionary of metadata
        """
        if exclude is None:
            exclude = set()
        if exclude_common:
            exclude = exclude | CommonMeta.allowed()
        return {meta.key: meta.value for meta in self.metadata if meta.key not in exclude}


def _is_factory(obj: Any) -> tuple[bool, bool]:
    """Check if object is a factory function.

    Args:
        obj: Object to check

    Returns:
        Tuple of (is_factory, is_async)
    """
    if not callable(obj):
        return (False, False)
    if is_coro_func(obj):
        return (True, True)
    return (True, False)
