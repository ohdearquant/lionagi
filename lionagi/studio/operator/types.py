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


class UpdateConversationRequest(WireModel):
    """Partial update: only fields present in the request body are applied.

    ``status`` only accepts ``active``/``archived`` here -- deletion stays on
    the dedicated DELETE route so it keeps its own, more final, semantics.
    """

    # Only presence matters here: the route reads ``model_fields_set`` and
    # forwards a field only when the body carried it, so these defaults are
    # never applied to a conversation. That is also why ``pinned`` and
    # ``status`` are NOT nullable while ``title`` is. A null title clears the
    # title, which is a real thing to ask for; a null pinned or status has no
    # meaning, and accepting one would silently unpin (falsey reaches the
    # store's ``1 if pinned else 0``) or reach the store with a status it has
    # to reject as a conflict rather than as the malformed request it is.
    title: str | None = Field(default=None, max_length=512)
    pinned: bool = False
    status: Literal["active", "archived"] = "active"


class ForkConversationRequest(WireModel):
    up_to_sequence: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=512)


class OperatorContextSnapshot(WireModel):
    project: str | None = Field(default=None, max_length=512)
    space: Literal["mission", "designer", "library", "history", "schedules", "system"]
    route: str = Field(min_length=1, max_length=4096)
    selection: dict[str, str] | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    # Who observed this view, and how many views they had seen when they did.
    #
    # Ordering is by count and deliberately not by any clock. Server arrival
    # order answers a different question, since a view seen before an
    # instruction can arrive after it. A wall clock answers it wrongly too: it
    # can step backwards, and then a view from before the step outranks
    # everything observed after it.
    #
    # A count means nothing outside the page that did the counting, which is why
    # the observer travels with it. Two tabs on one conversation are looking at
    # two different pages; only the one the instruction came from can say where
    # the human is, and comparing the other's count against it is what makes an
    # abandoned page look current. A reload is a new observer for the same
    # reason.
    #
    # Both are optional here so a client that sends neither degrades to "cannot
    # establish freshness" rather than to a false claim of it.
    observation_seq: int | None = Field(default=None, ge=1, alias="observationSeq")
    observer_id: str | None = Field(default=None, min_length=1, max_length=128, alias="observerId")


class OperatorViewReport(OperatorContextSnapshot):
    """A view the browser reports outside of any turn.

    Both fields are required here, unlike on a turn's snapshot: a report exists
    only to answer "where is the human now", and one that cannot say who saw it
    or where it fell in their sequence cannot be ordered against anything, so
    accepting it would mean storing a view that can only ever be reported with
    the wrong freshness label.
    """

    observation_seq: int = Field(ge=1, alias="observationSeq")
    observer_id: str = Field(min_length=1, max_length=128, alias="observerId")


# The model still reaches a CLI argument, so it stays a closed set -- but the
# set now lives in the backend catalog (catalog.py) instead of a literal here,
# so a browser can only ever request a model the coordinator recognizes
# (resolve_selection rejects anything else before the turn is accepted).
OperatorProvider = Literal["claude_code", "codex", "gemini_code"]
OperatorEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class OperatorTurnRequest(WireModel):
    instruction: str = Field(min_length=1, max_length=32_768)
    context: OperatorContextSnapshot
    expected_last_sequence: int = Field(ge=0)
    model: str | None = Field(default=None, max_length=128)
    # Optional: an explicit provider disambiguates a model id and lets a turn
    # pin a provider without changing model. Omitted, it is inferred from
    # `model` (via the catalog) or falls back to the env-var default.
    provider: OperatorProvider | None = None
    effort: OperatorEffort | None = None
    # Omitting `model` means "keep whatever this conversation is pinned to",
    # so it cannot also mean "remove the pin". This asks for the pin to be
    # dropped, returning the conversation to the daemon's own default.
    clear_selection: bool = False

    @model_validator(mode="after")
    def _clear_is_not_also_a_pin(self) -> OperatorTurnRequest:
        if self.clear_selection and (
            self.model is not None or self.provider is not None or self.effort is not None
        ):
            raise ValueError("clearSelection cannot be combined with a provider, model, or effort")
        return self


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
    # The conversation's durable branch identity (see
    # OperatorStore.claim_branch_id). None only for a caller that never
    # claimed one (e.g. a bare unit test) -- build_operator_branch falls back
    # to a fresh random id in that case, the same default Branch() always had.
    branch_id: str | None = None
    model: str | None = None
    provider: str | None = None
    effort: str | None = None


class OperatorEngine(Protocol):
    def stream(self, turn: OperatorEngineTurn) -> AsyncIterator[OperatorEngineEvent]: ...


OperatorEngineFactory = Callable[[], OperatorEngine]
CommandExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
