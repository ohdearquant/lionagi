# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Policy registry: one complete LifecyclePolicy per managed entity type,
declaring its status vocabulary, declared-edge graph, and patch-field
allowlist. Registration self-validates (fails fast at import time on a
malformed policy) rather than deferring integrity checks to first use.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from .models import EdgePolicy, LifecyclePolicy


class ImmutableEdgeMap(Mapping):
    """An immutable mapping of from-status -> declared edges.

    Deliberately NOT a ``dict`` subclass: dict's C-level mutators reach the
    underlying storage without going through Python-level overrides
    (``dict.__setitem__(m, ...)``, inherited ``__ior__``, re-invoking
    ``__init__``), so a subclass can never actually guarantee immutability.
    Wrapping a private dict behind the ``Mapping`` interface leaves no
    inherited mutation surface at all: there is no ``__setitem__``,
    ``update``, or ``__ior__`` to reach, and re-invoking ``__init__`` is
    refused. ``pickle``/``copy.deepcopy`` round-trip via ``__reduce__``
    (reconstructing through the constructor), and ``dataclasses.asdict()``
    deep-copies the map rather than raising.
    """

    __slots__ = ("_edges",)

    def __init__(self, edges) -> None:
        if hasattr(self, "_edges"):
            raise TypeError(
                f"{type(self).__name__} is immutable; registered lifecycle "
                "policies cannot have their edge map reinitialized in place"
            )
        object.__setattr__(self, "_edges", dict(edges))

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


class PolicyRegistry:
    """Maps entity_type -> frozen LifecyclePolicy, validated at registration.

    Registered policies are stored with their edge maps wrapped in an
    immutable mapping (``ImmutableEdgeMap``) so a caller holding a
    policy returned by ``get()`` cannot mutate global transition behavior
    for the process. ``seal()`` additionally closes the registry to further
    registration; ``DEFAULT_REGISTRY`` seals itself once its built-in
    policies are registered, while a locally constructed ``PolicyRegistry()``
    stays open until its own caller seals it.
    """

    def __init__(self) -> None:
        self._by_entity_type: dict[str, LifecyclePolicy] = {}
        self._by_table: dict[str, str] = {}  # table -> entity_type
        self._sealed = False

    def register(self, policy: LifecyclePolicy) -> None:
        if self._sealed:
            raise RuntimeError(
                "lifecycle policy registration: registry is sealed; cannot register "
                f"entity_type {policy.entity_type!r}"
            )
        if policy.entity_type in self._by_entity_type:
            raise ValueError(
                f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                "is already registered"
            )
        if policy.table in self._by_table:
            raise ValueError(
                f"lifecycle policy registration: table {policy.table!r} is already "
                f"registered (for entity_type {self._by_table[policy.table]!r})"
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
        for from_status, edges in policy.edges.items():
            if from_status not in policy.statuses:
                raise ValueError(
                    f"lifecycle policy registration: entity_type {policy.entity_type!r} "
                    f"declares edges from unknown status {from_status!r}"
                )
            for edge in edges:
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
        # Defensively wrap the edge map in an immutable mapping before storing —
        # the caller's own dict (and any built-in `_edges(...)` dict) stays
        # mutable in the caller's hands, but the copy this registry hands
        # back from `get()` cannot be reassigned through item assignment.
        # ImmutableEdgeMap has no inherited mutation surface (unlike a dict
        # subclass) while remaining compatible with dataclasses.asdict(),
        # pickle, and copy.deepcopy().
        frozen_policy = dataclasses.replace(policy, edges=ImmutableEdgeMap(policy.edges))
        self._by_entity_type[policy.entity_type] = frozen_policy
        self._by_table[policy.table] = policy.entity_type

    def seal(self) -> None:
        """Close this registry to further registration."""
        self._sealed = True

    def get(self, entity_type: str) -> LifecyclePolicy:
        try:
            return self._by_entity_type[entity_type]
        except KeyError:
            raise ValueError(
                f"lifecycle policy: unknown entity_type {entity_type!r}; registered "
                f"types are {sorted(self._by_entity_type)}"
            ) from None

    def __contains__(self, entity_type: str) -> bool:
        return entity_type in self._by_entity_type

    def entity_types(self) -> frozenset[str]:
        return frozenset(self._by_entity_type)


def _edges(*pairs: tuple[str, tuple[EdgePolicy, ...]]) -> dict[str, tuple[EdgePolicy, ...]]:
    return dict(pairs)


def _to(*statuses: str) -> tuple[EdgePolicy, ...]:
    return tuple(EdgePolicy(to_status=s) for s in statuses)


def build_default_registry() -> PolicyRegistry:
    registry = PolicyRegistry()

    # ── session / invocation ────────────────────────────────────────────
    # Same seven-value execution vocabulary and same execution graph.
    # No exit from a terminal status without override.
    session_statuses = frozenset(
        {"running", "completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
    )
    session_terminal = frozenset(
        {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
    )
    session_edges = _edges(("running", _to(*sorted(session_terminal))))
    session_patch_fields = frozenset(
        {"ended_at", "input_tokens", "output_tokens", "total_cost_usd", "num_turns", "duration_ms"}
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

    # ── show ─────────────────────────────────────────────────────────────
    # Deliberately permissive compatibility graph: either nonterminal
    # status may move to any *other* declared show status. completed/
    # aborted require override to exit.
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

    # ── play ─────────────────────────────────────────────────────────────
    # Same compatibility-graph shape as show: any nonterminal play status
    # may move to any other declared play status. merged/escalated/
    # gate_failed/blocked/aborted_after_finish require override to exit.
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

    # ── team ─────────────────────────────────────────────────────────────
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

    # ── schedule_run ─────────────────────────────────────────────────────
    # This target graph reconciles the shipped schema vocabulary with the
    # pre-existing partial validators. timed_out joins the terminal set
    # here, closing a gap where the legacy `update_status()` terminal set
    # omitted it.
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

    # ── dispatch ─────────────────────────────────────────────────────────
    # dead_letter/expired are terminal but operator-recoverable via an
    # ordinary declared edge (not a generic override) back to pending.
    # delivering -> delivering is the same-status crash-recovery claim: two
    # workers racing on the same row must never both win it, so the edge
    # declares required_guard_fields — the service refuses the edge unless
    # the caller supplies an equivalent expected_version or extra_guard
    # covering those columns (see SQLAlchemyLifecycleService._transition).
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
                # A consumer may present its ack_token while the delivery loop
                # still holds the row mid-tick (fast ack); ack must not have to
                # wait for the row to loop back to pending.
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
