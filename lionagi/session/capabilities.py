# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Render an agent's capability grant into a system-prompt instruction block."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from lionagi.ln.types import Operable

__all__ = (
    "render_capabilities_prompt",
    "CapabilityViolation",
    "EmissionRejected",
    "CAP_BEGIN",
    "CAP_END",
)


class CapabilityViolation(BaseModel):
    """Emitted when an agent emits a key outside its grant; the block is not honored."""

    offending: list[str] = Field(description="Emitted keys outside the grant.")
    allowed: list[str] = Field(description="The granted capability names.")
    block: dict | None = Field(default=None, description="The raw rejected block.")


class EmissionRejected(BaseModel):
    """Emitted when an in-grant capability block fails schema validation; carries the verbatim error."""

    branch_name: str = Field(default="", description="The emitting branch, for attribution.")
    error: str = Field(description="The validation error, verbatim.")
    block: dict | None = Field(default=None, description="The raw rejected block.")


# Idempotency markers so a re-grant replaces (rather than stacks) the block.
CAP_BEGIN = "<!-- lionagi:capabilities -->"
CAP_END = "<!-- /lionagi:capabilities -->"


def render_capabilities_prompt(operable: Operable) -> str:
    """Render the capability grant into a system-prompt section with its JSON schema."""
    model = operable.create_model(model_name=operable.name or "Capabilities")
    schema = model.model_json_schema()
    contract: dict = {"properties": schema.get("properties", {})}
    if "$defs" in schema:
        contract["$defs"] = schema["$defs"]
    from lionagi.ln import json_dumps

    block = json_dumps(contract, pretty=True, safe_fallback=True)
    names = ", ".join(sorted(operable.allowed()))

    return (
        "## Structured capabilities\n\n"
        "As you work, you may emit structured signals by including a fenced "
        "```json code block in your reply. Each top-level key is a capability; "
        "include only those relevant to the current step — you may emit several "
        "at once, or none. Keep your normal narration as usual; the JSON block "
        "is in addition to it, not a replacement.\n\n"
        f"Allowed capability keys (emitting any other key is rejected): {names}.\n\n"
        "Each key's value must conform to this schema:\n\n"
        f"```json\n{block}\n```"
    )
