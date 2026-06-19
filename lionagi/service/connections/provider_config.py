# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Mixin for per-provider config enums — tuple members drive EndpointMeta auto-creation via ``@config.register``."""

from __future__ import annotations

import importlib
from typing import Any

from pydantic import BaseModel

from .registry import EndpointType, register_endpoint


class LazyType:
    """Deferred type import resolved on first ``.resolve()`` call; hashable for use in Enum member tuples."""

    __slots__ = ("_ref", "_resolved")

    def __init__(self, ref: str) -> None:
        if ":" not in ref:
            raise ValueError(f"LazyType ref must be 'module:Class', got {ref!r}")
        self._ref = ref
        self._resolved: type | None = None

    def resolve(self) -> type[BaseModel]:
        if self._resolved is None:
            module_path, class_name = self._ref.rsplit(":", 1)
            mod = importlib.import_module(module_path)
            self._resolved = getattr(mod, class_name)
        return self._resolved

    def __hash__(self) -> int:
        return hash(self._ref)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LazyType):
            return self._ref == other._ref
        return NotImplemented

    def __repr__(self) -> str:
        status = "resolved" if self._resolved is not None else "pending"
        return f"LazyType({self._ref!r}, {status})"


def _get(value: tuple, idx: int, default=None):
    return value[idx] if len(value) > idx else default


class ProviderConfig:
    """Mixin for provider endpoint Enum classes; members are 4-7-element tuples (see module docstring)."""

    # --- Tuple accessors ---

    @property
    def endpoint_path(self) -> str:
        return self.value[0]

    @property
    def aliases(self) -> list[str]:
        return self.value[1]

    @property
    def endpoint_type(self) -> EndpointType:
        return self.value[2]

    @property
    def options(self) -> type[BaseModel] | None:
        raw = _get(self.value, 3)
        if isinstance(raw, LazyType):
            return raw.resolve()
        return raw

    @property
    def base_url(self) -> str | None:
        return _get(self.value, 4)

    @property
    def auth_type(self) -> str | None:
        return _get(self.value, 5)

    @property
    def content_type(self) -> str | None:
        return _get(self.value, 6, "application/json")

    # --- Provider info (set via _ignore_ or post-definition) ---

    @property
    def provider(self) -> str:
        return type(self)._PROVIDER

    @property
    def provider_aliases(self) -> list[str]:
        return type(self)._PROVIDER_ALIASES

    @property
    def api_key_env(self) -> str | None:
        """Settings attribute name for this provider's API key; None for providers with no key."""
        return getattr(type(self), "_API_KEY_ENV", None)

    # --- Registry integration ---

    def as_registry_kwargs(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_aliases": self.provider_aliases,
            "endpoint": self.endpoint_path,
            "aliases": self.aliases,
            "endpoint_type": self.endpoint_type,
            "options": self.options,
            "base_url": self.base_url,
            "auth_type": self.auth_type,
            "content_type": self.content_type,
            "api_key_env": self.api_key_env,
        }

    def register(self, cls=None):
        """Decorator: register an endpoint class for this config member."""
        decorator = register_endpoint(**self.as_registry_kwargs())
        if cls is not None:
            return decorator(cls)
        return decorator

    @classmethod
    def available(cls) -> frozenset[str]:
        return frozenset(m.endpoint_path for m in cls)
