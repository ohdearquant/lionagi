# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, SecretStr, ValidationError

from lionagi.service._api_telemetry import claim_cache_source, mark_uncached_response
from lionagi.service.resilience import (
    APIClientError,
    CircuitBreaker,
    RetryConfig,
    _reraise_inner,
    retry_with_backoff,
)
from lionagi.service.types.stream_chunk import StreamChunk

from .endpoint_config import AUTHORIZATION_KWARG_NAMES, RUNTIME_STATE_NAMES, EndpointConfig
from .header_factory import HeaderFactory

_CONFIGURED_TIMEOUT = object()

# What ``from_dict`` never takes from a stored config: the credential source, the
# local-network opt-in and the kwargs a config never writes down. A setting named as a
# credential or a proxy is dropped too, by ``_names_credential_setting``.
_NOT_RESTORED = frozenset(
    (
        "api_key",
        "api_key_env",
        "allow_local_network",
        *AUTHORIZATION_KWARG_NAMES,
        *RUNTIME_STATE_NAMES,
    )
)

# Kwargs whose value is a set of headers; a restored one is filtered as default_headers is.
_HEADER_KWARGS = frozenset({"headers", "extra_headers"})


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


# Built on first use, as httpx is imported only when a request is made.
_REFUSED_REDIRECT_ERROR = None


def _refused_redirect_error():
    """The ``httpx.ConnectError`` a pinned client raises for a redirect off https."""
    global _REFUSED_REDIRECT_ERROR
    if _REFUSED_REDIRECT_ERROR is None:
        import httpx

        class RefusedRedirectError(httpx.ConnectError):
            """A redirect off https from a pinned request; sent again, it would be refused again."""

        _REFUSED_REDIRECT_ERROR = RefusedRedirectError
    return _REFUSED_REDIRECT_ERROR


def _refuse_a_redirect_off_https():
    """A request hook for a pinned client: once a request has gone over https, a redirect
    to plain http is refused.

    The pin is checked only on a TLS connection, so following such a redirect would send the
    request, body included, to a server the pin never checked. Hops over plain http before
    the first https one are left alone, as aiohttp left them. Each request gets a client of
    its own, so the hook sees the hops of one request.
    """
    https_seen = False

    async def hook(request):
        nonlocal https_seen
        if request.url.scheme == "https":
            https_seen = True
        elif https_seen:
            raise _refused_redirect_error()(
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
    ``EndpointConfig.timeout`` does, and a phase takes the smallest bound set on it:
    connecting is bounded by ``sock_connect``, ``connect`` and ``total``, reading by
    ``sock_read`` and ``total``, and waiting for a pooled connection by ``connect`` and
    ``total``. A value of zero or below is no timeout, as aiohttp read it, except a negative
    ``sock_read``: aiohttp timed every read out at once on one, so it is refused here.
    """
    import httpx

    sock_read = timeout.sock_read
    if isinstance(sock_read, (int, float)) and sock_read < 0:
        raise ValueError(
            f"sock_read={sock_read!r} would time every read out at once; "
            "pass None or 0 for no read timeout"
        )
    total = _seconds_or_none(timeout.total)

    def bound(*values):
        bounds = [v for v in (*map(_seconds_or_none, values), total) if v is not None]
        return min(bounds, default=None)

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
        if "timeout" in options:
            # A configured number of zero or below is no timeout, as the config's own is.
            options["timeout"] = _seconds_or_none(options["timeout"])
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

    def _require_api_key(self) -> None:
        if not self._account_routing_enabled():
            self.config._require_api_key()

    def _dispatch_headers(self, headers: dict) -> dict:
        if (
            self._account_routing_enabled()
            or self.config.auth_type == "none"
            or not self.config._api_key_reference()
        ):
            return headers
        credential_headers = HeaderFactory.get_header(
            auth_type=self.config.auth_type, content_type=None, api_key=self.config._api_key
        )
        preview_headers = HeaderFactory.get_header(
            auth_type=self.config.auth_type, content_type=None, api_key="dummy-key-for-testing"
        )
        reference_headers = HeaderFactory.get_header(
            auth_type=self.config.auth_type,
            content_type=None,
            api_key=self.config._api_key_reference(),
        )
        headers = dict(headers)
        for credential_name, value in credential_headers.items():
            names = [
                name
                for name in headers
                if isinstance(name, str) and name.lower() == credential_name.lower()
            ]
            if not names:
                headers[credential_name] = value
            for name in names:
                if headers[name] in (
                    preview_headers[credential_name],
                    reference_headers[credential_name],
                ):
                    headers[name] = value
        return headers

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
            api_key = self.config._api_key
            if not api_key and self.config._api_key_reference():
                api_key = "dummy-key-for-testing"
            headers = HeaderFactory.get_header(
                auth_type=self.config.auth_type,
                content_type=self.config.content_type,
                api_key=api_key,
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

        import httpx

        from lionagi.ln._ssrf import is_ssrf_safe

        # The host the HTTP client connects to, in its own ASCII encoding: a name outside
        # ASCII is encoded differently by the resolver (IDNA 2003 maps ``straße`` to
        # ``strasse``) than by the client (IDNA 2008, ``xn--strae-oqa``), so checking the
        # name as written would resolve a host the request never reaches. A URL the
        # client cannot read is checked as before; the client refuses to send it.
        try:
            hostname = httpx.URL(self.config.full_url).raw_host.decode("ascii")
        except (httpx.InvalidURL, UnicodeError):
            hostname = urlparse(self.config.full_url).hostname or ""
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
        cache_source = claim_cache_source()
        routed = self._account_routing_enabled()
        self._require_api_key()
        # Only a routed call hands the scope down: an unrouted endpoint's _call never
        # receives a keyword its signature may not take.
        scope_kwargs = {"_routing_scope": self._account_scope(_routing_scope)} if routed else {}
        use_cache = cache_control and not routed
        if not use_cache:
            mark_uncached_response(cache_source)
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

        headers = self._dispatch_headers(headers)
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
                mark_uncached_response(cache_source)
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
            try:
                response = await client.request(
                    method=self.config.method,
                    url=self.config.full_url,
                    headers=headers,
                    **body_kwargs,
                    **request_kwargs,
                )
            except _refused_redirect_error() as exc:
                # Sent again, the request would reach the pinned server and be refused again.
                raise _NonRetryableClientError(exc) from exc

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
        self._require_api_key()
        scope_kwargs = {"_routing_scope": self._account_scope(_routing_scope)} if routed else {}
        payload, headers = self.create_payload(request, extra_headers, **kwargs)
        headers = self._dispatch_headers(headers)
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
    def from_dict(
        cls,
        data: dict,
        *,
        base_url: str | None = None,
        api_key: str | SecretStr | None = None,
        allow_local_network: bool | None = None,
    ):
        """Rebuild an endpoint from ``to_dict`` output.

        The stored config never supplies the API key, the local-network opt-in, a grant
        or runtime state. The first two come from the keyword arguments here; a grant is
        handed to the live config again by the caller whose role names it. ``base_url``
        given here replaces the stored one. A stored header whose name reads as a
        credential or picks the server is dropped (see ``_without_credential_headers``),
        and of the stored client options only the headers are kept. A setting at the top
        level of the config or of its ``kwargs`` whose name reads as a credential or a
        proxy is dropped (see ``_names_credential_setting``); other stored ``kwargs`` are
        restored as written, a value nested inside one included. A user and password in
        the stored URL are removed, and a stored URL that is not in a form they can be
        removed from is refused (see ``_without_userinfo``). A stored config that does not
        validate is refused with the fields and reasons only, none of its values, and a
        stored ``retry_config`` or ``circuit_breaker`` that does not build is refused naming
        the part and the error's type only. Without the keyword arguments the config has no
        API key and the opt-in is off; ``iModel.from_dict`` then applies the provider's
        registration.
        """
        from lionagi.utils import to_dict

        data = to_dict(data, recursive=True)
        retry_config = data.get("retry_config")
        circuit_breaker = data.get("circuit_breaker")
        config = data.get("config")

        if retry_config:
            retry_config = _restored_part(RetryConfig, "retry_config", retry_config)
        if circuit_breaker:
            circuit_breaker = _restored_part(CircuitBreaker, "circuit_breaker", circuit_breaker)
        if config:
            # None of these is taken from a stored row. The config writes down only the
            # opt-in; a row written by an older version, or by any other writer, may
            # carry the rest. A name the config has no field for is moved into kwargs,
            # so both places are cleared. A grant belongs to the role of the caller
            # restoring the row, who hands it again if the role names it.
            # A name the config has no field for becomes a kwarg, which the request body
            # carries, so the setting rule and the header filter apply at the top level too.
            config = _restored_settings(config)
            kwargs = config.get("kwargs")
            if isinstance(kwargs, dict):
                # A base_url there is a copy no request reads, the config's own field being
                # the URL the client uses, so it is dropped with any user and password in it.
                # A stored set of headers kept there is filtered as default_headers is.
                config["kwargs"] = _restored_settings(kwargs, drop=frozenset({"base_url"}))
            # Nor does a stored header. The endpoint merges default_headers over the
            # credential header it writes, so a stored Authorization or x-api-key would
            # replace the restored key, at whatever URL the model is restored to. Of the
            # client options only the headers are kept, filtered the same way: the rest
            # set up this process's HTTP client, and some hand it a credential (auth,
            # proxy_auth, cookies) or let it read one here (trust_env reads .netrc).
            if isinstance(config.get("default_headers"), Mapping):
                config["default_headers"] = _without_credential_headers(config["default_headers"])
            client_kwargs = config.get("client_kwargs")
            if isinstance(client_kwargs, Mapping):
                config["client_kwargs"] = {
                    name: headers
                    for name, value in client_kwargs.items()
                    if name == "headers"
                    and (headers := _without_credential_headers(value)) is not None
                }
            # The stored URL keeps no user and password either: the HTTP client would
            # send them as Basic authentication to the host the URL names. A URL the
            # caller passes replaces the stored one unread.
            if base_url is not None:
                config["base_url"] = base_url
            elif isinstance(stored_url := _stored_base_url(config), str):
                config["base_url"] = _without_userinfo(stored_url)
            if api_key is not None:
                config["api_key"] = api_key
            if allow_local_network is not None:
                config["allow_local_network"] = allow_local_network
            # pydantic's message repeats the values it could not read, and a stored value
            # may be a password, so the refusal names each field and reason only. A
            # location past the field is a key of the stored value (``params.<key>``),
            # which is stored text too, so only the field is named. It is raised outside
            # the handler, so the original error is not attached to it.
            try:
                config = EndpointConfig(**config)
                problems = None
            except ValidationError as exc:
                problems = "; ".join(
                    f"{error['loc'][0] if error['loc'] else 'config'}: {error['msg']}"
                    for error in exc.errors(
                        include_url=False, include_context=False, include_input=False
                    )
                )
            except TypeError:
                # A name that is not a string, bytes say, which no field takes.
                problems = "a setting's name is not a string"
            if problems is not None:
                raise ValueError(
                    f"The stored endpoint config does not validate ({problems}). Its values "
                    "are left out of this message. Correct the stored config, or pass a live "
                    "model to Branch.from_dict (chat_model=, parse_model=)."
                )

        return cls(
            config=config,
            circuit_breaker=circuit_breaker,
            retry_config=retry_config,
        )


def _restored_part(part: type, field: str, stored: Any) -> Any:
    """``part`` built from the stored ``field``, or refused without the stored text.

    The constructor's message names an unknown stored key, which is stored text and may be
    a password, so the refusal names the field and the error's class only. It is raised
    outside the handler, so the original error is not attached to it.
    """
    try:
        return part(**stored)
    except (TypeError, ValueError) as exc:
        reason = type(exc).__name__
    raise ValueError(
        f"The stored {field} does not build ({reason}). Its names and values are left out "
        "of this message. Correct the stored config, or pass a live model to "
        "Branch.from_dict (chat_model=, parse_model=)."
    )


def _restored_settings(settings: Mapping, *, drop: frozenset[str] = frozenset()) -> dict:
    """``settings`` without what a restored config never takes from a stored row.

    That is a name in ``_NOT_RESTORED`` or ``drop`` and one ``_names_credential_setting``
    reads as a credential or a proxy, each test reading a bytes name as Latin-1, as the
    HTTP client reads it. A stored set of headers loses its credential headers (see
    ``_without_credential_headers``), and one that cannot be read for its names is dropped.
    """
    restored = {}
    for key, value in settings.items():
        name = bytes(key).decode("latin-1") if isinstance(key, (bytes, bytearray)) else key
        if name in _NOT_RESTORED or name in drop or _names_credential_setting(name):
            continue
        if name in _HEADER_KWARGS:
            value = _without_credential_headers(value)
            if value is None:
                continue
        restored[key] = value
    return restored


# Read in a name with its case, separators and camel-case run together, so ``X-AuthKey``,
# ``auth_key`` and ``authKey`` read alike. The stored settings and the stored headers are
# both tested against these; see ``_names_credential_setting``.
_CREDENTIAL_NAME_PARTS = (
    "secret",
    "password",
    "passwd",
    "credential",
    "bearer",
    "cookie",
    "proxy",
    "proxies",
    "authorization",
    "authorisation",
    "authentication",
    "authkey",
    "authtoken",
    "apikey",
    "accesskey",
    "privatekey",
    "sessionkey",
    "clientkey",
    "signingkey",
    "encryptionkey",
    "masterkey",
    "subscriptionkey",
    "licensekey",
)


def _names_credential_setting(name: Any) -> bool:
    """Whether a stored setting's or header's name reads as a credential or a proxy.

    The name is read with its case, separators and camel-case run together. It reads as a
    credential when it is ``auth``, ``authentication``, ``bearer`` or ``key``; begins with
    ``xauth`` (``X-Auth``, ``XAuth``, ``x_authentication``); ends in ``token``
    (``access_token``, ``accessToken``), so ``max_tokens`` and ``token_limit`` are kept; or
    holds one of ``_CREDENTIAL_NAME_PARTS``: ``Authorisation``, ``authentication_key``,
    ``api_key_id``, ``session_key``, ``clientKey``, ``Ocp-Apim-Subscription-Key``,
    ``client_secret``, ``Cookie2`` and a name holding ``proxy`` or ``proxies``, whose URL
    can hold a user and password. A setting otherwise named, ``author``, ``user``,
    ``prompt_cache_key`` or ``custom_key`` say, is kept. Headers are read by this rule and
    two more (see ``_without_credential_headers``). A bytes name is read as Latin-1, as the
    HTTP client reads it. Only the name is read, so a setting nested inside another keeps
    its names: a JSON schema field named ``token`` is sent.
    """
    from lionagi.libs.credential_fields import fold_field_name

    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("latin-1")
    if not isinstance(name, str):
        return False
    compact = fold_field_name(name).replace("_", "")
    return (
        compact in ("auth", "authentication", "bearer", "key")
        or compact.startswith("xauth")
        or compact.endswith("token")
        or any(part in compact for part in _CREDENTIAL_NAME_PARTS)
    )


def _without_credential_headers(headers: Any) -> Any:
    """``headers`` without each stored header a restored model must not send.

    That is a header whose name reads as a credential or a proxy as a stored setting's
    does (see ``_names_credential_setting``: ``Authorisation``, ``X-Auth``, ``AuthKey``,
    ``X-Session-Key``, ``Cookie2``) and three more: one with ``token`` anywhere in its name
    (``X-Token-Auth``), since no header counts tokens as ``max_tokens`` does; one whose
    last word is ``key`` (``X-Functions-Key``, ``X-Cache-Key``), since a header so named
    carries a key where a setting so named, ``custom_key``, may be the caller's own; and
    ``Host``, which would send the restored key to another virtual host at the same address.
    Only the name is read: a header named otherwise, ``X-Signature``, ``X-Monkey`` or
    ``Passphrase`` say, is kept. A bytes name is read as Latin-1, as the HTTP client reads
    it. A mapping and a list of name-value pairs are filtered, and an entry of the list that
    is not a pair is dropped. Any other value, such as a string of header lines, cannot be
    read for its names and is returned as ``None``, so no header from it is restored.
    """
    from lionagi.libs.credential_fields import fold_field_name

    def names_credential(name: Any) -> bool:
        if isinstance(name, (bytes, bytearray)):
            name = bytes(name).decode("latin-1")
        if not isinstance(name, str):
            return False
        folded = fold_field_name(name).strip("_")
        compact = folded.replace("_", "")
        return (
            _names_credential_setting(name)
            or compact == "host"
            or "token" in compact
            or folded.endswith("_key")
        )

    if isinstance(headers, Mapping):
        return {name: value for name, value in headers.items() if not names_credential(name)}
    if isinstance(headers, (list, tuple)):
        return [
            pair
            for pair in headers
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and not names_credential(pair[0])
        ]
    return None


_STORED_URL_REMEDY = (
    "Pass the URL to iModel.from_dict or Endpoint.from_dict (base_url=), pass a live "
    "model to Branch.from_dict (chat_model=, parse_model=), or correct the stored URL."
)

# Layers of percent-encoding a stored URL is read through before it counts as unreadable.
_URL_DECODE_PASSES = 16


def _stored_base_url(config: Mapping) -> Any:
    """The stored ``base_url`` as the config reads it: bytes are decoded as UTF-8, as the
    config's string field decodes them, and any other value is returned as it is. Bytes
    that do not decode are refused without their text, which may hold a password."""
    value = config.get("base_url")
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode()
        except UnicodeDecodeError:
            pass
        # Raised here, outside the handler, so the decode error and the bytes it holds
        # are not attached to the refusal.
        raise ValueError(
            "The stored base_url is bytes that do not decode as UTF-8, so it cannot be read "
            f"for a user and password and is not restored. {_STORED_URL_REMEDY}"
        )
    return value


def _without_userinfo(url: str) -> str:
    """``url`` without the ``user:password@`` part the HTTP client reads in it, or refused.

    A stored URL is restored only in this form, read from the stored text itself:
    ``http://`` or ``https://`` from its first character, an optional user and password
    running to the last ``@`` and holding no slash, backslash, ``?`` or ``#``, then a host
    name of letters, digits, ``-``, ``.`` and ``_`` (outside ASCII, a letter's combining
    marks count as letters, in the composed form, NFC, that the client accepts), or an IPv6
    address in brackets with an optional zone of letters, digits and ``-._~``, then an
    optional port of digits, which may be empty and is at most 65535, and no ``@`` anywhere
    once the user and password are removed. Text with no ``:`` and no ``@``, such as
    ``internal``, is returned as it is, since it has no place for a password. Anything else
    is refused, because which part of it is a password cannot be told and the config would
    write it back. The check for ``:`` and ``@`` reads the text
    with its percent-encoding removed and its compatibility characters folded until it
    stops changing, so ``%2540`` and a fullwidth ``＠`` count as an ``@``.
    """
    import re
    import unicodedata
    from urllib.parse import unquote

    import httpx

    def plain(text: str) -> str | None:
        # One pass past the limit, to see that the last layer read was the last one.
        for _ in range(_URL_DECODE_PASSES + 1):
            once = unquote(unicodedata.normalize("NFKC", text))
            if once == text:
                return text
            text = once
        # Still changing after that many layers: no client sends such a URL, and what
        # the layers hide cannot be told.
        return None

    def refused(reason: str) -> ValueError:
        return ValueError(
            f"The stored base_url {reason}, so it cannot be read for a user and password "
            f"and is not restored. {_STORED_URL_REMEDY}"
        )

    deep = f"is still percent-encoded after {_URL_DECODE_PASSES} layers are read"
    whole = plain(url)
    if whole is not None and ":" not in whole and "@" not in whole:
        return url
    if re.match(r"(?i:https?)://", url, re.ASCII) is None:
        raise refused(deep if whole is None else "does not begin with http:// or https://")
    # Every refusal is raised outside an exception handler: the client's error repeats
    # the text it could not read, and a refusal raised inside would carry it.
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, UnicodeError):
        parsed = None
    if parsed is None:
        raise refused("does not read as a URL the HTTP client can send")
    if not parsed.host:
        raise refused("has no host")
    # The form is read from the stored text, never from the client's normalized copy of
    # it: parsers end the host at different characters (a backslash ends it for a browser
    # and not for this client), so a check of the copy can pass a host the text does not
    # name. The user and password run to the last '@' before any slash, backslash, '?' or
    # '#', and a backslash before the host fails the form. This client ends the authority
    # at the first slash, '?' or '#' and takes the user and password to its last '@', so
    # it reads in a URL of this form the host the form reads.
    stored = re.fullmatch(
        r"(?i:https?)://"
        r"(?:[^/?#\\]+@)?"
        r"(?:\[[0-9A-Fa-f:.]+(?:%25[A-Za-z0-9._~-]+)?\]|(?P<name>[^\[\]:/?#@\\]+))"
        r"(?::(?P<port>[0-9]*))?"
        r"(?:[/?#].*)?",
        url,
        re.DOTALL | re.ASCII,
    )

    # The client has checked an IPv6 address already; a name it accepts may still hold
    # percent-encoding or other punctuation. Outside ASCII a letter can be written with a
    # combining mark, as in Devanagari, so a mark counts there as a letter does.
    def name_char(char: str) -> bool:
        if char.isascii():
            return char.isalnum() or char in "-._"
        return unicodedata.category(char)[0] in "LMN"

    if (
        stored is None
        or not (stored["name"] is None or all(name_char(char) for char in stored["name"]))
        or (stored["port"] and int(stored["port"]) > 65535)
    ):
        raise refused("has something other than a host name or IP address and a port number")
    restored = str(parsed.copy_with(userinfo=b"")) if parsed.userinfo else url
    after = plain(restored)
    if after is None:
        raise refused(deep)
    if "@" in after:
        raise refused("has an '@' after its host")
    return restored
