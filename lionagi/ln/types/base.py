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

    key: Any  # Represents any key-type pair


@dataclass(slots=True, frozen=True)
class ModelConfig:
    """Configuration for Params and DataClass behavior.

    Attributes:
        none_as_sentinel: If True, None is treated as a sentinel value (excluded from to_dict).
        empty_as_sentinel: If True, empty collections are treated as sentinels (excluded from to_dict).
        strict: If True, no sentinels allowed (all fields must have values).
        prefill_unset: If True, unset fields are prefilled with Unset.
        use_enum_values: If True, use enum values instead of enum instances in to_dict().
    """

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
    """Base class for parameters used in various functions.

    Use the ModelConfig class attribute to customize behavior:

    Example:
        @dataclass(slots=True, frozen=True, init=False)
        class MyParams(Params):
            _config: ClassVar[ModelConfig] = ModelConfig(strict=True)
            param1: str
            param2: int
    """

    _config: ClassVar[ModelConfig] = ModelConfig()
    """Configuration for this Params class."""

    _allowed_keys: ClassVar[set[str]] = field(default=set(), init=False, repr=False)
    """Class variable cache to store allowed keys for parameters."""

    def __init__(self, **kwargs: Any):
        """Initialize the Params object with keyword arguments."""
        # Set all attributes from kwargs, allowing for sentinel values
        for k, v in kwargs.items():
            if k in self.allowed():
                object.__setattr__(self, k, v)
            else:
                raise ValueError(f"Invalid parameter: {k}")

        # Validate after setting all attributes
        self._validate()

    @classmethod
    def _is_sentinel(cls, value: Any) -> bool:
        """Check if a value is a sentinel (Undefined or Unset)."""
        return is_sentinel(
            value,
            none_as_sentinel=cls._config.none_as_sentinel,
            empty_as_sentinel=cls._config.empty_as_sentinel,
        )

    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        """Normalize a value for serialization.

        Handles:
        - Enum values if use_enum_values is True
        - Can be extended for other transformations
        """
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
        # create a partial function with the current parameters
        dict_ = self.to_dict()

        # handle kwargs if present, handle both 'kwargs' and 'kw'
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
    """A base class for data classes with strict parameter handling.

    Use the ModelConfig class attribute to customize behavior:

    Example:
        @dataclass(slots=True)
        class MyDataClass(DataClass):
            _config: ClassVar[ModelConfig] = ModelConfig(strict=True, prefill_unset=False)
            field1: str
            field2: int
    """

    _config: ClassVar[ModelConfig] = ModelConfig()
    """Configuration for this DataClass."""

    _allowed_keys: ClassVar[set[str]] = field(default=set(), init=False, repr=False)
    """Class variable cache to store allowed keys for parameters."""

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
        """Check if a value is a sentinel (Undefined or Unset)."""
        return is_sentinel(
            value,
            none_as_sentinel=cls._config.none_as_sentinel,
            empty_as_sentinel=cls._config.empty_as_sentinel,
        )

    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        """Normalize a value for serialization.

        Handles:
        - Enum values if use_enum_values is True
        - Can be extended for other transformations
        """
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


# Concrete key-container types accepted by fuzzy_match_keys.
# Bare ``str`` is intentionally excluded: iterating a str yields individual
# characters, not key names.  Generic Sequences other than list/tuple are also
# excluded because the runtime guard rejects them.
KeysLike = list[str] | tuple[str, ...] | set[str] | frozenset[str] | KeysDict


@dataclass(slots=True, frozen=True)
class Meta:
    """Immutable metadata container for field templates and other configurations."""

    key: str
    value: Any

    @override
    def __hash__(self) -> int:
        """Make metadata hashable for caching.

        Note: For callables, we hash by id to maintain identity semantics.
        """
        # For callables, use their id
        if callable(self.value):
            return hash((self.key, id(self.value)))
        # For other values, try to hash directly
        try:
            return hash((self.key, self.value))
        except TypeError:
            # Fallback for unhashable types
            return hash((self.key, str(self.value)))

    @override
    def __eq__(self, other: object) -> bool:
        """Compare metadata for equality.

        For callables, compare by id to increase cache hits when the same
        validator instance is reused. For other values, use standard equality.
        """
        if not isinstance(other, Meta):
            return NotImplemented

        if self.key != other.key:
            return False

        # For callables, compare by identity
        if callable(self.value) and callable(other.value):
            return id(self.value) == id(other.value)

        # For other values, use standard equality
        return bool(self.value == other.value)
