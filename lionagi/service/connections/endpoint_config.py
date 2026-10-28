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
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from lionagi.ln import is_pydantic_model_class

from .header_factory import AUTH_TYPES

logger = logging.getLogger(__name__)

# Keyed on id(cls), not the class object, to bypass metaclass __eq__/__hash__ overrides —
# see docs/internals/runtime.md.
_FIELD_KEYS_BY_CLASS: dict[int, tuple[type, frozenset[str]]] = {}


B = TypeVar("B", bound=type[BaseModel])

__all__ = ("AUTHORIZATION_KWARG_NAMES", "RUNTIME_STATE_NAMES", "EndpointConfig")

# Values a caller hands an endpoint for the lifetime of the process and never to
# be written down. ``env`` is a child process's environment and commonly holds
# credentials; ``on_spawn`` is a callback whose representation carries whatever
# object it is bound to. They are named here, in the model that would otherwise
# serialize them, because that is the one place every route to a written-down
# config has to pass through.
RUNTIME_STATE_NAMES: tuple[str, ...] = ("env", "on_spawn")

# Kwargs that grant the call an authority, kept in ``kwargs`` for the live call
# and never written down. ``account_connectors`` lets a codex request reach the
# signed-in account's app connectors, and it belongs to the role the call runs
# under, not to the branch: a branch restored from a snapshot or a run record by
# a caller under another role would otherwise run with the grant. Restored
# without it, the request falls back to what its persisted settings say, and a
# caller whose role names connectors hands them again.
AUTHORIZATION_KWARG_NAMES: tuple[str, ...] = ("account_connectors",)

# Both sets, for the serialization seams. Kept as one name so a seam cannot be
# updated for one category and silently miss the other.
_UNWRITTEN_KWARG_NAMES: tuple[str, ...] = RUNTIME_STATE_NAMES + AUTHORIZATION_KWARG_NAMES


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
    api_key: str | SecretStr | None = Field(None, exclude=True, repr=False)
    api_key_env: str | None = Field(None, exclude=True, repr=False)
    timeout: int = 300
    max_retries: int = 3
    # A retried POST to a non-idempotent creation endpoint (image generation,
    # batch creation) can double-execute billable work when the first attempt
    # was actually accepted but its response was lost. When True, every retry
    # of one logical request carries the same "Idempotency-Key" header so the
    # provider can dedupe the ambiguous replay instead of re-running it.
    idempotent_retries: bool = False
    openai_compatible: bool = False
    requires_tokens: bool = False
    context_window: int | None = None
    # Prompt-cache placement (ADR-0127): read by the endpoint, never sent in a request body. It
    # changes a request only on an endpoint that declares prefix caching of a native system block.
    prompt_caching: bool = False
    kwargs: dict = Field(default_factory=dict)
    client_kwargs: dict = Field(default_factory=dict)
    allow_local_network: bool = False
    serialize_by_alias: bool = False
    _api_key: str | None = PrivateAttr(None)
    _api_key_explicit: bool = PrivateAttr(default=False)

    def __init__(self, **data: Any) -> None:
        api_key_explicit = "api_key" in data
        super().__init__(**data)
        self._api_key_explicit = api_key_explicit

    @model_validator(mode="before")
    def _validate_kwargs(cls, data: dict):
        kwargs = data.pop("kwargs", {})
        # Field keys (aliases included) are cached per class — rebuilding the JSON
        # schema on every construction costs more than the rest of validation combined.
        entry = _FIELD_KEYS_BY_CLASS.get(id(cls))
        if entry is not None and entry[0] is cls:
            field_keys = entry[1]
        else:
            properties = cls.model_json_schema().get("properties", {})
            field_keys = frozenset(properties)
            _FIELD_KEYS_BY_CLASS[id(cls)] = (cls, field_keys)
        for k in list(data.keys()):
            if k not in field_keys:
                kwargs[k] = data.pop(k)
        data["kwargs"] = kwargs
        return data

    @model_validator(mode="after")
    def _validate_api_key(self):
        if name := self._api_key_reference():
            self._api_key = self._resolve_api_key_reference(name)
        elif self.api_key is not None:
            if isinstance(self.api_key, SecretStr):
                self._api_key = self.api_key.get_secret_value()
            elif isinstance(self.api_key, str):
                if self.provider == "ollama" and self.api_key == "ollama_key":
                    self._api_key = "ollama_key"
                elif self.api_key.startswith("dummy-key"):
                    self._api_key = self.api_key
                else:
                    from lionagi.config import settings

                    try:
                        self._api_key = settings.get_secret(self.api_key)
                    except ValueError:
                        self.api_key_env = self.api_key
                        self._api_key = os.getenv(self.api_key)
                    except AttributeError:
                        self._api_key = os.getenv(self.api_key, self.api_key)

        return self

    def _api_key_reference(self) -> str | None:
        if not self.api_key_env or isinstance(self.api_key, SecretStr):
            return None
        if isinstance(self.api_key, str) and (
            self.api_key.startswith("dummy-key")
            or (self.provider == "ollama" and self.api_key == "ollama_key")
        ):
            return None
        return self.api_key_env if self.api_key in (None, self.api_key_env) else None

    @staticmethod
    def _resolve_api_key_reference(name: str) -> str | None:
        from lionagi.config import settings

        try:
            return settings.get_secret(name)
        except (AttributeError, ValueError):
            return os.getenv(name)

    def _require_api_key(self) -> None:
        if self.auth_type != "none" and (name := self._api_key_reference()):
            if not self._api_key:
                self._api_key = self._resolve_api_key_reference(name)
            if not self._api_key:
                from lionagi._errors import CredentialMissingError

                raise CredentialMissingError(name) from None

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
        if v is None:
            return None

        try:
            if is_pydantic_model_class(v):
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

    @field_serializer("kwargs")
    def _serialize_kwargs(self, v: dict):
        """Excludes RUNTIME_STATE_NAMES (env, on_spawn) and AUTHORIZATION_KWARG_NAMES
        (account_connectors) from every dump of this config (model_dump, to_dict,
        run snapshots), independent of caller.
        See docs/internals/service-layer.md#runtime-state-across-serialization-channels.
        """
        return {k: val for k, val in v.items() if k not in _UNWRITTEN_KWARG_NAMES}

    @model_serializer(mode="wrap")
    def _omit_prompt_caching_when_off(self, handler: SerializerFunctionWrapHandler):
        # A config dump is stored with every model a branch records, so a dump with the option
        # off stays the one written before the option existed; a stored config without the key
        # loads with the default, and a dump with the option on keeps it for the reload.
        data = handler(self)
        if isinstance(data, dict) and not self.prompt_caching:
            data.pop("prompt_caching", None)
        return data

    def __iter__(self):
        """Applies the same exclusion on the ``dict(config)``/
        ``list(config)`` path, which bypasses the field serializer above. See
        docs/internals/service-layer.md#runtime-state-across-serialization-channels.
        """
        for name, value in super().__iter__():
            if name == "kwargs" and isinstance(value, dict):
                value = {k: val for k, val in value.items() if k not in _UNWRITTEN_KWARG_NAMES}
            yield name, value

    def __repr_args__(self):
        """Unlike dump/iter, repr reports RUNTIME_STATE_NAMES presence (not
        content) rather than omitting it — repr is never rebuilt from, so a
        reader needs to see that e.g. ``env`` is set. See
        docs/internals/service-layer.md#runtime-state-across-serialization-channels.
        """
        for name, value in super().__repr_args__():
            if name == "kwargs" and isinstance(value, dict):
                value = {
                    k: (f"<{k}: set, not shown>" if k in RUNTIME_STATE_NAMES else val)
                    for k, val in value.items()
                }
            yield name, value

    def update(self, **kwargs):
        """Update the config with new values."""
        if "kwargs" in kwargs:
            self.kwargs.update(kwargs.pop("kwargs"))

        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
                if key == "api_key":
                    self._api_key_explicit = True
            else:
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
