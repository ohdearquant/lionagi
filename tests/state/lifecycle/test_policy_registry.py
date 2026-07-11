# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0058 Phase 1 gate: PolicyRegistry self-validation and the 7 built-in
policies' structural integrity."""

from __future__ import annotations

import dataclasses
import pickle

import pytest

from lionagi.state.lifecycle import DEFAULT_REGISTRY, EdgePolicy, LifecyclePolicy
from lionagi.state.lifecycle.policy import ImmutableEdgeMap, PolicyRegistry


def _policy(**overrides) -> LifecyclePolicy:
    base = dict(
        entity_type="widget",
        table="widgets",
        statuses=frozenset({"open", "closed"}),
        initial_statuses=frozenset({"open"}),
        terminal_statuses=frozenset({"closed"}),
        edges={},
        same_status="append",
        patch_fields=frozenset(),
        reason_prefixes=frozenset(),
    )
    base.update(overrides)
    return LifecyclePolicy(**base)


def test_default_registry_has_the_seven_built_in_entity_types() -> None:
    assert DEFAULT_REGISTRY.entity_types() == frozenset(
        {"session", "invocation", "show", "play", "team", "schedule_run", "dispatch"}
    )


@pytest.mark.parametrize(
    "entity_type", ["session", "invocation", "show", "play", "team", "schedule_run", "dispatch"]
)
def test_default_registry_policy_is_internally_consistent(entity_type: str) -> None:
    policy = DEFAULT_REGISTRY.get(entity_type)
    assert policy.initial_statuses <= policy.statuses
    assert policy.terminal_statuses <= policy.statuses
    for from_status, edges in policy.edges.items():
        assert from_status in policy.statuses
        for edge in edges:
            assert edge.to_status in policy.statuses
            assert edge.required_patch_fields <= policy.patch_fields
            assert edge.required_guard_fields <= policy.patch_fields


@pytest.mark.parametrize(
    "entity_type", ["session", "invocation", "show", "play", "team", "schedule_run", "dispatch"]
)
def test_default_registry_required_guard_fields_within_patch_fields_allowlist(
    entity_type: str,
) -> None:
    """Built-in-consistency check: every edge's required_guard_fields must be
    a subset of the owning policy's patch_fields allowlist."""
    policy = DEFAULT_REGISTRY.get(entity_type)
    for edges in policy.edges.values():
        for edge in edges:
            assert edge.required_guard_fields <= policy.patch_fields


def test_get_unknown_entity_type_raises_with_registered_types_listed() -> None:
    with pytest.raises(ValueError, match="unknown entity_type"):
        DEFAULT_REGISTRY.get("bogus")


def test_register_duplicate_entity_type_rejected() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_policy(table="other_widgets"))


def test_register_duplicate_table_rejected() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    with pytest.raises(ValueError, match="table .* is already registered"):
        registry.register(_policy(entity_type="other_widget"))


def test_register_initial_status_outside_statuses_rejected() -> None:
    registry = PolicyRegistry()
    with pytest.raises(ValueError, match="initial_statuses outside statuses"):
        registry.register(_policy(initial_statuses=frozenset({"nonexistent"})))


def test_register_terminal_status_outside_statuses_rejected() -> None:
    registry = PolicyRegistry()
    with pytest.raises(ValueError, match="terminal_statuses outside statuses"):
        registry.register(_policy(terminal_statuses=frozenset({"nonexistent"})))


def test_register_edge_from_unknown_status_rejected() -> None:
    registry = PolicyRegistry()
    with pytest.raises(ValueError, match="edges from unknown status"):
        registry.register(_policy(edges={"nonexistent": (EdgePolicy(to_status="closed"),)}))


def test_register_edge_to_unknown_status_rejected() -> None:
    registry = PolicyRegistry()
    with pytest.raises(ValueError, match="targets an unknown status"):
        registry.register(_policy(edges={"open": (EdgePolicy(to_status="nonexistent"),)}))


def test_register_edge_required_patch_field_outside_allowlist_rejected() -> None:
    registry = PolicyRegistry()
    with pytest.raises(ValueError, match="outside the policy's patch_fields allowlist"):
        registry.register(
            _policy(
                edges={
                    "open": (
                        EdgePolicy(to_status="closed", required_patch_fields=frozenset({"nope"})),
                    )
                },
                patch_fields=frozenset({"reason"}),
            )
        )


def test_register_edge_required_guard_field_outside_allowlist_rejected() -> None:
    registry = PolicyRegistry()
    with pytest.raises(ValueError, match="outside the policy's patch_fields allowlist"):
        registry.register(
            _policy(
                edges={
                    "open": (
                        EdgePolicy(
                            to_status="closed",
                            required_guard_fields=frozenset({"not_a_column"}),
                        ),
                    )
                },
                patch_fields=frozenset({"reason"}),
            )
        )


def test_get_returns_a_registered_policy() -> None:
    registry = PolicyRegistry()
    policy = _policy()
    registry.register(policy)
    # Not `is policy` — register() wraps the edge map in an immutable view,
    # which for a frozen dataclass means storing a new instance, not the
    # caller's original object. Field-for-field equality still holds.
    stored = registry.get("widget")
    assert stored == policy
    assert stored.entity_type == "widget"


def test_contains() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    assert "widget" in registry
    assert "bogus" not in registry


# ── Registry/policy immutability ────────────────────────────────────────────


def test_registered_policy_edge_map_mutation_raises() -> None:
    """A caller holding a policy from get() must not be able to mutate the
    edge map in place and change global transition behavior for the process."""
    registry = PolicyRegistry()
    registry.register(_policy(edges={"open": (EdgePolicy(to_status="closed"),)}))
    policy = registry.get("widget")
    with pytest.raises(TypeError):
        policy.edges["open"] = ()


def test_default_registry_edge_map_mutation_raises() -> None:
    policy = DEFAULT_REGISTRY.get("dispatch")
    with pytest.raises(TypeError):
        policy.edges["delivering"] = ()


def test_default_registry_is_sealed_late_registration_raises() -> None:
    with pytest.raises(RuntimeError, match="registry is sealed"):
        DEFAULT_REGISTRY.register(_policy(entity_type="late_widget", table="late_widgets"))


def test_locally_constructed_registry_accepts_registration_before_sealing() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())  # does not raise — unsealed by default
    assert "widget" in registry
    registry.seal()
    with pytest.raises(RuntimeError, match="registry is sealed"):
        registry.register(_policy(entity_type="other_widget", table="other_widgets"))


# ── ImmutableEdgeMap: immutable but serialization-safe ──────────────────────


def test_immutable_edge_map_item_assignment_raises() -> None:
    edge_map = ImmutableEdgeMap({"open": (EdgePolicy(to_status="closed"),)})
    with pytest.raises(TypeError):
        edge_map["open"] = ()


def test_immutable_edge_map_has_no_mutator_surface() -> None:
    edge_map = ImmutableEdgeMap({"open": (EdgePolicy(to_status="closed"),)})
    with pytest.raises(TypeError):
        del edge_map["open"]
    for mutator in ("update", "clear", "pop", "popitem", "setdefault"):
        assert not hasattr(edge_map, mutator)


def test_immutable_edge_map_inherited_dict_paths_cannot_mutate() -> None:
    """The mutation paths a dict subclass cannot close must all fail here:
    the in-place union operator, re-invoking __init__, reaching for dict's
    C-level mutators directly, and attribute reassignment — and the map's
    contents must be unchanged after every attempt."""
    edge_map = ImmutableEdgeMap({"open": (EdgePolicy(to_status="closed"),)})
    before = dict(edge_map)
    with pytest.raises(TypeError):
        edge_map |= {"new": ()}
    with pytest.raises(TypeError):
        edge_map.__init__({"new": ()})
    with pytest.raises(TypeError):
        dict.__setitem__(edge_map, "new", ())
    with pytest.raises(TypeError):
        edge_map._edges = {}
    assert dict(edge_map) == before


def test_registered_policy_edges_cannot_be_mutated_via_inherited_paths() -> None:
    registry = PolicyRegistry()
    registry.register(_policy(edges={"open": (EdgePolicy(to_status="closed"),)}))
    edges = registry.get("widget").edges
    before = dict(edges)
    with pytest.raises(TypeError):
        registry.get("widget").edges["new"] = ()
    with pytest.raises(TypeError):
        edges.__init__({"new": ()})
    assert dict(registry.get("widget").edges) == before


def test_registered_policy_dataclasses_replace_works() -> None:
    registry = PolicyRegistry()
    registry.register(_policy(edges={"open": (EdgePolicy(to_status="closed"),)}))
    policy = registry.get("widget")
    replaced = dataclasses.replace(policy, entity_type="widget2")
    assert replaced.entity_type == "widget2"
    assert replaced.edges == policy.edges


def test_registered_policy_dataclasses_asdict_works() -> None:
    """asdict() must not raise on a registered policy. A non-dict Mapping is
    deep-copied by asdict rather than recursed into, so the edges value stays
    an ImmutableEdgeMap holding EdgePolicy instances — and the copy is
    independent of the registered original."""
    registry = PolicyRegistry()
    registry.register(_policy(edges={"open": (EdgePolicy(to_status="closed"),)}))
    policy = registry.get("widget")
    as_dict = dataclasses.asdict(policy)
    assert isinstance(as_dict, dict)
    assert isinstance(as_dict["edges"], ImmutableEdgeMap)
    assert as_dict["edges"]["open"] == (EdgePolicy(to_status="closed"),)
    assert as_dict["edges"] is not policy.edges


def test_registered_policy_pickle_round_trips() -> None:
    registry = PolicyRegistry()
    registry.register(_policy(edges={"open": (EdgePolicy(to_status="closed"),)}))
    policy = registry.get("widget")
    restored = pickle.loads(pickle.dumps(policy))
    assert restored == policy
    assert isinstance(restored.edges, ImmutableEdgeMap)
    with pytest.raises(TypeError):
        restored.edges["open"] = ()
