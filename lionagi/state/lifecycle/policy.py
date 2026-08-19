# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Policy registry: one complete LifecyclePolicy per managed entity type,
declaring its status vocabulary, declared-edge graph, and patch-field
allowlist. Registration self-validates (fails fast at import time on a
malformed policy) rather than deferring integrity checks to first use.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from lionagi.ln.types import Params, Registry, RegistryEntry, RegistryFragment

from .models import EdgePolicy, LifecyclePolicy, SameStatusRule

_LIFECYCLE_POLICY_CATALOG_NAME = "lifecycle_policies"
_LIFECYCLE_POLICY_CATALOG_VERSION = "1"
_LIFECYCLE_POLICY_FRAGMENT_OWNER = "lionagi.state.lifecycle.policy"
_LIFECYCLE_POLICY_FRAGMENT_VERSION = "1"


class ImmutableEdgeMap(Mapping):
    """An immutable mapping of from-status -> declared edges. Deliberately
    not a ``dict`` subclass — see docs/internals/runtime.md for why a dict
    subclass can't actually guarantee immutability here."""

    __slots__ = ("_edges",)

    def __init__(self, edges) -> None:
        if hasattr(self, "_edges"):
            raise TypeError(
                f"{type(self).__name__} is immutable; registered lifecycle "
                "policies cannot have their edge map reinitialized in place"
            )
        # backing store must itself be read-only, or the private slot is a mutation path
        object.__setattr__(self, "_edges", MappingProxyType(dict(edges)))

    def __setattr__(self, name, value) -> None:
        raise TypeError(
            f"{type(self).__name__} is immutable; registered lifecycle "
            "policies cannot have their edge map mutated in place"
        )

    def __getitem__(self, key):
        return self._edges[key]

    def __iter__(self):
        return iter(self._edges)

    def __len__(self) -> int:
        return len(self._edges)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._edges!r})"

    def __reduce__(self):
        return (type(self), (dict(self._edges),))


@dataclass(frozen=True, slots=True, init=False, eq=False)
class _LifecyclePolicyDeclaration(Params):
    """Structurally immutable lifecycle policy declaration."""

    entity_type: str
    table: str
    statuses: frozenset[str]
    initial_statuses: frozenset[str]
    terminal_statuses: frozenset[str]
    edge_items: tuple[tuple[str, tuple[EdgePolicy, ...]], ...]
    same_status: SameStatusRule
    patch_fields: frozenset[str]
    reason_prefixes: frozenset[str]
    reason_columns: bool = True


class _LifecyclePolicyCatalog(Registry[_LifecyclePolicyDeclaration]):
    """Private declaration authority; PolicyRegistry remains the public facade."""


def _declaration_from_policy(
    policy: LifecyclePolicy,
    *,
    edge_items: tuple[tuple[str, tuple[EdgePolicy, ...]], ...],
) -> _LifecyclePolicyDeclaration:
    return _LifecyclePolicyDeclaration(
        entity_type=policy.entity_type,
        table=policy.table,
        statuses=policy.statuses,
        initial_statuses=policy.initial_statuses,
        terminal_statuses=policy.terminal_statuses,
        edge_items=edge_items,
        same_status=policy.same_status,
        patch_fields=policy.patch_fields,
        reason_prefixes=policy.reason_prefixes,
        reason_columns=policy.reason_columns,
    )


def _projection_from_declaration(
    declaration: _LifecyclePolicyDeclaration,
) -> LifecyclePolicy:
    return LifecyclePolicy(
        entity_type=declaration.entity_type,
        table=declaration.table,
        statuses=declaration.statuses,
        initial_statuses=declaration.initial_statuses,
        terminal_statuses=declaration.terminal_statuses,
        edges=ImmutableEdgeMap(declaration.edge_items),
        same_status=declaration.same_status,
        patch_fields=declaration.patch_fields,
        reason_prefixes=declaration.reason_prefixes,
        reason_columns=declaration.reason_columns,
    )


class PolicyRegistry:
    """Maps entity_type -> frozen LifecyclePolicy, validated at registration.
    Edge maps are wrapped in ``ImmutableEdgeMap`` so a caller holding a
    policy from ``get()`` cannot mutate global transition behavior.
    ``DEFAULT_REGISTRY`` seals itself once its built-ins are registered; a
    locally constructed ``PolicyRegistry()`` stays open until its caller
    calls ``seal()``."""

    def __init__(self) -> None:
        self._staged: tuple[
            tuple[RegistryEntry[_LifecyclePolicyDeclaration], LifecyclePolicy], ...
        ] = ()
        self._catalog: _LifecyclePolicyCatalog | None = None
        self._projections: tuple[LifecyclePolicy, ...] = ()
        self._sealed = False

    def register(self, policy: LifecyclePolicy) -> None:
        if self._sealed:
            raise RuntimeError(
                "lifecycle policy registration: registry is sealed; cannot register "
                f"entity_type {policy.entity_type!r}"
            )
        if any(entry.key == policy.entity_type for entry, _ in self._staged):
            raise ValueError(
                f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                "is already registered"
            )
        incumbent = next(
            (
                declaration
                for entry, _ in self._staged
                if (declaration := entry.value).table == policy.table
            ),
            None,
        )
        if incumbent is not None:
            raise ValueError(
                f"lifecycle policy registration: table {policy.table!r} is already "
                f"registered (for entity_type {incumbent.entity_type!r})"
            )
        unknown_initial = policy.initial_statuses - policy.statuses
        if unknown_initial:
            raise ValueError(
                f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                f"declares initial_statuses outside statuses: {sorted(unknown_initial)}"
            )
        unknown_terminal = policy.terminal_statuses - policy.statuses
        if unknown_terminal:
            raise ValueError(
                f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                f"declares terminal_statuses outside statuses: {sorted(unknown_terminal)}"
            )
        edge_items: list[tuple[str, tuple[EdgePolicy, ...]]] = []
        for from_status, raw_edges in policy.edges.items():
            if from_status not in policy.statuses:
                raise ValueError(
                    f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                    f"declares edges from unknown status {from_status!r}"
                )
            snapshot_edges: list[EdgePolicy] = []
            for edge in raw_edges:
                if edge.to_status not in policy.statuses:
                    raise ValueError(
                        f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                        f"edge {from_status!r} -> {edge.to_status!r} targets an unknown status"
                    )
                unknown_patch = edge.required_patch_fields - policy.patch_fields
                if unknown_patch:
                    raise ValueError(
                        f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                        f"edge {from_status!r} -> {edge.to_status!r} requires patch field(s) "
                        f"{sorted(unknown_patch)} outside the policy's patch_fields allowlist"
                    )
                unknown_guard = edge.required_guard_fields - policy.patch_fields
                if unknown_guard:
                    raise ValueError(
                        f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                        f"edge {from_status!r} -> {edge.to_status!r} requires guard field(s) "
                        f"{sorted(unknown_guard)} outside the policy's patch_fields allowlist"
                    )
                snapshot_edges.append(edge)
            edge_items.append((from_status, tuple(snapshot_edges)))
        declaration = _declaration_from_policy(policy, edge_items=tuple(edge_items))
        entry = RegistryEntry(key=declaration.entity_type, value=declaration)
        projection = _projection_from_declaration(declaration)
        self._staged += ((entry, projection),)

    def seal(self) -> None:
        """Close this registry to further registration."""
        if self._sealed:
            return

        staged = self._staged
        fragment = RegistryFragment(
            owner=_LIFECYCLE_POLICY_FRAGMENT_OWNER,
            version=_LIFECYCLE_POLICY_FRAGMENT_VERSION,
            items=tuple(entry for entry, _ in staged),
        )
        catalog = _LifecyclePolicyCatalog.compose(
            fragment,
            name=_LIFECYCLE_POLICY_CATALOG_NAME,
            version=_LIFECYCLE_POLICY_CATALOG_VERSION,
        )
        projections = tuple(projection for _, projection in staged)
        self._catalog, self._projections, self._staged, self._sealed = (
            catalog,
            projections,
            (),
            True,
        )

    def get(self, entity_type: str) -> LifecyclePolicy:
        if self._catalog is not None:
            for index, record in enumerate(self._catalog.items):
                if record.entry.key == entity_type:
                    return self._projections[index]
        else:
            for entry, projection in self._staged:
                if entry.key == entity_type:
                    return projection
        raise ValueError(
            f"lifecycle policy: unknown entity_type {entity_type!r}; registered "
            f"types are {sorted(self.entity_types())}"
        ) from None

    def __contains__(self, entity_type: str) -> bool:
        if self._catalog is not None:
            return entity_type in self._catalog
        return any(entry.key == entity_type for entry, _ in self._staged)

    def entity_types(self) -> frozenset[str]:
        if self._catalog is not None:
            return frozenset(self._catalog.keys())
        return frozenset(entry.key for entry, _ in self._staged)


def _edges(*pairs: tuple[str, tuple[EdgePolicy, ...]]) -> dict[str, tuple[EdgePolicy, ...]]:
    return dict(pairs)


def _to(*statuses: str) -> tuple[EdgePolicy, ...]:
    return tuple(EdgePolicy(to_status=s) for s in statuses)


def build_default_registry() -> PolicyRegistry:
    registry = PolicyRegistry()

    # session/invocation: same execution vocabulary/graph; no exit from terminal without override
    session_statuses = frozenset(
        {"running", "completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
    )
    session_terminal = frozenset(
        {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
    )
    session_edges = _edges(("running", _to(*sorted(session_terminal))))
    session_patch_fields = frozenset(
        {
            "ended_at",
            # Describes the ended_at beside it and is written by the same
            # transitions, so it has to be declared here or those writes are
            # only legal because they are applied after this list is checked.
            "ended_at_is_approximate",
            "input_tokens",
            "output_tokens",
            "total_cost_usd",
            "num_turns",
            "duration_ms",
            # liveness markers a sweep reads; must move atomically with status
            "node_metadata",
        }
    )
    registry.register(
        LifecyclePolicy(
            entity_type="session",
            table="sessions",
            statuses=session_statuses,
            initial_statuses=frozenset({"running"}),
            terminal_statuses=session_terminal,
            edges=session_edges,
            same_status="append",
            patch_fields=session_patch_fields,
            reason_prefixes=frozenset({"run", "session"}),
        )
    )
    registry.register(
        LifecyclePolicy(
            entity_type="invocation",
            table="invocations",
            statuses=session_statuses,
            initial_statuses=frozenset({"running"}),
            terminal_statuses=session_terminal,
            edges=session_edges,
            same_status="append",
            patch_fields=frozenset({"ended_at"}),
            reason_prefixes=frozenset({"run"}),
        )
    )

    # show: any nonterminal status may move to any other declared status; completed/aborted require override to exit
    show_statuses = frozenset({"active", "completed", "aborted", "imported"})
    show_terminal = frozenset({"completed", "aborted"})
    show_nonterminal = show_statuses - show_terminal
    show_edges = _edges(
        *((src, _to(*sorted(show_statuses - {src}))) for src in sorted(show_nonterminal))
    )
    registry.register(
        LifecyclePolicy(
            entity_type="show",
            table="shows",
            statuses=show_statuses,
            initial_statuses=frozenset({"active", "imported"}),
            terminal_statuses=show_terminal,
            edges=show_edges,
            same_status="append",
            patch_fields=frozenset({"status_source"}),
            reason_prefixes=frozenset({"show"}),
        )
    )

    # play: same compatibility-graph shape as show; terminal statuses require override to exit
    play_statuses = frozenset(
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
    play_terminal = frozenset(
        {"merged", "escalated", "gate_failed", "blocked", "aborted_after_finish"}
    )
    play_nonterminal = play_statuses - play_terminal
    play_edges = _edges(
        *((src, _to(*sorted(play_statuses - {src}))) for src in sorted(play_nonterminal))
    )
    registry.register(
        LifecyclePolicy(
            entity_type="play",
            table="plays",
            statuses=play_statuses,
            initial_statuses=frozenset({"pending"}),
            terminal_statuses=play_terminal,
            edges=play_edges,
            same_status="append",
            patch_fields=frozenset(
                {"ended_at", "exit_code", "merge_sha", "merged_at", "gate_passed", "gate_feedback"}
            ),
            reason_prefixes=frozenset({"play"}),
        )
    )

    registry.register(
        LifecyclePolicy(
            entity_type="team",
            table="teams",
            statuses=frozenset({"active", "archived"}),
            initial_statuses=frozenset({"active"}),
            terminal_statuses=frozenset({"archived"}),
            edges=_edges(("active", _to("archived"))),
            same_status="append",
            patch_fields=frozenset(),
            reason_prefixes=frozenset({"team"}),
        )
    )

    # schedule_run: timed_out joins the terminal set here, closing a gap the
    # legacy update_status() terminal set omitted.
    schedule_run_statuses = frozenset(
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
    schedule_run_terminal = frozenset({"completed", "failed", "timed_out", "skipped", "cancelled"})
    schedule_run_edges = _edges(
        ("queued", _to("waiting_dependency", "running", "skipped", "cancelled")),
        ("waiting_dependency", _to("queued", "cancelled")),
        ("running", _to("completed", "failed", "timed_out", "retry_wait", "queued", "cancelled")),
        ("retry_wait", _to("queued", "cancelled")),
    )
    registry.register(
        LifecyclePolicy(
            entity_type="schedule_run",
            table="schedule_runs",
            statuses=schedule_run_statuses,
            initial_statuses=frozenset({"queued", "running", "failed", "skipped"}),
            terminal_statuses=schedule_run_terminal,
            edges=schedule_run_edges,
            same_status="append",
            patch_fields=frozenset(
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
            reason_prefixes=frozenset({"run", "schedule"}),
        )
    )

    # dispatch: dead_letter/expired are terminal but operator-recoverable via
    # a declared edge (not a generic override) back to pending. delivering ->
    # delivering is the same-status crash-recovery claim, guarded by
    # required_guard_fields so two racing workers can't both win it.
    dispatch_statuses = frozenset(
        {"pending", "delivering", "delivered", "acked", "dead_letter", "expired"}
    )
    dispatch_terminal = frozenset({"delivered", "acked", "dead_letter", "expired"})
    dispatch_edges = _edges(
        ("pending", _to("delivering", "expired", "acked")),
        (
            "delivering",
            (
                EdgePolicy(to_status="delivering", required_guard_fields=frozenset({"attempt"})),
                EdgePolicy(to_status="pending"),
                EdgePolicy(to_status="delivered"),
                # a fast ack_token can arrive mid-tick; needn't wait for pending
                EdgePolicy(to_status="acked"),
                EdgePolicy(to_status="dead_letter"),
                EdgePolicy(to_status="expired"),
            ),
        ),
        (
            "dead_letter",
            (
                EdgePolicy(
                    to_status="pending",
                    actor_types=frozenset({"operator"}),
                    required_patch_fields=frozenset({"attempt", "next_attempt_at", "last_error"}),
                ),
            ),
        ),
        (
            "expired",
            (
                EdgePolicy(
                    to_status="pending",
                    actor_types=frozenset({"operator"}),
                    required_patch_fields=frozenset({"attempt", "next_attempt_at", "last_error"}),
                ),
            ),
        ),
    )
    registry.register(
        LifecyclePolicy(
            entity_type="dispatch",
            table="dispatch_outbox",
            statuses=dispatch_statuses,
            initial_statuses=frozenset({"pending"}),
            terminal_statuses=dispatch_terminal,
            edges=dispatch_edges,
            same_status="append",
            patch_fields=frozenset({"attempt", "next_attempt_at", "last_error"}),
            reason_prefixes=frozenset({"dispatch"}),
            reason_columns=False,
        )
    )

    registry.seal()
    return registry


DEFAULT_REGISTRY = build_default_registry()
