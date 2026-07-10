# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Immutable command/result/policy dataclasses for the unified lifecycle service.

See docs/adr/ADR-0058-unified-lifecycle-transition-service.md D1/D2 for the
normative contract these types implement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

ActorType = Literal["executor", "agent", "admin", "system", "scheduler", "operator", "webhook"]
TransitionResultKind = Literal["applied", "conflict", "rejected"]
SameStatusRule = Literal["append", "noop", "reject"]


@dataclass(frozen=True)
class ActorRecord:
    type: ActorType
    id: str


@dataclass(frozen=True)
class ReasonRecord:
    code: str
    summary: str = ""
    evidence_refs: tuple[Mapping[str, JsonValue], ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class OverrideRecord:
    actor: str
    justification: str


@dataclass(frozen=True)
class InitialStateCommand:
    entity_type: str
    entity_id: str
    status: str
    reason: ReasonRecord
    actor: ActorRecord


@dataclass(frozen=True)
class TransitionCommand:
    entity_type: str
    entity_id: str
    to_status: str
    reason: ReasonRecord
    actor: ActorRecord
    expected_statuses: frozenset[str | None] | None = None
    expected_version: float | None = None
    patch: Mapping[str, JsonValue] = field(default_factory=dict)
    override: OverrideRecord | None = None


@dataclass(frozen=True)
class TransitionOutcome:
    result: TransitionResultKind
    previous_status: str | None
    current_status: str
    transition_id: str | None


@dataclass(frozen=True)
class EdgePolicy:
    to_status: str
    actor_types: frozenset[ActorType] | None = None
    required_patch_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LifecyclePolicy:
    entity_type: str
    table: str
    statuses: frozenset[str]
    initial_statuses: frozenset[str]
    terminal_statuses: frozenset[str]
    edges: Mapping[str, tuple[EdgePolicy, ...]]
    same_status: SameStatusRule
    patch_fields: frozenset[str]
    reason_prefixes: frozenset[str]
