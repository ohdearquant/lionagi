# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os
from typing import Any, TypeVar

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from .header_factory import AUTH_TYPES

logger = logging.getLogger(__name__)


B = TypeVar("B", bound=type[BaseModel])

__all__ = ("EndpointConfig",)


class EndpointConfig(BaseModel):
    name: str
    provider: str
    base_url: str | None = None
    endpoint: str
    endpoint_params: list[str] | None = None
    method: str = "POST"
    params: dict[str, str] = Field(default_factory=dict)
    content_type: str | None = "application/json"
    auth_type: AUTH_TYPES = "bearer"
    default_headers: dict = {}
    request_options: B | None = None
    api_key: str | SecretStr | None = Field(None, exclude=True)
    timeout: int = 300
    max_retries: int = 3
    openai_compatible: bool = False
    requires_tokens: bool = False
    context_window: int | None = None
    kwargs: dict = Field(default_factory=dict)
    client_kwargs: dict = Field(default_factory=dict)
    allow_local_network: bool = False
    serialize_by_alias: bool = False
    _api_key: str | None = PrivateAttr(None)

    @model_validator(mode="before")
    def _validate_kwargs(cls, data: dict):
        kwargs = data.pop("kwargs", {})
        field_keys = list(cls.model_json_schema().get("properties", {}).keys())
        for k in list(data.keys()):
            if k not in field_keys:
                kwargs[k] = data.pop(k)
        data["kwargs"] = kwargs
        return data

    @model_validator(mode="after")
    def _validate_api_key(self):
        if self.api_key is not None:
            if isinstance(self.api_key, SecretStr):
                self._api_key = self.api_key.get_secret_value()
            elif isinstance(self.api_key, str):
                # Skip settings lookup for special cases
                if self.provider == "ollama" and self.api_key == "ollama_key":
                    self._api_key = "ollama_key"
                elif self.api_key.startswith("dummy-key"):
                    self._api_key = self.api_key
                else:
                    from lionagi.config import settings

                    try:
                        self._api_key = settings.get_secret(self.api_key)
                    except (AttributeError, ValueError):
                        self._api_key = os.getenv(self.api_key, self.api_key)

        return self

    @field_validator("provider", mode="before")
    def _validate_provider(cls, v: str):
        if not v:
            raise ValueError("Provider must be specified")
        return v.strip().lower()

    @property
    def full_url(self):
        if not self.endpoint_params:
            return f"{self.base_url}/{self.endpoint}"
        return f"{self.base_url}/{self.endpoint.format(**self.params)}"

    @field_validator("request_options", mode="before")
    def _validate_request_options(cls, v):
        # Create a simple empty model if None is provided
        if v is None:
            return None

        try:
            if isinstance(v, type) and issubclass(v, BaseModel):
                return v
            if isinstance(v, BaseModel):
                return v.__class__
            if isinstance(v, dict | str):
                from lionagi.libs.schema.load_pydantic_model_from_schema import (
                    load_pydantic_model_from_schema,
                )

                return load_pydantic_model_from_schema(v)
        except Exception as e:
            raise ValueError("Invalid request options") from e
        raise ValueError("Invalid request options: must be a Pydantic model or a schema dict")

    @field_serializer("request_options")
    def _serialize_request_options(self, v: B | None):
        if v is None:
            return None
        return v.model_json_schema()

    def update(self, **kwargs):
        """Update the config with new values."""
        # Handle the special case of kwargs dict
        if "kwargs" in kwargs:
            # Merge the kwargs dicts
            self.kwargs.update(kwargs.pop("kwargs"))

        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                # Add to kwargs dict if not a direct attribute
                self.kwargs[key] = value

    def validate_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate payload data against the request_options model."""
        if not self.request_options:
            return data

        try:
            obj = self.request_options.model_validate(data)
            if self.serialize_by_alias:
                return obj.model_dump(by_alias=True, exclude_none=True)
            return data
        except Exception as e:
            raise ValueError("Invalid payload") from e
