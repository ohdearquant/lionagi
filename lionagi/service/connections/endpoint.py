# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from lionagi.service.resilience import (
    APIClientError,
    CircuitBreaker,
    RetryConfig,
    _reraise_inner,
    retry_with_backoff,
)
from lionagi.service.types.stream_chunk import StreamChunk

from .endpoint_config import AUTHORIZATION_KWARG_NAMES, EndpointConfig
from .header_factory import HeaderFactory

_CONFIGURED_TIMEOUT = object()

# Built on first use and shared by every client, as aiohttp shared its own.
_VERIFIED_SSL_CONTEXT = None


def _verified_ssl_context():
    # ssl.create_default_context() reads the system trust store and SSL_CERT_FILE and
    # SSL_CERT_DIR, as aiohttp's verification did; httpx's own default is certifi's bundle.
    global _VERIFIED_SSL_CONTEXT
    if _VERIFIED_SSL_CONTEXT is None:
        import ssl

        _VERIFIED_SSL_CONTEXT = ssl.create_default_context()
    return _VERIFIED_SSL_CONTEXT


def _pinned_ssl_context(fingerprint: bytes):
    """A context for an ``aiohttp.Fingerprint``: no chain check, and the server's certificate
    must have this SHA-256 digest, checked when the handshake completes and before the request
    is sent, as aiohttp checked it. A mismatch fails the connection with an ``ssl.SSLError``."""
    import hashlib
    import ssl

    class _PinnedSSLObject(ssl.SSLObject):
        def do_handshake(self):
            super().do_handshake()
            # aiohttp's Fingerprint takes SHA-256 digests only.
            got = hashlib.sha256(self.getpeercert(binary_form=True)).digest()
            if got != fingerprint:
                raise ssl.SSLCertVerificationError(
                    f"the server certificate's SHA-256 fingerprint is {got.hex()}, "
                    f"expected {fingerprint.hex()}"
                )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.sslobject_class = _PinnedSSLObject
    return context


def _refuse_a_redirect_off_https():
    """A request hook for a pinned client: a redirect from https to plain http is refused.

    The pin is checked only on a TLS connection, so following such a redirect would send the
    request, body included, to a server the pin never checked. A request first sent over
    plain http is left alone, as aiohttp left it.
    """
    import httpx

    first_scheme = None

    async def hook(request):
        nonlocal first_scheme
        if first_scheme is None:
            first_scheme = request.url.scheme
        elif first_scheme == "https" and request.url.scheme != "https":
            raise httpx.ConnectError(
                f"refusing a redirect from https to {request.url.scheme}: "
                "the certificate fingerprint cannot be checked there",
                request=request,
            )

    return hook


def _seconds_or_none(value):
    """A timeout as httpx takes it: aiohttp read zero or below as no timeout, httpx as none left."""
    if isinstance(value, (int, float)) and value <= 0:
        return None
    return value


def _httpx_timeout(timeout):
    """httpx's timeout for an ``aiohttp.ClientTimeout``.

    httpx has no bound on a whole request, so ``total`` bounds each phase, as
    ``EndpointConfig.timeout`` does, and caps the phases ``connect``, ``sock_connect`` and
    ``sock_read`` set. A value of zero or below is no timeout, as aiohttp read it.
    """
    import httpx

    total = _seconds_or_none(timeout.total)

    def bound(*values):
        value = next((v for v in map(_seconds_or_none, values) if v is not None), None)
        if value is None:
            return total
        return value if total is None else min(value, total)

    return httpx.Timeout(
        connect=bound(timeout.sock_connect, timeout.connect),
        read=bound(timeout.sock_read),
        write=total,
        pool=bound(timeout.connect),
    )


# The event types that say the call itself failed: the Responses API's
# ``response.failed`` and Codex's ``turn.failed``. A bare ``failed`` is read the same
# way, although no provider in this package sends one. Any other type ending in
# ``.failed``, such as ``response.mcp_call.failed``, is read as about an item inside the
# call; a type that ends in ``failed`` without the dot is read like any other event.
_CALL_FAILED_EVENT_TYPES = frozenset({"response.failed", "turn.failed", "failed"})

# An SSE ``event:`` name that says the call failed, whatever the event's data holds: a
# provider can name the event ``error`` and put an error subtype, or plain text, in it.
_SSE_CALL_FAILURE_NAMES = frozenset({"error", "response.error"}) | _CALL_FAILED_EVENT_TYPES


def _event_reports_call_failure(event: Any) -> bool:
    """Whether a provider event or response says the call failed.

    The event counts as failed when its type is one that says the call itself
    failed (``_CALL_FAILED_EVENT_TYPES``). Any other type ending in ``.failed``,
    such as ``response.mcp_call.failed``, is read as about an item inside the call
    and does not count, whatever else the event carries. Otherwise it counts when
    the event itself, its ``message`` or its ``response`` has an ``error`` or
    ``is_error`` that is set, a ``status`` of ``failed``, or a choice whose
    ``finish_reason`` is ``error``.
    """
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type", ""))
    if event_type in _CALL_FAILED_EVENT_TYPES:
        return True
    if event_type.endswith(".failed"):
        return False
    for holder in (event, event.get("message"), event.get("response")):
        if not isinstance(holder, dict):
            continue
        if holder.get("error") or holder.get("is_error") or holder.get("status") == "failed":
            return True
        choices = holder.get("choices")
        if isinstance(choices, list) and any(
            isinstance(choice, dict) and choice.get("finish_reason") == "error"
            for choice in choices
        ):
            return True
    return False


def _call_failure_detail(chunk: StreamChunk) -> str:
    """What a chunk marked ``call_failed`` says went wrong, for the error that ends the call.

    An error chunk's own text comes first: it is what the consumer was shown, after any
    redaction the endpoint applied, and the raw event can hold text the chunk masked.
    """
    if chunk.type == "error" and isinstance(chunk.content, str) and chunk.content:
        return chunk.content
    raw = chunk.metadata.get("raw")
    if isinstance(raw, dict):
        for holder in (raw, raw.get("response")):
            if not isinstance(holder, dict):
                continue
            error = holder.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if isinstance(error, str) and error:
                return error
        if raw.get("message"):
            return str(raw["message"])
        return f"provider event {raw.get('type') or 'without a type'}"
    return chunk.content or "provider reported a failed call"


@dataclass(slots=True)
class _SSEFrame:
    """One server-sent event being assembled: its data lines and its ``event:`` name."""

    data: list[str] = field(default_factory=list)
    event: str | None = None


class _NonRetryableClientError(Exception):
    """Sentinel: 4xx (non-429) error that must not be retried. Wraps the original
    via __cause__ so callers can inspect it while the retry engine sees an excluded type."""

    def __init__(self, original: Exception):
        super().__init__(str(original))
        self.original = original


class _RoutedAttemptError(Exception):
    """Private failure passed between the request and retry layers.

    Its message holds only the code; ``account``, ``attempt_id`` and ``http_status`` are
    attributes.
    Raised ``from None``, it still keeps the exception it replaced as ``__context__``.
    """

    def __init__(
        self,
        code: str,
        *,
        account: str | None = None,
        attempt_id: Any = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.account = account
        self.attempt_id = attempt_id
        self.http_status = http_status
        super().__init__(code)


class _RoutedRetryableError(_RoutedAttemptError):
    pass


class _RoutedNoRetryError(_RoutedAttemptError):
    pass


def _error_in_body(payload: Any, body: Any) -> Any:
    """The provider error a successful reply to a chat request carries instead of a result.

    Returns None for a result. Only a chat request (one that sends ``messages``) is
    checked, since other APIs, such as a scrape that answers ``success: false`` with an
    ``error``, return their own result shapes. Only a bare error envelope counts. A
    typed API object (one with ``object`` set, such as an OpenAI Responses result whose
    ``status`` is ``failed``) is a result even when it carries an ``error`` field.
    """
    if (
        isinstance(payload, dict)
        and "messages" in payload
        and isinstance(body, dict)
        and body.get("error")
        and "choices" not in body
        and "object" not in body
    ):
        return body["error"]
    return None


logger = logging.getLogger(__name__)


__all__ = ("Endpoint",)


class Endpoint:
    is_cli: ClassVar[bool] = False
    transport_arg_keys: ClassVar[tuple[str, ...]] = ()
    _account_routing_provider: ClassVar[str | None] = None
    # Whether the provider accepts a native system block and caches requests by prompt prefix
    # (ADR-0127 D3). Only such an endpoint changes a request when ``prompt_caching`` is on.
    caches_system_prefix: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for old_name, new_name in (
            ("_call_aiohttp", "_call_http"),
            ("_stream_aiohttp", "_stream_http"),
        ):
            if old_name in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} overrides a removed transport method; use {new_name} instead"
                )

    def __init__(
        self,
        config: dict | EndpointConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_config: RetryConfig | None = None,
        **kwargs,
    ):
        if config is None:
            meta = getattr(type(self), "_ENDPOINT_META", None)
            if meta is not None:
                _config = meta.create_config(**kwargs)
            else:
                raise ValueError(
                    "No config provided and no _ENDPOINT_META on class. "
                    "Either pass a config or use @register_endpoint."
                )
        elif isinstance(config, dict):
            _config = EndpointConfig(**config, **kwargs)
        elif isinstance(config, EndpointConfig):
            _config = config.model_copy(deep=True)
            _config.update(**kwargs)
        else:
            raise ValueError("Config must be a dict, EndpointConfig, or None")
        self.config = _config
        self.circuit_breaker = circuit_breaker
        self.retry_config = retry_config
        self._direct_account_scope_id = None

        logger.debug(
            f"Initialized Endpoint with provider={self.config.provider}, "
            f"endpoint={self.config.endpoint}, circuit_breaker={circuit_breaker is not None}, "
            f"retry_config={retry_config is not None}"
        )

    def _create_http_session(
        self, client_kwargs: dict[str, Any] | None = None, *, timeout: Any = _CONFIGURED_TIMEOUT
    ):
        import httpx

        if timeout is _CONFIGURED_TIMEOUT:
            timeout = self.config.timeout
        options = dict(self.config.client_kwargs if client_kwargs is None else client_kwargs)
        options.setdefault("follow_redirects", True)
        options.setdefault("trust_env", False)
        options.setdefault("verify", _verified_ssl_context())
        options.setdefault("timeout", httpx.Timeout(_seconds_or_none(timeout)))
        return httpx.AsyncClient(**options)

    def _client_for_request(self, request_kwargs: dict[str, Any]):
        """The client for one request and the options left for the request itself.

        aiohttp took ``ssl``, ``proxy``, ``allow_redirects`` and ``timeout`` with each
        request. httpx takes the first two from the client, as ``verify`` and ``proxy``, the
        third as ``follow_redirects`` and the fourth as an ``httpx.Timeout``, so a request
        that passes ``ssl`` or ``proxy`` gets a client of its own. A request's ``ssl=True``
        verifies certificates even when the configured client does not.
        """
        if not request_kwargs.keys() & {"ssl", "proxy", "allow_redirects", "timeout"}:
            return self._create_http_session(), request_kwargs
        import ssl
        import sys

        import httpx

        # Set only when the caller has imported aiohttp, as building its types requires.
        aiohttp = sys.modules.get("aiohttp")
        request_kwargs = dict(request_kwargs)
        if aiohttp is not None and isinstance(request_kwargs.get("timeout"), aiohttp.ClientTimeout):
            request_kwargs["timeout"] = _httpx_timeout(request_kwargs["timeout"])
        elif "timeout" in request_kwargs:
            request_kwargs["timeout"] = _seconds_or_none(request_kwargs["timeout"])
        if "allow_redirects" in request_kwargs:
            if "follow_redirects" in request_kwargs:
                raise TypeError("pass allow_redirects or follow_redirects, not both")
            request_kwargs["follow_redirects"] = request_kwargs.pop("allow_redirects")
        client_options: dict[str, Any] = {}
        if "ssl" in request_kwargs:
            verify = request_kwargs.pop("ssl")
            if verify is None or verify is True:
                # aiohttp read None as True.
                if self.config.client_kwargs.get("verify", True) is False:
                    client_options["verify"] = _verified_ssl_context()
            elif verify is False or isinstance(verify, ssl.SSLContext):
                client_options["verify"] = verify
            elif aiohttp is not None and isinstance(verify, aiohttp.Fingerprint):
                client_options["verify"] = _pinned_ssl_context(verify.fingerprint)
                configured = self.config.client_kwargs.get("event_hooks") or {}
                hooks = {name: list(handlers) for name, handlers in configured.items()}
                hooks.setdefault("request", []).append(_refuse_a_redirect_off_https())
                client_options["event_hooks"] = hooks
            else:
                raise TypeError(
                    "ssl takes True, False, None, an ssl.SSLContext or an aiohttp.Fingerprint, "
                    f"not {type(verify).__name__}"
                )
        if "proxy" in request_kwargs:
            proxy = request_kwargs.pop("proxy")
            # aiohttp went direct for an empty proxy: None, "" or an empty URL.
            if proxy:
                client_options["proxy"] = proxy if isinstance(proxy, httpx.Proxy) else str(proxy)
        if not client_options:
            return self._create_http_session(), request_kwargs
        return (
            self._create_http_session({**self.config.client_kwargs, **client_options}),
            request_kwargs,
        )

    def copy_runtime_state_to(self, other: Endpoint) -> None:
        pass

    def drain_runtime_state(self) -> None:
        """Endpoints that hold runtime-only state move it out of the config here.

        A plain endpoint has none, so this does nothing. It is declared beside
        ``copy_runtime_state_to`` because the two are called as a pair, and a
        caller that has to ask whether an endpoint supports draining will
        eventually skip it for the one that needs it.
        """

    @property
    def places_prompt_cache(self) -> bool:
        """True when requests get ADR-0127's cache placement: the option is on and declared here."""
        return bool(self.config.prompt_caching) and self.caches_system_prefix

    @property
    def request_options(self):
        return self.config.request_options

    @request_options.setter
    def request_options(self, value):
        self.config.request_options = EndpointConfig._validate_request_options(value)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        routed = self._account_routing_enabled()
        if routed:
            from lionagi.service.accounts._router import header_pairs

            # Read as every routed path reads headers, so a form the merge cannot read is
            # refused as a credential conflict rather than failing inside the merge.
            # A later pair replaces an earlier one of the same name, compared case-insensitively,
            # so a caller's `content-type` or `anthropic-version` takes the default's place.
            headers = HeaderFactory.get_header(
                auth_type="none", content_type=self.config.content_type
            )
            for name, value in (
                *header_pairs(self.config.default_headers),
                *header_pairs(extra_headers),
            ):
                for same in [key for key in headers if key.lower() == name.lower()]:
                    del headers[same]
                headers[name] = value
        else:
            headers = HeaderFactory.get_header(
                auth_type=self.config.auth_type,
                content_type=self.config.content_type,
                api_key=self.config._api_key,
                default_headers=self.config.default_headers,
            )
            if extra_headers:
                headers.update(extra_headers)

        request = request if isinstance(request, dict) else request.model_dump(exclude_none=True)
        if routed and ("api_key" in request or "api_key" in kwargs):
            from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

            raise AccountRoutingError(RouterErrorCode.CREDENTIAL_CONFLICT) from None

        if routed and any(
            source.get("response_mode", "json") != "json" for source in (request, kwargs)
        ):
            from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

            # A routed call is settled from the usage in its JSON body and its key is
            # masked in that body as parsed; a mode that returns anything else can do
            # neither, so it is refused here, before a request is built or sent.
            raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING) from None

        payload = self.config.kwargs.copy()
        payload.update(request)

        if kwargs:
            payload.update(kwargs)

        if self.config.request_options is not None:
            valid_fields = set(self.config.request_options.model_fields.keys())
            filtered_payload = {k: v for k, v in payload.items() if k in valid_fields}
            payload = self.config.validate_payload(filtered_payload)
        else:
            non_api_params = {
                "provider",
                "base_url",
                "endpoint",
                "endpoint_params",
                "api_key",
                "queue_capacity",
                "capacity_refresh_time",
                "invoke_with_endpoint",
                "extra_headers",
                "headers",
                "include_token_usage_to_model",
                "chat_model",
                "imodel",
                "branch",
                "aggregation_sources",
                "aggregation_count",
                "action_strategy",
                "parse_model",
                "actions",
                "return_operative",
                # Removed operation aliases — drop so a stale caller passing
                # one as **kwargs can't leak it into a schema-less payload.
                "request_model",
                "operative_model",
                # The prompt-cache option and the compiler's system blocks are read by the endpoint;
                # a caller that passes either per call must not get it sent to the provider.
                "prompt_caching",
                "system_blocks",
            }
            payload = {k: v for k, v in payload.items() if k not in non_api_params}

        if routed:
            from lionagi.service.accounts._router import validate_routed_request

            validate_routed_request(
                self,
                headers,
                request_kwargs={},
                payload=payload,
            )
        return (payload, headers)

    def _account_routing_enabled(self) -> bool:
        if self._account_routing_provider != "anthropic":
            return False
        from lionagi.config import settings

        return bool(settings.ANTHROPIC_ACCOUNTS)

    def _account_scope(self, supplied_scope: Any = None) -> Any:
        if supplied_scope is not None:
            return supplied_scope
        from uuid import uuid4

        from lionagi.service.accounts._types import Scope

        if self._direct_account_scope_id is None:
            self._direct_account_scope_id = uuid4()
        return Scope("endpoint", self._direct_account_scope_id, "anthropic")

    @staticmethod
    def _public_routed_failure(exc: _RoutedAttemptError) -> Exception:
        from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

        return AccountRoutingError(
            RouterErrorCode(exc.code),
            account=exc.account,
            attempt_id=exc.attempt_id,
            http_status=exc.http_status,
        )

    def _assert_ssrf_safe_url(self) -> None:
        """Raise PermissionError if full_url resolves to a blocked address."""
        from urllib.parse import urlparse

        from lionagi.ln._ssrf import is_ssrf_safe

        parsed = urlparse(self.config.full_url)
        hostname = parsed.hostname or ""
        allow_local = getattr(self.config, "allow_local_network", False)
        if not is_ssrf_safe(hostname, allow_local=allow_local):
            raise PermissionError(
                f"SSRF guard: request to {hostname!r} is blocked "
                "(hostname resolves to a private or reserved IP address)"
            )

    async def _call(self, payload: dict, headers: dict, **kwargs):
        return await self._call_http(payload=payload, headers=headers, **kwargs)

    def _with_idempotency_key(self, headers: dict, *, routed: bool) -> dict:
        """*headers* with the key one logical request sends on every attempt, when retries are
        idempotent.

        Minted once per logical request, before any retry path (native _call_http, the
        opt-in RetryConfig wrapper, or a routed stream's attempts) is entered, and reused
        verbatim across every attempt, so the provider can recognize a lost-response replay as
        the same request. It also takes the place of a client-level key: one key shared by every
        request would let the provider answer a new request with an earlier one's response.
        """
        if not (self.config.idempotent_retries and self.config.method.upper() == "POST"):
            return headers
        import uuid

        if routed:
            # Routed headers were read as name-value pairs: each name is a token given once
            # and each value a string. A caller's key is kept whatever the case of its name,
            # and an empty or blank one, which names no request, is replaced.
            named = [name for name in headers if name.lower() == "idempotency-key"]
            if not any(headers[name].strip() for name in named):
                headers = {name: value for name, value in headers.items() if name not in named}
                headers["Idempotency-Key"] = str(uuid.uuid4())
        elif "Idempotency-Key" not in headers:
            headers = {**headers, "Idempotency-Key": str(uuid.uuid4())}
        return headers

    async def call(
        self,
        request: dict | BaseModel,
        cache_control: bool = False,
        skip_payload_creation: bool = False,
        _routing_scope: Any = None,
        **kwargs,
    ):
        routed = self._account_routing_enabled()
        # Only a routed call hands the scope down: an unrouted endpoint's _call never
        # receives a keyword its signature may not take.
        scope_kwargs = {"_routing_scope": self._account_scope(_routing_scope)} if routed else {}
        use_cache = cache_control and not routed
        extra_headers = kwargs.pop("extra_headers", None)

        payload, headers = None, None
        if skip_payload_creation:
            # Treat request as the ready payload
            payload = request if isinstance(request, dict) else request.model_dump()
            headers = extra_headers or {}
            if routed:
                from lionagi.service.accounts._router import header_pairs

                # A list of pairs becomes the mapping the steps below expect; any other form is
                # refused as a credential conflict before anything is reserved.
                headers = dict(header_pairs(headers))
        else:
            payload, headers = self.create_payload(request, extra_headers=extra_headers, **kwargs)

        headers = self._with_idempotency_key(headers, routed=routed)

        call_func = self._call

        if self.retry_config:

            async def call_func(p, h, **kw):
                # 4xx sentinel stays excluded until this retry layer gives up, else a
                # broad retry_exceptions config could replay a single-shot 4xx response.
                retry_kwargs = self.retry_config.as_kwargs()
                retry_kwargs["exclude_exceptions"] = (
                    *retry_kwargs.get("exclude_exceptions", ()),
                    _NonRetryableClientError,
                )
                if routed:
                    retry_kwargs["exclude_exceptions"] = (
                        *retry_kwargs["exclude_exceptions"],
                        _RoutedNoRetryError,
                    )
                    retry_kwargs["retry_exceptions"] = (
                        *retry_kwargs["retry_exceptions"],
                        _RoutedRetryableError,
                    )
                try:
                    return await retry_with_backoff(self._call, p, h, **kw, **retry_kwargs)
                except _NonRetryableClientError as exc:
                    # Unwrap the sentinel without making the original its own cause:
                    # the judge walks __cause__ to find a provider refusal, and a
                    # self-cause would make that walk never end.
                    _reraise_inner(exc.original)
                except _RoutedAttemptError as exc:
                    raise self._public_routed_failure(exc) from None

        if self.circuit_breaker:
            if self.retry_config:
                if not use_cache:
                    return await self.circuit_breaker.execute(
                        call_func,
                        payload,
                        headers,
                        **scope_kwargs,
                        **kwargs,
                    )
            else:
                if not use_cache:
                    return await self.circuit_breaker.execute(
                        self._call,
                        payload,
                        headers,
                        **scope_kwargs,
                        **kwargs,
                    )

        if use_cache:
            from aiocache import cached

            from lionagi.config import settings

            @cached(**settings.aiocache_config.as_kwargs())
            async def _cached_call(payload: dict, headers: dict, **kwargs):
                if self.circuit_breaker and self.retry_config:
                    return await self.circuit_breaker.execute(call_func, payload, headers, **kwargs)
                if self.circuit_breaker:
                    return await self.circuit_breaker.execute(
                        self._call, payload, headers, **kwargs
                    )
                if self.retry_config:
                    return await call_func(payload, headers, **kwargs)

                return await self._call(payload, headers, **kwargs)

            return await _cached_call(payload, headers, **kwargs)

        if self.retry_config:
            return await call_func(
                payload,
                headers,
                **scope_kwargs,
                **kwargs,
            )

        return await self._call(
            payload,
            headers,
            **scope_kwargs,
            **kwargs,
        )

    def _can_retry(self) -> bool:
        """Whether more than one request attempt can occur for this endpoint.
        Single-shot endpoints (max_retries<=1, no RetryConfig) never replay a body —
        see docs/internals/runtime.md."""
        if self.retry_config:
            return self.retry_config.max_retries > 0
        return self.config.max_retries > 1

    async def _request_attempt(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        body_kwargs: dict[str, Any],
        request_kwargs: dict[str, Any],
        response_mode: Literal["json", "bytes"],
        error_context: str,
        _routing_scope: Any = None,
    ) -> Any:
        import httpx

        client, request_kwargs = self._client_for_request(request_kwargs)
        async with client:
            response = await client.request(
                method=self.config.method,
                url=self.config.full_url,
                headers=headers,
                **body_kwargs,
                **request_kwargs,
            )

            status = response.status_code
            if status == 429 or status >= 500:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise APIClientError(
                        str(exc), status_code=status, headers=response.headers
                    ) from exc
            elif status != 200:
                error_kwargs = {}
                try:
                    error_body = response.json()
                    error_message = f"{error_context} failed with status {status}: {error_body}"
                    error_kwargs["response_data"] = error_body
                except Exception:
                    error_message = f"{error_context} failed with status {status}"

                original = APIClientError(
                    error_message,
                    status_code=status,
                    headers=response.headers,
                    **error_kwargs,
                )
                # 4xx (non-429): wrap in sentinel so the native retry layer
                # gives up immediately, preserving the original error on __cause__.
                raise _NonRetryableClientError(original) from original

            if response_mode == "bytes":
                return response.content
            try:
                body = response.json()
            except json.JSONDecodeError:
                # An empty body is None, as aiohttp read it; httpx raises on one.
                if response.content.strip():
                    raise
                body = None
            error = _error_in_body(payload, body)
            if error is None:
                return body

            # Some OpenAI-compatible gateways answer a refused request with
            # HTTP 200 and an error object in place of a result. Fail it the
            # way the status it names would have failed.
            code = error.get("code") if isinstance(error, dict) else None
            status = (
                code
                if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 600
                else response.status_code
            )
            original = APIClientError(
                f"{error_context} failed with status {status}: {body}",
                status_code=status,
                headers=response.headers,
                response_data=body,
            )
            if status == 429 or status >= 500:
                raise original
            raise _NonRetryableClientError(original) from original

    async def _call_http(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        request_body_factory: Callable[[], dict[str, Any]] | None = None,
        response_mode: Literal["json", "bytes"] = "json",
        error_context: str = "Request",
        _routing_scope: Any = None,
        **request_kwargs: Any,
    ) -> Any:
        routed = self._account_routing_enabled()
        if routed:
            from lionagi.service.accounts._router import validate_routed_request
            from lionagi.service.accounts._types import AccountRoutingError

            try:
                validate_routed_request(
                    self,
                    headers,
                    request_kwargs=request_kwargs,
                    payload=payload,
                )
            except AccountRoutingError as exc:
                if not self.retry_config:
                    raise
                # call() unwraps this after its RetryConfig layer, which must not replay it.
                raise _RoutedNoRetryError(
                    exc.code.value,
                    account=exc.account,
                    attempt_id=exc.attempt_id,
                    http_status=exc.http_status,
                ) from None
            _routing_scope = self._account_scope(_routing_scope)
        self._assert_ssrf_safe_url()

        import httpx

        build_body = request_body_factory or (lambda: {"json": payload})

        async def _make_request():
            # Body rebuilt per-attempt, not before retry orchestration: multipart bodies and
            # file streams get consumed by the first POST and must not be silently replayed.
            body_kwargs = build_body()
            collision = set(body_kwargs) & set(request_kwargs)
            if collision:
                if routed:
                    from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

                    raise AccountRoutingError(RouterErrorCode.CREDENTIAL_CONFLICT) from None
                raise ValueError(
                    f"request_body_factory and request_kwargs both supply {sorted(collision)}"
                )
            return await self._request_attempt(
                payload=payload,
                headers=headers,
                body_kwargs=body_kwargs,
                request_kwargs=request_kwargs,
                response_mode=response_mode,
                error_context=error_context,
                _routing_scope=_routing_scope,
            )

        # When retry_config is set, call() already wraps this in retry_with_backoff and
        # owns the sentinel unwrap — just run the raw request and let it propagate.
        if self.retry_config:
            return await _make_request()

        # No RetryConfig: native path excludes the 4xx sentinel so it gives up immediately.
        # max_retries is a total-attempt cap; retry_with_backoff runs +1 attempts, so subtract one.
        retry_exceptions = (httpx.RequestError, APIClientError, asyncio.TimeoutError)
        excluded_exceptions: tuple[type[Exception], ...] = (_NonRetryableClientError,)
        if routed:
            retry_exceptions = (*retry_exceptions, _RoutedRetryableError)
            excluded_exceptions = (*excluded_exceptions, _RoutedNoRetryError)
        try:
            return await retry_with_backoff(
                _make_request,
                retry_exceptions=retry_exceptions,
                exclude_exceptions=excluded_exceptions,
                max_retries=max(0, self.config.max_retries - 1),
                base_delay=1.0,
                max_delay=60.0,
                backoff_factor=2.0,
                jitter=True,
                jitter_factor=0.5,
            )
        except _NonRetryableClientError as exc:
            _reraise_inner(exc.original)
        except _RoutedAttemptError as exc:
            raise self._public_routed_failure(exc) from None

    async def stream(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        _routing_scope: Any = None,
        **kwargs,
    ):
        routed = self._account_routing_enabled()
        scope_kwargs = {"_routing_scope": self._account_scope(_routing_scope)} if routed else {}
        payload, headers = self.create_payload(request, extra_headers, **kwargs)
        if routed:
            # A routed stream retries before its first chunk, so its attempts share one key
            # as a routed call's do.
            headers = self._with_idempotency_key(headers, routed=True)

        stream = self._stream_http(
            payload=payload,
            headers=headers,
            **scope_kwargs,
            **kwargs,
        )
        if not routed:
            async for chunk in stream:
                yield chunk
            return
        try:
            async for chunk in stream:
                yield chunk
        finally:
            # A consumer that stops early closes this generator. Close the routed attempt
            # too, so it records how the stream ended now rather than when collected.
            await stream.aclose()

    async def _stream_http(
        self,
        payload: dict,
        headers: dict,
        *,
        _routing_scope: Any = None,
        **kwargs,
    ):
        self._assert_ssrf_safe_url()

        payload["stream"] = True

        client, kwargs = self._client_for_request(kwargs)
        async with client:
            async with client.stream(
                method=self.config.method,
                url=self.config.full_url,
                headers=headers,
                json=payload,
                **kwargs,
            ) as response:
                if response.status_code != 200:
                    raise APIClientError(
                        f"Request failed with status {response.status_code}",
                        status_code=response.status_code,
                        headers=response.headers,
                    )

                pending = ""
                frame = _SSEFrame()
                async for text in response.aiter_text():
                    if not text:
                        continue
                    pending += text
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        for data, name in self._frame_stream_line(line, frame):
                            yield self._line_to_stream_chunk(data, sse_event=name)

                if pending:
                    for data, name in self._frame_stream_line(pending, frame):
                        yield self._line_to_stream_chunk(data, sse_event=name)
                if frame.data:
                    data, name = self._take_sse_event(frame)
                    yield self._line_to_stream_chunk(data, sse_event=name)

    @staticmethod
    def _take_sse_event(frame: _SSEFrame) -> tuple[str, str | None]:
        """The event *frame* holds, as its data and its ``event:`` name; the frame starts over."""
        event = ("\n".join(frame.data), frame.event)
        frame.data.clear()
        frame.event = None
        return event

    @classmethod
    def _frame_stream_line(cls, line: str, frame: _SSEFrame) -> list[tuple[str, str | None]]:
        """Feed one HTTP stream line to *frame*; return the events it completes.

        Each event is its data and the name its ``event:`` field gave it, or None. A blank
        line ends the event being assembled, and an ``event:`` field names that event,
        before or after its ``data:`` lines. A stream without blank lines is split as well:
        an ``event:`` field arriving after a named event's data starts the next event, and
        a ``data:`` line arriving after data that is already complete JSON (or ``[DONE]``)
        does too. A line that is no SSE field is an event of its own with no name, after
        any event still waiting, and the name being assembled is dropped with it.
        """
        stripped = line.rstrip("\r").strip()

        if not stripped:
            if frame.data:
                return [cls._take_sse_event(frame)]
            frame.event = None
            return []

        if stripped.startswith(":") or stripped.startswith(("id:", "retry:")):
            return []

        if stripped.startswith("event:"):
            events = []
            if frame.data and frame.event is not None:
                events.append(cls._take_sse_event(frame))
            frame.event = stripped.removeprefix("event:").strip() or None
            return events

        if stripped.startswith("data:"):
            data = stripped.removeprefix("data:")
            if data.startswith(" "):
                data = data[1:]
            events = []
            if frame.data and cls._looks_like_complete_stream_data(frame.data):
                events.append(cls._take_sse_event(frame))
            frame.data.append(data)
            return events

        events = [cls._take_sse_event(frame)] if frame.data else []
        frame.event = None
        events.append((stripped, None))
        return events

    @staticmethod
    def _looks_like_complete_stream_data(data_lines: list[str]) -> bool:
        data = "\n".join(data_lines).strip()
        if data == "[DONE]":
            return True
        try:
            json.loads(data)
        except json.JSONDecodeError:
            return False
        return True

    def _line_to_stream_chunk(self, line: str, sse_event: str | None = None) -> StreamChunk:
        """Convert a generic HTTP stream line into the StreamChunk contract.

        *sse_event* is the name the event's SSE ``event:`` field gave it. Only a name that
        says the call failed (``_SSE_CALL_FAILURE_NAMES``) is read: such an event fails the
        call whatever its data holds, the name stands in for a ``type`` the JSON body does
        not carry, and the chunk records it as ``sse_event``. Any other name, ``message``
        included, leaves the chunk exactly as the data alone converts.

        A chunk whose event says the call itself failed (an ``error`` event, what
        ``_event_reports_call_failure`` reads as one, or a failure name) is marked
        ``call_failed`` in its metadata. The chunk keeps the type it converts to, so its
        content still reaches the consumer; ``APICalling`` reads the mark and fails the
        call when the stream ends. Data that is not a JSON object under a failure name
        becomes an error chunk holding that text.
        """
        data = line
        if line.startswith("data:"):
            data = line.removeprefix("data:").strip()

        if data == "[DONE]":
            return StreamChunk(type="result", metadata={"done": True})

        failure_name = sse_event if sse_event in _SSE_CALL_FAILURE_NAMES else None

        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            event, decoded = None, False
        else:
            decoded = True

        if not isinstance(event, dict):
            # As before: undecodable data keeps its line as the raw event, decoded JSON
            # that is not an object keeps the decoded value.
            text, raw = (str(event), event) if decoded else (data, line)
            if failure_name is not None:
                return StreamChunk(
                    type="error",
                    content=text,
                    is_error=True,
                    metadata={"raw": raw, "sse_event": failure_name, "call_failed": True},
                )
            return StreamChunk(
                type="text",
                content=text,
                is_delta=True,
                metadata={"raw": raw},
            )

        if failure_name is not None and "type" not in event:
            event = {**event, "type": failure_name}

        chunk = self._event_to_stream_chunk(event)
        if chunk is None:
            chunk = StreamChunk(type="system", metadata={"raw": event})
        if failure_name is not None:
            chunk.metadata["sse_event"] = failure_name
        if (
            failure_name is not None
            or (chunk.type == "error" and chunk.is_error)
            or _event_reports_call_failure(event)
        ):
            chunk.metadata["call_failed"] = True
        return chunk

    def _event_to_stream_chunk(self, event: dict) -> StreamChunk | None:
        """Best-effort conversion for OpenAI-compatible and SSE JSON events."""
        typ = event.get("type")
        if typ in {"error", "response.error"}:
            err = event.get("error", event)
            return StreamChunk(
                type="error",
                content=str(err),
                is_error=True,
                metadata={"raw": event},
            )

        if typ in {"response.output_text.delta", "response.refusal.delta"}:
            return StreamChunk(
                type="text",
                content=event.get("delta", ""),
                is_delta=True,
                metadata={"raw": event},
            )
        if typ == "response.completed":
            return StreamChunk(type="result", metadata={"raw": event})

        if typ == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") in {"text_delta", "thinking_delta"}:
                chunk_type = "thinking" if delta.get("type") == "thinking_delta" else "text"
                return StreamChunk(
                    type=chunk_type,
                    content=delta.get("text") or delta.get("thinking", ""),
                    is_delta=True,
                    metadata={"raw": event},
                )

        choices = event.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] or {}
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if content:
                    return StreamChunk(
                        type="text",
                        content=content,
                        is_delta=True,
                        metadata={"raw": event},
                    )
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    call = tool_calls[0] or {}
                    function = call.get("function") or {}
                    return StreamChunk(
                        type="tool_use",
                        tool_name=function.get("name"),
                        tool_id=call.get("id"),
                        tool_input={"arguments": function.get("arguments")},
                        is_delta=True,
                        metadata={"raw": event},
                    )
            return StreamChunk(type="system", metadata={"raw": event})

        # Usage-only terminal event: a Chat Completions stream with
        # stream_options.include_usage ends with {"choices": [], "usage": {...}}.
        # Without this branch the empty-choices list would fall through to a
        # "system" chunk and consumers that read terminal accounting from result
        # chunks would miss the provider-reported usage.
        usage = event.get("usage")
        if usage is not None and isinstance(choices, list):
            return StreamChunk(type="result", metadata={"raw": event, "usage": usage})

        return None

    def to_dict(self):
        return {
            "retry_config": (self.retry_config.to_dict() if self.retry_config else None),
            "circuit_breaker": (self.circuit_breaker.to_dict() if self.circuit_breaker else None),
            "config": self.config.model_dump(exclude_none=True),
        }

    @classmethod
    def from_dict(cls, data: dict):
        from lionagi.utils import to_dict

        data = to_dict(data, recursive=True)
        retry_config = data.get("retry_config")
        circuit_breaker = data.get("circuit_breaker")
        config = data.get("config")

        if retry_config:
            retry_config = RetryConfig(**retry_config)
        if circuit_breaker:
            circuit_breaker = CircuitBreaker(**circuit_breaker)
        if config:
            # The config never writes a grant down, but this reads a stored row
            # as it stands: one written by an older version, or by any other
            # writer, may still carry it. A grant belongs to the role of the
            # caller restoring the row, who hands it again if the role names it.
            kwargs = config.get("kwargs")
            if isinstance(kwargs, dict):
                config = {
                    **config,
                    "kwargs": {
                        k: v for k, v in kwargs.items() if k not in AUTHORIZATION_KWARG_NAMES
                    },
                }
            config = EndpointConfig(**config)

        return cls(
            config=config,
            circuit_breaker=circuit_breaker,
            retry_config=retry_config,
        )
