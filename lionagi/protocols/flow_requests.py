# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Request contracts shared by flow execution and session signals."""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = (
    "EscalationRequest",
    "SpawnRequest",
    "SPAWN_ALLOWED_OPERATIONS",
)


_SpawnOperation = Literal["operate", "chat", "communicate", "ReAct"]
SPAWN_ALLOWED_OPERATIONS: frozenset[str] = frozenset(get_args(_SpawnOperation))


class _EmissionModel(BaseModel):
    """Private base for all emission contracts; extra='forbid'."""

    model_config = ConfigDict(extra="forbid")


class EscalationRequest(_EmissionModel):
    """Hand off to a human or higher authority, or ask for help while continuing.
    ``urgency`` is authoritative; ``blocking`` is a deprecated read-only alias for ``urgency == "blocked"``."""

    reason: str = Field(
        description="Why escalation is needed — the blocker, uncertainty, or decision beyond your authority."
    )
    context: dict = Field(
        default_factory=dict,
        description="Structured context the recipient needs to decide (relevant state, options).",
    )
    urgency: Literal["fyi", "blocked"] = Field(
        default="blocked",
        description=(
            "'fyi': soft — you are continuing, this is informational (a help signal). "
            "'blocked': hard — you cannot proceed until this is resolved (the default, "
            "matching the historical 'blocking=True' behavior)."
        ),
    )
    from_role: str | None = Field(default=None, description="The role raising the escalation.")

    @model_validator(mode="before")
    @classmethod
    def _map_legacy_blocking(cls, data: Any) -> Any:
        if isinstance(data, dict) and "blocking" in data:
            data = dict(data)
            legacy = data.pop("blocking")
            data.setdefault("urgency", "blocked" if legacy else "fyi")
        return data

    @property
    def blocking(self) -> bool:
        """Deprecated back-compat alias for ``urgency == "blocked"``. Read-only."""
        return self.urgency == "blocked"


class SpawnRequest(_EmissionModel):
    """Request to add a new operation to the running workflow."""

    instruction: str = Field(
        description="The new unit of work, stated as a concrete, self-contained objective."
    )
    assignee: str | None = Field(
        default=None,
        description="Role to execute it (researcher, implementer, critic, ...). "
        "Omit to reuse the emitter's own role/branch.",
    )
    operation: _SpawnOperation = Field(
        default="operate",
        description="lionagi operation to run: operate | chat | communicate | ReAct. Default operate.",
    )
    independent: bool = Field(
        default=False,
        description="If true the new op starts immediately with no dependency on you. "
        "If false (default) it runs after you and inherits your output as context.",
    )
    reason: str | None = Field(
        default=None,
        description="Why this work is needed now and why it fell outside the original plan.",
    )
