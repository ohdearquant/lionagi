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

# Keyed on id(cls), not the class object, to bypass metaclass __eq__/__hash__ overrides —
# see docs/internals/runtime.md.
_FIELD_KEYS_BY_CLASS: dict[int, tuple[type, frozenset[str]]] = {}


B = TypeVar("B", bound=type[BaseModel])

__all__ = ("RUNTIME_STATE_NAMES", "EndpointConfig")

# Values a caller hands an endpoint for the lifetime of the process and never to
# be written down. ``env`` is a child process's environment and commonly holds
# credentials; ``on_spawn`` is a callback whose representation carries whatever
# object it is bound to. They are named here, in the model that would otherwise
# serialize them, because that is the one place every route to a written-down
# config has to pass through.
RUNTIME_STATE_NAMES: tuple[str, ...] = ("env", "on_spawn")


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
        if self.api_key is not None:
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

    @field_serializer("kwargs")
    def _serialize_kwargs(self, v: dict):
        """Keep runtime-only values out of every dump of this config.

        The endpoint holding these drains them out of ``kwargs`` when it
        serializes itself, which covers ``Endpoint.to_dict``, ``iModel.to_dict``
        and the run snapshots written underneath them. That drain does not cover
        this model's own public API. ``EndpointConfig`` inherits Pydantic's
        ``model_dump``, callers use it directly, and two supported routes put a
        runtime value back into ``kwargs`` after an endpoint has already drained
        it once: a post-construction ``update()``, and ``iModel.from_dict``,
        which assigns a freshly hydrated config over the drained one.

        Excluding here rather than draining on every read is what makes the
        answer independent of who is asking. A drain has to be reached, and the
        set of routes that reach it is open-ended because this model is public.

        Omitted rather than replaced with a placeholder: a dump is something a
        config is rebuilt from, and a stand-in string would hydrate as a real
        value of the wrong type. ``repr`` is not rebuilt from, so it reports
        presence instead — see :meth:`__repr_args__`.
        """
        return {k: val for k, val in v.items() if k not in RUNTIME_STATE_NAMES}

    def __iter__(self):
        """The same exclusion on the conversion Pydantic's dump does not run.

        ``dict(config)`` and ``list(config)`` go through ``BaseModel.__iter__``,
        which yields the raw values held in ``__dict__``. No field serializer
        runs on that path, so the exclusion above does not reach it, and
        ``json.dumps(dict(config), default=str)`` is an ordinary way to write a
        config to a log or a file.

        Omits rather than reports presence, matching the dump: a mapping of
        field names to values is something a config can be rebuilt from, so a
        stand-in string would come back as a real value of the wrong type.
        ``repr`` is the one channel nothing is rebuilt from, and it is the only
        one that names what it withheld.
        """
        for name, value in super().__iter__():
            if name == "kwargs" and isinstance(value, dict):
                value = {k: val for k, val in value.items() if k not in RUNTIME_STATE_NAMES}
            yield name, value

    def __repr_args__(self):
        """The same values, on the channel a dump does not reach.

        ``repr`` walks ``kwargs`` whole, so a config whose ``env`` is excluded
        from every dump still prints it in a traceback, a log line, or a
        debugger. Presence is reported and content is not: a reader asking why
        an environment was not applied needs to know one is set, and that is the
        entire question ``repr`` gets asked here.
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
