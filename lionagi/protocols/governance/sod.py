# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Separation of Duties (SoD) enforcement for the governance layer.

The ``SoDEnforcer`` checks whether a proposed action would violate any
SoD rule compiled from a Charter.  It maintains an in-memory record of
which actors (by identity and role) have acted within each scope, and
returns a ``SoDViolation`` when a conflict is detected before the action
is allowed to proceed.

Typical usage::

    from lionagi.protocols.governance.sod import SoDEnforcer
    from lionagi.protocols.governance.compiler import CharterCompiler
    from lionagi.protocols.governance.charter import parse_charter

    doc = parse_charter(yaml_text)
    result = CharterCompiler().compile(doc)

    enforcer = SoDEnforcer(result.sod_rules, sod_active=doc.sod.active)

    # Before an actor performs an action:
    violation = enforcer.check("agent-42", "author", "write", scope_id="session-1")
    if violation:
        raise PermissionError(str(violation))
    enforcer.record_action("agent-42", "author", "write", scope_id="session-1")
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import NamedTuple

from pydantic import BaseModel, Field

from lionagi.protocols.governance.targets import SoDRule

__all__ = [
    "ActorRecord",
    "SoDViolation",
    "SoDEnforcer",
]

_GLOBAL_SCOPE = "__global__"


class ActorRecord(NamedTuple):
    """Immutable record of one actor action within a rule-scope bucket."""

    actor_id: str
    role: str
    action: str


class SoDViolation(BaseModel):
    """Describes a single SoD rule violation detected before an action executes.

    Attributes:
        rule_id: The rule identifier that would be violated.
        conflicting_role: The role of the proposed action that triggers the rule.
        prior_role: The conflicting role already recorded in the scope.
        actor_id: Identity of the actor attempting the conflicting action.
        prior_actor_id: Identity of the actor who already performed the prior action.
        scope_id: Scope in which the conflict was detected.
        justification: Human-readable explanation of the violation.
    """

    rule_id: str = Field(description="SoD rule that would be violated.")
    conflicting_role: str = Field(description="Role the proposed actor would assume.")
    prior_role: str = Field(description="Role already recorded in this scope.")
    actor_id: str = Field(description="Actor attempting the conflicting action.")
    prior_actor_id: str = Field(description="Actor who already acted in the prior role.")
    scope_id: str = Field(description="Scope in which the conflict was detected.")
    justification: str = Field(description="Human-readable explanation.")

    def __str__(self) -> str:
        return (
            f"SoD violation [{self.rule_id}]: actor '{self.actor_id}' "
            f"with role '{self.conflicting_role}' conflicts with "
            f"'{self.prior_actor_id}' holding role '{self.prior_role}' "
            f"in scope '{self.scope_id}'"
        )


@dataclass
class _BucketState:
    """Per-(rule, scope) bucket: maps role → list[ActorRecord]."""

    by_role: dict[str, list[ActorRecord]] = field(default_factory=dict)

    def record(self, actor_id: str, role: str, action: str) -> None:
        rec = ActorRecord(actor_id=actor_id, role=role, action=action)
        self.by_role.setdefault(role, []).append(rec)

    def actors_with_role(self, role: str) -> list[ActorRecord]:
        return self.by_role.get(role, [])


def _scope_key_for_rule(rule: SoDRule, scope_id: str) -> str:
    """Return the canonical storage key for *rule* given *scope_id*.

    ``GLOBAL`` rules always map to ``__global__``; all others use the
    caller-supplied ``scope_id`` unchanged.
    """
    if rule.scope == "global":
        return _GLOBAL_SCOPE
    return scope_id


class SoDEnforcer:
    """Thread-safe, in-process SoD enforcement engine.

    Rules are loaded from the compiled ``SoDRule`` objects produced by
    ``CharterCompiler``.  Each rule names two conflicting roles
    (``role_a`` / ``role_b``) and a scope level.  The enforcer keeps a
    per-rule-per-scope record of which actors have acted, and rejects any
    action that would place the same actor in both conflicting roles within
    the same effective scope.

    Scope semantics
    ---------------
    ``"session"``
        Conflicts are isolated per *session* scope identifier.  Actions
        recorded in ``"session-A"`` do not affect ``"session-B"``.

    ``"task"``
        Conflicts are isolated per *task* scope identifier.  Equivalent
        to per-flow isolation.

    ``"global"``
        All actions share a single ``"__global__"`` bucket regardless of
        the ``scope_id`` argument passed in.

    Storage model
    -------------
    Records are stored in ``_buckets[(rule_index, resolved_scope_key)]``.
    This keeps global and session/task rules completely independent even
    when both are active simultaneously.

    Conflict semantics
    ------------------
    The enforcer checks whether the *same actor_id* attempts to hold both
    ``role_a`` and ``role_b`` of a rule within the same resolved scope.
    The ``conflict_type`` field records the governance rationale but does
    not alter the enforcement algorithm.

    Args:
        rules: Compiled SoD rules, typically from
            ``CompilationResult.sod_rules``.
        sod_active: When ``False``, ``check()`` always returns ``None``.
            Records are still accepted.  Mirrors the ``SodDef.active`` flag.
    """

    def __init__(
        self,
        rules: list[SoDRule],
        *,
        sod_active: bool = True,
    ) -> None:
        self._rules = list(rules)
        self._active = sod_active
        # (rule_index, resolved_scope_key) → _BucketState
        self._buckets: dict[tuple[int, str], _BucketState] = {}
        self._lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────────

    def record_action(
        self,
        actor_id: str,
        role: str,
        action: str,
        scope_id: str,
    ) -> None:
        """Record that *actor_id* (holding *role*) performed *action* in *scope_id*.

        Calling this method multiple times for the same
        (actor_id, role, action, scope_id) is safe — each call appends a
        new ``ActorRecord`` to the bucket, but that does not create
        spurious violations for the *same* actor/role combination.

        Args:
            actor_id: Stable identity string of the acting agent.
            role: Role under which the actor operates.
            action: Name of the action performed (e.g. ``"author"``).
            scope_id: Opaque scope key (session-id, flow-id, etc.).
        """
        with self._lock:
            for idx, rule in enumerate(self._rules):
                # Only record in rules where this role is relevant.
                if role not in (rule.role_a, rule.role_b):
                    continue
                key = _scope_key_for_rule(rule, scope_id)
                bucket_id = (idx, key)
                self._buckets.setdefault(bucket_id, _BucketState()).record(actor_id, role, action)

    def check(
        self,
        actor_id: str,
        role: str,
        action: str,
        scope_id: str,
    ) -> SoDViolation | None:
        """Check whether *actor_id* performing *action* as *role* would violate SoD.

        Returns the first ``SoDViolation`` found, or ``None``.  This call
        is a pure read — it does not record the proposed action.

        Args:
            actor_id: Stable identity string of the actor.
            role: Role under which the actor would operate.
            action: Proposed action name.
            scope_id: Opaque scope key.

        Returns:
            ``SoDViolation`` describing the first conflict, or ``None``.
        """
        if not self._active or not self._rules:
            return None

        with self._lock:
            for idx, rule in enumerate(self._rules):
                # Determine the peer role for this rule.
                if role == rule.role_a:
                    peer_role = rule.role_b
                elif role == rule.role_b:
                    peer_role = rule.role_a
                else:
                    continue  # rule does not involve the proposed role

                key = _scope_key_for_rule(rule, scope_id)
                bucket = self._buckets.get((idx, key))
                if bucket is None:
                    continue

                # Violation: same actor already holds the peer role in this bucket.
                for prior in bucket.actors_with_role(peer_role):
                    if prior.actor_id == actor_id:
                        rule_id = rule.conflict_type or f"sod:{rule.role_a}:{rule.role_b}"
                        return SoDViolation(
                            rule_id=rule_id,
                            conflicting_role=role,
                            prior_role=peer_role,
                            actor_id=actor_id,
                            prior_actor_id=prior.actor_id,
                            scope_id=scope_id,
                            justification=(
                                f"Actor '{actor_id}' already acted as '{peer_role}' "
                                f"in scope '{scope_id}'; cannot also act as '{role}' "
                                f"(rule: {rule_id})."
                            ),
                        )
        return None

    def clear_scope(self, scope_id: str) -> None:
        """Remove all recorded actions associated with *scope_id*.

        For SESSION/TASK rules, removes records where the resolved scope
        key equals *scope_id*.  For GLOBAL rules, only removed if
        *scope_id* is ``"__global__"``.

        This is typically called when a session or flow completes.

        Args:
            scope_id: The scope key to clear (as supplied to
                ``record_action`` / ``check``).
        """
        with self._lock:
            to_delete = [(idx, key) for (idx, key) in list(self._buckets) if key == scope_id]
            for bucket_id in to_delete:
                del self._buckets[bucket_id]

    @property
    def active(self) -> bool:
        """Whether SoD enforcement is currently active."""
        return self._active

    @property
    def rules(self) -> list[SoDRule]:
        """Read-only list of loaded SoD rules."""
        return list(self._rules)
