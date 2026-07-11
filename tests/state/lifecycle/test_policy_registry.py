# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0058 Phase 1 gate: PolicyRegistry self-validation and the 7 built-in
policies' structural integrity."""

from __future__ import annotations

import pytest

from lionagi.state.lifecycle import DEFAULT_REGISTRY, EdgePolicy, LifecyclePolicy
from lionagi.state.lifecycle.policy import PolicyRegistry


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
