# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0119 D6 gates for lifecycle's private immutable catalog."""

from __future__ import annotations

import copy
import pickle
from collections.abc import ItemsView, Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

import lionagi.state.lifecycle as lifecycle
import lionagi.state.lifecycle.policy as policy_module
from lionagi.ln import Registry, RegistryEntry, Unset
from lionagi.ln.types.base import Params
from lionagi.state.lifecycle import EdgePolicy, LifecyclePolicy
from lionagi.state.lifecycle.policy import DEFAULT_REGISTRY, PolicyRegistry, build_default_registry

_BUILTIN_ORDER = (
    "session",
    "invocation",
    "show",
    "play",
    "team",
    "schedule_run",
    "dispatch",
)
_DECLARATION_FIELDS = (
    "entity_type",
    "table",
    "statuses",
    "initial_statuses",
    "terminal_statuses",
    "edge_items",
    "same_status",
    "patch_fields",
    "reason_prefixes",
    "reason_columns",
)


class _FlippingEdges(Mapping[str, tuple[EdgePolicy, ...]]):
    """Return a different graph if registration reads the Mapping twice."""

    def __init__(self) -> None:
        self.calls = 0

    def __getitem__(self, key: str) -> tuple[EdgePolicy, ...]:
        if key != "open":
            raise KeyError(key)
        return (EdgePolicy(to_status="closed"),)

    def __iter__(self) -> Iterator[str]:
        return iter(("open",))

    def __len__(self) -> int:
        return 1

    def items(self) -> ItemsView[str, tuple[EdgePolicy, ...]]:
        self.calls += 1
        if self.calls == 1:
            return {"open": (EdgePolicy(to_status="closed"),)}.items()
        return {"ghost": (EdgePolicy(to_status="missing"),)}.items()


class _NeverReadEdges(Sequence[EdgePolicy]):
    """Fail if an unknown edge source's values are consumed."""

    def __getitem__(self, index: int) -> EdgePolicy:
        raise AssertionError(f"edge value was read at index {index}")

    def __len__(self) -> int:
        raise AssertionError("edge value length was read")


class _InvalidThenExplodingEdges(Sequence[EdgePolicy]):
    """Yield one invalid edge, then fail if validation reads ahead."""

    def __getitem__(self, index: int) -> EdgePolicy:
        if index == 0:
            return EdgePolicy(to_status="missing")
        raise RuntimeError("edge iterable was read past the first invalid edge")

    def __len__(self) -> int:
        return 2


def _policy(*, entity_type: str = "widget", table: str = "widgets") -> LifecyclePolicy:
    return LifecyclePolicy(
        entity_type=entity_type,
        table=table,
        statuses=frozenset({"open", "closed"}),
        initial_statuses=frozenset({"open"}),
        terminal_statuses=frozenset({"closed"}),
        edges={"open": (EdgePolicy(to_status="closed"),)},
        same_status="append",
        patch_fields=frozenset(),
        reason_prefixes=frozenset({"widget"}),
    )


def _declaration_type() -> type[Params]:
    value = getattr(policy_module, "_LifecyclePolicyDeclaration", None)
    assert isinstance(value, type), "policy.py must define _LifecyclePolicyDeclaration"
    assert issubclass(value, Params)
    return value


def _catalog_type() -> type[Registry[Any]]:
    value = getattr(policy_module, "_LifecyclePolicyCatalog", None)
    assert isinstance(value, type), "policy.py must define _LifecyclePolicyCatalog"
    assert issubclass(value, Registry)
    return value


def _direct_state_values(instance: object) -> tuple[object, ...]:
    values: list[object] = list(getattr(instance, "__dict__", {}).values())
    seen_slots: set[str] = set()
    for owner in type(instance).__mro__:
        slots = owner.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name in {"__dict__", "__weakref__"} or name in seen_slots:
                continue
            seen_slots.add(name)
            try:
                value = object.__getattribute__(instance, name)
            except AttributeError:
                continue
            values.append(value)
    return tuple(values)


def _catalog_for(registry: PolicyRegistry) -> Registry[Any]:
    catalog_type = _catalog_type()
    candidates: list[Registry[Any]] = []
    for value in _direct_state_values(registry):
        if isinstance(value, catalog_type):
            candidates.append(value)
        elif type(value) is tuple:
            candidates.extend(item for item in value if isinstance(item, catalog_type))
    assert len(candidates) == 1, "a sealed facade must own exactly one private catalog"
    return candidates[0]


def _assert_no_mutable_collection_authority(registry: PolicyRegistry) -> None:
    assert not hasattr(registry, "_by_entity_type")
    assert not hasattr(registry, "_by_table")
    mutable = [value for value in _direct_state_values(registry) if type(value) in {dict, list}]
    assert mutable == []


def test_private_declaration_and_catalog_have_the_required_closed_surface() -> None:
    declaration_type = _declaration_type()
    catalog_type = _catalog_type()

    assert declaration_type.field_names() == _DECLARATION_FIELDS
    assert catalog_type.__bases__ == (Registry,)
    assert catalog_type.override_rules == ()
    assert "_LifecyclePolicyDeclaration" not in lifecycle.__all__
    assert "_LifecyclePolicyCatalog" not in lifecycle.__all__
    assert not hasattr(lifecycle, "_LifecyclePolicyDeclaration")
    assert not hasattr(lifecycle, "_LifecyclePolicyCatalog")


def test_sealed_default_uses_one_hashable_provenance_carrying_catalog() -> None:
    declaration_type = _declaration_type()
    catalog = _catalog_for(DEFAULT_REGISTRY)

    assert catalog.keys() == _BUILTIN_ORDER
    assert tuple(record.entry.key for record in catalog.items) == _BUILTIN_ORDER
    assert all(type(value) is declaration_type for value in catalog.values())
    assert all(not isinstance(value, LifecyclePolicy) for value in catalog.values())
    assert catalog.overrides == ()
    assert hash(catalog) == hash(copy.deepcopy(catalog))

    # These are new private D6 contract identifiers, not legacy public API.
    assert catalog.name == "lifecycle_policies"
    assert catalog.version == "1"
    assert len(catalog.fragments) == 1
    fragment = catalog.fragments[0]
    assert fragment.owner == "lionagi.state.lifecycle.policy"
    assert fragment.version == "1"
    assert fragment.feature is Unset
    assert tuple(fragment.items) == tuple(record.entry for record in catalog.items)
    assert all(record.owner == fragment.owner for record in catalog.items)
    assert all(record.fragment_version == fragment.version for record in catalog.items)
    assert all(catalog.owner_of(key) == fragment.owner for key in _BUILTIN_ORDER)

    for record, declaration in zip(catalog.items, catalog.values(), strict=True):
        key = record.entry.key
        rebuilt = RegistryEntry(key=key, value=declaration)
        assert rebuilt.key == key
        assert rebuilt.value is declaration
        assert rebuilt == record.entry
        assert hash(rebuilt) == hash(record.entry)


def test_catalog_declarations_preserve_ordered_edges_and_public_projection_values() -> None:
    declaration_type = _declaration_type()
    catalog = _catalog_for(DEFAULT_REGISTRY)

    for entity_type in _BUILTIN_ORDER:
        declaration = catalog[entity_type]
        projection = DEFAULT_REGISTRY.get(entity_type)
        assert type(declaration) is declaration_type
        edge_items = getattr(declaration, "edge_items")
        assert type(edge_items) is tuple
        assert edge_items == tuple(projection.edges.items())
        assert not hasattr(declaration, "edges")
        for field_name in _DECLARATION_FIELDS:
            if field_name != "edge_items":
                assert getattr(declaration, field_name) == getattr(projection, field_name)
        assert hash(declaration) == hash(copy.deepcopy(declaration))
        assert isinstance(projection, LifecyclePolicy)
        assert not isinstance(projection, Registry)


def test_two_default_builds_own_distinct_equivalent_catalogs() -> None:
    first = build_default_registry()
    second = build_default_registry()
    first_catalog = _catalog_for(first)
    second_catalog = _catalog_for(second)

    assert first_catalog is not second_catalog
    assert first_catalog == second_catalog
    assert hash(first_catalog) == hash(second_catalog)
    for entity_type in _BUILTIN_ORDER:
        assert first.get(entity_type) is not second.get(entity_type)
        assert first.get(entity_type) == second.get(entity_type)


def test_sealed_facade_copy_protocol_preserves_catalog_ownership() -> None:
    registry = build_default_registry()
    catalog = _catalog_for(registry)

    shallow = copy.copy(registry)
    assert _catalog_for(shallow) is catalog

    for cloned in (copy.deepcopy(registry), pickle.loads(pickle.dumps(registry))):
        cloned_catalog = _catalog_for(cloned)
        assert cloned_catalog == catalog
        assert cloned_catalog is not catalog


def test_sealed_and_unsealed_facades_have_no_dict_or_list_authority() -> None:
    local = PolicyRegistry()
    local.register(_policy())
    _assert_no_mutable_collection_authority(local)
    local.seal()
    _assert_no_mutable_collection_authority(local)
    _assert_no_mutable_collection_authority(DEFAULT_REGISTRY)


def test_raw_lifecycle_policy_is_not_a_registry_declaration() -> None:
    with pytest.raises(TypeError, match="mutable structural value"):
        RegistryEntry(key="widget", value=_policy())


def test_registration_snapshots_the_callers_edge_mapping_once() -> None:
    edges = {"open": (EdgePolicy(to_status="closed"),)}
    registry = PolicyRegistry()
    registry.register(
        LifecyclePolicy(
            entity_type="widget",
            table="widgets",
            statuses=frozenset({"open", "closed"}),
            initial_statuses=frozenset({"open"}),
            terminal_statuses=frozenset({"closed"}),
            edges=edges,
            same_status="append",
            patch_fields=frozenset(),
            reason_prefixes=frozenset({"widget"}),
        )
    )
    projection = registry.get("widget")

    edges.clear()
    registry.seal()

    declaration = _catalog_for(registry)["widget"]
    assert declaration.edge_items == (("open", (EdgePolicy(to_status="closed"),)),)
    assert tuple(projection.edges.items()) == declaration.edge_items


def test_registration_reads_a_custom_edge_mapping_exactly_once() -> None:
    edges = _FlippingEdges()
    registry = PolicyRegistry()
    registry.register(
        LifecyclePolicy(
            entity_type="widget",
            table="widgets",
            statuses=frozenset({"open", "closed"}),
            initial_statuses=frozenset({"open"}),
            terminal_statuses=frozenset({"closed"}),
            edges=edges,
            same_status="append",
            patch_fields=frozenset(),
            reason_prefixes=frozenset(),
        )
    )
    registry.seal()

    assert edges.calls == 1
    declaration = _catalog_for(registry)["widget"]
    assert declaration.edge_items == (("open", (EdgePolicy(to_status="closed"),)),)
    assert tuple(registry.get("widget").edges) == ("open",)


def test_registration_rejects_an_unknown_source_before_reading_its_edges() -> None:
    registry = PolicyRegistry()
    policy = replace(_policy(), edges={"ghost": _NeverReadEdges()})

    with pytest.raises(ValueError) as caught:
        registry.register(policy)

    assert str(caught.value) == (
        "lifecycle policy registration: entity_type 'widget' declares edges "
        "from unknown status 'ghost'"
    )


def test_registration_rejects_an_invalid_edge_without_reading_ahead() -> None:
    registry = PolicyRegistry()
    policy = replace(_policy(), edges={"open": _InvalidThenExplodingEdges()})

    with pytest.raises(ValueError) as caught:
        registry.register(policy)

    assert str(caught.value) == (
        "lifecycle policy registration: entity_type 'widget' edge 'open' -> "
        "'missing' targets an unknown status"
    )


def test_empty_registry_seals_to_one_empty_catalog() -> None:
    registry = PolicyRegistry()

    assert registry.seal() is None
    assert registry.seal() is None
    assert _catalog_for(registry).keys() == ()
    assert registry.entity_types() == frozenset()
    assert "widget" not in registry


def test_rejected_registration_leaves_staging_usable_and_unchanged() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    projection = registry.get("widget")

    invalid = LifecyclePolicy(
        entity_type="gadget",
        table="gadgets",
        statuses=frozenset({"open", "closed"}),
        initial_statuses=frozenset({"missing"}),
        terminal_statuses=frozenset({"closed"}),
        edges={},
        same_status="append",
        patch_fields=frozenset(),
        reason_prefixes=frozenset(),
    )
    with pytest.raises(ValueError, match="initial_statuses outside statuses"):
        registry.register(invalid)

    assert registry.entity_types() == frozenset({"widget"})
    assert registry.get("widget") is projection
    registry.register(_policy(entity_type="gadget", table="gadgets"))
    registry.seal()
    assert _catalog_for(registry).keys() == ("widget", "gadget")


def test_preseal_projection_identity_survives_catalog_composition() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    before = registry.get("widget")

    registry.seal()

    catalog = _catalog_for(registry)
    assert catalog.keys() == ("widget",)
    assert registry.get("widget") is before
    assert registry.get("widget") is registry.get("widget")


def test_unsealed_shallow_copy_has_isolated_tuple_staging() -> None:
    original = PolicyRegistry()
    original.register(_policy())
    cloned = copy.copy(original)

    assert cloned.get("widget") is original.get("widget")
    cloned.register(_policy(entity_type="gadget", table="gadgets"))

    assert cloned.entity_types() == frozenset({"widget", "gadget"})
    assert original.entity_types() == frozenset({"widget"})
    assert "gadget" not in original
    _assert_no_mutable_collection_authority(original)
    _assert_no_mutable_collection_authority(cloned)


@pytest.mark.parametrize(
    "clone",
    (
        pytest.param(copy.deepcopy, id="deepcopy"),
        pytest.param(lambda value: pickle.loads(pickle.dumps(value)), id="pickle"),
    ),
)
def test_unsealed_deepcopy_and_pickle_have_isolated_staging(clone) -> None:
    original = PolicyRegistry()
    original.register(_policy())
    cloned = clone(original)

    assert cloned.get("widget") == original.get("widget")
    assert cloned.get("widget") is not original.get("widget")
    cloned.register(_policy(entity_type="gadget", table="gadgets"))

    assert cloned.entity_types() == frozenset({"widget", "gadget"})
    assert original.entity_types() == frozenset({"widget"})
    assert "gadget" not in original


def test_seal_is_single_thread_exception_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    real_catalog_type = _catalog_type()
    registry = PolicyRegistry()
    registry.register(_policy())
    projection = registry.get("widget")

    class ExplodingCatalog:
        @classmethod
        def compose(cls, *args, **kwargs):
            raise RuntimeError("injected catalog composition failure")

    with monkeypatch.context() as patch:
        patch.setattr(policy_module, "_LifecyclePolicyCatalog", ExplodingCatalog)
        with pytest.raises(RuntimeError, match="injected catalog composition failure"):
            registry.seal()

    assert getattr(policy_module, "_LifecyclePolicyCatalog") is real_catalog_type
    assert registry.get("widget") is projection
    assert registry.entity_types() == frozenset({"widget"})
    registry.register(_policy(entity_type="gadget", table="gadgets"))
    registry.seal()
    assert _catalog_for(registry).keys() == ("widget", "gadget")
