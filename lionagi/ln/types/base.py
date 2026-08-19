from __future__ import annotations

from collections.abc import Callable, Collection, Hashable, MutableMapping
from dataclasses import MISSING, dataclass, fields
from enum import Enum as _Enum
from functools import lru_cache
from typing import Any, ClassVar, Literal, cast
from weakref import finalize

from typing_extensions import Self, TypedDict, override

from .._structural import _IdentityKey, _structural_hash, _structural_key
from ._sentinel import Undefined, Unset, _compat_policy, _SentinelPolicy

__all__ = (
    "Enum",
    "ModelConfig",
    "Params",
    "DataClass",
    "Meta",
    "KeysDict",
    "KeysLike",
)


class Enum(_Enum):
    """Enhanced Enum with allowed() classmethod."""

    @classmethod
    def allowed(cls) -> tuple[str, ...]:
        return tuple(e.value for e in cls)


class KeysDict(TypedDict, total=False):
    """TypedDict for keys dictionary."""

    key: Any


@dataclass(slots=True, frozen=True)
class ModelConfig:
    """Serialization and validation flags for Params/DataClass subclasses."""

    # Sentinel handling (controls what gets excluded from to_dict)
    none_as_sentinel: bool = False
    empty_as_sentinel: bool = False

    # Validation
    strict: bool = False
    prefill_unset: bool = True

    # Serialization
    use_enum_values: bool = False
    serialize_exclude: frozenset[str] = frozenset()


@dataclass(slots=True, frozen=True)
class _FieldLayout:
    declared: tuple[Any, ...]
    names: tuple[str, ...]
    allowed: frozenset[str]


# Keyed off the type rather than stored on it: any attribute name this could use is
# also a name a subclass may declare as a field, and under slots that field becomes a
# descriptor in the class namespace which reads back as a cached layout. Keyed on
# id() rather than the type itself because a metaclass may overload __eq__/__hash__,
# which would collide two unrelated models onto one layout. An int key holds no
# reference, so the type stays collectable; the finalizer is what stops a dead type's
# entry from being served to whatever later type reuses its id. Uncapped, so nothing
# evicts.
_LAYOUTS: MutableMapping[int, _FieldLayout] = {}


def _field_layout(model_type: type[Any]) -> _FieldLayout:
    """One immutable public-field layout per model type, keyed on that type's identity."""
    key = id(model_type)
    cached = _LAYOUTS.get(key)
    if cached is not None:
        return cached
    declared = tuple(
        field_info for field_info in fields(model_type) if not field_info.name.startswith("_")
    )
    names = tuple(field_info.name for field_info in declared)
    layout = _FieldLayout(declared=declared, names=names, allowed=frozenset(names))
    _LAYOUTS[key] = layout
    finalize(model_type, _LAYOUTS.pop, key, None)
    return layout


def _validate_declared_fields(
    instance: Any,
    set_field: Callable[[Any, str, Any], None],
) -> None:
    sentinel_predicate = _sentinel_predicate(instance)
    for name in _field_layout(type(instance)).names:
        _validate_declared_field(
            instance,
            name,
            set_field,
            sentinel_predicate=sentinel_predicate,
        )


def _validate_declared_field(
    instance: Any,
    name: str,
    set_field: Callable[[Any, str, Any], None],
    *,
    sentinel_predicate: Callable[[Any], bool] | None = None,
) -> None:
    value = getattr(instance, name, Undefined)
    sentinel_predicate = sentinel_predicate or _sentinel_predicate(instance)
    if instance._config.strict and sentinel_predicate(value):
        raise ValueError(f"Missing required parameter: {name}")
    if instance._config.prefill_unset and value is Undefined:
        set_field(instance, name, Unset)


def _declared_fields_to_dict(
    instance: Any,
    exclude: Collection[str] | None,
) -> dict[str, Any]:
    excluded = frozenset(exclude or ())
    sentinel_predicate = _sentinel_predicate(instance)
    data: dict[str, Any] = {}
    for name in _field_layout(type(instance)).names:
        if name in excluded:
            continue
        value = getattr(instance, name, Undefined)
        if not sentinel_predicate(value):
            data[name] = instance._normalize_value(value)
    return data


def _declared_field_state(instance: Any) -> dict[str, Any]:
    """Copy the complete in-memory field state without wire omission."""
    return {
        name: getattr(instance, name, Undefined) for name in _field_layout(type(instance)).names
    }


def _apply_serialization_mode(
    data: dict[str, Any],
    mode: Literal["python", "json"],
) -> dict[str, Any]:
    if mode == "python":
        return data
    if mode == "json":
        from .._json_dump import _to_json_value

        return _to_json_value(data)
    raise ValueError(f"Unsupported serialization mode: {mode!r}")


@lru_cache(maxsize=256)
def _config_sentinel_policy(
    owner_key: _IdentityKey,
    _config_identity: int,
    config: ModelConfig,
) -> _SentinelPolicy:
    """Compile one lexically-owned class policy outside the field hot path."""
    owner = cast(type[Any], owner_key.target)
    return _compat_policy(
        site=f"{owner.__module__}.{owner.__qualname__}._config",
        none_as_sentinel=config.none_as_sentinel,
        empty_as_sentinel=config.empty_as_sentinel,
    )


def _effective_config_sentinel_policy(model_type: type[Any]) -> _SentinelPolicy:
    """Resolve the live lexical owner before consulting the compiled-policy cache."""
    config = model_type._config
    owner = next(base for base in model_type.__mro__ if base.__dict__.get("_config") is config)
    return _config_sentinel_policy(_IdentityKey(owner), id(config), config)


def _is_config_sentinel(model_type: type[Any], value: Any) -> bool:
    """Apply the effective immutable ModelConfig through its compiled policy."""
    return _effective_config_sentinel_policy(model_type).is_sentinel(value)


def _sentinel_predicate(instance: Any) -> Callable[[Any], bool]:
    """Batch the stock policy while preserving the public override seam."""
    predicate = instance._is_sentinel
    implementation = getattr(predicate, "__func__", predicate)
    if (
        implementation is Params._is_sentinel.__func__
        or implementation is DataClass._is_sentinel.__func__
    ):
        return _effective_config_sentinel_policy(type(instance)).is_sentinel
    return predicate


@dataclass(slots=True, frozen=True, init=False, eq=False)
class Params:
    """Immutable keyword-argument parameter bag; configure via _config = ModelConfig(...)."""

    _config: ClassVar[ModelConfig] = ModelConfig()

    def __init__(self, **kwargs: Any):
        unknown = next((key for key in kwargs if key not in self.allowed()), None)
        if unknown is not None:
            raise ValueError(f"Invalid parameter: {unknown}")

        for field_info in _field_layout(type(self)).declared:
            if field_info.name in kwargs:
                value = kwargs[field_info.name]
            elif field_info.default is not MISSING:
                value = field_info.default
            elif field_info.default_factory is not MISSING:
                value = field_info.default_factory()
            else:
                value = Undefined
            object.__setattr__(self, field_info.name, value)

        self._validate()

    @classmethod
    def _is_sentinel(cls, value: Any) -> bool:
        return _is_config_sentinel(cls, value)

    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        """Apply use_enum_values coercion before serialization."""
        if cls._config.use_enum_values and isinstance(value, _Enum):
            return value.value
        return value

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Return public fields in inherited dataclass declaration order."""
        return _field_layout(cls).names

    @classmethod
    def allowed(cls) -> frozenset[str]:
        """Return the immutable membership view of declared public fields."""
        return _field_layout(cls).allowed

    @override
    def _validate(self) -> None:
        _validate_declared_fields(self, object.__setattr__)

    def default_kw(self) -> Any:
        dict_ = self.to_dict()

        # Merge both 'kwargs' and 'kw' conventions into a single flat dict.
        kw_ = {}
        kw_.update(dict_.pop("kwargs", {}))
        kw_.update(dict_.pop("kw", {}))
        dict_.update(kw_)
        return dict_

    def to_dict(
        self,
        exclude: Collection[str] | None = None,
        *,
        mode: Literal["python", "json"] = "python",
    ) -> dict[str, Any]:
        return _apply_serialization_mode(
            _declared_fields_to_dict(self, exclude),
            mode,
        )

    def _key(self) -> Hashable:
        return _structural_key(self)

    def __hash__(self) -> int:
        return _structural_hash(self)

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        return type(self) is type(other) and self._key() == cast(Params, other)._key()

    def with_updates(self, **kwargs: Any) -> Self:
        """Return a new instance with updated fields."""
        dict_ = self._field_state()
        dict_.update(kwargs)
        return type(self)(**dict_)

    def _field_state(self) -> dict[str, Any]:
        """Return constructor values without applying wire omission rules."""
        return _declared_field_state(self)


@dataclass(slots=True)
class DataClass:
    """Mutable dataclass base with sentinel-aware serialization; configure via _config = ModelConfig(...)."""

    _config: ClassVar[ModelConfig] = ModelConfig()

    def __post_init__(self):
        self._validate()

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Return public fields in inherited dataclass declaration order."""
        return _field_layout(cls).names

    @classmethod
    def allowed(cls) -> frozenset[str]:
        """Return the immutable membership view of declared public fields."""
        return _field_layout(cls).allowed

    @override
    def _validate(self) -> None:
        _validate_declared_fields(self, setattr)

    def to_dict(
        self,
        exclude: Collection[str] | None = None,
        *,
        mode: Literal["python", "json"] = "python",
    ) -> dict[str, Any]:
        return _apply_serialization_mode(
            _declared_fields_to_dict(self, exclude),
            mode,
        )

    @classmethod
    def _is_sentinel(cls, value: Any) -> bool:
        return _is_config_sentinel(cls, value)

    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        """Apply use_enum_values coercion before serialization."""
        from enum import Enum as _Enum

        if cls._config.use_enum_values and isinstance(value, _Enum):
            return value.value
        return value

    def with_updates(self, **kwargs: Any) -> Self:
        """Return a new instance with updated fields."""
        layout = _field_layout(type(self))
        unknown = next((name for name in kwargs if name not in layout.allowed), None)
        if unknown is not None:
            raise TypeError(
                f"{type(self).__name__}.__init__() got an unexpected keyword argument {unknown!r}"
            )

        dict_ = self._field_state()
        dict_.update(kwargs)
        constructor_values = {
            field_info.name: dict_[field_info.name]
            for field_info in layout.declared
            if field_info.init
        }
        updated = type(self)(**constructor_values)
        deferred = tuple(field_info for field_info in layout.declared if not field_info.init)
        for field_info in deferred:
            setattr(updated, field_info.name, dict_[field_info.name])
        if deferred:
            updated._validate()
        return updated

    def _field_state(self) -> dict[str, Any]:
        """Return constructor values without applying wire omission rules."""
        return _declared_field_state(self)

    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: object) -> bool:
        """Compare public field state without granting mutable hash authority."""
        if self is other:
            return True
        return type(self) is type(other) and _structural_key(
            self._field_state()
        ) == _structural_key(cast(DataClass, other)._field_state())


# Concrete key-container types accepted by fuzzy_match_keys. Bare ``str`` is
# intentionally excluded: iterating a str yields characters, not key names.
KeysLike = list[str] | tuple[str, ...] | set[str] | frozenset[str] | KeysDict


@dataclass(slots=True, frozen=True, eq=False)
class Meta:
    """Immutable metadata container for field templates and other configurations."""

    key: str
    value: Any

    def _key(self) -> Hashable:
        return _structural_key(self)

    @override
    def __hash__(self) -> int:
        return _structural_hash(self)

    @override
    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        return type(self) is type(other) and self._key() == cast(Meta, other)._key()
