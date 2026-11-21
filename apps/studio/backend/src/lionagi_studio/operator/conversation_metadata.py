# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Validated presentation metadata for an Operator's own conversation."""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lionagi.state.session_naming import validate_name_characters

__all__ = ("UpdateConversationInput", "normalize_description")


def normalize_description(value: str | None) -> str | None:
    """Normalize a short description while preserving embedded tabs and newlines."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("description must be a string or null")
    value = value.strip()
    if len(value) > 2000:
        raise ValueError("description must contain at most 2000 characters")
    if any(
        unicodedata.category(char) in {"Cf", "Zl", "Zp"}
        or (unicodedata.category(char) == "Cc" and char not in "\n\t")
        for char in value
    ):
        raise ValueError("description contains unsupported control or separator characters")
    return value or None


class UpdateConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("title must be a nonempty string")
        return validate_name_characters(value)

    @field_validator("description", mode="before")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return normalize_description(value)

    @model_validator(mode="after")
    def _require_update(self) -> UpdateConversationInput:
        if not self.model_fields_set.intersection({"title", "description"}):
            raise ValueError("provide a title or description to update")
        return self
