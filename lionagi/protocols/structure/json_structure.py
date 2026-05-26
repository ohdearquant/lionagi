# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from typing import Any

import orjson
from pydantic import BaseModel

from lionagi.ln import extract_json, fuzzy_validate_mapping, to_list
from lionagi.ln.fuzzy import FuzzyMatchKeysParams

from .base import Structure

logger = logging.getLogger(__name__)

__all__ = ("JsonStructure",)

_DEFAULT_FUZZY = FuzzyMatchKeysParams(
    handle_unmatched="force",
    fill_value=None,
    strict=False,
)


class JsonStructure(Structure):
    """JSON-format structure using lionagi's established rendering and parsing.

    Handles both BaseModel and dict response formats.
    """

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

    # ------------------------------------------------------------------
    # Canonical parsing — copied from operations/parse
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_dict_or_model(
        text: str,
        response_format: type[BaseModel] | dict | Any,
        fuzzy_match_params: FuzzyMatchKeysParams | dict = None,
    ):
        try:
            if isinstance(fuzzy_match_params, dict):
                fuzzy_match_params = FuzzyMatchKeysParams(**fuzzy_match_params)

            d_ = extract_json(text, fuzzy_parse=True, return_one_if_single=False)
            dict_, keys_ = None, None
            if d_:
                dict_ = to_list(d_, flatten=True)[0]
            if isinstance(fuzzy_match_params, FuzzyMatchKeysParams):
                keys_ = (
                    response_format.model_fields
                    if isinstance(response_format, type)
                    else response_format
                )
                dict_ = fuzzy_validate_mapping(dict_, keys_, **fuzzy_match_params.to_dict())
            elif fuzzy_match_params:
                keys_ = (
                    response_format.model_fields
                    if isinstance(response_format, type)
                    else response_format
                )
                dict_ = fuzzy_validate_mapping(
                    dict_,
                    keys_,
                    handle_unmatched="force",
                    fill_value=None,
                    strict=False,
                )
            if isinstance(response_format, type) and issubclass(response_format, BaseModel):
                return response_format.model_validate(dict_)
            return dict_

        except Exception as e:
            raise ValueError(f"Failed to parse text: {e}") from e
