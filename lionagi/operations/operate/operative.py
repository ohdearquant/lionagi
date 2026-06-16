# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import TYPE_CHECKING, Any, Literal

from lionagi.ln.types import Operable

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pydantic import BaseModel


class Operative:
    """Operation handler: one Operable as single source of truth, delegates field materialization to an adapter."""

    def __init__(
        self,
        name: str | None = None,
        adapter: Literal["pydantic"] = "pydantic",
        strict: bool = False,
        auto_retry_parse: bool = True,
        max_retries: int = 3,
        base_type: type["BaseModel"] | None = None,
        operable: Operable | None = None,
        request_exclude: set[str] | None = None,
    ):
        self.name = name or (base_type.__name__ if base_type else "Operative")
        self.adapter = adapter
        self.strict = strict
        self.auto_retry_parse = auto_retry_parse
        self.max_retries = max_retries
        self.base_type = base_type

        # Single source of truth
        self.operable = operable or Operable((), name=self.name)
        self.request_exclude = request_exclude or set()

        # Materialized models (cached)
        self._request_model_cls = None
        self._response_model_cls = None

        # Response state
        self.response_model = None
        self.response_str_dict = None
        self._should_retry = None

    def _get_adapter(self):
        if self.adapter == "pydantic":
            from lionagi.adapters.spec_adapters import PydanticSpecAdapter

            return PydanticSpecAdapter
        else:
            raise ValueError(f"Unsupported adapter: {self.adapter}")

    def create_request_model(self) -> type:
        """Materialize request specs into a Pydantic model, excluding ``request_exclude`` fields."""
        if self._request_model_cls:
            return self._request_model_cls

        self._request_model_cls = self.operable.create_model(
            adapter=self.adapter,
            model_name=f"{self.name}Request",
            base_type=self.base_type,
            exclude=self.request_exclude,
        )
        return self._request_model_cls

    def create_response_model(self) -> type:
        """Materialize all specs into a response model (superset of request model)."""
        if self._response_model_cls:
            return self._response_model_cls

        if not self._request_model_cls:
            self.create_request_model()

        self._response_model_cls = self.operable.create_model(
            adapter=self.adapter,
            model_name=f"{self.name}Response",
            base_type=self._request_model_cls,
        )

        return self._response_model_cls

    def validate_response(self, text: str, strict: bool | None = None) -> Any:
        """Validate raw response text; returns model instance or None on failure (raises if strict)."""
        strict = self.strict if strict is None else strict

        if not self._response_model_cls:
            self.create_response_model()

        adapter_cls = self._get_adapter()

        try:
            self.response_model = adapter_cls.validate_response(
                text,
                self._response_model_cls,
                strict=strict,
                fuzzy_parse=True,
            )
            self._should_retry = False
            return self.response_model

        except Exception as e:
            self.response_str_dict = text
            self._should_retry = strict

            if strict:
                raise e

            # Try fuzzy validation if auto-retry enabled
            if self.auto_retry_parse and not strict:
                try:
                    self.response_model = adapter_cls.validate_response(
                        text,
                        self._response_model_cls,
                        strict=False,
                        fuzzy_parse=True,
                    )
                    self._should_retry = False
                    return self.response_model
                except Exception:
                    logger.debug(
                        "Fuzzy validation fallback failed for %s; returning None for retry.",
                        self._response_model_cls,
                    )

            return None

    def update_response_model(self, text: str | None = None, data: dict | None = None) -> Any:
        """Update response model from raw text or a dict of field overrides; returns updated model or raw data."""
        if text is None and data is None:
            raise ValueError("Either text or data must be provided")

        if text:
            self.response_str_dict = text
            self.validate_response(text, strict=False)

        if data and self._response_model_cls and self.response_model:
            adapter_cls = self._get_adapter()
            self.response_model = adapter_cls.update_model(
                self.response_model, data, self._response_model_cls
            )

        return self.response_model or self.response_str_dict

    @property
    def request_type(self) -> type | None:
        if not self._request_model_cls:
            self.create_request_model()
        return self._request_model_cls

    @property
    def response_type(self) -> type | None:
        if not self._response_model_cls:
            self.create_response_model()
        return self._response_model_cls


__all__ = ("Operative",)
