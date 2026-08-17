# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Explicit immutable registry fragments and composed snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar, cast, get_origin, overload

from typing_extensions import Self, override

from .._structural import UnhashableStructuralValueError, _try_stable_cache_key
from ._sentinel import MaybeUnset, Undefined, Unset
from .base import Params

__all__ = (
    "AmbiguousRegistryOverrideError",
    "DuplicateRegistryKeyError",
    "DuplicateRegistryOwnerError",
    "Registry",
    "RegistryCompositionError",
    "RegistryEntry",
    "RegistryFragment",
    "RegistryOverride",
    "RegistryOverrideRule",
    "RegistryRecord",
)

ItemT = TypeVar("ItemT")
DefaultT = TypeVar("DefaultT")

_REGISTRY_INVARIANT_NAMES = (
    "__init__",
    "__new__",
    "__setattr__",
    "__delattr__",
    "__eq__",
    "__hash__",
    "__slots__",
    "__bases__",
    "_key",
    "_validate",
    "_field_state",
    "_params_init_closed",
    "compose",
    "to_dict",
    "with_updates",
    "get",
    "__getitem__",
    "__contains__",
    "keys",
    "values",
    "owner_of",
    "name",
    "fragments",
    "items",
    "overrides",
    "version",
)


class RegistryCompositionError(ValueError):
    """A set of explicit fragments cannot form one registry snapshot."""


class DuplicateRegistryOwnerError(RegistryCompositionError):
    """The same fragment owner appears more than once in a composition."""


class DuplicateRegistryKeyError(RegistryCompositionError):
    """Two entries claim one key without one exact declared override."""


class AmbiguousRegistryOverrideError(RegistryCompositionError):
    """More than one declared override rule authorizes the same collision."""


def _validate_label(value: Any, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")


def _validate_registry_value(value: Any) -> None:
    if value is Undefined or value is Unset or _try_stable_cache_key(value) is None:
        raise UnhashableStructuralValueError("$.value", type(value))


def _is_classvar_annotation(annotation: Any) -> bool:
    """Recognize direct ClassVar annotations, including postponed forms."""
    if annotation is ClassVar or get_origin(annotation) is ClassVar:
        return True
    if not isinstance(annotation, str):
        return False

    text = annotation.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text.split("[", 1)[0].strip() in {"ClassVar", "typing.ClassVar"}


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryEntry(Params, Generic[ItemT]):
    """One explicitly keyed declaration contributed by a fragment."""

    key: str
    value: ItemT

    @override
    def _validate(self) -> None:
        Params._validate(self)
        _validate_label(self.key, "RegistryEntry.key")
        _validate_registry_value(self.value)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryFragment(Params, Generic[ItemT]):
    """An immutable ordered contribution from one canonical owner."""

    owner: str
    version: str
    items: tuple[RegistryEntry[ItemT], ...]
    feature: MaybeUnset[str] = Unset

    @override
    def _validate(self) -> None:
        Params._validate(self)
        _validate_label(self.owner, "RegistryFragment.owner")
        _validate_label(self.version, "RegistryFragment.version")
        if type(self.items) is not tuple:
            raise TypeError("RegistryFragment.items must be a tuple")
        if not all(type(item) is RegistryEntry for item in self.items):
            raise TypeError("RegistryFragment.items must contain only RegistryEntry values")
        if self.feature is not Unset:
            _validate_label(self.feature, "RegistryFragment.feature")


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryRecord(Params, Generic[ItemT]):
    """One active entry together with its exact source fragment."""

    entry: RegistryEntry[ItemT]
    owner: str
    fragment_version: str
    feature: MaybeUnset[str] = Unset

    @override
    def _validate(self) -> None:
        Params._validate(self)
        if type(self.entry) is not RegistryEntry:
            raise TypeError("RegistryRecord.entry must be a RegistryEntry")
        _validate_label(self.owner, "RegistryRecord.owner")
        _validate_label(self.fragment_version, "RegistryRecord.fragment_version")
        if self.feature is not Unset:
            _validate_label(self.feature, "RegistryRecord.feature")


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryOverrideRule(Params):
    """One exact, version-bound permission to replace a registry key."""

    key: str
    incumbent_owner: str
    incumbent_fragment_version: str
    replacement_owner: str
    replacement_fragment_version: str
    registry_version: str
    rule_version: str

    @override
    def _validate(self) -> None:
        Params._validate(self)
        for field_name in self.field_names():
            _validate_label(
                object.__getattribute__(self, field_name),
                f"RegistryOverrideRule.{field_name}",
            )

    def matches(
        self,
        *,
        key: str,
        incumbent: RegistryRecord[Any],
        replacement: RegistryRecord[Any],
        registry_version: MaybeUnset[str],
    ) -> bool:
        """Return whether this rule exactly authorizes one collision."""
        return (
            registry_version is not Unset
            and self.key == key
            and self.incumbent_owner == incumbent.owner
            and self.incumbent_fragment_version == incumbent.fragment_version
            and self.replacement_owner == replacement.owner
            and self.replacement_fragment_version == replacement.fragment_version
            and self.registry_version == registry_version
        )


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryOverride(Params, Generic[ItemT]):
    """Recorded history for one authorized replacement."""

    rule: RegistryOverrideRule
    displaced: RegistryEntry[ItemT]
    replacement: RegistryEntry[ItemT]

    @override
    def _validate(self) -> None:
        Params._validate(self)
        if type(self.rule) is not RegistryOverrideRule:
            raise TypeError("RegistryOverride.rule must be a RegistryOverrideRule")
        if type(self.displaced) is not RegistryEntry:
            raise TypeError("RegistryOverride.displaced must be a RegistryEntry")
        if type(self.replacement) is not RegistryEntry:
            raise TypeError("RegistryOverride.replacement must be a RegistryEntry")


class _RegistryType(type):
    """Freeze composition authority when a concrete Registry subtype is defined."""

    def __new__(
        cls,
        name: str,
        bases: tuple[type[Any], ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> _RegistryType:
        registry_subtype = any(isinstance(base, _RegistryType) for base in bases)
        if registry_subtype:
            if len(bases) != 1 or not isinstance(bases[0], _RegistryType):
                raise TypeError("Registry subtypes must inherit from exactly one Registry base")
            replaced = next((item for item in _REGISTRY_INVARIANT_NAMES if item in namespace), None)
            if replaced is not None:
                raise TypeError(f"Registry subtype cannot replace invariant member {replaced}")
            annotations: dict[str, Any] = namespace.get("__annotations__", {})
            declared_classvars = {
                field_name
                for field_name, annotation in annotations.items()
                if _is_classvar_annotation(annotation)
            }
            declared_fields = set(annotations) - declared_classvars
            declared_fields.discard("override_rules")
            if declared_fields:
                field_name = sorted(declared_fields)[0]
                raise TypeError(f"Registry subtype cannot add snapshot field {field_name}")

            for field_name in sorted(declared_classvars):
                if any(
                    field_name in base_type.__dict__ for base in bases for base_type in base.__mro__
                ):
                    raise TypeError(
                        f"Registry subtype ClassVar {field_name} cannot shadow an inherited member"
                    )
                if field_name not in namespace:
                    raise TypeError(
                        f"Registry subtype ClassVar {field_name} must have an immutable value"
                    )
                value = namespace[field_name]
                if any("__get__" in value_type.__dict__ for value_type in type(value).__mro__):
                    raise TypeError(
                        f"Registry subtype ClassVar {field_name} cannot be a descriptor"
                    )
                if value is Undefined or value is Unset or _try_stable_cache_key(value) is None:
                    raise TypeError(
                        f"Registry subtype ClassVar {field_name} must be structurally immutable"
                    )

            permitted_names = {
                "__annotations__",
                "__classcell__",
                "__doc__",
                "__module__",
                "__orig_bases__",
                "__qualname__",
                "override_rules",
                *declared_classvars,
            }
            unsupported = sorted(set(namespace) - permitted_names)
            if unsupported:
                raise TypeError(f"Registry subtype cannot add authority member {unsupported[0]}")
            namespace["__slots__"] = ()

        inherited_rules = next(
            (
                base.__dict__["override_rules"]
                for base in bases
                if isinstance(base, _RegistryType) and "override_rules" in base.__dict__
            ),
            (),
        )
        rules = namespace.get("override_rules", inherited_rules)
        if type(rules) is not tuple or not all(
            type(rule) is RegistryOverrideRule for rule in rules
        ):
            raise TypeError(
                "Registry.override_rules must be a tuple of RegistryOverrideRule values"
            )

        inherited_classvars = frozenset(
            field_name
            for base in bases
            if isinstance(base, _RegistryType)
            for field_name in base.__dict__.get("_registry_classvars", ())
        )
        declared_classvars = frozenset(
            field_name
            for field_name, annotation in namespace.get("__annotations__", {}).items()
            if _is_classvar_annotation(annotation)
        )
        invariants_locked = registry_subtype or "__dataclass_fields__" in namespace
        namespace["override_rules"] = rules
        namespace["_registry_classvars"] = inherited_classvars | declared_classvars
        namespace["_registry_rules_locked"] = False
        namespace["_registry_invariants_locked"] = False

        registry_type = super().__new__(cls, name, bases, namespace, **kwargs)
        _RegistryType.__setattr__(registry_type, "_registry_rules_locked", True)
        _RegistryType.__setattr__(
            registry_type,
            "_registry_invariants_locked",
            invariants_locked,
        )
        return registry_type

    def __setattr__(cls, name: str, value: Any) -> None:
        root_dataclass_bootstrap = (
            cls.__name__ == "Registry"
            and cls.__module__ == __name__
            and not any(isinstance(base, _RegistryType) for base in cls.__bases__)
        )
        slot_helpers_pending = not {
            "__getstate__",
            "__setstate__",
        }.issubset(cls.__dict__)
        if (
            root_dataclass_bootstrap
            and slot_helpers_pending
            and name == "__qualname__"
            and type(value) is str
            and value == cls.__qualname__
        ):
            # dataclasses(slots=True) restores the unchanged qualified name after
            # recreating the class. It is the only post-create bootstrap write.
            super().__setattr__(name, value)
            return
        dataclass_slot_helper = {
            "__getstate__": "_dataclass_getstate",
            "__setstate__": "_dataclass_setstate",
        }.get(name)
        if (
            root_dataclass_bootstrap
            and dataclass_slot_helper is not None
            and name not in cls.__dict__
            and cast(Any, value).__module__ == "dataclasses"
            and cast(Any, value).__name__ == dataclass_slot_helper
        ):
            # Python 3.10 installs these two frozen-slot pickle helpers after
            # recreating the dataclass. Exact provenance keeps the exception narrow.
            super().__setattr__(name, value)
            return
        classvars = cls.__dict__.get("_registry_classvars", ())
        if name in classvars or name == "_registry_classvars":
            raise TypeError(f"Registry subtype ClassVar {name} is immutable after class creation")
        if cls.__dict__.get("_registry_rules_locked", False) and name in {
            "override_rules",
            "_registry_rules_locked",
        }:
            raise TypeError(f"Registry subtype {name} is immutable after class creation")
        if cls.__dict__.get("_registry_invariants_locked", False):
            raise TypeError(f"Registry subtype authority {name} is immutable after class creation")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        classvars = cls.__dict__.get("_registry_classvars", ())
        if name in classvars or name == "_registry_classvars":
            raise TypeError(f"Registry subtype ClassVar {name} is immutable after class creation")
        if cls.__dict__.get("_registry_rules_locked", False) and name in {
            "override_rules",
            "_registry_rules_locked",
        }:
            raise TypeError(f"Registry subtype {name} is immutable after class creation")
        if cls.__dict__.get("_registry_invariants_locked", False):
            raise TypeError(f"Registry subtype authority {name} is immutable after class creation")
        super().__delattr__(name)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class Registry(Params, Generic[ItemT], metaclass=_RegistryType):
    """An immutable, provenance-carrying snapshot composed from explicit fragments."""

    name: str
    fragments: tuple[RegistryFragment[ItemT], ...]
    items: tuple[RegistryRecord[ItemT], ...]
    overrides: tuple[RegistryOverride[ItemT], ...]
    version: MaybeUnset[str] = Unset

    override_rules: ClassVar[tuple[RegistryOverrideRule, ...]] = ()
    _params_init_closed: ClassVar[bool] = True

    def __init__(self, **kwargs: Any) -> None:
        raise TypeError("Registry snapshots are derived by Registry.compose(), not constructed")

    @override
    def _validate(self) -> None:
        Params._validate(self)
        _validate_label(self.name, "Registry.name")
        if self.version is not Unset:
            _validate_label(self.version, "Registry.version")
        if type(self.fragments) is not tuple or not all(
            type(fragment) is RegistryFragment for fragment in self.fragments
        ):
            raise TypeError("Registry.fragments must be a tuple of RegistryFragment values")
        if type(self.items) is not tuple or not all(
            type(record) is RegistryRecord for record in self.items
        ):
            raise TypeError("Registry.items must be a tuple of RegistryRecord values")
        if type(self.overrides) is not tuple or not all(
            type(item) is RegistryOverride for item in self.overrides
        ):
            raise TypeError("Registry.overrides must be a tuple of RegistryOverride values")

    @classmethod
    def compose(
        cls,
        *fragments: RegistryFragment[ItemT],
        name: str,
        version: MaybeUnset[str] = Unset,
    ) -> Self:
        """Compose fragments in declaration order without importing or mutating global state."""
        _validate_label(name, "Registry.name")
        if version is not Unset:
            _validate_label(version, "Registry.version")
        if not all(type(fragment) is RegistryFragment for fragment in fragments):
            raise TypeError("Registry.compose() accepts only RegistryFragment values")
        rules = cls.override_rules

        owner_positions: dict[str, int] = {}
        item_positions: dict[str, int] = {}
        source_positions: dict[str, tuple[int, int]] = {}
        records: list[RegistryRecord[ItemT]] = []
        overrides: list[RegistryOverride[ItemT]] = []

        for fragment_index, fragment in enumerate(fragments):
            previous_fragment_index = owner_positions.get(fragment.owner)
            if previous_fragment_index is not None:
                raise DuplicateRegistryOwnerError(
                    f"registry {name!r} repeats owner {fragment.owner!r} at fragment "
                    f"{previous_fragment_index} and fragment {fragment_index}"
                )
            owner_positions[fragment.owner] = fragment_index

            for item_index, entry in enumerate(fragment.items):
                record = RegistryRecord(
                    entry=entry,
                    owner=fragment.owner,
                    fragment_version=fragment.version,
                    feature=fragment.feature,
                )
                record_index = item_positions.get(entry.key)
                if record_index is None:
                    item_positions[entry.key] = len(records)
                    source_positions[entry.key] = (fragment_index, item_index)
                    records.append(record)
                    continue

                incumbent = records[record_index]
                incumbent_fragment_index, incumbent_item_index = source_positions[entry.key]
                matching_rules = tuple(
                    rule
                    for rule in rules
                    if rule.matches(
                        key=entry.key,
                        incumbent=incumbent,
                        replacement=record,
                        registry_version=version,
                    )
                )
                if len(matching_rules) > 1:
                    raise AmbiguousRegistryOverrideError(
                        f"registry {name!r} key {entry.key!r} matches "
                        f"{len(matching_rules)} override rules"
                    )
                if len(matching_rules) != 1:
                    raise DuplicateRegistryKeyError(
                        f"registry {name!r} has duplicate key {entry.key!r}: owner "
                        f"{incumbent.owner!r} version {incumbent.fragment_version!r} at "
                        f"fragment {incumbent_fragment_index} item {incumbent_item_index} "
                        f"conflicts with owner {record.owner!r} version "
                        f"{record.fragment_version!r} at fragment {fragment_index} item "
                        f"{item_index}"
                    )

                overrides.append(
                    RegistryOverride(
                        rule=matching_rules[0],
                        displaced=incumbent.entry,
                        replacement=entry,
                    )
                )
                records[record_index] = record
                source_positions[entry.key] = (fragment_index, item_index)

        snapshot = object.__new__(cls)
        Params._initialize_derived(
            snapshot,
            name=name,
            fragments=tuple(fragments),
            items=tuple(records),
            overrides=tuple(overrides),
            version=version,
        )
        return snapshot

    @overload
    def get(self, key: str, /) -> MaybeUnset[ItemT]: ...

    @overload
    def get(self, key: str, /, default: DefaultT) -> ItemT | DefaultT: ...

    def get(self, key: str, /, default: Any = Unset) -> Any:
        """Return the active value for ``key`` or the caller's default."""
        for record in self.items:
            if record.entry.key == key:
                return record.entry.value
        return default

    def __getitem__(self, key: str, /) -> ItemT:
        value = self.get(key)
        if value is Unset:
            raise KeyError(key)
        return cast(ItemT, value)

    def __contains__(self, key: object, /) -> bool:
        return isinstance(key, str) and any(record.entry.key == key for record in self.items)

    def keys(self) -> tuple[str, ...]:
        """Return active keys in composition order."""
        return tuple(record.entry.key for record in self.items)

    def values(self) -> tuple[ItemT, ...]:
        """Return active values in composition order."""
        return tuple(record.entry.value for record in self.items)

    def owner_of(self, key: str, /) -> str:
        """Return the active source owner for ``key``."""
        for record in self.items:
            if record.entry.key == key:
                return record.owner
        raise KeyError(key)

    @override
    def with_updates(self, **kwargs: Any) -> Self:
        raise TypeError("Registry snapshots are derived by Registry.compose() and cannot update")
