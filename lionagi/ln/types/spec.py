"""Framework-agnostic field specification."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Collection, Hashable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, cast, get_origin

from .._structural import _structural_hash, _structural_key
from ._annotation import _materialize_annotation
from ._sentinel import (
    MaybeSentinel,
    MaybeUndefined,
    Undefined,
    is_sentinel,
    not_sentinel,
)
from .base import Meta, _apply_serialization_mode

__all__ = ("Spec", "CommonMeta")


class CommonMeta(Enum):
    """Standard metadata keys for field specifications."""

    NAME = "name"
    NULLABLE = "nullable"
    LISTABLE = "listable"
    VALIDATOR = "validator"
    DEFAULT = "default"
    DEFAULT_FACTORY = "default_factory"

    @classmethod
    def allowed(cls) -> set[str]:
        return {i.value for i in cls}

    @classmethod
    def _validate_common_metas(cls, **kw):
        # Key-presence checks (not truthiness) so default=0/False works
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
    def prepare(
        cls, *args: Meta, metadata: tuple[Meta, ...] | None = None, **kw: Any
    ) -> tuple[Meta, ...]:
        from .._to_list import to_list

        seen_keys = set()
        metas = []

        if metadata:
            for meta in metadata:
                if meta.key in seen_keys:
                    raise ValueError(f"Duplicate metadata key: {meta.key}")
                seen_keys.add(meta.key)
                metas.append(meta)

        if args:
            _args = to_list(args, flatten=True, flatten_tuple_set=True, dropna=True)
            for meta in _args:
                if meta.key in seen_keys:
                    raise ValueError(f"Duplicate metadata key: {meta.key}")
                seen_keys.add(meta.key)
                metas.append(meta)

        for k, v in kw.items():
            if k in seen_keys:
                raise ValueError(f"Duplicate metadata key: {k}")
            seen_keys.add(k)
            metas.append(Meta(k, v))

        meta_dict = {m.key: m.value for m in metas}
        cls._validate_common_metas(**meta_dict)

        return tuple(metas)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class Spec:
    """Framework-agnostic field type + metadata specification."""

    base_type: MaybeSentinel[type[Any]]
    metadata: tuple[Meta, ...]

    def __init__(
        self,
        base_type: MaybeSentinel[type[Any]] = Undefined,
        *args,
        metadata: tuple[Meta, ...] | None = None,
        **kw,
    ) -> None:
        metas = CommonMeta.prepare(*args, metadata=metadata, **kw)

        if not_sentinel(base_type):
            import types
            import typing

            # get_origin, not hasattr("__origin__"): any object can define that
            # attribute, so presence alone lets an arbitrary instance pose as a
            # type annotation. get_origin only answers for real typing forms.
            is_valid_type = (
                isinstance(base_type, type)
                or typing.get_origin(base_type) is not None
                or isinstance(base_type, types.UnionType)
            )
            if not is_valid_type:
                raise ValueError(f"base_type must be a type or type annotation, got {base_type}")

        if kw.get("default_factory") and _is_coro_func(kw["default_factory"]):
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
        for meta in self.metadata:
            if meta.key == key:
                return meta.value
        raise KeyError(f"Metadata key '{key}' undefined in Spec.")

    def _key(self) -> Hashable:
        return _structural_key(self)

    def __hash__(self) -> int:
        return _structural_hash(self)

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        return type(self) is type(other) and self._key() == cast(Spec, other)._key()

    def get(self, key: str, default: Any = Undefined) -> Any:
        with contextlib.suppress(KeyError):
            return self[key]
        return default

    @property
    def name(self) -> MaybeUndefined[str]:
        return self.get(CommonMeta.NAME.value)

    @property
    def q(self):
        """Entry point to the filter DSL via FieldRef.

        Separate from Spec because Spec.__eq__ is load-bearing (sets/dedup/caches).
        """
        from .filters import FieldRef

        return FieldRef(self.name)

    @property
    def is_nullable(self) -> bool:
        return self.get(CommonMeta.NULLABLE.value) is True

    @property
    def is_listable(self) -> bool:
        return self.get(CommonMeta.LISTABLE.value) is True

    @property
    def default(self) -> MaybeUndefined[Any]:
        return self.get(
            CommonMeta.DEFAULT.value,
            self.get(CommonMeta.DEFAULT_FACTORY.value),
        )

    @property
    def has_default_factory(self) -> bool:
        return _is_factory(self.get(CommonMeta.DEFAULT_FACTORY.value))[0]

    @property
    def has_async_default_factory(self) -> bool:
        return _is_factory(self.get(CommonMeta.DEFAULT_FACTORY.value))[1]

    def create_default_value(self) -> Any:
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
        if self.has_async_default_factory:
            return await self.default()
        return self.create_default_value()

    def with_updates(self, **kw):
        _filtered = [meta for meta in self.metadata if meta.key not in kw]
        for k, v in kw.items():
            if not_sentinel(v):
                _filtered.append(Meta(k, v))
        _metas = tuple(_filtered)
        return type(self)(self.base_type, metadata=_metas)

    def to_dict(
        self,
        exclude: Collection[str] | None = None,
        *,
        mode: Literal["python", "json"] = "python",
    ) -> dict[str, Any]:
        """Project this neutral declaration without inventing a sentinel wire value."""
        excluded = frozenset(exclude or ())
        data: dict[str, Any] = {}
        if "base_type" not in excluded and not_sentinel(self.base_type):
            data["base_type"] = self.base_type
        if "metadata" not in excluded:
            data["metadata"] = self.metadata
        return _apply_serialization_mode(data, mode)

    def as_nullable(self) -> Spec:
        return self.with_updates(nullable=True)

    def as_listable(self) -> Spec:
        return self.with_updates(listable=True)

    def with_default(self, default: Any) -> Spec:
        if callable(default):
            return self.with_updates(default_factory=default)
        return self.with_updates(default=default)

    def with_validator(self, validator: Callable[..., Any] | list[Callable[..., Any]]) -> Spec:
        return self.with_updates(validator=validator)

    @property
    def annotation(self) -> type[Any]:
        if is_sentinel(self.base_type):
            return Any
        t_: Any = self.base_type
        if self.is_listable and get_origin(t_) is not list:
            t_ = list[t_]
        if self.is_nullable:
            return t_ | None
        return t_

    def annotated(self) -> type[Any]:
        """Materialize through the shared identity-safe annotation cache."""
        return _materialize_annotation(
            owner=self,
            base_type=self.base_type,
            metadata=self.metadata,
            sentinel_predicate=_spec_is_sentinel,
        )

    def metadict(
        self, exclude: set[str] | None = None, exclude_common: bool = False
    ) -> dict[str, Any]:
        if exclude is None:
            exclude = set()
        if exclude_common:
            exclude = exclude | CommonMeta.allowed()
        return {meta.key: meta.value for meta in self.metadata if meta.key not in exclude}


def _is_coro_func(obj: Any) -> bool:
    """Deferred import: avoids pulling anyio/.concurrency onto the cold `import lionagi` path."""
    from lionagi.ln.concurrency.utils import is_coro_func

    return is_coro_func(obj)


def _spec_is_sentinel(value: Any) -> bool:
    """Keep the public sentinel helper on its direct-invocation contract."""
    return is_sentinel(value)


def _is_factory(obj: Any) -> tuple[bool, bool]:
    """Return (is_factory, is_async)."""
    if not callable(obj):
        return (False, False)
    if _is_coro_func(obj):
        return (True, True)
    return (True, False)
