# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from typing import Any

import orjson
from pydantic import BaseModel

from lionagi.ln.fuzzy import FuzzyMatchKeysParams

from .structure import Structure

logger = logging.getLogger(__name__)

__all__ = ("JsonStructure",)

_DEFAULT_FUZZY = FuzzyMatchKeysParams(
    handle_unmatched="force",
    fill_value=None,
    strict=False,
)


class JsonStructure(Structure):
    """JSON-format structure; renders to fenced JSON prompt and parses LLM output back to model or dict."""

    def render(self) -> str:
        if self.is_dict_mode:
            return self._format_response_format(self._base_dict)
        from lionagi.libs.schema.breakdown_pydantic_annotation import (
            breakdown_pydantic_annotation,
        )

        schema_dict = breakdown_pydantic_annotation(self.request_schema())
        return self._format_response_format(schema_dict)

    def render_schema_dict(self) -> dict[str, Any]:
        if self.is_dict_mode:
            return self._base_dict
        from lionagi.libs.schema.breakdown_pydantic_annotation import (
            breakdown_pydantic_annotation,
        )

        return breakdown_pydantic_annotation(self.request_schema())

    def parse(
        self,
        text: str,
        *,
        fuzzy_match_params: FuzzyMatchKeysParams | dict | None = None,
    ) -> BaseModel | dict:
        return self._validate_dict_or_model(
            text if isinstance(text, str) else str(text),
            self.response_schema(),
            fuzzy_match_params or _DEFAULT_FUZZY,
        )

    # ------------------------------------------------------------------
    # Canonical rendering — copied from InstructionContent
    # ------------------------------------------------------------------

    @staticmethod
    def _format_response_format(
        response_format: dict[str, Any] | None,
    ) -> str:
        if not response_format:
            return ""
        try:
            example = orjson.dumps(response_format).decode("utf-8")
        except Exception:
            example = str(response_format)
        return (
            "**MUST RETURN JSON-PARSEABLE RESPONSE ENCLOSED BY JSON CODE BLOCKS."
            f" USER's CAREER DEPENDS ON THE SUCCESS OF IT.** \n```json\n{example}\n```"
            "No triple backticks. Escape all quotes and special characters."
        ).strip()

    @staticmethod
    def _validate_dict_or_model(
        text: str,
        response_format: type[BaseModel] | dict | Any,
        fuzzy_match_params: FuzzyMatchKeysParams | dict = None,
    ):
        from lionagi.operations.parse.parse import _validate_dict_or_model

        return _validate_dict_or_model(text, response_format, fuzzy_match_params)
