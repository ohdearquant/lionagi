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
    assert registry.get("widget") is policy


def test_contains() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    assert "widget" in registry
    assert "bogus" not in registry
