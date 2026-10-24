# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Immutable declarations retain ordered contributions and typed runtime identity."""

from __future__ import annotations

import dataclasses
import functools
import json
import subprocess
import sys
import types
import typing
from dataclasses import FrozenInstanceError, dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, ClassVar
from uuid import UUID

import pytest

from lionagi import ln
from lionagi.ln import _structural as structural
from lionagi.ln.types import (
    DuplicateRegistryKeyError,
    DuplicateRegistryOwnerError,
    Meta,
    Params,
    Registry,
    RegistryEntry,
    RegistryFragment,
    RegistryOverride,
    RegistryOverrideRule,
    RegistryRecord,
    RegistrySource,
    RegistryValueError,
    Undefined,
    Unset,
)


def _fragment(owner="first", version="v1", **items):
    return RegistryFragment(
        owner=owner,
        version=version,
        items=tuple(RegistryEntry(key=key, value=value) for key, value in items.items()),
    )


def _rule(**changes):
    fields = dict(
        key="a",
        incumbent_owner="first",
        incumbent_fragment_version="v1",
        replacement_owner="second",
        replacement_fragment_version="v1",
        registry_version="v2",
        rule_version="rule-v1",
    )
    fields.update(changes)
    return RegistryOverrideRule(**fields)


def _policy(*rules):
    @dataclass(frozen=True, slots=True, init=False, eq=False)
    class Policy(Registry[tuple]):
        override_rules: ClassVar[tuple[RegistryOverrideRule, ...]] = rules

    return Policy


def test_three_source_derived_declarations_preserve_order_and_isolation():
    lifecycle_data = (
        ("entity_type", "invocation"),
        ("table", "invocations"),
        (
            "statuses",
            frozenset(
                {
                    "running",
                    "completed",
                    "completed_empty",
                    "failed",
                    "timed_out",
                    "aborted",
                    "cancelled",
                }
            ),
        ),
        ("initial_statuses", frozenset({"running"})),
        (
            "terminal_statuses",
            frozenset(
                {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
            ),
        ),
        (
            "edges",
            (
                (
                    "running",
                    ("aborted", "cancelled", "completed", "completed_empty", "failed", "timed_out"),
                ),
            ),
        ),
        ("same_status", "append"),
        ("patch_fields", frozenset({"ended_at"})),
        ("reason_prefixes", frozenset({"run"})),
        ("reason_columns", True),
    )
    provider_data = (
        ("provider", "openai"),
        ("endpoint", "chat/completions"),
        ("aliases", ("chat",)),
        ("endpoint_type", "api"),
        (
            "options_reference",
            "lionagi.providers.openai._chat_schemas:OpenAIChatCompletionsRequest",
        ),
        ("implementation_reference", "lionagi.providers.openai.chat:OpenaiChatEndpoint"),
        ("base_url", "https://api.openai.com/v1"),
        ("auth_type", "bearer"),
    )
    cli_data = (
        ("orchestrate", "Multi-agent orchestration patterns.", "lionagi.cli.orchestrate", ("o",)),
        (
            "agent",
            "Spawn one-shot subagent (blocking); prints final response.",
            "lionagi.cli.agent",
            (),
        ),
    )
    fragments = (
        _fragment("example.lifecycle.execution", invocation=lifecycle_data),
        _fragment("example.providers.openai", **{"openai:chat/completions": provider_data}),
        RegistryFragment(
            owner="example.cli.core",
            version="v1",
            items=tuple(RegistryEntry(key=value[0], value=value) for value in cli_data),
        ),
    )
    selected = Registry.compose(*fragments, name="examples", version="v1")
    assert selected.keys() == ("invocation", "openai:chat/completions", "orchestrate", "agent")
    assert selected.items == (lifecycle_data, provider_data, *cli_data)
    assert selected.fragments == fragments
    assert [(r.source.fragment_index, r.source.item_index) for r in selected.records] == [
        (0, 0),
        (1, 0),
        (2, 0),
        (2, 1),
    ]
    another = Registry.compose(fragments[2], name="examples")
    assert another.keys() == ("orchestrate", "agent")
    assert selected.keys()[0] == "invocation"


def test_empty_composition_and_ordinary_query_contract():
    empty = Registry.compose(name="empty")
    assert (empty.fragments, empty.records, empty.overrides, empty.items, empty.keys()) == ((),) * 5
    assert len(empty) == 0 and empty.version is Unset
    registry = Registry.compose(_fragment(a=None, b=2), name="query")
    marker = object()
    assert "a" in registry and registry["a"] is None and registry.get("a", marker) is None
    assert "missing" not in registry and registry.get("missing", marker) is marker
    for key in ("missing", "", " "):
        with pytest.raises(KeyError):
            registry[key]
        with pytest.raises(KeyError):
            registry.source_of(key)
    source = registry.source_of("b")
    assert (source.owner, source.fragment_version, source.fragment_index, source.item_index) == (
        "first",
        "v1",
        0,
        1,
    )

    class String(str):
        pass

    for key in (1, True, None, String("a")):
        for query in (
            registry.__contains__,
            registry.__getitem__,
            registry.get,
            registry.source_of,
        ):
            with pytest.raises(TypeError, match="exact str"):
                query(key)
    for iterate in (iter, list, tuple):
        with pytest.raises(TypeError, match=r"keys\(\)"):
            iterate(registry)
    assert not hasattr(registry, "values") and not hasattr(registry, "owner_of")
    assert not callable(registry.items)


@pytest.mark.parametrize("value", [None, True, 1, b"a", type("Label", (str,), {})("a")])
def test_non_exact_string_labels_are_type_errors(value):
    with pytest.raises(TypeError):
        RegistryEntry(key=value, value=1)
    with pytest.raises(TypeError):
        Registry.compose(name=value)


@pytest.mark.parametrize("value", ["", " a", "a ", "\ta"])
def test_empty_or_untrimmed_labels_are_value_errors(value):
    with pytest.raises(ValueError):
        RegistryEntry(key=value, value=1)
    with pytest.raises(ValueError):
        Registry.compose(name=value)
    with pytest.raises(ValueError):
        _fragment(owner=value)
    with pytest.raises(ValueError):
        _rule(rule_version=value)


def test_competing_failures_follow_policy_payload_owner_key_order():
    poison = RegistryEntry(key="a", value=1)
    object.__setattr__(poison, "value", Undefined)
    invalid = object.__new__(RegistryFragment)
    for key, value in dict(owner="first", version="v1", items=(poison,), feature=Unset).items():
        object.__setattr__(invalid, key, value)
    duplicate = _fragment(a=2)
    policy = _policy(_rule(), _rule(rule_version="another"))
    with pytest.raises(ValueError, match="duplicate complete override"):
        policy.compose(invalid, duplicate, name="x", version="v2")
    with pytest.raises(RegistryValueError) as info:
        Registry.compose(invalid, duplicate, name="x")
    assert info.value.reason == "sentinel"
    with pytest.raises(DuplicateRegistryOwnerError) as info:
        Registry.compose(_fragment(a=1), duplicate, name="x")
    assert (info.value.first_fragment_index, info.value.duplicate_fragment_index) == (0, 1)
    with pytest.raises(DuplicateRegistryKeyError):
        Registry.compose(_fragment(a=1), _fragment("second", a=2), name="x")


def test_all_displaced_payloads_are_validated_before_an_authorized_override():
    first = _fragment(a=1)
    object.__setattr__(first.items[0], "value", [])
    with pytest.raises(RegistryValueError) as info:
        _policy(_rule()).compose(first, _fragment("second", a=2), name="x", version="v2")
    assert info.value.reason == "mutable"


def test_override_chains_bind_current_incumbent_and_preserve_every_source():
    first = _fragment(a=1, b=2)
    second = _fragment("second", a=3)
    third = _fragment("third", a=4)
    policy = _policy(
        _rule(), _rule(incumbent_owner="second", replacement_owner="third", rule_version="r2")
    )
    registry = policy.compose(first, second, third, name="x", version="v2")
    assert registry.keys() == ("a", "b") and registry.items == (4, 2)
    assert [(o.displaced.source.owner, o.replacement.source.owner) for o in registry.overrides] == [
        ("first", "second"),
        ("second", "third"),
    ]
    assert [o.displaced.entry.value for o in registry.overrides] == [1, 3]
    assert registry.fragments == (first, second, third)
    assert registry.source_of("a").fragment_index == 2
    with pytest.raises(DuplicateRegistryKeyError):
        _policy(_rule(), _rule(replacement_owner="third")).compose(
            first, second, third, name="x", version="v2"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("key", "other"),
        ("incumbent_owner", "other"),
        ("incumbent_fragment_version", "other"),
        ("replacement_owner", "other"),
        ("replacement_fragment_version", "other"),
        ("registry_version", "other"),
    ],
)
def test_every_collision_binding_is_exact(field, value):
    with pytest.raises(DuplicateRegistryKeyError):
        _policy(_rule(**{field: value})).compose(
            _fragment(a=1), _fragment("second", a=2), name="x", version="v2"
        )


def test_unset_same_fragment_and_same_owner_cannot_authorize_replacement():
    policy = _policy(_rule())
    with pytest.raises(DuplicateRegistryKeyError):
        policy.compose(_fragment(a=1), _fragment("second", a=2), name="x")
    repeated = RegistryFragment(
        owner="first",
        version="v1",
        items=(RegistryEntry(key="a", value=1), RegistryEntry(key="a", value=2)),
    )
    with pytest.raises(DuplicateRegistryKeyError):
        policy.compose(repeated, name="x", version="v2")
    with pytest.raises(DuplicateRegistryOwnerError):
        policy.compose(_fragment(a=1), _fragment(version="v2", b=2), name="x", version="v2")


def test_evidence_and_params_remain_the_only_equality_authority():
    policy = _policy(_rule())
    first, second = _fragment(a=1), _fragment("second", a=1)
    one = policy.compose(first, second, name="x", version="v2")
    two = policy.compose(first, second, name="x", version="v2")
    assert one == two and hash(one) == hash(two)
    assert one != Registry.compose(first, name="x", version="v2")
    assert one.overrides[0].displaced != one.overrides[0].replacement
    policy.override_rules = (_rule(key="unused"),)
    assert one == two and one.overrides[0].rule.key == "a"
    same_one = policy.compose(first, name="x", version="v2")
    policy.override_rules = ()
    assert same_one == policy.compose(first, name="x", version="v2")
    for cls in (
        RegistryEntry,
        RegistryFragment,
        RegistryOverrideRule,
        RegistrySource,
        RegistryRecord,
        RegistryOverride,
        Registry,
    ):
        assert cls.__eq__ is Params.__eq__ and cls.__hash__ is Params.__hash__


def test_authored_updates_are_fresh_and_derived_values_are_readonly():
    entry = RegistryEntry(key="a", value=1)
    fragment = RegistryFragment(owner="first", version="v1", items=(entry,))
    rule = _rule()
    assert entry.with_updates(value=2).value == 2 and entry.value == 1
    assert fragment.with_updates(version="v2").version == "v2" and fragment.version == "v1"
    assert (
        rule.with_updates(rule_version="r2").rule_version == "r2" and rule.rule_version == "rule-v1"
    )
    with pytest.raises(RegistryValueError):
        entry.with_updates(value=[])
    registry = _policy(rule).compose(fragment, _fragment("second", a=2), name="x", version="v2")
    values = (
        entry,
        fragment,
        rule,
        registry,
        registry.records[0],
        registry.source_of("a"),
        registry.overrides[0],
    )
    for value in values:
        field = dataclasses.fields(value)[0].name
        with pytest.raises((FrozenInstanceError, TypeError)):
            setattr(value, field, "changed")
        with pytest.raises((FrozenInstanceError, TypeError)):
            delattr(value, field)
    for value in values[3:]:
        with pytest.raises(TypeError):
            type(value)(**value._field_state())
        with pytest.raises(TypeError):
            value.with_updates()


@dataclass(frozen=True)
class _Box:
    child: Any


@pytest.mark.parametrize("nested", [False, True])
def test_plain_subclass_cannot_inherit_frozen_dataclass_admission(nested):
    class PlainBox(_Box):
        pass

    value = PlainBox(1)
    value.extra = []
    value.extra.append("mutable")
    payload = (value,) if nested else value
    with pytest.raises(RegistryValueError) as info:
        RegistryEntry(key="x", value=payload)
    assert (info.value.reason, info.value.path, info.value.value_type) == (
        "mutable",
        "$[0]" if nested else "$",
        PlainBox,
    )
    # Admission must not change the ordinary inherited dataclass key.
    assert structural._structural_key(value) == structural._structural_key(PlainBox(1))


@pytest.mark.parametrize("slots", [False, True])
def test_explicitly_frozen_dataclass_subclass_is_still_admitted(slots):
    @dataclass(frozen=True, slots=slots)
    class FrozenBox(_Box):
        label: str = "frozen"

    value = FrozenBox((1, 2))
    entry = RegistryEntry(key="x", value=value)
    assert entry.value is value
    assert structural._registry_structural_key(value) == structural._structural_key(value)
    with pytest.raises((FrozenInstanceError, TypeError)):
        value.extra = []


class _Choice(Enum):
    FIRST = ("first", 1)


@pytest.mark.parametrize(
    "value",
    [
        None,
        Ellipsis,
        True,
        1,
        1.0,
        -0.0,
        complex(1, -0.0),
        "x",
        b"x",
        PurePosixPath("a/b"),
        Path("a/b"),
        PureWindowsPath("A/B"),
        UUID(int=1),
        _Choice.FIRST,
        (),
        (1, _Box(("x", frozenset({1, "a"})))),
        frozenset({1, "a"}),
        typing.Any,
        typing.NoReturn,
        float("nan"),
        typing.Literal["one", 1],
        tuple[int, str],
        typing.Callable[[int, str], bool],
        lambda: None,
        int,
        len,
    ],
)
def test_admitted_keys_equal_fresh_ordinary_typed_keys(value, monkeypatch):
    class NoCache:
        _pending_removals = types.SimpleNamespace(append=lambda key: None)

        def get(self, key):
            raise AssertionError("admission read projection cache")

        def put(self, key, value):
            raise AssertionError("admission wrote projection cache")

    ordinary_cache = structural._stable_dataclass_keys
    monkeypatch.setattr(structural, "_stable_dataclass_keys", NoCache())
    admitted = structural._registry_structural_key(value)

    # A fresh ordinary projection is compared without using a warmed identity entry.
    class EmptyCache:
        _pending_removals = types.SimpleNamespace(append=lambda key: None)

        def get(self, key):
            return None

        def put(self, key, value):
            pass

    monkeypatch.setattr(structural, "_stable_dataclass_keys", EmptyCache())
    assert admitted == structural._structural_key(value)
    monkeypatch.setattr(structural, "_stable_dataclass_keys", ordinary_cache)
    assert RegistryEntry(key="x", value=value).value is value


@pytest.mark.parametrize(
    "value,reason,path",
    [
        (Undefined, "sentinel", "$"),
        (Unset, "sentinel", "$"),
        ((1, Undefined), "sentinel", "$[1]"),
        (_Box(Unset), "sentinel", "$.child"),
        ([], "mutable", "$"),
        ({}, "mutable", "$"),
        (set(), "mutable", "$"),
        (bytearray(), "mutable", "$"),
        (object(), "unsupported_type", "$"),
        (functools.partial(int, "1"), "unstable_identity", "$"),
        ((1, memoryview(b"a")), "memoryview", "$[1]"),
    ],
)
def test_refusals_have_typed_paths_without_payload_representation(value, reason, path):
    with pytest.raises(RegistryValueError) as info:
        RegistryEntry(key="x", value=value)
    assert (info.value.reason, info.value.path, info.value.value_type) == (
        reason,
        path,
        type(value) if path == "$" else type(value.child) if path == "$.child" else type(value[1]),
    )


def test_poison_children_models_callable_dataclasses_and_unknown_subclasses_are_not_executed():
    effects = []

    class Poison:
        def __hash__(self):
            effects.append("hash")
            raise AssertionError

        def __repr__(self):
            effects.append("repr")
            raise AssertionError

    fake_base = type("BaseModel", (), {"__module__": "pydantic.fake"})

    @dataclass(frozen=True)
    class Model(fake_base):
        child: Any

        def model_dump(self, **kwargs):
            effects.append("serializer")
            raise AssertionError

    @dataclass(frozen=True)
    class CallableBox:
        child: Any

        def __call__(self):
            effects.append("call")
            raise AssertionError

    class Mutable:
        def method(self):
            pass

    @dataclass
    class MutableBox:
        child: Any

    cases = [
        ([Poison()], "mutable"),
        (Poison(), "unsupported_type"),
        (Model(Poison()), "model"),
        (CallableBox(Poison()), "unstable_identity"),
        (Mutable().method, "unstable_identity"),
        (MutableBox(Poison()), "mutable"),
        (type("Number", (int,), {})(1), "unsupported_type"),
        (type("Tuple", (tuple,), {})((1,)), "unsupported_type"),
    ]
    for value, reason in cases:
        with pytest.raises(RegistryValueError) as info:
            RegistryEntry(key="x", value=value)
        assert info.value.reason == reason
    assert effects == []


def test_memoryviews_all_refuse_without_reading_released_state():
    released = memoryview(b"x")
    released.release()
    for value in (memoryview(b"x"), memoryview(bytearray(b"x")), released):
        with pytest.raises(RegistryValueError) as info:
            RegistryEntry(key="x", value=value)
        assert info.value.reason == "memoryview"


def test_nested_warm_cache_is_not_admission_and_admission_never_writes(monkeypatch):
    inner = _Box(1)
    structural._structural_key(inner)
    object.__setattr__(inner, "child", Unset)
    with pytest.raises(RegistryValueError) as info:
        RegistryEntry(key="x", value=(_Box(inner),))
    assert (info.value.reason, info.value.path) == ("sentinel", "$[0].child.child")

    class PoisonCache:
        _pending_removals = types.SimpleNamespace(append=lambda key: None)

        def get(self, key):
            raise AssertionError("cache read")

        def put(self, key, value):
            raise AssertionError("cache write")

    monkeypatch.setattr(structural, "_stable_dataclass_keys", PoisonCache())
    RegistryEntry(key="x", value=(_Box(_Box(1)),))


def test_cache_retention_limits_do_not_become_payload_admission(monkeypatch):
    monkeypatch.setattr(structural, "_MAX_CACHED_WEIGHT", 1)
    local_function = lambda: None
    value = (_Box("a" * 1000), local_function, type("Local", (), {}))
    assert structural._try_stable_cache_key(value) is None
    assert RegistryEntry(key="x", value=value).value is value


def test_identity_admission_does_not_resolve_module_or_class_names(monkeypatch):
    def refuse_lookup(value):
        raise AssertionError("identity admission performed cache pinning lookup")

    monkeypatch.setattr(structural, "_reachable_by_name", refuse_lookup)
    value = (lambda: None, type("Unnamed", (), {}), len)
    assert RegistryEntry(key="x", value=value).value is value


def test_typed_numeric_distinctions_and_equal_value_hash_agreement():
    entries = [RegistryEntry(key="x", value=value) for value in (True, 1, 1.0, 0.0, -0.0)]
    for i, left in enumerate(entries):
        for j, right in enumerate(entries):
            assert (left == right) is (i == j)
    for left in entries:
        assert left == RegistryEntry(key="x", value=left.value)
        assert hash(left) == hash(RegistryEntry(key="x", value=left.value))


def test_cycles_in_each_recursive_branch_and_repeated_dag_sharing(monkeypatch):
    child = _Box(None)
    object.__setattr__(child, "child", child)

    class Loop(Enum):
        MEMBER = 1

    object.__setattr__(Loop.MEMBER, "_value_", Loop.MEMBER)
    alias = tuple[int]
    real_args = typing.get_args
    monkeypatch.setattr(
        typing, "get_args", lambda value: (alias,) if value is alias else real_args(value)
    )
    for value, path in ((child, "$.child"), (Loop.MEMBER, "$.value"), (alias, "$.args[0]")):
        with pytest.raises(RegistryValueError) as info:
            RegistryEntry(key="x", value=value)
        assert (info.value.reason, info.value.path) == ("cycle", path)
    shared = _Box((1, 2))
    assert RegistryEntry(key="x", value=(shared, shared)).value == (shared, shared)
    nested = 1
    for _ in range(30):
        nested = (nested,)
    RegistryEntry(key="x", value=nested)


def test_tuple_and_frozenset_cycle_guards_and_noncycle_recursion_errors(monkeypatch):
    for container in (tuple, frozenset):
        box = _Box(1)
        value = container((box,))
        object.__setattr__(box, "child", value)
        with pytest.raises(RegistryValueError) as info:
            RegistryEntry(key="x", value=value)
        assert info.value.reason == "cycle"
        assert info.value.path == ("$[0].child" if container is tuple else "$[*].child")

    def exhausted(value):
        raise RecursionError("independent projection defect")

    monkeypatch.setattr(structural, "_encode_int", exhausted)
    with pytest.raises(RecursionError, match="independent projection defect"):
        RegistryEntry(key="x", value=(1,))


def test_models_are_refused_before_their_real_serializer():
    from pydantic import BaseModel

    calls = []

    class Model(BaseModel):
        value: int = 1

        def model_dump(self, **kwargs):
            calls.append("model_dump")
            raise AssertionError("serializer must not run")

    with pytest.raises(RegistryValueError) as info:
        RegistryEntry(key="x", value=Model())
    assert info.value.reason == "model" and calls == []


def test_msgspec_mro_is_refused_without_importing_a_serializer():
    base = type("Struct", (), {"__module__": "msgspec"})

    @dataclass(frozen=True)
    class Data(base):
        child: Any

    with pytest.raises(RegistryValueError) as info:
        RegistryEntry(key="x", value=Data(Unset))
    assert info.value.reason == "model" and info.value.path == "$"


def test_only_exact_fragment_and_policy_containers_are_accepted():
    for items in ([], iter(()), set()):
        with pytest.raises(TypeError):
            RegistryFragment(owner="x", version="v", items=items)
    with pytest.raises(TypeError):
        RegistryFragment(owner="x", version="v", items=(1,))
    with pytest.raises(TypeError):
        Registry.compose(None, name="x")
    policy = _policy()
    policy.override_rules = []
    with pytest.raises(TypeError):
        policy.compose(name="x")


def test_internal_result_factory_validates_sources_and_override_bindings():
    from lionagi.ln.types.registry import _internal

    for index in (-1, True, 1.0):
        with pytest.raises(TypeError):
            _internal(
                RegistrySource, owner="a", fragment_version="v1", fragment_index=index, item_index=0
            )
    registry = _policy(_rule()).compose(
        _fragment(a=1), _fragment("second", a=2), name="x", version="v2"
    )
    evidence = registry.overrides[0]
    with pytest.raises(ValueError, match="does not match"):
        _internal(
            RegistryOverride,
            rule=_rule(key="other"),
            displaced=evidence.displaced,
            replacement=evidence.replacement,
        )
    with pytest.raises(TypeError):
        _internal(RegistryRecord, entry=RegistryEntry(key="a", value=1), source=object())


def test_params_default_factory_and_field_model_construction_remain_compatible():
    from lionagi.models.field_model import FieldModel

    @dataclass(frozen=True, slots=True, init=False, eq=False)
    class Defaults(Params):
        first: int = 1
        second: tuple = dataclasses.field(default_factory=tuple)

    original = Defaults()
    updated = original.with_updates(first=2)
    assert (original.first, original.second, updated.first, updated.second) == (1, (), 2, ())
    field = FieldModel(name="answer", annotation=int)
    assert field.name == "answer" and field.base_type is int


def test_enum_typing_and_unordered_members_propagate_admission(monkeypatch):
    class Bad(Enum):
        MEMBER = Unset

    for value, path in ((Bad.MEMBER, "$.value"), (frozenset({Unset}), "$[*]")):
        with pytest.raises(RegistryValueError) as info:
            RegistryEntry(key="x", value=value)
        assert (info.value.reason, info.value.path) == ("sentinel", path)
    alias = tuple[int]
    real_args = typing.get_args
    monkeypatch.setattr(
        typing, "get_args", lambda value: (Undefined,) if value is alias else real_args(value)
    )
    with pytest.raises(RegistryValueError) as info:
        RegistryEntry(key="x", value=alias)
    assert (info.value.reason, info.value.path) == ("sentinel", "$.args[0]")


def test_ordinary_projection_stays_fields_first_for_callable_dataclasses():
    @dataclass(frozen=True)
    class CallableData:
        field: int

        def __call__(self):
            return self.field

    assert structural._structural_key(CallableData(1)) == structural._structural_key(
        CallableData(1)
    )
    assert structural._structural_key(CallableData(1)) != structural._structural_key(
        CallableData(2)
    )
    assert structural._structural_key(Meta("x", Unset)).hash_safe


def test_policy_shape_is_checked_when_used():
    @dataclass(frozen=True, slots=True, init=False)
    class GeneratedEquality(Registry[int]):
        pass

    @dataclass(frozen=True, slots=True, init=False, eq=False)
    class NewField(Registry[int]):
        extra: int = 1

    class Undecorated(Registry[int]):
        pass

    @dataclass(frozen=True, slots=True, init=False, eq=False)
    class NewBehavior(Registry[int]):
        def get(self, key, default=None):
            return default

    for policy in (GeneratedEquality, NewField, Undecorated, NewBehavior):
        with pytest.raises(TypeError):
            policy.compose(name="x")
    assert _policy().compose(name="x").items == ()
    foreign_slot = _policy()
    foreign_slot.name = Registry.__dict__["name"]
    with pytest.raises(TypeError):
        foreign_slot.compose(name="x")


def test_exports_and_cold_import_do_not_activate_domains_or_optional_models():
    root = str(Path(__file__).parents[2])
    source = """
import sys, importlib.abc, importlib.util, importlib.metadata, asyncio, builtins
sys.path.insert(0, ROOT)
blocked = ("pydantic", "sqlalchemy", "lionagi.state", "lionagi.cli",
           "lionagi.studio", "lionagi.providers", "openai", "anthropic")
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in blocked):
            raise AssertionError("forbidden import " + fullname)
sys.meta_path.insert(0, Block())
old_find = importlib.util.find_spec
def find(name, *args, **kwargs):
    if any(name == p or name.startswith(p + ".") for p in blocked):
        raise AssertionError("forbidden probe " + name)
    return old_find(name, *args, **kwargs)
importlib.util.find_spec = find
def refuse(*args, **kwargs): raise AssertionError("activation")
importlib.metadata.entry_points = refuse
asyncio.create_task = refuse
old_open = builtins.open
def read_only(file, mode="r", *args, **kwargs):
    if any(c in mode for c in "wax+"): raise AssertionError("write")
    return old_open(file, mode, *args, **kwargs)
builtins.open = read_only
from lionagi import ln
from lionagi.ln.types import Registry, RegistryEntry, RegistryFragment
assert ln.Registry is Registry
a = Registry.compose(RegistryFragment(owner="a", version="v", items=(RegistryEntry(key="x", value=1),)), name="a")
b = Registry.compose(name="b")
assert a.keys() == ("x",) and b.keys() == ()
def state(registry):
    return (registry.name, registry.version, registry.keys(), registry.items,
            tuple((r.entry.key, r.source.owner, r.source.fragment_version,
                   r.source.fragment_index, r.source.item_index) for r in registry.records),
            registry.fragments, registry.overrides)
before = state(a)
import lionagi.ln.types.registry as registry_module
importlib.reload(registry_module)
fresh = registry_module.Registry.compose(
    registry_module.RegistryFragment(owner="new", version="v2", items=(
        registry_module.RegistryEntry(key="y", value=2),)), name="new")
assert fresh.keys() == ("y",) and fresh.items == (2,)
assert state(a) == before and a["x"] == 1 and a.source_of("x").owner == "a"
assert "x" in a and a.get("absent", 9) == 9 and b.keys() == ()
assert not any(any(m == p or m.startswith(p + ".") for p in blocked) for m in sys.modules)
print("cold-ok")
""".replace("ROOT", repr(root))
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", source], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cold-ok"
    for cls in (
        Registry,
        RegistryEntry,
        RegistryFragment,
        RegistryOverrideRule,
        RegistrySource,
        RegistryRecord,
        RegistryOverride,
        RegistryValueError,
    ):
        assert getattr(ln, cls.__name__) is cls
