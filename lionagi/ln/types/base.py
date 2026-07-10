from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum as _Enum
from typing import Any, ClassVar

from typing_extensions import TypedDict, override

from ._sentinel import Undefined, Unset, is_sentinel

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


@dataclass(slots=True, frozen=True, init=False)
class Params:
    """Immutable keyword-argument parameter bag; configure via _config = ModelConfig(...)."""

    _config: ClassVar[ModelConfig] = ModelConfig()

    _allowed_keys: ClassVar[set[str]] = field(default=set(), init=False, repr=False)

    def __init__(self, **kwargs: Any):
        for k, v in kwargs.items():
            if k in self.allowed():
                object.__setattr__(self, k, v)
            else:
                raise ValueError(f"Invalid parameter: {k}")

        self._validate()

    @classmethod
    def _is_sentinel(cls, value: Any) -> bool:
        return is_sentinel(
            value,
            none_as_sentinel=cls._config.none_as_sentinel,
            empty_as_sentinel=cls._config.empty_as_sentinel,
        )

    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        """Apply use_enum_values coercion before serialization."""
        if cls._config.use_enum_values and isinstance(value, _Enum):
            return value.value
        return value

    @classmethod
    def allowed(cls) -> set[str]:
        """Return the keys of the parameters."""
        if "_allowed_keys" in cls.__dict__ and cls.__dict__["_allowed_keys"]:
            return cls._allowed_keys
        cls._allowed_keys = {i for i in cls.__dataclass_fields__.keys() if not i.startswith("_")}
        return cls._allowed_keys

    @override
    def _validate(self) -> None:
        def _validate_strict(k):
            if self._config.strict and self._is_sentinel(getattr(self, k, Unset)):
                raise ValueError(f"Missing required parameter: {k}")
            if self._config.prefill_unset and getattr(self, k, Undefined) is Undefined:
                object.__setattr__(self, k, Unset)

        for k in self.allowed():
            _validate_strict(k)

    def default_kw(self) -> Any:
        dict_ = self.to_dict()

        # Merge both 'kwargs' and 'kw' conventions into a single flat dict.
        kw_ = {}
        kw_.update(dict_.pop("kwargs", {}))
        kw_.update(dict_.pop("kw", {}))
        dict_.update(kw_)
        return dict_

    def to_dict(self, exclude: set[str] = None) -> dict[str, str]:
        data = {}
        exclude = exclude or set()
        for k in self.allowed():
            if k not in exclude:
                v = getattr(self, k, Undefined)
                if not self._is_sentinel(v):
                    data[k] = self._normalize_value(v)
        return data

    def __hash__(self) -> int:
        from .._hash import hash_dict

        return hash_dict(self.to_dict())

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Params):
            return False
        return hash(self) == hash(other)

    def with_updates(self, **kwargs: Any) -> DataClass:
        """Return a new instance with updated fields."""
        dict_ = self.to_dict()
        dict_.update(kwargs)
        return type(self)(**dict_)


@dataclass(slots=True)
class DataClass:
    """Mutable dataclass base with sentinel-aware serialization; configure via _config = ModelConfig(...)."""

    _config: ClassVar[ModelConfig] = ModelConfig()

    _allowed_keys: ClassVar[set[str]] = field(default=set(), init=False, repr=False)

    def __post_init__(self):
        """Post-initialization to ensure all fields are set."""
        self._validate()

    @classmethod
    def allowed(cls) -> set[str]:
        """Return the keys of the parameters."""
        if "_allowed_keys" in cls.__dict__ and cls.__dict__["_allowed_keys"]:
            return cls._allowed_keys
        cls._allowed_keys = {i for i in cls.__dataclass_fields__.keys() if not i.startswith("_")}
        return cls._allowed_keys

    @override
    def _validate(self) -> None:
        def _validate_strict(k):
            if self._config.strict and self._is_sentinel(getattr(self, k, Unset)):
                raise ValueError(f"Missing required parameter: {k}")
            if self._config.prefill_unset and getattr(self, k, Undefined) is Undefined:
                self.__setattr__(k, Unset)

        for k in self.allowed():
            _validate_strict(k)

    def to_dict(self, exclude: set[str] = None) -> dict[str, str]:
        data = {}
        exclude = exclude or set()
        for k in type(self).allowed():
            if k not in exclude:
                v = getattr(self, k)
                if not self._is_sentinel(v):
                    data[k] = self._normalize_value(v)
        return data

    @classmethod
    def _is_sentinel(cls, value: Any) -> bool:
        return is_sentinel(
            value,
            none_as_sentinel=cls._config.none_as_sentinel,
            empty_as_sentinel=cls._config.empty_as_sentinel,
        )

    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        """Apply use_enum_values coercion before serialization."""
        from enum import Enum as _Enum

        if cls._config.use_enum_values and isinstance(value, _Enum):
            return value.value
        return value

    def with_updates(self, **kwargs: Any) -> DataClass:
        """Return a new instance with updated fields."""
        dict_ = self.to_dict()
        dict_.update(kwargs)
        return type(self)(**dict_)

    def __hash__(self) -> int:
        from .._hash import hash_dict

        return hash_dict(self.to_dict())

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, DataClass):
            return False
        return hash(self) == hash(other)


# Concrete key-container types accepted by fuzzy_match_keys. Bare ``str`` is
# intentionally excluded: iterating a str yields characters, not key names.
KeysLike = list[str] | tuple[str, ...] | set[str] | frozenset[str] | KeysDict


@dataclass(slots=True, frozen=True)
class Meta:
    """Immutable metadata container for field templates and other configurations."""

    key: str
    value: Any

    @override
    def __hash__(self) -> int:
        # callables hash by id
        if callable(self.value):
            return hash((self.key, id(self.value)))
        try:
            return hash((self.key, self.value))
        except TypeError:
            return hash((self.key, str(self.value)))

    @override
    def __eq__(self, other: object) -> bool:
        # callables compare by identity to maximize cache hits
        if not isinstance(other, Meta):
            return NotImplemented

        if self.key != other.key:
            return False

        if callable(self.value) and callable(other.value):
            return id(self.value) == id(other.value)

        return bool(self.value == other.value)
