# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility goldens for the fixed seven-policy lifecycle registry.

These tests intentionally know the whole legacy declaration.  The catalog
migration may replace the backing authority, but it must not change a single
public policy value, validation diagnostic, or copy/serialization behavior.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import pickle
import subprocess
import sys
from typing import Any, cast

import pytest

import lionagi.state.lifecycle as lifecycle
import lionagi.state.lifecycle.policy as policy_module
from lionagi.state.lifecycle import EdgePolicy, LifecyclePolicy
from lionagi.state.lifecycle.policy import (
    DEFAULT_REGISTRY,
    ImmutableEdgeMap,
    PolicyRegistry,
    build_default_registry,
)

_BUILTIN_ORDER = (
    "session",
    "invocation",
    "show",
    "play",
    "team",
    "schedule_run",
    "dispatch",
)


def _edge(
    to_status: str,
    *,
    actor_types: tuple[str, ...] | None = None,
    required_patch_fields: tuple[str, ...] = (),
    required_guard_fields: tuple[str, ...] = (),
) -> tuple[str, frozenset[str] | None, frozenset[str], frozenset[str]]:
    return (
        to_status,
        None if actor_types is None else frozenset(actor_types),
        frozenset(required_patch_fields),
        frozenset(required_guard_fields),
    )


_SESSION_STATUSES = frozenset(
    {
        "running",
        "completed",
        "completed_empty",
        "failed",
        "timed_out",
        "aborted",
        "cancelled",
    }
)
_SESSION_TERMINAL = frozenset(
    {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
)
_SHOW_STATUSES = frozenset({"active", "completed", "aborted", "imported"})
_SHOW_TERMINAL = frozenset({"completed", "aborted"})
_PLAY_STATUSES = frozenset(
    {
        "pending",
        "prepared",
        "running",
        "running_complete",
        "gated",
        "gate_failed",
        "redoing",
        "merged",
        "escalated",
        "blocked",
        "aborted_after_finish",
    }
)
_PLAY_TERMINAL = frozenset(
    {"merged", "escalated", "gate_failed", "blocked", "aborted_after_finish"}
)
_SCHEDULE_RUN_STATUSES = frozenset(
    {
        "queued",
        "waiting_dependency",
        "running",
        "retry_wait",
        "completed",
        "failed",
        "timed_out",
        "skipped",
        "cancelled",
    }
)
_SCHEDULE_RUN_TERMINAL = frozenset({"completed", "failed", "timed_out", "skipped", "cancelled"})
_DISPATCH_STATUSES = frozenset(
    {"pending", "delivering", "delivered", "acked", "dead_letter", "expired"}
)
_DISPATCH_TERMINAL = frozenset({"delivered", "acked", "dead_letter", "expired"})


def _complete_targets(statuses: frozenset[str], source: str):
    """Expand only pinned golden data, never values read from production."""
    return tuple(_edge(target) for target in sorted(statuses - {source}))


_LEGACY_SEVEN_GOLDEN = (
    (
        "session",
        {
            "entity_type": "session",
            "table": "sessions",
            "statuses": _SESSION_STATUSES,
            "initial_statuses": frozenset({"running"}),
            "terminal_statuses": _SESSION_TERMINAL,
            "edges": (("running", tuple(_edge(value) for value in sorted(_SESSION_TERMINAL))),),
            "same_status": "append",
            "patch_fields": frozenset(
                {
                    "ended_at",
                    "ended_at_is_approximate",
                    "input_tokens",
                    "output_tokens",
                    "total_cost_usd",
                    "num_turns",
                    "duration_ms",
                    "node_metadata",
                }
            ),
            "reason_prefixes": frozenset({"run", "session"}),
            "reason_columns": True,
        },
    ),
    (
        "invocation",
        {
            "entity_type": "invocation",
            "table": "invocations",
            "statuses": _SESSION_STATUSES,
            "initial_statuses": frozenset({"running"}),
            "terminal_statuses": _SESSION_TERMINAL,
            "edges": (("running", tuple(_edge(value) for value in sorted(_SESSION_TERMINAL))),),
            "same_status": "append",
            "patch_fields": frozenset({"ended_at"}),
            "reason_prefixes": frozenset({"run"}),
            "reason_columns": True,
        },
    ),
    (
        "show",
        {
            "entity_type": "show",
            "table": "shows",
            "statuses": _SHOW_STATUSES,
            "initial_statuses": frozenset({"active", "imported"}),
            "terminal_statuses": _SHOW_TERMINAL,
            "edges": tuple(
                (source, _complete_targets(_SHOW_STATUSES, source))
                for source in ("active", "imported")
            ),
            "same_status": "append",
            "patch_fields": frozenset({"status_source"}),
            "reason_prefixes": frozenset({"show"}),
            "reason_columns": True,
        },
    ),
    (
        "play",
        {
            "entity_type": "play",
            "table": "plays",
            "statuses": _PLAY_STATUSES,
            "initial_statuses": frozenset({"pending"}),
            "terminal_statuses": _PLAY_TERMINAL,
            "edges": tuple(
                (source, _complete_targets(_PLAY_STATUSES, source))
                for source in (
                    "gated",
                    "pending",
                    "prepared",
                    "redoing",
                    "running",
                    "running_complete",
                )
            ),
            "same_status": "append",
            "patch_fields": frozenset(
                {
                    "ended_at",
                    "exit_code",
                    "merge_sha",
                    "merged_at",
                    "gate_passed",
                    "gate_feedback",
                }
            ),
            "reason_prefixes": frozenset({"play"}),
            "reason_columns": True,
        },
    ),
    (
        "team",
        {
            "entity_type": "team",
            "table": "teams",
            "statuses": frozenset({"active", "archived"}),
            "initial_statuses": frozenset({"active"}),
            "terminal_statuses": frozenset({"archived"}),
            "edges": (("active", (_edge("archived"),)),),
            "same_status": "append",
            "patch_fields": frozenset(),
            "reason_prefixes": frozenset({"team"}),
            "reason_columns": True,
        },
    ),
    (
        "schedule_run",
        {
            "entity_type": "schedule_run",
            "table": "schedule_runs",
            "statuses": _SCHEDULE_RUN_STATUSES,
            "initial_statuses": frozenset({"queued", "running", "failed", "skipped"}),
            "terminal_statuses": _SCHEDULE_RUN_TERMINAL,
            "edges": (
                (
                    "queued",
                    tuple(
                        _edge(value)
                        for value in ("waiting_dependency", "running", "skipped", "cancelled")
                    ),
                ),
                ("waiting_dependency", (_edge("queued"), _edge("cancelled"))),
                (
                    "running",
                    tuple(
                        _edge(value)
                        for value in (
                            "completed",
                            "failed",
                            "timed_out",
                            "retry_wait",
                            "queued",
                            "cancelled",
                        )
                    ),
                ),
                ("retry_wait", (_edge("queued"), _edge("cancelled"))),
            ),
            "same_status": "append",
            "patch_fields": frozenset(
                {
                    "ended_at",
                    "exit_code",
                    "error_detail",
                    "invocation_id",
                    "queued_at",
                    "leased_by",
                    "lease_expires_at",
                    "lease_attempts",
                }
            ),
            "reason_prefixes": frozenset({"run", "schedule"}),
            "reason_columns": True,
        },
    ),
    (
        "dispatch",
        {
            "entity_type": "dispatch",
            "table": "dispatch_outbox",
            "statuses": _DISPATCH_STATUSES,
            "initial_statuses": frozenset({"pending"}),
            "terminal_statuses": _DISPATCH_TERMINAL,
            "edges": (
                (
                    "pending",
                    (_edge("delivering"), _edge("expired"), _edge("acked")),
                ),
                (
                    "delivering",
                    (
                        _edge("delivering", required_guard_fields=("attempt",)),
                        _edge("pending"),
                        _edge("delivered"),
                        _edge("acked"),
                        _edge("dead_letter"),
                        _edge("expired"),
                    ),
                ),
                (
                    "dead_letter",
                    (
                        _edge(
                            "pending",
                            actor_types=("operator",),
                            required_patch_fields=(
                                "attempt",
                                "next_attempt_at",
                                "last_error",
                            ),
                        ),
                    ),
                ),
                (
                    "expired",
                    (
                        _edge(
                            "pending",
                            actor_types=("operator",),
                            required_patch_fields=(
                                "attempt",
                                "next_attempt_at",
                                "last_error",
                            ),
                        ),
                    ),
                ),
            ),
            "same_status": "append",
            "patch_fields": frozenset({"attempt", "next_attempt_at", "last_error"}),
            "reason_prefixes": frozenset({"dispatch"}),
            "reason_columns": False,
        },
    ),
)


def _policy(**overrides) -> LifecyclePolicy:
    values = {
        "entity_type": "widget",
        "table": "widgets",
        "statuses": frozenset({"open", "closed"}),
        "initial_statuses": frozenset({"open"}),
        "terminal_statuses": frozenset({"closed"}),
        "edges": {},
        "same_status": "append",
        "patch_fields": frozenset(),
        "reason_prefixes": frozenset(),
    }
    values.update(overrides)
    return LifecyclePolicy(**values)


def _project(policy: LifecyclePolicy) -> dict[str, object]:
    return {
        "entity_type": policy.entity_type,
        "table": policy.table,
        "statuses": policy.statuses,
        "initial_statuses": policy.initial_statuses,
        "terminal_statuses": policy.terminal_statuses,
        "edges": tuple(
            (
                source,
                tuple(
                    (
                        edge.to_status,
                        edge.actor_types,
                        edge.required_patch_fields,
                        edge.required_guard_fields,
                    )
                    for edge in edges
                ),
            )
            for source, edges in policy.edges.items()
        ),
        "same_status": policy.same_status,
        "patch_fields": policy.patch_fields,
        "reason_prefixes": policy.reason_prefixes,
        "reason_columns": policy.reason_columns,
    }


def _registered_order(registry: PolicyRegistry) -> tuple[str, ...]:
    legacy = getattr(registry, "_by_entity_type", None)
    if isinstance(legacy, dict):
        return tuple(legacy)

    catalog_type = getattr(policy_module, "_LifecyclePolicyCatalog", None)
    if catalog_type is not None:
        direct_values = tuple(getattr(registry, "__dict__", {}).values())
        for value in direct_values:
            if isinstance(value, catalog_type):
                return value.keys()
    raise AssertionError("PolicyRegistry exposes neither legacy order nor one catalog authority")


def test_legacy_seven_policy_golden_is_exhaustive_and_ordered() -> None:
    """Removal, reordering, or any field/edge mutation must break this gate."""
    assert _registered_order(DEFAULT_REGISTRY) == _BUILTIN_ORDER
    assert (
        tuple(
            (entity_type, _project(DEFAULT_REGISTRY.get(entity_type)))
            for entity_type in _BUILTIN_ORDER
        )
        == _LEGACY_SEVEN_GOLDEN
    )


def test_policy_registry_public_imports_and_signatures_are_unchanged() -> None:
    assert lifecycle.PolicyRegistry is PolicyRegistry
    assert lifecycle.DEFAULT_REGISTRY is DEFAULT_REGISTRY
    assert lifecycle.build_default_registry is build_default_registry
    assert str(inspect.signature(PolicyRegistry)) == "() -> 'None'"
    assert str(inspect.signature(PolicyRegistry.register)) == (
        "(self, policy: 'LifecyclePolicy') -> 'None'"
    )
    assert str(inspect.signature(PolicyRegistry.seal)) == "(self) -> 'None'"
    assert str(inspect.signature(PolicyRegistry.get)) == (
        "(self, entity_type: 'str') -> 'LifecyclePolicy'"
    )
    assert str(inspect.signature(PolicyRegistry.__contains__)) == (
        "(self, entity_type: 'str') -> 'bool'"
    )
    assert str(inspect.signature(PolicyRegistry.entity_types)) == "(self) -> 'frozenset[str]'"
    assert str(inspect.signature(build_default_registry)) == "() -> 'PolicyRegistry'"


def test_lifecycle_consumers_keep_the_captured_default_facade_and_injection() -> None:
    from lionagi.state.lifecycle import deliveries, service

    service_default = (
        inspect.signature(service.SQLAlchemyLifecycleService.__init__)
        .parameters["registry"]
        .default
    )
    delivery_default = (
        inspect.signature(deliveries.reconcile_unacknowledged).parameters["registry"].default
    )
    assert service_default is service.DEFAULT_REGISTRY is DEFAULT_REGISTRY
    assert delivery_default is deliveries.DEFAULT_REGISTRY is DEFAULT_REGISTRY

    injected = PolicyRegistry()
    lifecycle_service = service.SQLAlchemyLifecycleService(cast(Any, object()), registry=injected)
    assert lifecycle_service._registry is injected


def test_sealed_registration_wins_before_all_other_validation() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    registry.seal()
    invalid_duplicate = _policy(
        table="other_widgets",
        initial_statuses=frozenset({"missing"}),
    )
    with pytest.raises(RuntimeError) as caught:
        registry.register(invalid_duplicate)
    assert str(caught.value) == (
        "lifecycle policy registration: registry is sealed; cannot register entity_type 'widget'"
    )


def test_duplicate_entity_precedes_table_and_policy_validation() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    duplicate = _policy(
        table="other_widgets",
        initial_statuses=frozenset({"missing"}),
    )
    with pytest.raises(ValueError) as caught:
        registry.register(duplicate)
    assert str(caught.value) == (
        "lifecycle policy registration: entity_type 'widget' is already registered"
    )


def test_duplicate_table_precedes_policy_validation() -> None:
    registry = PolicyRegistry()
    registry.register(_policy())
    duplicate = _policy(
        entity_type="other_widget",
        initial_statuses=frozenset({"missing"}),
    )
    with pytest.raises(ValueError) as caught:
        registry.register(duplicate)
    assert str(caught.value) == (
        "lifecycle policy registration: table 'widgets' is already registered "
        "(for entity_type 'widget')"
    )


@pytest.mark.parametrize(
    ("candidate", "expected_message"),
    (
        (
            _policy(
                initial_statuses=frozenset({"missing_initial"}),
                terminal_statuses=frozenset({"missing_terminal"}),
            ),
            "lifecycle policy registration: entity_type 'widget' declares "
            "initial_statuses outside statuses: ['missing_initial']",
        ),
        (
            _policy(
                terminal_statuses=frozenset({"missing_terminal"}),
                edges={"missing_source": (EdgePolicy(to_status="missing_target"),)},
            ),
            "lifecycle policy registration: entity_type 'widget' declares "
            "terminal_statuses outside statuses: ['missing_terminal']",
        ),
        (
            _policy(
                edges={
                    "missing_source": (EdgePolicy(to_status="missing_target"),),
                    "open": (EdgePolicy(to_status="missing_target"),),
                }
            ),
            "lifecycle policy registration: entity_type 'widget' declares edges "
            "from unknown status 'missing_source'",
        ),
        (
            _policy(
                edges={
                    "open": (
                        EdgePolicy(
                            to_status="missing_target",
                            required_patch_fields=frozenset({"missing_patch"}),
                            required_guard_fields=frozenset({"missing_guard"}),
                        ),
                    )
                }
            ),
            "lifecycle policy registration: entity_type 'widget' edge 'open' -> "
            "'missing_target' targets an unknown status",
        ),
        (
            _policy(
                edges={
                    "open": (
                        EdgePolicy(
                            to_status="closed",
                            required_patch_fields=frozenset({"z_patch", "a_patch"}),
                            required_guard_fields=frozenset({"missing_guard"}),
                        ),
                    )
                }
            ),
            "lifecycle policy registration: entity_type 'widget' edge 'open' -> "
            "'closed' requires patch field(s) ['a_patch', 'z_patch'] outside the "
            "policy's patch_fields allowlist",
        ),
        (
            _policy(
                edges={
                    "open": (
                        EdgePolicy(
                            to_status="closed",
                            required_guard_fields=frozenset({"z_guard", "a_guard"}),
                        ),
                    )
                }
            ),
            "lifecycle policy registration: entity_type 'widget' edge 'open' -> "
            "'closed' requires guard field(s) ['a_guard', 'z_guard'] outside the "
            "policy's patch_fields allowlist",
        ),
    ),
)
def test_policy_validation_order_and_full_messages(
    candidate: LifecyclePolicy,
    expected_message: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        PolicyRegistry().register(candidate)
    assert str(caught.value) == expected_message


def test_missing_lookup_preserves_full_sorted_diagnostic() -> None:
    registry = PolicyRegistry()
    registry.register(_policy(entity_type="zeta", table="zeta_table"))
    registry.register(_policy(entity_type="alpha", table="alpha_table"))
    with pytest.raises(ValueError) as caught:
        registry.get("missing")
    assert str(caught.value) == (
        "lifecycle policy: unknown entity_type 'missing'; registered types are ['alpha', 'zeta']"
    )


def test_sealed_missing_lookup_preserves_full_sorted_diagnostic() -> None:
    with pytest.raises(ValueError) as caught:
        DEFAULT_REGISTRY.get("missing")
    assert str(caught.value) == (
        "lifecycle policy: unknown entity_type 'missing'; registered types are "
        "['dispatch', 'invocation', 'play', 'schedule_run', 'session', 'show', 'team']"
    )


def test_preseal_lookup_contains_and_idempotent_seal_are_preserved() -> None:
    registry = PolicyRegistry()
    policy = _policy()
    registry.register(policy)
    first_projection = registry.get("widget")
    assert first_projection is registry.get("widget")
    assert isinstance(first_projection.edges, ImmutableEdgeMap)
    assert "widget" in registry
    assert "missing" not in registry
    assert registry.entity_types() == frozenset({"widget"})
    assert registry.seal() is None
    assert registry.seal() is None
    assert registry.get("widget") is first_projection


def test_default_builds_and_local_builders_are_isolated() -> None:
    first = build_default_registry()
    second = build_default_registry()
    assert first is not second
    assert first.entity_types() == second.entity_types() == frozenset(_BUILTIN_ORDER)
    for entity_type in _BUILTIN_ORDER:
        assert first.get(entity_type) == second.get(entity_type)
        assert first.get(entity_type) is not second.get(entity_type)

    local_one = PolicyRegistry()
    local_two = PolicyRegistry()
    local_one.register(_policy())
    assert "widget" in local_one
    assert "widget" not in local_two
    assert "widget" not in DEFAULT_REGISTRY
    assert local_two.entity_types() == frozenset()


def test_returned_policy_copy_deepcopy_pickle_and_identity_are_preserved() -> None:
    policy = build_default_registry().get("dispatch")
    assert policy is not copy.copy(policy)
    assert copy.copy(policy) == policy
    assert copy.copy(policy).edges is policy.edges

    deep = copy.deepcopy(policy)
    restored = pickle.loads(pickle.dumps(policy))
    for candidate in (deep, restored):
        assert candidate == policy
        assert candidate is not policy
        assert candidate.edges is not policy.edges
        assert isinstance(candidate.edges, ImmutableEdgeMap)
        with pytest.raises(TypeError):
            cast(Any, candidate.edges)["pending"] = ()

    assert dataclasses.replace(policy, entity_type="dispatch_copy").edges == policy.edges
    as_dict = dataclasses.asdict(policy)
    assert as_dict["entity_type"] == "dispatch"
    assert isinstance(as_dict["edges"], ImmutableEdgeMap)
    assert as_dict["edges"] is not policy.edges


def test_sealed_registry_copy_deepcopy_and_pickle_are_preserved() -> None:
    registry = build_default_registry()
    dispatch = registry.get("dispatch")

    shallow = copy.copy(registry)
    assert shallow is not registry
    assert shallow.entity_types() == registry.entity_types()
    assert shallow.get("dispatch") is dispatch

    deep = copy.deepcopy(registry)
    restored = pickle.loads(pickle.dumps(registry))
    for candidate in (deep, restored):
        candidate_dispatch = candidate.get("dispatch")
        assert candidate is not registry
        assert candidate.entity_types() == registry.entity_types()
        assert candidate_dispatch == dispatch
        assert candidate_dispatch is not dispatch
        assert isinstance(candidate_dispatch.edges, ImmutableEdgeMap)
        with pytest.raises(TypeError):
            cast(Any, candidate_dispatch.edges)["pending"] = ()
        with pytest.raises(RuntimeError) as caught:
            candidate.register(_policy(entity_type="late", table="late_table"))
        assert str(caught.value) == (
            "lifecycle policy registration: registry is sealed; cannot register entity_type 'late'"
        )


def test_module_reload_rebuilds_exactly_seven_entries_without_duplication() -> None:
    code = """
import importlib
import lionagi.state.lifecycle.policy as policy

expected = frozenset(
    {"session", "invocation", "show", "play", "team", "schedule_run", "dispatch"}
)
for _ in range(3):
    policy = importlib.reload(policy)
    registry = policy.DEFAULT_REGISTRY
    assert registry.entity_types() == expected
    assert all(registry.get(name) is registry.get(name) for name in expected)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
