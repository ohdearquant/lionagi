# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Wire and engine-side types for the ADR-0083 Operator protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

OperatorFrameType = Literal[
    "text",
    "tool_call",
    "tool_result",
    "ui_command",
    "proposal",
    "confirmation",
    "error",
    "done",
]
OperatorErrorCode = Literal[
    "auth_required",
    "validation",
    "not_found",
    "denied",
    "conflict",
    "stale_context",
    "rate_limited",
    "model_failure",
    "provider_unavailable",
    "service_failure",
    "service_restarted",
    "audit_unavailable",
    "replay_gap",
    "cancelled",
    "protocol_version",
]


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class WireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class CreateConversationRequest(WireModel):
    project: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, max_length=512)


class OperatorContextSnapshot(WireModel):
    project: str | None = Field(default=None, max_length=512)
    space: Literal["mission", "designer", "library", "history", "schedules", "system"]
    route: str = Field(min_length=1, max_length=4096)
    selection: dict[str, str] | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    # The browser's own count of the views it has observed in this conversation.
    # Every ordering question is answered in this one count -- report against
    # report, and report against the turn -- and deliberately not in any clock.
    # Server arrival order answers a different question, since a view seen
    # before an instruction can arrive after it. A wall clock answers it wrongly
    # too: it can step backwards, and then a view from before the step outranks
    # everything observed after it. A count that the browser seeds from the
    # server survives both. Optional so a client that does not send one degrades
    # to "cannot establish freshness" rather than to a false claim of it.
    observation_seq: int | None = Field(default=None, ge=1, alias="observationSeq")


class OperatorViewReport(OperatorContextSnapshot):
    """A view the browser reports outside of any turn.

    ``observation_seq`` is required here, unlike on a turn's snapshot: a report
    exists only to answer "where is the human now", and one that cannot say
    where it falls in the browser's own sequence cannot be ordered against
    anything, so accepting it would mean storing a view that can only ever be
    reported with the wrong freshness label.
    """

    observation_seq: int = Field(ge=1, alias="observationSeq")


# Closed set: the model reaches a CLI argument, so an open string would let the
# browser choose what the daemon executes.
OperatorModel = Literal["sonnet", "opus", "haiku"]


class OperatorTurnRequest(WireModel):
    instruction: str = Field(min_length=1, max_length=32_768)
    context: OperatorContextSnapshot
    expected_last_sequence: int = Field(ge=0)
    model: OperatorModel | None = None


class ConfirmProposalRequest(WireModel):
    expected_command_hash: str = Field(min_length=64, max_length=64)
    expected_target_version: str | None = None


class DecideProposalRequest(WireModel):
    decision: Literal["allow", "deny"]
    expected_command_hash: str | None = Field(default=None, min_length=64, max_length=64)
    expected_target_version: str | None = None

    @model_validator(mode="after")
    def _require_hash_for_allow(self) -> DecideProposalRequest:
        # A denial never executes the command, but an allow must bind the
        # human's decision to the exact command that was rendered.
        if self.decision == "allow" and self.expected_command_hash is None:
            raise ValueError("expectedCommandHash is required when allowing a proposal")
        return self


class AcknowledgeEffectRequest(WireModel):
    status: Literal["applied", "rejected"]
    client_route: str | None = None
    rejection_code: (
        Literal[
            "unsupported",
            "invalid_params",
            "stale_context",
            "not_visible",
            "client_error",
        ]
        | None
    ) = None


@dataclass(frozen=True, slots=True)
class OperatorEngineEvent:
    """One provider-neutral event emitted by an Operator engine."""

    type: OperatorFrameType
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    proposal_id: str
    result: dict[str, Any] | None = None


PermissionRequester = Callable[
    [str, dict[str, Any], Literal["mutate", "execute", "admin"], str],
    Awaitable[PermissionDecision],
]


@dataclass(frozen=True, slots=True)
class OperatorEngineTurn:
    conversation_id: str
    request_id: str
    instruction: str
    context: dict[str, Any]
    history: tuple[dict[str, Any], ...]
    request_permission: PermissionRequester
    runtime_branch: Any | None = None
    store_path: str | None = None
    run_dir: Any | None = None
    provider_session_id: str | None = None
    model: str | None = None


class OperatorEngine(Protocol):
    def stream(self, turn: OperatorEngineTurn) -> AsyncIterator[OperatorEngineEvent]: ...


OperatorEngineFactory = Callable[[], OperatorEngine]
CommandExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
