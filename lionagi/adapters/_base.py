# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Inlined adapter protocol stack (previously sourced from pydapter).

This module provides the Adaptable/AsyncAdaptable mixins and their supporting
registry/error infrastructure, eliminating the pydapter runtime dependency for
the core lionagi install.

Faithful port of pydapter 1.3.x core.py / async_core.py / exceptions.py / types.py.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any, ClassVar, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")

# ---------------------------------------------------------------------------
# URL / credential sanitization (ported from pydapter.types)
# ---------------------------------------------------------------------------

_CREDENTIAL_SCHEMES = frozenset(
    {
        "postgresql",
        "postgresql+asyncpg",
        "postgresql+psycopg2",
        "postgresql+psycopg",
        "mysql",
        "mysql+pymysql",
        "mysql+aiomysql",
        "mongodb",
        "mongodb+srv",
        "redis",
        "rediss",
        "amqp",
        "amqps",
        "http",
        "https",
        "neo4j",
        "neo4j+s",
        "neo4j+ssc",
        "bolt",
        "bolt+s",
        "bolt+ssc",
    }
)

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "api_key",
        "apikey",
        "dsn",
        "connection_string",
        "connection_url",
        "database_url",
        "db_url",
        "url",
        "uri",
    }
)

_MAX_DETAIL_LEN = 500

# Query-string parameter names that carry credentials.  Any key matching one
# of these (case-insensitive) is replaced with "***" in error output.
_SENSITIVE_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "api_token",
        "bearer_token",
        "secret",
        "api_key",
        "apikey",
        "api_secret",
        "client_secret",
        "auth",
        "authorization",
        "key",
        "password",
        "passwd",
        "pass",
        "sig",
        "signature",
        "callback",
        "webhook",
    }
)


def _redact_url(value: str) -> str:
    """Sanitize a URL string for safe inclusion in error messages.

    Redacts for ALL URL schemes (not just a known whitelist):
    - The password component of ``user:password@host`` netloc credentials.
    - Any query-string parameter whose name matches ``_SENSITIVE_QUERY_PARAMS``.
    """
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception:
        return value
    # Require a scheme so plain strings are not mis-parsed as URLs.
    if not parsed.scheme:
        return value

    replacements: dict[str, str] = {}

    # 1. Redact netloc password (user:pass@host form)
    if parsed.password:
        userinfo = parsed.username or ""
        userinfo = f"{userinfo}:***"
        host = parsed.hostname or ""
        netloc = f"{userinfo}@{host}:{parsed.port}" if parsed.port else f"{userinfo}@{host}"
        replacements["netloc"] = netloc

    # 2. Redact sensitive query parameters.
    # Use quote_via=urllib.parse.quote with safe="*" so that the "***"
    # placeholder is not percent-encoded in the final URL string.
    if parsed.query:
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        redacted_params = [
            (k, "***") if k.lower() in _SENSITIVE_QUERY_PARAMS else (k, v) for k, v in params
        ]
        new_query = urllib.parse.urlencode(redacted_params, quote_via=urllib.parse.quote, safe="*")
        if new_query != parsed.query:
            replacements["query"] = new_query

    if replacements:
        sanitized = parsed._replace(**replacements)
        return urllib.parse.urlunparse(sanitized)
    return value


def _redact_value(key: str, value: Any) -> Any:
    key_lower = key.lower()
    if key_lower in _SENSITIVE_KEYS:
        if isinstance(value, str):
            return _redact_url(value) if "://" in value else "***"
        if isinstance(value, dict):
            # The entire dict is under a sensitive key — blank all leaf values.
            return {k: "***" for k in value}
        if isinstance(value, list):
            return [_redact_value(key, item) for item in value]
        return "***"
    if isinstance(value, str) and "://" in value:
        return _redact_url(value)
    if isinstance(value, dict):
        # Non-sensitive key, but the nested dict may contain sensitive sub-keys
        # or URL values — recurse so they are caught.
        return _redact_details(value)
    if isinstance(value, list):
        # Each list element may be a dict with sensitive sub-keys, a URL string,
        # or another list — recurse with the same key so nested values are caught.
        return [_redact_value(key, item) for item in value]
    return value


def _redact_details(details: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys and URL credentials from a details dict."""
    redacted: dict[str, Any] = {}
    for k, v in details.items():
        v = _redact_value(k, v)
        if isinstance(v, str) and len(v) > _MAX_DETAIL_LEN:
            v = v[:_MAX_DETAIL_LEN] + "... (truncated)"
        redacted[k] = v
    return redacted


# ---------------------------------------------------------------------------
# Exception hierarchy (ported from pydapter.exceptions)
# ---------------------------------------------------------------------------

_ADAPTER_PYTHON_ERRORS = (KeyError, ImportError, AttributeError, ValueError)


class AdapterError(Exception):
    """Base exception for all adapter errors."""

    default_message: ClassVar[str] = "Adapter error"
    default_status_code: ClassVar[int] = 500
    __slots__ = ("message", "details", "status_code")

    def __init__(
        self,
        message: str | None = None,
        *,
        adapter: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        cause: Exception | None = None,
        **extra_context: Any,
    ):
        details = details or {}
        if adapter:
            details["adapter"] = adapter
        details.update(extra_context)
        super().__init__(message or self.default_message)
        if cause:
            self.__cause__ = cause
        self.message = message or self.default_message
        self.details = details
        self.status_code = status_code or type(self).default_status_code

    def __str__(self) -> str:
        safe = _redact_details(self.details)
        details_str = ", ".join(f"{k}={v!r}" for k, v in safe.items())
        if details_str:
            return f"{self.message} ({details_str})"
        return self.message

    def __getattr__(self, name: str) -> Any:
        if name == "context":
            return self.details
        if name in self.details:
            return self.details[name]
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


class AdapterValidationError(AdapterError):
    """Raised when data validation fails."""

    default_message = "Validation failed"
    default_status_code = 422
    __slots__ = ()

    def __init__(
        self,
        message: str | None = None,
        *,
        data: Any | None = None,
        errors: list[dict] | None = None,
        field_path: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        cause: Exception | None = None,
        adapter: str | None = None,
        **extra_context: Any,
    ):
        details = details or {}
        for k, v in {"data": data, "errors": errors, "field_path": field_path}.items():
            if v is not None:
                details[k] = v
        details.update(extra_context)
        super().__init__(
            message,
            details=details,
            status_code=status_code,
            cause=cause,
            adapter=adapter,
        )


class AdapterParseError(AdapterError):
    """Raised when data parsing fails."""

    default_message = "Parse failed"
    default_status_code = 400
    __slots__ = ()


class AdapterNotFoundError(AdapterError):
    """Raised when no adapter is registered for a key."""

    default_message = "Adapter not found"
    default_status_code = 404
    __slots__ = ()

    def __init__(
        self,
        message: str | None = None,
        *,
        obj_key: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        cause: Exception | None = None,
        adapter: str | None = None,
        **extra_context: Any,
    ):
        details = details or {}
        if obj_key is not None:
            details["obj_key"] = obj_key
        details.update(extra_context)
        super().__init__(
            message,
            details=details,
            status_code=status_code,
            cause=cause,
            adapter=adapter,
        )


class AdapterConfigurationError(AdapterError):
    """Raised when adapter configuration is invalid."""

    default_message = "Configuration invalid"
    default_status_code = 500
    __slots__ = ()


class AdapterResourceError(AdapterError):
    """Raised when a resource cannot be accessed."""

    default_message = "Resource not found"
    default_status_code = 404
    __slots__ = ()


class AdapterConnectionError(AdapterError):
    """Raised when a connection to a data source fails."""

    default_message = "Connection failed"
    default_status_code = 503
    __slots__ = ()


class AdapterQueryError(AdapterError):
    """Raised when a query execution fails."""

    default_message = "Query failed"
    default_status_code = 400
    __slots__ = ()


# ---------------------------------------------------------------------------
# dispatch_adapt_meth (ported from pydapter.core / pydapter.async_core)
# ---------------------------------------------------------------------------


def dispatch_adapt_meth(
    adapt_meth: str | Callable,
    obj: Any,
    adapt_kw: dict[str, Any] | None = None,
    cls: type | None = None,
) -> Any:
    if callable(adapt_meth):
        return adapt_meth(obj, **(adapt_kw or {}))
    if cls is None:
        raise ValueError("cls required when adapt_meth is a string")
    return getattr(cls, adapt_meth)(obj, **(adapt_kw or {}))


# ---------------------------------------------------------------------------
# Adapter protocol (ported from pydapter.core)
# ---------------------------------------------------------------------------


@runtime_checkable
class Adapter(Protocol[T]):
    """Protocol for stateless data format adapters."""

    adapter_key: ClassVar[str]
    obj_key: ClassVar[str]  # backward compatibility

    @classmethod
    def from_obj(
        cls,
        subj_cls: type[T],
        obj: Any,
        /,
        *,
        many: bool = False,
        adapt_meth: str | Callable = "model_validate",
        adapt_kw: dict[str, Any] | None = None,
        **kw: Any,
    ) -> T | list[T]: ...

    @classmethod
    def to_obj(
        cls,
        subj: T | list[T],
        /,
        *,
        many: bool = False,
        adapt_meth: str | Callable = "model_dump",
        adapt_kw: dict[str, Any] | None = None,
        **kw: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# AdapterBase helper (ported from pydapter.core.AdapterBase)
# ---------------------------------------------------------------------------


class AdapterBase:
    """Base class providing _handle_error() for consistent exception wrapping."""

    adapter_key: str = "base"

    _error_mapping: dict[str, type] = {
        "parse": AdapterParseError,
        "validation": AdapterValidationError,
        "connection": AdapterConnectionError,
        "query": AdapterQueryError,
        "resource": AdapterResourceError,
    }

    @classmethod
    def _sanitize_url(cls, url: str) -> str:
        if not isinstance(url, str):
            return url
        return _redact_url(url)

    @classmethod
    def _handle_error(cls, exc: Exception, category: str, **extra_details) -> None:
        error_class = cls._error_mapping.get(category, AdapterError)
        details: dict[str, Any] = {
            "category": category,
            "original_exception": exc.__class__.__name__,
        }
        for key in ("source", "data"):
            if key in extra_details:
                value = extra_details[key]
                if isinstance(value, str | bytes):
                    extra_details[key] = value[:100] if len(value) > 100 else value
        _url_fields = frozenset(("url", "connection", "connection_string", "database_url", "dsn"))
        for key, value in extra_details.items():
            if key in _url_fields and isinstance(value, str):
                extra_details[key] = cls._sanitize_url(value)
        details.update(extra_details)
        adapter_key = getattr(cls, "adapter_key", None) or getattr(cls, "obj_key", "unknown")
        raise error_class(
            message=str(exc),
            adapter=adapter_key,
            details=details,
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# AdapterRegistry (ported from pydapter.core.AdapterRegistry)
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """Registry for managing data format adapters."""

    def __init__(self) -> None:
        self._reg: dict[str, type[Adapter]] = {}

    def register(self, adapter_cls: type[Adapter]) -> None:
        key = getattr(adapter_cls, "adapter_key", None) or getattr(adapter_cls, "obj_key", None)
        if not key:
            raise AdapterConfigurationError(
                "Adapter must define 'adapter_key' or 'obj_key'",
                adapter=getattr(adapter_cls, "__name__", str(adapter_cls)),
            )
        self._reg[key] = adapter_cls

    def get(self, obj_key: str) -> type[Adapter]:
        try:
            return self._reg[obj_key]
        except KeyError as exc:
            raise AdapterNotFoundError(
                f"No adapter registered for '{obj_key}'", obj_key=obj_key
            ) from exc

    def adapt_from(
        self,
        subj_cls: type[T],
        obj: Any,
        *,
        obj_key: str,
        adapt_meth: str = "model_validate",
        **kw: Any,
    ) -> T | list[T]:
        try:
            result = self.get(obj_key).from_obj(subj_cls, obj, adapt_meth=adapt_meth, **kw)
            if result is None:
                raise AdapterError(f"Adapter {obj_key} returned None", adapter=obj_key)
            return result
        except Exception as exc:
            if isinstance(exc, (AdapterError, *_ADAPTER_PYTHON_ERRORS)):
                raise
            raise AdapterError(f"Error adapting from {obj_key}", original_error=str(exc)) from exc

    def adapt_to(
        self,
        subj: Any,
        *,
        obj_key: str,
        adapt_meth: str = "model_dump",
        **kw: Any,
    ) -> Any:
        try:
            result = self.get(obj_key).to_obj(subj, adapt_meth=adapt_meth, **kw)
            if result is None:
                raise AdapterError(f"Adapter {obj_key} returned None", adapter=obj_key)
            return result
        except Exception as exc:
            if isinstance(exc, (AdapterError, *_ADAPTER_PYTHON_ERRORS)):
                raise
            raise AdapterError(f"Error adapting to {obj_key}", original_error=str(exc)) from exc


# ---------------------------------------------------------------------------
# Adaptable mixin (ported from pydapter.core.Adaptable)
# ---------------------------------------------------------------------------


class Adaptable:
    """Mixin adding synchronous adapter functionality to Python classes."""

    @classmethod
    def _registry(cls) -> AdapterRegistry:
        registry_attr = f"__adapter_registry_{cls.__name__}_{id(cls)}"
        if not hasattr(cls, registry_attr):
            setattr(cls, registry_attr, AdapterRegistry())
        return getattr(cls, registry_attr)

    @classmethod
    def register_adapter(cls, adapter_cls: type[Adapter]) -> None:
        cls._registry().register(adapter_cls)

    @classmethod
    def adapt_from(
        cls,
        obj: Any,
        *,
        obj_key: str,
        adapt_meth: str = "model_validate",
        **kw: Any,
    ) -> Any:
        return cls._registry().adapt_from(cls, obj, obj_key=obj_key, adapt_meth=adapt_meth, **kw)

    def adapt_to(self, *, obj_key: str, adapt_meth: str = "model_dump", **kw: Any) -> Any:
        return self._registry().adapt_to(self, obj_key=obj_key, adapt_meth=adapt_meth, **kw)


# ---------------------------------------------------------------------------
# AsyncAdapter protocol (ported from pydapter.async_core)
# ---------------------------------------------------------------------------


@runtime_checkable
class AsyncAdapter(Protocol[T]):
    """Protocol for stateless async data format adapters."""

    obj_key: ClassVar[str]

    @classmethod
    async def from_obj(
        cls,
        subj_cls: type[T],
        obj: Any,
        /,
        *,
        many: bool = False,
        adapt_meth: str = "model_validate",
        **kw: Any,
    ) -> T | list[T]: ...

    @classmethod
    async def to_obj(
        cls,
        subj: T | list[T],
        /,
        *,
        many: bool = False,
        adapt_meth: str = "model_dump",
        **kw: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# AsyncAdapterRegistry (ported from pydapter.async_core)
# ---------------------------------------------------------------------------


class AsyncAdapterRegistry:
    def __init__(self) -> None:
        self._reg: dict[str, type[AsyncAdapter]] = {}

    def register(self, adapter_cls: type[AsyncAdapter]) -> None:
        key = getattr(adapter_cls, "obj_key", None)
        if not key:
            raise AdapterConfigurationError(
                "AsyncAdapter must define 'obj_key'",
                adapter=getattr(adapter_cls, "__name__", str(adapter_cls)),
            )
        self._reg[key] = adapter_cls

    def get(self, obj_key: str) -> type[AsyncAdapter]:
        try:
            return self._reg[obj_key]
        except KeyError as exc:
            raise AdapterNotFoundError(
                f"No async adapter for '{obj_key}'", obj_key=obj_key
            ) from exc

    async def adapt_from(
        self,
        subj_cls: type[T],
        obj: Any,
        *,
        obj_key: str,
        adapt_meth: str = "model_validate",
        **kw: Any,
    ) -> T | list[T]:
        try:
            result = await self.get(obj_key).from_obj(subj_cls, obj, adapt_meth=adapt_meth, **kw)
            if result is None:
                raise AdapterError(f"Async adapter {obj_key} returned None", adapter=obj_key)
            return result
        except Exception as exc:
            if isinstance(exc, (AdapterError, *_ADAPTER_PYTHON_ERRORS)):
                raise
            raise AdapterError(
                f"Error in async adapt_from for {obj_key}", original_error=str(exc)
            ) from exc

    async def adapt_to(
        self,
        subj: Any,
        *,
        obj_key: str,
        adapt_meth: str = "model_dump",
        **kw: Any,
    ) -> Any:
        try:
            result = await self.get(obj_key).to_obj(subj, adapt_meth=adapt_meth, **kw)
            if result is None:
                raise AdapterError(f"Async adapter {obj_key} returned None", adapter=obj_key)
            return result
        except Exception as exc:
            if isinstance(exc, (AdapterError, *_ADAPTER_PYTHON_ERRORS)):
                raise
            raise AdapterError(
                f"Error in async adapt_to for {obj_key}", original_error=str(exc)
            ) from exc


# ---------------------------------------------------------------------------
# AsyncAdaptable mixin (ported from pydapter.async_core.AsyncAdaptable)
# ---------------------------------------------------------------------------


class AsyncAdaptable:
    """Mixin that endows any Pydantic model with async adapt-from / adapt-to."""

    _async_registry: ClassVar[AsyncAdapterRegistry | None] = None

    @classmethod
    def _areg(cls) -> AsyncAdapterRegistry:
        if cls._async_registry is None:
            cls._async_registry = AsyncAdapterRegistry()
        return cls._async_registry

    @classmethod
    def register_async_adapter(cls, adapter_cls: type[AsyncAdapter]) -> None:
        cls._areg().register(adapter_cls)

    @classmethod
    async def adapt_from_async(
        cls,
        obj: Any,
        *,
        obj_key: str,
        adapt_meth: str = "model_validate",
        **kw: Any,
    ) -> Any:
        return await cls._areg().adapt_from(cls, obj, obj_key=obj_key, adapt_meth=adapt_meth, **kw)

    async def adapt_to_async(
        self, *, obj_key: str, adapt_meth: str = "model_dump", **kw: Any
    ) -> Any:
        return await self._areg().adapt_to(self, obj_key=obj_key, adapt_meth=adapt_meth, **kw)


# Public API
__all__ = (
    "Adaptable",
    "AsyncAdaptable",
    "Adapter",
    "AsyncAdapter",
    "AdapterBase",
    "AdapterRegistry",
    "AsyncAdapterRegistry",
    "dispatch_adapt_meth",
    # Exceptions
    "AdapterError",
    "AdapterValidationError",
    "AdapterParseError",
    "AdapterNotFoundError",
    "AdapterConfigurationError",
    "AdapterResourceError",
    "AdapterConnectionError",
    "AdapterQueryError",
    "_ADAPTER_PYTHON_ERRORS",
)
