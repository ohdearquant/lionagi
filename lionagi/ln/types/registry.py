# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Explicit ordered immutable declarations and version-bound replacement evidence.

Labels must be exact strings: wrong types raise TypeError, and empty or untrimmed
labels raise ValueError. Invalid policy declarations raise TypeError; duplicate
complete override targets raise ValueError. Composition validates policy first,
then every payload, then duplicate owners, and finally key collisions.

Frozen values support ordinary assignment protection, not initializer re-entry or
deliberate low-level mutation. Runtime structural hashes are not durable digests.
Functions, classes and module-level builtins are admitted by identity; their internal
state is outside the immutability promise.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import MemberDescriptorType
from typing import Any, ClassVar, Generic, Literal, NoReturn, TypeVar, get_origin, overload

from typing_extensions import Self

from .._structural import _registry_structural_key, _RegistryAdmissionError
from ._sentinel import Unset, UnsetType
from .base import Params

__all__ = (
    "RegistryEntry",
    "RegistryFragment",
    "RegistryOverrideRule",
    "RegistrySource",
    "RegistryRecord",
    "RegistryOverride",
    "Registry",
    "RegistryCompositionError",
    "DuplicateRegistryOwnerError",
    "DuplicateRegistryKeyError",
    "RegistryValueError",
)

ItemT = TypeVar("ItemT")
DefaultT = TypeVar("DefaultT")
_Reason = Literal[
    "sentinel", "mutable", "model", "unstable_identity", "memoryview", "unsupported_type", "cycle"
]


class RegistryValueError(TypeError):
    """An entry contains a payload outside the immutable typed admission contract."""

    def __init__(self, *, path: str, value_type: type[Any], reason: _Reason) -> None:
        self.path = path
        self.value_type = value_type
        self.reason = reason
        super().__init__(
            f"registry payload at {path} ({value_type.__module__}.{value_type.__qualname__}): {reason}"
        )


def _label(value: Any, field: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact str")
    if not value or value.strip() != value:
        raise ValueError(f"{field} must be nonempty with no surrounding whitespace")


def _optional_label(value: Any, field: str) -> None:
    if value is not Unset:
        _label(value, field)


def _admit(value: Any) -> None:
    try:
        _registry_structural_key(value)
    except _RegistryAdmissionError as exc:
        raise RegistryValueError(
            path=exc.path, value_type=exc.value_type, reason=exc.reason
        ) from None


def _internal(cls: type[ItemT], **kwargs: Any) -> ItemT:
    instance = object.__new__(cls)
    Params.__init__(instance, **kwargs)
    return instance


def _readonly(self: Any, **kwargs: Any) -> Self:
    raise TypeError(f"{type(self).__name__} is derived; compose a new Registry")


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryEntry(Params, Generic[ItemT]):
    key: str
    value: ItemT

    def __init__(self, *, key: str, value: ItemT) -> None:
        Params.__init__(self, key=key, value=value)

    def _validate(self) -> None:
        _label(self.key, "key")
        _admit(self.value)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryFragment(Params, Generic[ItemT]):
    owner: str
    version: str
    items: tuple[RegistryEntry[ItemT], ...]
    feature: str | UnsetType

    def __init__(
        self,
        *,
        owner: str,
        version: str,
        items: tuple[RegistryEntry[ItemT], ...],
        feature: str | UnsetType = Unset,
    ) -> None:
        Params.__init__(self, owner=owner, version=version, items=items, feature=feature)

    def _validate(self) -> None:
        _label(self.owner, "owner")
        _label(self.version, "version")
        _optional_label(self.feature, "feature")
        if type(self.items) is not tuple:
            raise TypeError("fragment items must be an exact tuple")
        for entry in self.items:
            if type(entry) is not RegistryEntry:
                raise TypeError("fragment items must be exact RegistryEntry values")
            entry._validate()


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryOverrideRule(Params):
    key: str
    incumbent_owner: str
    incumbent_fragment_version: str
    replacement_owner: str
    replacement_fragment_version: str
    registry_version: str
    rule_version: str

    def __init__(
        self,
        *,
        key: str,
        incumbent_owner: str,
        incumbent_fragment_version: str,
        replacement_owner: str,
        replacement_fragment_version: str,
        registry_version: str,
        rule_version: str,
    ) -> None:
        Params.__init__(
            self,
            key=key,
            incumbent_owner=incumbent_owner,
            incumbent_fragment_version=incumbent_fragment_version,
            replacement_owner=replacement_owner,
            replacement_fragment_version=replacement_fragment_version,
            registry_version=registry_version,
            rule_version=rule_version,
        )

    def _validate(self) -> None:
        for name in self.field_names():
            _label(getattr(self, name), name)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistrySource(Params):
    owner: str
    fragment_version: str
    fragment_index: int
    item_index: int

    __init__ = _readonly
    with_updates = _readonly

    def _validate(self) -> None:
        _label(self.owner, "owner")
        _label(self.fragment_version, "fragment_version")
        for name in ("fragment_index", "item_index"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"{name} must be an exact nonnegative int")


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryRecord(Params, Generic[ItemT]):
    entry: RegistryEntry[ItemT]
    source: RegistrySource

    __init__ = _readonly
    with_updates = _readonly

    def _validate(self) -> None:
        if type(self.entry) is not RegistryEntry or type(self.source) is not RegistrySource:
            raise TypeError("records require exact entry and source values")
        self.entry._validate()
        self.source._validate()


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryOverride(Params, Generic[ItemT]):
    rule: RegistryOverrideRule
    displaced: RegistryRecord[ItemT]
    replacement: RegistryRecord[ItemT]

    __init__ = _readonly
    with_updates = _readonly

    def _validate(self) -> None:
        if type(self.rule) is not RegistryOverrideRule:
            raise TypeError("override rule must be an exact RegistryOverrideRule")
        self.rule._validate()
        for record in (self.displaced, self.replacement):
            if type(record) is not RegistryRecord:
                raise TypeError("override evidence must contain exact records")
            record._validate()
        if not _matches(self.rule, self.displaced, self.replacement, self.rule.registry_version):
            raise ValueError("override evidence does not match its rule")


class RegistryCompositionError(ValueError):
    """An explicit composition cannot form an unambiguous snapshot."""

    def __init__(
        self, *, registry_name: str, registry_version: str | UnsetType, message: str
    ) -> None:
        self.registry_name = registry_name
        self.registry_version = registry_version
        super().__init__(message)


class DuplicateRegistryOwnerError(RegistryCompositionError):
    def __init__(
        self,
        *,
        registry_name: str,
        registry_version: str | UnsetType,
        owner: str,
        first_fragment_index: int,
        duplicate_fragment_index: int,
    ) -> None:
        self.owner = owner
        self.first_fragment_index = first_fragment_index
        self.duplicate_fragment_index = duplicate_fragment_index
        super().__init__(
            registry_name=registry_name,
            registry_version=registry_version,
            message=f"registry {registry_name}: duplicate owner {owner} at fragments "
            f"{first_fragment_index} and {duplicate_fragment_index}",
        )


class DuplicateRegistryKeyError(RegistryCompositionError):
    def __init__(
        self,
        *,
        registry_name: str,
        registry_version: str | UnsetType,
        key: str,
        incumbent: RegistryRecord[Any],
        candidate: RegistryRecord[Any],
    ) -> None:
        self.key = key
        self.incumbent = incumbent
        self.candidate = candidate
        super().__init__(
            registry_name=registry_name,
            registry_version=registry_version,
            message=f"registry {registry_name}: duplicate key {key} from "
            f"{incumbent.source.owner} and {candidate.source.owner}",
        )


def _target(rule: RegistryOverrideRule) -> tuple[str, ...]:
    return (
        rule.key,
        rule.incumbent_owner,
        rule.incumbent_fragment_version,
        rule.replacement_owner,
        rule.replacement_fragment_version,
        rule.registry_version,
    )


def _matches(
    rule: RegistryOverrideRule,
    incumbent: RegistryRecord[Any],
    candidate: RegistryRecord[Any],
    version: str | UnsetType,
) -> bool:
    return (
        incumbent.source.fragment_index != candidate.source.fragment_index
        and incumbent.entry.key == candidate.entry.key
        and _target(rule)
        == (
            candidate.entry.key,
            incumbent.source.owner,
            incumbent.source.fragment_version,
            candidate.source.owner,
            candidate.source.fragment_version,
            version,
        )
    )


def _policy(cls: type[Registry[Any]]) -> tuple[RegistryOverrideRule, ...]:
    if cls is not Registry:
        params = cls.__dict__.get("__dataclass_params__")
        bases = cls.__dict__.get("__orig_bases__", ())
        field_names = tuple(field.name for field in dataclasses.fields(Registry))
        slots = cls.__dict__.get("__slots__")
        protected = {
            "__init__",
            "compose",
            "items",
            "keys",
            "__len__",
            "__contains__",
            "__getitem__",
            "get",
            "source_of",
            "with_updates",
            "__eq__",
            "__hash__",
            "_validate",
            "_key",
            "_field_state",
            "_record",
            "__getattribute__",
            "__getattr__",
        }
        if (
            cls.__bases__ != (Registry,)
            or len(bases) != 1
            or get_origin(bases[0]) is not Registry
            or params is None
            or not params.frozen
            or params.init
            or params.eq
            or params.unsafe_hash
            # Python3.10 repeats inherited fields as slots; later versions omit them.
            or type(slots) is not tuple
            or slots not in ((), field_names)
            or dataclasses.fields(cls) != dataclasses.fields(Registry)
            or protected.intersection(cls.__dict__)
            or any(
                not name.startswith("__")
                and name != "override_rules"
                and not (
                    name in field_names
                    and type(value) is MemberDescriptorType
                    and value.__objclass__ is cls
                )
                for name, value in cls.__dict__.items()
            )
        ):
            raise TypeError("registry policies may only declare an immutable override_rules tuple")
    rules = cls.override_rules
    if type(rules) is not tuple:
        raise TypeError("override_rules must be an exact tuple")
    targets = set()
    for rule in rules:
        if type(rule) is not RegistryOverrideRule:
            raise TypeError("override_rules must contain exact RegistryOverrideRule values")
        rule._validate()
        target = _target(rule)
        if target in targets:
            raise ValueError("duplicate complete override rule target")
        targets.add(target)
    return rules


@dataclass(frozen=True, slots=True, init=False, eq=False)
class Registry(Params, Generic[ItemT]):
    name: str
    fragments: tuple[RegistryFragment[ItemT], ...]
    records: tuple[RegistryRecord[ItemT], ...]
    overrides: tuple[RegistryOverride[ItemT], ...]
    version: str | UnsetType
    override_rules: ClassVar[tuple[RegistryOverrideRule, ...]] = ()

    __init__ = _readonly
    with_updates = _readonly

    def _validate(self) -> None:
        _label(self.name, "name")
        _optional_label(self.version, "version")
        for name, expected in (
            ("fragments", RegistryFragment),
            ("records", RegistryRecord),
            ("overrides", RegistryOverride),
        ):
            values = getattr(self, name)
            if type(values) is not tuple or any(type(value) is not expected for value in values):
                raise TypeError(f"{name} must be an exact tuple of declaration values")

    @classmethod
    def compose(
        cls, *fragments: RegistryFragment[ItemT], name: str, version: str | UnsetType = Unset
    ) -> Self:
        _label(name, "name")
        _optional_label(version, "version")
        rules = _policy(cls)
        for fragment in fragments:
            if type(fragment) is not RegistryFragment:
                raise TypeError("compose requires exact RegistryFragment values")
            fragment._validate()
        owners: dict[str, int] = {}
        for index, fragment in enumerate(fragments):
            if fragment.owner in owners:
                raise DuplicateRegistryOwnerError(
                    registry_name=name,
                    registry_version=version,
                    owner=fragment.owner,
                    first_fragment_index=owners[fragment.owner],
                    duplicate_fragment_index=index,
                )
            owners[fragment.owner] = index
        records: list[RegistryRecord[ItemT]] = []
        positions: dict[str, int] = {}
        overrides: list[RegistryOverride[ItemT]] = []
        for fragment_index, fragment in enumerate(fragments):
            for item_index, entry in enumerate(fragment.items):
                candidate = _internal(
                    RegistryRecord,
                    entry=entry,
                    source=_internal(
                        RegistrySource,
                        owner=fragment.owner,
                        fragment_version=fragment.version,
                        fragment_index=fragment_index,
                        item_index=item_index,
                    ),
                )
                if entry.key not in positions:
                    positions[entry.key] = len(records)
                    records.append(candidate)
                    continue
                position = positions[entry.key]
                incumbent = records[position]
                matching = tuple(
                    rule for rule in rules if _matches(rule, incumbent, candidate, version)
                )
                if not matching:
                    raise DuplicateRegistryKeyError(
                        registry_name=name,
                        registry_version=version,
                        key=entry.key,
                        incumbent=incumbent,
                        candidate=candidate,
                    )
                if len(matching) != 1:
                    raise ValueError("ambiguous internal override state")
                rule = RegistryOverrideRule(**matching[0]._field_state())
                overrides.append(
                    _internal(
                        RegistryOverride, rule=rule, displaced=incumbent, replacement=candidate
                    )
                )
                records[position] = candidate
        return _internal(
            cls,
            name=name,
            version=version,
            fragments=fragments,
            records=tuple(records),
            overrides=tuple(overrides),
        )

    @property
    def items(self) -> tuple[ItemT, ...]:
        return tuple(record.entry.value for record in self.records)

    def keys(self) -> tuple[str, ...]:
        return tuple(record.entry.key for record in self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> NoReturn:
        raise TypeError("Registry is not iterable; use keys()")

    def __contains__(self, key: object) -> bool:
        try:
            self._record(key)
        except KeyError:
            return False
        return True

    def _record(self, key: object) -> RegistryRecord[ItemT]:
        if type(key) is not str:
            raise TypeError("registry query keys must be exact str values")
        for record in self.records:
            if record.entry.key == key:
                return record
        raise KeyError(key)

    def __getitem__(self, key: str) -> ItemT:
        return self._record(key).entry.value

    @overload
    def get(self, key: str, /) -> ItemT | None: ...

    @overload
    def get(self, key: str, default: DefaultT, /) -> ItemT | DefaultT: ...

    def get(self, key: str, default: Any = None, /) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def source_of(self, key: str) -> RegistrySource:
        return self._record(key).source
