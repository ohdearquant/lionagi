# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Routed Anthropic request-path behavior."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager
from uuid import uuid4

import anyio
import httpx
import pytest

import lionagi.config as config_module
import lionagi.service.accounts._router as router_module
from lionagi.config import AppSettings
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionagi.service.accounts._credentials import CredentialSources
from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode, Scope
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import _branch_routing_context, iModel
from lionagi.service.resilience import RetryConfig
from lionagi.session.branch import Branch

KEY_NAME = "LIONAGI_ROUTER_CANARY"
CANARY = "sk-ant-api03-canary-0123456789"
# A key without the public prefix every Anthropic key starts with.
OTHER_KEY = "test-canary-key-0123456789"
MODEL = "claude-sonnet-5-5"
REQUEST = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "A short request"}],
    "max_tokens": 32,
}


class ByteStream:
    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = pieces

    def __aiter__(self):
        self._pieces = iter(self.pieces)
        return self

    async def __anext__(self):
        try:
            piece = next(self._pieces)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(piece, BaseException):
            raise piece
        return piece


class StubResponse:
    def __init__(self, status: int = 200, body: dict | None = None, pieces=None) -> None:
        self.status_code = status
        self.status = status
        self.body = body or {}
        self.content = ByteStream(list(pieces or []))
        self.closed = False
        self.headers = httpx.Headers({"content-type": "application/json"})

    def json(self):
        return self.body

    async def read(self):
        return json.dumps(self.body).encode()

    def release(self):
        self.closed = True

    async def aiter_bytes(self):
        async for piece in self.content:
            yield piece

    async def aiter_text(self):
        async for piece in self.aiter_bytes():
            yield piece.decode()


class StubTransport:
    def __init__(self, responses) -> None:
        self.responses = deque(responses)
        self.calls: list[dict] = []
        self.client_kwargs: list = []
        self.timeouts: list = []

    def session(self, client_kwargs=None, *, timeout=None):
        self.client_kwargs.append(client_kwargs)
        self.timeouts.append(timeout)
        return StubSession(self, timeout=timeout)


class StubSession:
    def __init__(self, transport: StubTransport, *, timeout=None) -> None:
        self.transport = transport
        self.timeout = httpx.Timeout(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def _request(self, method, url, **kwargs):
        content = kwargs.get("content")
        call = {"method": method, "url": url, **kwargs, "data": content}
        if content:
            try:
                call["json"] = json.loads(content)
            except (TypeError, ValueError):
                pass
        self.transport.calls.append(call)
        response = self.transport.responses.popleft()
        if isinstance(response, _FailingResponse):
            raise response.error
        return response

    async def request(self, method, url, **kwargs):
        return self._request(method, url, **kwargs)

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        response = self._request(method, url, **kwargs)
        try:
            yield response
        finally:
            response.closed = True


def _response(*, text: str = "A completed response", usage: dict | None = None) -> StubResponse:
    return StubResponse(
        body={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": MODEL,
            "usage": usage or {"input_tokens": 100, "output_tokens": 5},
        }
    )


def _install_accounts(monkeypatch, tmp_path, *, accounts=None, key=CANARY):
    declarations = accounts or [
        {"name": "primary", "key_env": KEY_NAME, "budget_usd": "20"},
        {"name": "secondary", "key_env": "LIONAGI_ROUTER_SECONDARY", "budget_usd": "20"},
    ]
    settings = AppSettings(_env_file=None, ANTHROPIC_ACCOUNTS=declarations)
    monkeypatch.setattr(config_module, "settings", settings)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for item in declarations:
        monkeypatch.setenv(item["key_env"], key)
    sources = CredentialSources(
        dotenv_files=(),
        dotenv_encoding="utf-8",
        lookup=CredentialSources.capture(settings, lookup_settings={}).lookup,
        account_key_names=frozenset(item["key_env"] for item in declarations),
    )
    path = tmp_path / "routed-ledger.sqlite3"
    router_module._reset_router_test_dependencies()
    router_module._set_router_test_dependencies(ledger_path=path, sources=sources)
    return path


def _install_unconfigured(monkeypatch, tmp_path):
    settings = AppSettings(_env_file=None, ANTHROPIC_ACCOUNTS=(), ANTHROPIC_API_KEY=CANARY)
    monkeypatch.setattr(config_module, "settings", settings)
    path = tmp_path / "unused-ledger.sqlite3"
    router_module._reset_router_test_dependencies()
    router_module._set_router_test_dependencies(ledger_path=path)
    return path


def _model(monkeypatch, responses) -> tuple[iModel, StubTransport]:
    model = iModel(provider="anthropic", model=MODEL)
    transport = StubTransport(responses)
    model.endpoint._create_http_session = transport.session
    model.endpoint._assert_ssrf_safe_url = lambda: None
    return model, transport


def _attempts(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT id, account, state, cost_nusd, outcome FROM attempt ORDER BY rowid"
        ).fetchall()


def _cleanup_router():
    router_module._reset_router_test_dependencies()


@pytest.fixture(autouse=True)
def _reset_router_after_test():
    yield
    _cleanup_router()


@pytest.mark.asyncio
async def test_unconfigured_messages_keep_the_direct_key_path(monkeypatch, tmp_path):
    path = _install_unconfigured(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    payload, headers = model.endpoint.create_payload(REQUEST)
    result = await model.endpoint.call(REQUEST)

    assert payload == REQUEST
    assert headers["x-api-key"] == CANARY
    assert headers["anthropic-version"] == "2023-06-01"
    assert transport.calls[0]["headers"] == headers
    assert transport.calls[0]["json"] == payload
    assert result["content"][0]["text"] == "A completed response"
    assert not path.exists()


@pytest.mark.asyncio
async def test_an_unrouted_request_keeps_its_idempotency_key_handling(monkeypatch, tmp_path):
    """Without accounts a key is minted unless a header is named exactly `Idempotency-Key`, as
    before routing existed; the handling of other spellings is for routed requests."""
    _install_unconfigured(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response(), _response()])
    model.endpoint.config.idempotent_retries = True

    await model.endpoint.call(REQUEST, extra_headers={"Idempotency-Key": "caller"})
    await model.endpoint.call(REQUEST, extra_headers={"idempotency-key": "caller"})

    exact, lower = (call["headers"] for call in transport.calls)
    assert exact["Idempotency-Key"] == "caller"
    assert lower["idempotency-key"] == "caller"
    assert lower["Idempotency-Key"] not in ("", "caller")


@pytest.mark.parametrize("accounts", [False, True])
@pytest.mark.asyncio
async def test_an_unrouted_endpoint_call_hands_its_call_no_new_keyword(
    monkeypatch, tmp_path, accounts
):
    # A subclass written before routing existed overrides _call without **kwargs.
    # Declared accounts route Anthropic Messages only, so this endpoint stays unrouted.
    if accounts:
        path = _install_accounts(monkeypatch, tmp_path)
    else:
        path = _install_unconfigured(monkeypatch, tmp_path)

    class PlainEndpoint(Endpoint):
        async def _call(self, payload, headers):
            return {"payload": payload}

    endpoint = PlainEndpoint(
        config=EndpointConfig(
            name="plain_chat",
            endpoint="chat",
            provider="openai",
            base_url="https://api.openai.com/v1",
            endpoint_params=["chat", "completions"],
            openai_compatible=True,
            auth_type="none",
        )
    )

    result = await endpoint.call({"model": "a-model", "messages": []})

    assert result["payload"]["model"] == "a-model"
    assert not path.exists()


@pytest.mark.asyncio
async def test_routed_invoke_prices_scrubs_and_skips_application_cache(
    monkeypatch, tmp_path, caplog
):
    path = _install_accounts(monkeypatch, tmp_path)
    first = _response(
        text=f"Echo {CANARY}",
        usage={
            "input_tokens": 100,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )
    second = _response(
        usage={
            "input_tokens": 100,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    )
    model, transport = _model(monkeypatch, [first, second])
    event = model.create_api_calling(**copy.deepcopy(REQUEST), cache_control=True)

    assert "x-api-key" not in {name.lower() for name in event.headers}
    assert CANARY not in repr(event)
    assert CANARY not in json.dumps(event.model_dump(), default=str)
    assert CANARY not in json.dumps(event.payload, default=str)
    assert event.payload["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    await event.invoke()
    second_result = await model.endpoint.call(
        event.payload,
        cache_control=True,
        skip_payload_creation=True,
        extra_headers=event.headers,
        _routing_scope=event._routing_scope,
    )

    assert transport.calls[0]["headers"]["x-api-key"] == CANARY
    assert transport.calls[0]["headers"]["Content-Type"] == "application/json"
    assert isinstance(transport.calls[0]["data"], bytes)
    wire_payload = json.loads(transport.calls[0]["data"])
    assert wire_payload["service_tier"] == "standard_only"
    assert wire_payload["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert CANARY not in transport.calls[0]["data"].decode()
    assert event.execution.response["content"][0]["text"] == "Echo [REDACTED]"
    assert CANARY not in repr(event)
    assert CANARY not in json.dumps(event.to_dict(), default=str)
    assert second_result["model"] == MODEL
    assert len(transport.calls) == 2
    rows = _attempts(path)
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1] == "primary"
    assert all(row[2] == "settled" and row[3] > 0 for row in rows), rows
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_configuration_conflicts_refuse_before_key_resolution(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    lookups = []
    monkeypatch.setattr(router_module, "resolve_account_key", lambda *args: lookups.append(args))

    models = [
        iModel(provider="anthropic", model=MODEL, api_key=CANARY),
        iModel(provider="anthropic", model=MODEL, base_url="https://custom.invalid/v1"),
        iModel(provider="anthropic", model=MODEL),
        iModel(provider="anthropic", model=MODEL),
        iModel(provider="anthropic", model=MODEL),
    ]
    models[2].endpoint.config.default_headers = {"X-API-KEY": CANARY}
    models[3].endpoint.config.client_kwargs = {"auth": object()}
    models[4].endpoint.config.client_kwargs = {"trace_configs": []}
    for model in models:
        with pytest.raises(AccountRoutingError) as caught:
            model.endpoint.create_payload(REQUEST)
        assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    with pytest.raises(AccountRoutingError) as caught:
        models[2].endpoint.create_payload({**REQUEST, "api_key": CANARY})
    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    # On a model with nothing else to refuse, so only the header can refuse it.
    plain = iModel(provider="anthropic", model=MODEL)
    for name in ("Authorization", "X_Api_Key"):
        with pytest.raises(AccountRoutingError) as caught:
            plain.endpoint.create_payload(REQUEST, extra_headers={name: "x"})
        assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    models.append(iModel(provider="anthropic", model=MODEL))
    models[-1].endpoint.config.client_kwargs = {"allow_redirects": True}
    with pytest.raises(AccountRoutingError) as caught:
        models[-1].endpoint.create_payload(REQUEST)
    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not lookups
    assert not path.exists()


@pytest.mark.parametrize("where", ["extra_headers", "default_headers", "client_headers"])
@pytest.mark.parametrize(
    "name", ["anthropic-beta", "Anthropic-Beta", "ANTHROPIC-BETA", "anthropic_beta"]
)
def test_a_beta_header_is_refused_before_key_resolution(monkeypatch, tmp_path, where, name):
    path = _install_accounts(monkeypatch, tmp_path)
    lookups = []
    monkeypatch.setattr(router_module, "resolve_account_key", lambda *args: lookups.append(args))
    model = iModel(provider="anthropic", model=MODEL)
    extra = None
    if where == "extra_headers":
        extra = {name: "context-management-2025-06-27"}
    elif where == "default_headers":
        model.endpoint.config.default_headers = {
            **model.endpoint.config.default_headers,
            name: "context-management-2025-06-27",
        }
    else:
        model.endpoint.config.client_kwargs = {"headers": {name: "files-api-2025-04-14"}}

    with pytest.raises(AccountRoutingError) as caught:
        model.endpoint.create_payload(REQUEST, extra_headers=extra)

    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert not lookups
    assert not path.exists()


@pytest.mark.parametrize(
    "headers",
    [
        {b"anthropic-beta": "context-management-2025-06-27"},
        {1: "x"},
        {"anthropic-version": 5},
        {"anthropic-version": b"5"},
        {"anthropic-version": "a\r\nx-api-key: b"},
        {"anthropic-version\n": "a"},
        {("anthropic-version", "a")},
        {"x bad": "1"},
        {"x:trace": "1"},
        {"anthropic-version ": "1"},
        {" anthropic-beta ": "files-api-2025-04-14"},
        {"": "1"},
        {"x-api-key:": "caller-fake"},
        "anthropic-version: 1",
        ["anthropic-version", "1"],
        [("anthropic-version", "1"), ("Anthropic-Version", "2")],
        [("anthropic_version", "1"), ("anthropic-version", "2")],
        {"Host": "attacker.example"},
        {"X-Forwarded-Host": "attacker.example"},
        {"x-trace": "1"},
        {"anthropic_version": "2023-06-01"},
        {"Idempotency-\u212aey": "x"},
    ],
    ids=[
        "bytes-name",
        "int-name",
        "int-value",
        "bytes-value",
        "line-break-in-value",
        "line-break-in-name",
        "set-of-pairs",
        "space-in-name",
        "colon-in-name",
        "trailing-space-in-name",
        "spaced-beta-name",
        "empty-name",
        "auth-name-with-colon",
        "a-string",
        "list-of-strings",
        "repeated-name",
        "repeated-name-with-underscore",
        "host",
        "forwarded-host",
        "unlisted-name",
        "underscore-spelling-of-a-listed-name",
        "kelvin-sign-in-a-listed-name",
    ],
)
@pytest.mark.parametrize(
    "where", ["extra_headers", "prebuilt_headers", "default_headers", "client_headers"]
)
@pytest.mark.asyncio
async def test_headers_a_routed_call_will_not_send_are_refused_before_a_reservation(
    monkeypatch, tmp_path, headers, where
):
    path = _install_accounts(monkeypatch, tmp_path)
    lookups = []
    monkeypatch.setattr(router_module, "resolve_account_key", lambda *args: lookups.append(args))
    model, transport = _model(monkeypatch, [_response()])

    with pytest.raises(AccountRoutingError) as caught:
        if where == "extra_headers":
            await model.endpoint.call(REQUEST, extra_headers=headers)
        elif where == "prebuilt_headers":
            payload, _ = model.endpoint.create_payload(REQUEST)
            await model.endpoint.call(payload, skip_payload_creation=True, extra_headers=headers)
        else:
            if where == "default_headers":
                model.endpoint.config.default_headers = headers
            else:
                model.endpoint.config.client_kwargs = {"headers": headers}
            await model.endpoint.call(REQUEST)

    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not lookups
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.parametrize(
    ("pair", "code"),
    [
        (("anthropic-version", "2023-01-01"), None),
        (("anthropic-beta", "files-api-2025-04-14"), RouterErrorCode.UNSUPPORTED_PRICING),
        (("Authorization", "x"), RouterErrorCode.CREDENTIAL_CONFLICT),
    ],
    ids=["plain", "beta", "auth"],
)
@pytest.mark.parametrize(
    "where", ["prebuilt_headers", "extra_headers", "default_headers", "client_headers"]
)
@pytest.mark.asyncio
async def test_a_list_of_header_pairs_is_read_pair_by_pair(
    monkeypatch, tmp_path, pair, code, where
):
    path = _install_accounts(monkeypatch, tmp_path)
    lookups = []
    real_resolve = router_module.resolve_account_key

    async def resolve(*args):
        lookups.append(args)
        return await real_resolve(*args)

    monkeypatch.setattr(router_module, "resolve_account_key", resolve)
    model, transport = _model(monkeypatch, [_response()])
    headers = [pair]

    async def send():
        if where == "prebuilt_headers":
            payload, _ = model.endpoint.create_payload(REQUEST)
            return await model.endpoint.call(
                payload, skip_payload_creation=True, extra_headers=headers
            )
        if where == "extra_headers":
            return await model.endpoint.call(REQUEST, extra_headers=headers)
        if where == "default_headers":
            model.endpoint.config.default_headers = headers
        else:
            model.endpoint.config.client_kwargs = {"headers": headers}
        return await model.endpoint.call(REQUEST)

    if code is not None:
        with pytest.raises(AccountRoutingError) as caught:
            await send()
        assert caught.value.code is code
        assert not lookups
        assert not transport.calls
        assert not path.exists() or not _attempts(path)
        return

    result = await send()

    assert result["model"] == MODEL
    if where == "client_headers":
        assert transport.client_kwargs == [{"headers": [pair]}]
        assert transport.calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    else:
        assert transport.calls[0]["headers"]["anthropic-version"] == "2023-01-01"
    assert _attempts(path)[0][2] == "settled"


@pytest.mark.parametrize("where", ["prebuilt_headers", "client_headers"])
@pytest.mark.asyncio
async def test_a_header_item_that_is_not_a_pair_is_refused(monkeypatch, tmp_path, where):
    # Defaults are built into the call's headers with dict.update, which refuses this shape on
    # every path, so only the two places that carry it as given are routed cases.
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    headers = [("anthropic-version", "a", "b")]

    with pytest.raises(AccountRoutingError) as caught:
        if where == "prebuilt_headers":
            payload, _ = model.endpoint.create_payload(REQUEST)
            await model.endpoint.call(payload, skip_payload_creation=True, extra_headers=headers)
        else:
            model.endpoint.config.client_kwargs = {"headers": headers}
            await model.endpoint.call(REQUEST)

    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


class _ItemsDisagree(Mapping):
    """A mapping whose keys and items name different headers."""

    def __init__(self, keys, items) -> None:
        self._keys = list(keys)
        self._items = list(items)

    def __getitem__(self, name):
        return dict(self._items)[name]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def items(self):
        return list(self._items)


@pytest.mark.parametrize("where", ["prebuilt_headers", "client_headers"])
@pytest.mark.asyncio
async def test_a_mapping_of_headers_is_read_by_its_items(monkeypatch, tmp_path, where):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    headers = _ItemsDisagree(["x-other"], [("anthropic-beta", "files-api-2025-04-14")])

    with pytest.raises(AccountRoutingError) as caught:
        if where == "prebuilt_headers":
            payload, _ = model.endpoint.create_payload(REQUEST)
            await model.endpoint.call(payload, skip_payload_creation=True, extra_headers=headers)
        else:
            model.endpoint.config.client_kwargs = {"headers": headers}
            await model.endpoint.call(REQUEST)

    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.parametrize("stream", [False, True], ids=["call", "stream"])
@pytest.mark.asyncio
async def test_a_header_set_while_the_account_is_reserved_is_not_sent(
    monkeypatch, tmp_path, stream
):
    from lionagi.service.accounts._ledger import CredentialLedger

    path = _install_accounts(monkeypatch, tmp_path)
    if stream:
        events = [_MESSAGE_START, _PARTIAL_TEXT, {"type": "message_stop"}]
        model, transport = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    else:
        model, transport = _model(monkeypatch, [_response()])
    call_headers = {"anthropic-version": "2023-01-01"}
    client_headers = {"idempotency-key": "client-1"}
    model.endpoint.config.client_kwargs = {"headers": client_headers}
    # The header mapping the attempt is handed. The attempt copies it, and reads the client's,
    # before it reserves an account, so neither change made below while the account is
    # reserved may reach the wire.
    held = []
    name = "_routed_stream_attempt" if stream else "_request_attempt"
    attempt = getattr(model.endpoint, name)

    def hold(*args, **kwargs):
        held.append(kwargs["headers"] if "headers" in kwargs else args[1])
        return attempt(*args, **kwargs)

    monkeypatch.setattr(model.endpoint, name, hold)
    real_reserve = CredentialLedger.reserve

    async def reserve_then_change(self, *args, **kwargs):
        # another task changes both while the account is being reserved
        for headers in (*held, client_headers):
            headers["anthropic-beta"] = "files-api-2025-04-14"
            headers["Authorization"] = "Bearer late"
        return await real_reserve(self, *args, **kwargs)

    monkeypatch.setattr(CredentialLedger, "reserve", reserve_then_change)

    if stream:
        async for _ in model.endpoint.stream(REQUEST, extra_headers=call_headers):
            pass
    else:
        payload, _ = model.endpoint.create_payload(REQUEST)
        await model.endpoint.call(payload, skip_payload_creation=True, extra_headers=call_headers)

    assert len(held) == 1
    sent = transport.calls[0]["headers"]
    assert sent["anthropic-version"] == "2023-01-01"
    assert not {name.lower() for name in sent} & {"anthropic-beta", "authorization"}
    assert transport.client_kwargs == [{"headers": [("idempotency-key", "client-1")]}]
    assert len(_attempts(path)) == 1


@pytest.mark.parametrize("stream", [False, True], ids=["call", "stream"])
@pytest.mark.asyncio
async def test_a_url_method_or_timeout_set_while_the_account_is_reserved_is_not_used(
    monkeypatch, tmp_path, stream
):
    from lionagi.service.accounts._ledger import CredentialLedger

    path = _install_accounts(monkeypatch, tmp_path)
    if stream:
        events = [_MESSAGE_START, _PARTIAL_TEXT, {"type": "message_stop"}]
        model, transport = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    else:
        model, transport = _model(monkeypatch, [_response()])
    config = model.endpoint.config
    timeout = config.timeout
    real_reserve = CredentialLedger.reserve

    async def reserve_then_change(self, *args, **kwargs):
        # another task points the endpoint elsewhere while the account is being reserved
        config.base_url = "https://attacker.invalid/v1"
        config.endpoint = "other"
        config.method = "PUT"
        config.timeout = timeout + 7
        return await real_reserve(self, *args, **kwargs)

    monkeypatch.setattr(CredentialLedger, "reserve", reserve_then_change)

    if stream:
        async for _ in model.endpoint.stream(REQUEST):
            pass
    else:
        await model.endpoint.call(REQUEST)

    (sent,) = transport.calls
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["method"] == "POST"
    assert transport.timeouts == [timeout]
    assert len(_attempts(path)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "messages/batches"},
        {"base_url": "https://api.anthropic.com/v1/"},
        {"method": "GET"},
        {"client_kwargs": {"connector": object()}},
        {"client_kwargs": {"cookies": {"session": "x"}}},
        {"client_kwargs": {"trust_env": True}},
        {"client_kwargs": {"timeout": 5}},
        {
            "client_kwargs": {
                "headers": {"anthropic-version": "2023-06-01"},
                "connector_owner": False,
            }
        },
    ],
    ids=[
        "other-path",
        "trailing-slash",
        "get",
        "connector",
        "cookies",
        "trust-env",
        "timeout",
        "headers-and-another",
    ],
)
def test_a_target_or_a_client_option_other_than_headers_is_refused_before_key_resolution(
    monkeypatch, tmp_path, change
):
    path = _install_accounts(monkeypatch, tmp_path)
    lookups = []
    monkeypatch.setattr(router_module, "resolve_account_key", lambda *args: lookups.append(args))
    model = iModel(provider="anthropic", model=MODEL)
    for name, value in change.items():
        setattr(model.endpoint.config, name, value)

    with pytest.raises(AccountRoutingError) as caught:
        model.endpoint.create_payload(REQUEST)

    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not lookups
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_list_of_header_pairs_takes_an_idempotency_key(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    model.endpoint.config.idempotent_retries = True
    payload, _ = model.endpoint.create_payload(REQUEST)

    await model.endpoint.call(
        payload, skip_payload_creation=True, extra_headers=[("anthropic-version", "2023-01-01")]
    )

    (sent,) = transport.calls
    assert sent["headers"]["anthropic-version"] == "2023-01-01"
    assert sent["headers"]["Idempotency-Key"]
    assert len(_attempts(path)) == 1


@pytest.mark.asyncio
async def test_a_default_header_outside_the_list_is_refused_on_a_prebuilt_call(
    monkeypatch, tmp_path
):
    """A prebuilt call sends none of the endpoint's default headers and is refused all the same."""
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    payload, _ = model.endpoint.create_payload(REQUEST)
    model.endpoint.config.default_headers = {"Host": "attacker.example"}

    with pytest.raises(AccountRoutingError) as caught:
        await model.endpoint.call(payload, skip_payload_creation=True)

    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.asyncio
async def test_a_session_is_opened_with_the_timeout_it_is_given(monkeypatch, tmp_path):
    _install_accounts(monkeypatch, tmp_path)
    endpoint = iModel(provider="anthropic", model=MODEL).endpoint
    endpoint.config.timeout = 30

    async with endpoint._create_http_session({}, timeout=5) as session:
        assert session.timeout.connect == 5
        assert session.timeout.read == 5
    async with endpoint._create_http_session({}) as session:
        assert session.timeout.connect == 30
        assert session.timeout.read == 30


@pytest.mark.asyncio
async def test_a_callers_header_replaces_the_default_of_the_same_name(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    await model.endpoint.call(
        REQUEST,
        extra_headers={"content-type": "application/json", "Anthropic-Version": "2023-01-01"},
    )

    (sent,) = transport.calls
    names = [name.lower() for name in sent["headers"]]
    assert names.count("anthropic-version") == 1
    assert names.count("content-type") == 1
    assert sent["headers"]["Anthropic-Version"] == "2023-01-01"
    assert _attempts(path)[0][2] == "settled"


@pytest.mark.asyncio
async def test_a_callers_idempotency_key_is_the_one_sent(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    model.endpoint.config.idempotent_retries = True

    await model.endpoint.call(REQUEST, extra_headers={"idempotency-key": "caller-key"})

    (sent,) = transport.calls
    keys = [value for name, value in sent["headers"].items() if name.lower() == "idempotency-key"]
    assert keys == ["caller-key"]
    assert len(_attempts(path)) == 1


@pytest.mark.parametrize("value", ["", "  "], ids=["empty", "blank"])
@pytest.mark.asyncio
async def test_an_empty_idempotency_key_is_replaced_by_a_minted_one(monkeypatch, tmp_path, value):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    model.endpoint.config.idempotent_retries = True

    await model.endpoint.call(REQUEST, extra_headers={"idempotency-key": value})

    (sent,) = transport.calls
    keys = [value for name, value in sent["headers"].items() if name.lower() == "idempotency-key"]
    assert len(keys) == 1
    assert keys[0].strip()
    assert len(_attempts(path)) == 1


@pytest.mark.asyncio
async def test_a_call_header_replaces_a_client_header_of_the_same_name_on_the_wire(
    monkeypatch, tmp_path
):
    """HTTPX sends the request header in place of a client header with the same name."""

    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    model.endpoint.config.client_kwargs = {"headers": {"anthropic-version": "2023-01-01"}}

    await model.endpoint.call(REQUEST, extra_headers={"Anthropic-Version": "2023-02-02"})

    (client_kwargs,) = transport.client_kwargs
    (sent,) = transport.calls
    wire_headers = []
    async with httpx.AsyncClient(
        **client_kwargs,
        transport=httpx.MockTransport(
            lambda request: (
                wire_headers.append(request.headers)
                or httpx.Response(200, json={}, request=request)
            )
        ),
    ) as client:
        await client.post("https://api.example.test", headers=sent["headers"])
    assert wire_headers[0].get_list("anthropic-version") == ["2023-02-02"]


@pytest.mark.asyncio
async def test_an_idempotency_key_in_the_client_options_is_replaced_on_the_wire(
    monkeypatch, tmp_path
):
    """A client-level key would be shared by every request, so each request's minted key is
    sent in its place."""
    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    model.endpoint.config.idempotent_retries = True
    model.endpoint.config.client_kwargs = {"headers": {"idempotency-key": "client-1"}}

    await model.endpoint.call(REQUEST)

    (client_kwargs,) = transport.client_kwargs
    (sent,) = transport.calls
    wire_headers = []
    async with httpx.AsyncClient(
        **client_kwargs,
        transport=httpx.MockTransport(
            lambda request: (
                wire_headers.append(request.headers)
                or httpx.Response(200, json={}, request=request)
            )
        ),
    ) as client:
        await client.post("https://api.example.test", headers=sent["headers"])
    (key,) = wire_headers[0].get_list("idempotency-key")
    assert key.strip()
    assert key != "client-1"


@pytest.mark.asyncio
async def test_auth_failure_blocks_the_bound_account_without_retrying(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(401, {"error": CANARY})])

    with pytest.raises(AccountRoutingError) as caught:
        await model.endpoint.call(REQUEST)

    assert caught.value.code is RouterErrorCode.AUTH_FAILED
    assert CANARY not in str(caught.value)
    assert len(transport.calls) == 1
    assert len(_attempts(path)) == 1
    with sqlite3.connect(path) as connection:
        reason = connection.execute(
            "SELECT block_reason FROM account WHERE name = 'primary'"
        ).fetchone()[0]
    assert reason == "auth_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, RouterErrorCode.CREDENTIAL_MISSING),
        # Below the minimum length, so refused as invalid; distinctive, so a leak is found.
        ("kQ7zXw3", RouterErrorCode.CREDENTIAL_INVALID),
    ],
    ids=["missing", "invalid"],
)
async def test_a_key_refusal_names_the_bound_account_and_its_unsent_attempt(
    monkeypatch, tmp_path, value, code
):
    path = _install_accounts(
        monkeypatch,
        tmp_path,
        accounts=[{"name": "primary", "key_env": KEY_NAME, "budget_usd": "20"}],
    )
    if value is None:
        monkeypatch.delenv(KEY_NAME)
    else:
        monkeypatch.setenv(KEY_NAME, value)
    model, transport = _model(monkeypatch, [])

    with pytest.raises(AccountRoutingError) as caught:
        await model.endpoint.call(REQUEST)

    error = caught.value
    assert error.code is code
    assert error.account == "primary"
    assert error.http_status is None
    assert str(error) == f"{code.value} account=primary attempt={error.attempt_id}"
    assert KEY_NAME not in str(error)
    # Only the code-only refusal it replaced is linked to it, and no link holds the value.
    linked = _every_linked_error(error)
    assert all(isinstance(each, AccountRoutingError) for each in linked)
    if value is not None:
        assert not any(value in repr(each.args) for each in linked)
        # Nor does any library frame those errors keep alive: a refusal's traceback holds
        # the locals of the frames that raised it.
        assert not any(value in repr(local) for local in _library_frame_locals(linked))
    assert transport.calls == []
    rows = _attempts(path)
    assert [(row[0], row[1], row[4]) for row in rows] == [
        (str(error.attempt_id), "primary", "not_sent")
    ]


@pytest.mark.asyncio
async def test_rate_limit_retries_with_a_new_reservation(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(429), _response()])
    model.endpoint.config.max_retries = 2

    result = await model.endpoint.call(REQUEST)

    assert result["model"] == MODEL
    assert len(transport.calls) == 2
    rows = _attempts(path)
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    assert rows[0][2:] == ("open", None, "rate_limited")
    assert rows[1][2] == "settled", rows


@pytest.mark.asyncio
async def test_retry_config_uses_the_same_per_send_reservation(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(429), _response()])
    model.endpoint.retry_config = RetryConfig(max_retries=1, base_delay=0.01, jitter=False)

    await model.endpoint.call(REQUEST)

    assert len(transport.calls) == 2
    rows = _attempts(path)
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    assert rows[0][4] == "rate_limited"
    assert rows[1][2] == "settled", rows


@pytest.mark.asyncio
async def test_other_client_errors_are_not_retried_and_unknown_usage_keeps_the_ceiling(
    monkeypatch, tmp_path
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(400, {"error": CANARY})])

    with pytest.raises(AccountRoutingError) as caught:
        await model.endpoint.call(REQUEST)
    assert caught.value.code is RouterErrorCode.REQUEST_REJECTED
    assert CANARY not in str(caught.value)
    assert len(transport.calls) == 1

    model, transport = _model(
        monkeypatch,
        [StubResponse(body={"id": "msg_unknown", "model": MODEL, "usage": {"input_tokens": 3}})],
    )
    await model.endpoint.call(REQUEST)
    row = _attempts(path)[-1]
    assert row[2] == "open" and row[3] is None and row[4] == "usage_incomplete"


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"speed": "fast"}, "unpriced_speed"),
        ({"cache_storage_hours": 1}, "unknown_usage_key_cache_storage_hours"),
    ],
    ids=["fast_speed", "unknown_field"],
)
@pytest.mark.asyncio
async def test_a_response_left_unpriced_settles_with_the_reason(
    monkeypatch, tmp_path, extra, reason
):
    path = _install_accounts(monkeypatch, tmp_path)
    usage = {"input_tokens": 3, "output_tokens": 2, **extra}
    model, transport = _model(
        monkeypatch, [StubResponse(body={"id": "msg_unpriced", "model": MODEL, "usage": usage})]
    )
    await model.endpoint.call(REQUEST)
    assert _attempts(path)[-1][2:] == ("open", None, reason)


@pytest.mark.asyncio
async def test_stream_replaces_usage_fields_and_masks_a_split_secret(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream",
                "model": MODEL,
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": CANARY},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    encoded = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    split = encoded.index(CANARY.encode()) + 9
    response = StubResponse(pieces=[encoded[:split], encoded[split:]])
    model, transport = _model(monkeypatch, [response])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert any(chunk.content == "[REDACTED]" for chunk in chunks)
    assert all(CANARY not in repr(chunk) for chunk in chunks)
    assert len(transport.calls) == 1
    wire_payload = json.loads(transport.calls[0]["data"])
    assert wire_payload["stream"] is True
    assert wire_payload["service_tier"] == "standard_only"
    row = _attempts(path)[0]
    assert row[2] == "settled" and row[3] == 250_000, row


@pytest.mark.asyncio
async def test_stream_retries_before_yield_with_a_new_reservation(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream_retry",
                "model": MODEL,
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    pieces = [b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)]
    model, transport = _model(monkeypatch, [StubResponse(429), StubResponse(pieces=pieces)])
    model.endpoint.retry_config = RetryConfig(max_retries=1, base_delay=0.01, jitter=False)

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert chunks
    assert len(transport.calls) == 2
    rows = _attempts(path)
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    assert rows[0][4] == "rate_limited"
    assert rows[1][2] == "settled", rows


def _stream_pieces() -> list[bytes]:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream_key",
                "model": MODEL,
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return [b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)]


def _sent_keys(call: dict) -> list[str]:
    return [value for name, value in call["headers"].items() if name.lower() == "idempotency-key"]


@pytest.mark.asyncio
async def test_a_routed_stream_sends_one_idempotency_key_on_every_attempt(monkeypatch, tmp_path):
    """A retried stream is the same logical request, so the provider sees one key."""
    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(
        monkeypatch, [StubResponse(429), StubResponse(pieces=_stream_pieces())]
    )
    model.endpoint.config.idempotent_retries = True
    model.endpoint.retry_config = RetryConfig(max_retries=1, base_delay=0.01, jitter=False)

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert chunks
    first, second = (_sent_keys(call) for call in transport.calls)
    assert len(first) == 1 and first[0].strip()
    assert second == first


@pytest.mark.asyncio
async def test_a_routed_stream_sends_a_callers_idempotency_key(monkeypatch, tmp_path):
    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(pieces=_stream_pieces())])
    model.endpoint.config.idempotent_retries = True

    chunks = [
        chunk
        async for chunk in model.endpoint.stream(
            REQUEST, extra_headers={"idempotency-key": "caller-key"}
        )
    ]

    assert chunks
    (sent,) = transport.calls
    assert _sent_keys(sent) == ["caller-key"]


@pytest.mark.parametrize(
    "texts",
    [
        [CANARY[start : start + 5] for start in range(0, len(CANARY), 5)],
        ["see " + CANARY[3:20] + " here"],
    ],
    ids=["split across blocks", "a run in one block"],
)
@pytest.mark.asyncio
async def test_a_response_masks_the_key_in_the_text_its_blocks_join(monkeypatch, tmp_path, texts):
    """A routed response masks a key split across its text blocks, and a run inside one."""
    _install_accounts(monkeypatch, tmp_path)
    body = _response().body
    body["content"] = [{"type": "text", "text": text} for text in texts]
    model, _ = _model(monkeypatch, [StubResponse(body=body)])

    result = await model.endpoint.call(REQUEST)

    joined = "".join(block["text"] for block in result["content"])
    assert "[REDACTED]" in joined
    for start in range(len(CANARY) - 7):
        assert CANARY[start : start + 8] not in joined


def _walked_strings(value) -> list[str]:
    """Every string in ``value``, an object's names included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found = [name for name in value if isinstance(name, str)]
        for item in value.values():
            found.extend(_walked_strings(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_walked_strings(item))
        return found
    return []


def _masked_in_every_string(result) -> None:
    """No run of the key survives anywhere in the body, and a mask is in it."""
    for text in _walked_strings(result):
        assert _no_run_of_the_key(text, CANARY), text
    assert "[REDACTED]" in " ".join(_walked_strings(result))


async def _routed_call_on_content(monkeypatch, tmp_path, content):
    _install_accounts(monkeypatch, tmp_path)
    body = _response().body
    body["content"] = content
    model, _ = _model(monkeypatch, [StubResponse(body=body)])
    return await model.endpoint.call(REQUEST)


_RUN_IN_OTHER_STRINGS = {
    "tool_use input": lambda run: {
        "type": "tool_use",
        "name": "lookup",
        "input": {"query": run},
    },
    "nested list in input": lambda run: {
        "type": "tool_use",
        "name": "lookup",
        "input": {"items": [{"value": run}]},
    },
    "tool_result content text": lambda run: {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": [{"type": "text", "text": run}],
    },
    "mcp_tool_result content text": lambda run: {
        "type": "mcp_tool_result",
        "content": [{"type": "text", "text": run}],
    },
    "citation title": lambda run: {
        "type": "text",
        "text": "quoted",
        "citations": [{"type": "char_location", "cited_text": "quote", "title": run}],
    },
}


@pytest.mark.parametrize("place", list(_RUN_IN_OTHER_STRINGS), ids=list(_RUN_IN_OTHER_STRINGS))
@pytest.mark.asyncio
async def test_a_response_masks_a_key_run_in_any_other_string(monkeypatch, tmp_path, place):
    run = CANARY[4:20]
    content = [{"type": "text", "text": "before"}, _RUN_IN_OTHER_STRINGS[place](run)]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert "[REDACTED]" in json.dumps(result)


@pytest.mark.asyncio
async def test_a_response_masks_a_key_run_in_a_bare_string_item(monkeypatch, tmp_path):
    run = CANARY[4:20]

    result = await _routed_call_on_content(monkeypatch, tmp_path, ["plain", run])

    _masked_in_every_string(result)
    assert result["content"] == ["plain", "[REDACTED]"]


@pytest.mark.asyncio
async def test_a_response_masks_a_run_split_across_a_text_block_and_a_bare_string(
    monkeypatch,
    tmp_path,
):
    """Each piece holds six characters of the key, so neither is a run on its own and
    only the join of the two text pieces holds one."""
    run = CANARY[4:20]
    content = [{"type": "text", "text": run[:6]}, run[6:]]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert result["content"][0]["text"] == "[REDACTED]"
    assert result["content"][1] == "[REDACTED]"


@pytest.mark.asyncio
async def test_a_response_masks_a_key_run_when_its_content_is_one_block(monkeypatch, tmp_path):
    run = CANARY[4:20]

    result = await _routed_call_on_content(
        monkeypatch, tmp_path, {"type": "text", "text": "see " + run + " here"}
    )

    _masked_in_every_string(result)
    assert result["content"]["text"] == "see [REDACTED] here"


@pytest.mark.asyncio
async def test_a_response_leaves_text_sharing_short_pieces_with_the_key(
    monkeypatch,
    tmp_path,
):
    text = "start " + CANARY[4:11] + " end"

    result = await _routed_call_on_content(monkeypatch, tmp_path, [{"type": "text", "text": text}])

    assert result["content"][0]["text"] == text
    assert "[REDACTED]" not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_response_masks_a_run_that_only_its_text_blocks_join_holds(
    monkeypatch,
    tmp_path,
):
    """Each text block holds six characters of the key, separated by a thinking block:
    only the join of the text pieces holds a run, not the join of every piece."""
    first, second = CANARY[10:16], CANARY[16:22]
    content = [
        {"type": "text", "text": first},
        {"type": "thinking", "thinking": "junk"},
        {"type": "text", "text": second},
    ]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert result["content"][0]["text"] == "[REDACTED]"
    assert result["content"][2]["text"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_a_response_masks_a_run_split_across_two_citations(monkeypatch, tmp_path):
    """Each citation holds six characters of the key, so neither is a run on its own
    and only the join of the quoted texts holds one."""
    first, second = CANARY[6:12], CANARY[12:18]
    content = [
        {"type": "text", "text": "one", "citations": [{"cited_text": first, "title": "t1"}]},
        {"type": "text", "text": "two", "citations": [{"cited_text": second, "title": "t2"}]},
    ]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert result["content"][0]["citations"][0]["cited_text"] == "[REDACTED]"
    assert result["content"][1]["citations"][0]["cited_text"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_a_response_masks_a_key_run_in_a_names_own_text(monkeypatch, tmp_path):
    """A tool input's object name holds a run of the key: the name is replaced by its
    masked text and the value it held stays under it."""
    run = CANARY[4:20]
    content = [{"type": "tool_use", "name": "lookup", "input": {run: "kept"}}]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert result["content"][0]["input"] == {"[REDACTED]": "kept"}


@pytest.mark.asyncio
async def test_a_response_keeps_both_values_when_its_masked_name_is_taken(
    monkeypatch,
    tmp_path,
):
    """A name that masks to one already in the object is numbered, so neither value is
    lost to the other."""
    run = CANARY[4:20]
    content = [
        {
            "type": "tool_use",
            "name": "lookup",
            "input": {run: "run value", "[REDACTED]": "other value"},
        }
    ]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert result["content"][0]["input"] == {
        "[REDACTED]": "other value",
        "[REDACTED] (2)": "run value",
    }


@pytest.mark.asyncio
async def test_a_renamed_name_keeps_its_place_in_the_object(monkeypatch, tmp_path):
    """A name replaced by its masked text stays where its entry was, so the order of the
    object's other entries is unchanged."""
    run = CANARY[4:20]
    content = [{"type": "tool_use", "name": "lookup", "input": {"first": 1, run: 2, "third": 3}}]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert list(result["content"][0]["input"]) == ["first", "[REDACTED]", "third"]
    assert result["content"][0]["input"] == {"first": 1, "[REDACTED]": 2, "third": 3}


@pytest.mark.parametrize("value", [123456789, 123456789.0], ids=["int", "float"])
@pytest.mark.asyncio
async def test_a_response_masks_a_key_run_in_a_numbers_own_text(monkeypatch, tmp_path, value):
    """A number whose text holds a run of the key is replaced by the masked text of that
    number."""
    expected = "[REDACTED]" if isinstance(value, int) else "[REDACTED].0"
    content = [{"type": "tool_use", "name": "lookup", "input": {"count": value}}]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    _masked_in_every_string(result)
    assert result["content"][0]["input"]["count"] == expected


@pytest.mark.asyncio
async def test_a_response_leaves_a_name_or_a_number_with_no_run_alone(monkeypatch, tmp_path):
    content = [
        {
            "type": "tool_use",
            "name": "lookup",
            "input": {"query": "a question", "count": 42, "ratio": 1.5},
        }
    ]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    assert result["content"][0]["input"] == {"query": "a question", "count": 42, "ratio": 1.5}
    assert isinstance(result["content"][0]["input"]["count"], int)
    assert isinstance(result["content"][0]["input"]["ratio"], float)


@pytest.mark.asyncio
async def test_a_response_leaves_a_bool_and_none_alone(monkeypatch, tmp_path):
    content = [{"type": "tool_use", "name": "lookup", "input": {"ok": True, "missing": None}}]

    result = await _routed_call_on_content(monkeypatch, tmp_path, content)

    assert result["content"][0]["input"] == {"ok": True, "missing": None}
    assert result["content"][0]["input"]["ok"] is True


@pytest.mark.parametrize("mode", ["bytes", "text", None], ids=["bytes", "text", "None"])
@pytest.mark.asyncio
async def test_a_routed_invoke_asking_for_a_non_json_mode_is_refused_before_it_is_sent(
    monkeypatch,
    tmp_path,
    mode,
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    with pytest.raises(AccountRoutingError) as caught:
        await model.invoke(messages=REQUEST["messages"], max_tokens=32, response_mode=mode)

    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.asyncio
async def test_a_routed_stream_asking_for_bytes_is_refused_before_it_is_sent(
    monkeypatch,
    tmp_path,
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(pieces=_stream_pieces())])

    with pytest.raises(AccountRoutingError) as caught:
        [
            chunk
            async for chunk in model.stream(
                messages=REQUEST["messages"], max_tokens=32, response_mode="bytes"
            )
        ]

    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.asyncio
async def test_a_routed_invoke_asking_for_json_is_sent(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    completed = await model.invoke(
        messages=REQUEST["messages"], max_tokens=32, response_mode="json"
    )

    assert completed.status.value == "completed"
    assert len(transport.calls) == 1
    assert len(_attempts(path)) == 1


@pytest.mark.asyncio
async def test_an_unrouted_invoke_asking_for_bytes_is_unchanged(monkeypatch, tmp_path):
    _install_unconfigured(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    completed = await model.invoke(
        messages=REQUEST["messages"], max_tokens=32, response_mode="bytes"
    )

    assert completed.status.value == "completed"
    assert len(transport.calls) == 1
    assert transport.calls[0]["json"]["messages"] == REQUEST["messages"]


@pytest.mark.asyncio
async def test_a_routed_call_asking_for_bytes_is_refused_before_it_is_sent(
    monkeypatch,
    tmp_path,
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    payload, headers = model.endpoint.create_payload(REQUEST)

    with pytest.raises(AccountRoutingError) as caught:
        await model.endpoint._call_http(payload, headers, response_mode="bytes")

    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_yielding_a_chunk(monkeypatch, tmp_path):
    import httpx

    path = _install_accounts(monkeypatch, tmp_path)
    first = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "first chunk"},
    }
    # Text is held until its block ends, so the block closes before the failure;
    # text still held when a stream fails is dropped, like the byte redactor's tail.
    stop = {"type": "content_block_stop", "index": 0}
    pieces = [
        b"data: " + json.dumps(first).encode() + b"\n\n",
        b"data: " + json.dumps(stop).encode() + b"\n\n",
        b" " * 40,
        httpx.ConnectError("offline"),
    ]
    model, transport = _model(monkeypatch, [StubResponse(pieces=pieces), _response()])
    model.endpoint.config.max_retries = 2

    chunks = []
    with pytest.raises(AccountRoutingError) as caught:
        async for chunk in model.endpoint.stream(REQUEST):
            chunks.append(chunk)

    assert caught.value.code is RouterErrorCode.TRANSPORT_ERROR
    assert any(chunk.content == "first chunk" for chunk in chunks)
    assert len(transport.calls) == 1
    row = _attempts(path)[0]
    assert row[2] == "open" and row[4] == "transport_error", row


@pytest.mark.asyncio
async def test_branch_clones_capture_distinct_scopes_and_prebuilt_scope_mismatch_refuses(
    monkeypatch, tmp_path
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    branch = Branch(chat_model=model)
    clone = branch.clone()

    with _branch_routing_context(branch.id, model):
        original_event = model.create_api_calling(**REQUEST)
    with _branch_routing_context(clone.id, model):
        clone_event = model.create_api_calling(**REQUEST)
    assert original_event._routing_scope != clone_event._routing_scope
    assert original_event._routing_scope.kind == "branch"
    assert clone_event._routing_scope.kind == "branch"

    prebuilt = model.create_api_calling(**REQUEST)
    with _branch_routing_context(uuid4(), model):
        with pytest.raises(AccountRoutingError) as caught:
            await model.invoke(api_call=prebuilt)
    assert caught.value.code is RouterErrorCode.ACCOUNT_CONFIG_CONFLICT
    assert not transport.calls
    assert not path.exists()


@pytest.mark.asyncio
async def test_one_branch_keeps_its_account_across_calls(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response(), _response()])
    identity = uuid4()
    scope = Scope("branch", identity, "anthropic")

    with _branch_routing_context(identity, model):
        first = model.create_api_calling(**REQUEST)
        second = model.create_api_calling(**REQUEST)
    await first.invoke()
    await second.invoke()

    assert len(transport.calls) == 2
    rows = _attempts(path)
    assert len(rows) == 2
    assert {row[1] for row in rows} == {"primary"}
    with sqlite3.connect(path) as connection:
        binding = connection.execute(
            "SELECT account FROM binding WHERE scope_kind = ? AND scope_id = ?",
            (scope.kind, str(scope.id)),
        ).fetchone()[0]
    assert binding == "primary"


@pytest.mark.asyncio
async def test_cancellation_settles_only_when_the_transport_was_not_entered(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model = iModel(provider="anthropic", model=MODEL)
    model.endpoint._assert_ssrf_safe_url = lambda: None
    entered_context = asyncio.Event()
    allow_context = asyncio.Event()

    class WaitingSession:
        async def __aenter__(self):
            entered_context.set()
            await allow_context.wait()
            return self

        async def __aexit__(self, *_):
            return False

        async def request(self, **_):
            raise AssertionError("transport must not be entered")

    model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: WaitingSession()
    task = asyncio.create_task(model.endpoint.call(REQUEST))
    await entered_context.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = _attempts(path)[0]
    assert row[2:] == ("settled", 0, "not_sent")

    path.unlink()
    router_module._reset_router_test_dependencies()
    path = _install_accounts(monkeypatch, tmp_path)
    model = iModel(provider="anthropic", model=MODEL)
    model.endpoint._assert_ssrf_safe_url = lambda: None
    entered_request = asyncio.Event()
    never_finish = asyncio.Event()

    class SendingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def request(self, **_):
            entered_request.set()
            await never_finish.wait()

    model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: SendingSession()
    task = asyncio.create_task(model.endpoint.call(REQUEST))
    await entered_request.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = _attempts(path)[0]
    assert row[2:] == ("open", None, "cancelled")


def _sse(events) -> bytes:
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


_MESSAGE_START = {
    "type": "message_start",
    "message": {"id": "msg_s", "model": MODEL, "usage": {"input_tokens": 100, "output_tokens": 0}},
}
_PARTIAL_TEXT = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "text_delta", "text": "partial"},
}


def _attempt_scopes(path) -> list[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT scope_id FROM attempt ORDER BY rowid").fetchall()
    return [row[0] for row in rows]


def _sent_contents(transport) -> list[str]:
    return [json.loads(call["data"])["messages"][0]["content"] for call in transport.calls]


QUEUED_REQUEST = {**REQUEST, "messages": [{"role": "user", "content": "queued elsewhere"}]}


@pytest.mark.parametrize("hand_over", ["stream", "invoke"])
@pytest.mark.asyncio
async def test_a_refused_prebuilt_call_stays_where_its_owner_put_it(
    monkeypatch, tmp_path, hand_over
):
    from lionagi.protocols.generic.event import EventStatus

    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    with _branch_routing_context(uuid4(), model):
        queued = await model.create_event(stream=hand_over == "stream", **QUEUED_REQUEST)
    await model.executor.append(queued)

    with pytest.raises(AccountRoutingError) as caught:
        if hand_over == "stream":
            async for _ in model.stream(api_call=queued):
                pass
        else:
            await model.invoke(api_call=queued)
    assert caught.value.code is RouterErrorCode.ACCOUNT_CONFIG_CONFLICT
    assert queued.id in model.executor.pending
    assert queued.id in model.executor.pile
    assert queued.status is EventStatus.PENDING

    with _branch_routing_context(uuid4(), model):
        await model.invoke(**REQUEST)
    assert _sent_contents(transport) == ["A short request"]


@pytest.mark.asyncio
async def test_the_owner_sends_its_call_after_another_scope_was_refused(monkeypatch, tmp_path):
    from lionagi.protocols.generic.event import EventStatus

    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    owner = uuid4()
    with _branch_routing_context(owner, model):
        call = await model.create_event(**QUEUED_REQUEST)

    with _branch_routing_context(uuid4(), model):
        with pytest.raises(AccountRoutingError):
            await model.invoke(api_call=call)
    with _branch_routing_context(owner, model):
        sent = await model.invoke(api_call=call)

    assert sent.status is EventStatus.COMPLETED
    assert _sent_contents(transport) == ["queued elsewhere"]
    assert _attempt_scopes(path) == [str(owner)]


@pytest.mark.asyncio
async def test_a_refusal_during_the_owners_send_leaves_that_send_alone(monkeypatch, tmp_path):
    from lionagi.protocols.generic.event import EventStatus

    path = _install_accounts(monkeypatch, tmp_path)
    model = iModel(provider="anthropic", model=MODEL)
    model.endpoint._assert_ssrf_safe_url = lambda: None
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[dict] = []

    class HeldSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def request(self, **kwargs):
            calls.append(kwargs)
            entered.set()
            await release.wait()
            return _response()

    model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: HeldSession()
    owner = uuid4()
    with _branch_routing_context(owner, model):
        call = await model.create_event(**QUEUED_REQUEST)
        owner_send = asyncio.create_task(model.invoke(api_call=call))
    await entered.wait()

    with _branch_routing_context(uuid4(), model):
        with pytest.raises(AccountRoutingError):
            await model.invoke(api_call=call)
    release.set()
    sent = await owner_send

    assert sent.status is EventStatus.COMPLETED
    assert len(calls) == 1
    assert _attempt_scopes(path) == [str(owner)]


@pytest.mark.parametrize("where", ["client", "request", "stream"])
@pytest.mark.asyncio
async def test_raise_for_status_is_refused_before_a_key_is_read(monkeypatch, tmp_path, where):
    # httpx's raise_for_status would skip the 401/403 block and keep the sent headers,
    # key included, on the exception the routed error replaces.
    path = _install_accounts(monkeypatch, tmp_path)
    lookups = []
    monkeypatch.setattr(router_module, "resolve_account_key", lambda *args: lookups.append(args))
    model, transport = _model(monkeypatch, [_response()])
    if where == "client":
        model.endpoint.config.client_kwargs = {"raise_for_status": True}

    with pytest.raises(AccountRoutingError) as caught:
        if where == "client":
            await model.endpoint.call(REQUEST)
        elif where == "request":
            await model.endpoint.call(REQUEST, raise_for_status=True)
        else:
            async for _ in model.endpoint.stream(REQUEST, raise_for_status=True):
                pass
    assert type(caught.value) is AccountRoutingError
    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not lookups
    assert not transport.calls
    assert not path.exists()


@pytest.mark.parametrize("ready_payload", [False, True])
@pytest.mark.parametrize("tier", ["priority", "auto"])
@pytest.mark.asyncio
async def test_a_service_tier_other_than_standard_only_is_refused_not_rewritten(
    monkeypatch, tmp_path, tier, ready_payload
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    request = {**REQUEST, "service_tier": tier}

    with pytest.raises(AccountRoutingError) as caught:
        if ready_payload:
            payload, headers = model.endpoint.create_payload(REQUEST)
            await model.endpoint.call(
                {**payload, "service_tier": tier},
                skip_payload_creation=True,
                extra_headers=headers,
            )
        else:
            await model.endpoint.call(request)
    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.asyncio
async def test_a_standard_only_tier_named_by_the_caller_is_sent(monkeypatch, tmp_path):
    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    await model.endpoint.call({**REQUEST, "service_tier": "standard_only"})
    assert json.loads(transport.calls[0]["data"])["service_tier"] == "standard_only"


def test_the_stream_redactor_masks_a_key_that_straddles_its_cut():
    from pydantic import SecretStr

    from lionagi.service.accounts._router import StreamingSecretRedactor

    redactor = StreamingSecretRedactor(SecretStr(CANARY))
    # One feed holding the whole key, ending two bytes past it. A tail of the key's
    # length minus one, cut before replacing, would split the key and neither part match.
    out = redactor.feed(b"ab" + CANARY.encode() + b"cd") + redactor.feed(b"", final=True)
    assert out == b"ab[REDACTED]cd"

    redactor = StreamingSecretRedactor(SecretStr(CANARY))
    key = CANARY.encode()
    out = redactor.feed(b"ab" + key[:5]) + redactor.feed(key[5:] + b"cd")
    out += redactor.feed(b"", final=True)
    assert out == b"ab[REDACTED]cd"


def _finished_stream() -> StubResponse:
    return StubResponse(
        pieces=[
            _sse(
                [
                    _MESSAGE_START,
                    _PARTIAL_TEXT,
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 5},
                    },
                    {"type": "message_stop"},
                ]
            )
        ]
    )


@pytest.mark.asyncio
async def test_a_call_appended_then_streamed_is_not_sent_again(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_finished_stream(), _response()])
    owner = uuid4()
    with _branch_routing_context(owner, model):
        call = await model.create_event(stream=True, **QUEUED_REQUEST)
        await model.executor.append(call)
        async for _ in model.stream(api_call=call):
            pass

    assert call.id not in model.executor.pending
    await model.executor.forward()
    assert _sent_contents(transport) == ["queued elsewhere"]
    assert _attempt_scopes(path) == [str(owner)]


@pytest.mark.asyncio
async def test_a_forward_during_a_stream_does_not_send_that_stream_again(monkeypatch, tmp_path):
    _install_accounts(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    body = _finished_stream().content.pieces[0]

    class HeldBody:
        def __aiter__(self):
            self._sent = False
            return self

        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            started.set()
            await release.wait()
            self._sent = True
            return body

    held = StubResponse()
    held.content = HeldBody()
    model, transport = _model(monkeypatch, [held, _finished_stream()])

    async def consume():
        with _branch_routing_context(uuid4(), model):
            async for _ in model.stream(**REQUEST):
                pass

    streaming = asyncio.create_task(consume())
    await started.wait()
    await model.executor.forward()
    release.set()
    await streaming

    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_a_forward_during_the_stream_of_an_appended_call_does_not_send_it_again(
    monkeypatch, tmp_path
):
    """The call leaves pending when its stream starts, not when the stream ends."""
    path = _install_accounts(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    body = _finished_stream().content.pieces[0]

    class HeldBody:
        def __aiter__(self):
            self._sent = False
            return self

        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            started.set()
            await release.wait()
            self._sent = True
            return body

    held = StubResponse()
    held.content = HeldBody()
    model, transport = _model(monkeypatch, [held, _finished_stream()])
    owner = uuid4()

    async def consume(call):
        with _branch_routing_context(owner, model):
            async for _ in model.stream(api_call=call):
                pass

    with _branch_routing_context(owner, model):
        call = await model.create_event(stream=True, **QUEUED_REQUEST)
        await model.executor.append(call)
    streaming = asyncio.create_task(consume(call))
    await started.wait()
    assert call.id not in model.executor.pending
    await model.executor.forward()
    release.set()
    await streaming

    assert _sent_contents(transport) == ["queued elsewhere"]
    assert _attempt_scopes(path) == [str(owner)]


@pytest.mark.asyncio
async def test_a_queued_call_another_scope_was_refused_is_sent_once_for_its_owner(
    monkeypatch, tmp_path
):
    from lionagi.protocols.generic.event import EventStatus

    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    owner = uuid4()
    with _branch_routing_context(owner, model):
        queued = await model.create_event(**QUEUED_REQUEST)
    # Forwarded by its owner and deferred by the processor before the hand-over.
    await model.executor.start()
    await model.executor.processor.enqueue(queued)

    with _branch_routing_context(uuid4(), model):
        with pytest.raises(AccountRoutingError) as caught:
            await model.invoke(api_call=queued)
    assert caught.value.code is RouterErrorCode.ACCOUNT_CONFIG_CONFLICT

    await model.executor.processor.process()
    assert queued.status is EventStatus.COMPLETED
    assert _sent_contents(transport) == ["queued elsewhere"]
    assert _attempt_scopes(path) == [str(owner)]


@pytest.mark.asyncio
async def test_an_invoke_never_sends_a_call_another_caller_has_not_forwarded(monkeypatch, tmp_path):
    from lionagi.protocols.generic.event import EventStatus

    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response(), _response()])
    owner, other = uuid4(), uuid4()
    with _branch_routing_context(owner, model):
        waiting = await model.create_event(**QUEUED_REQUEST)
    await model.executor.append(waiting)

    with _branch_routing_context(other, model):
        await model.invoke(**REQUEST)
    assert _sent_contents(transport) == ["A short request"]
    assert waiting.status is EventStatus.PENDING
    assert waiting.id in model.executor.pending

    await model.executor.forward()
    assert waiting.status is EventStatus.COMPLETED
    assert _sent_contents(transport) == ["A short request", "queued elsewhere"]
    assert _attempt_scopes(path) == [str(other), str(owner)]


@pytest.mark.asyncio
async def test_invokes_after_a_finished_stream_send_their_own_calls(monkeypatch, tmp_path):
    from lionagi.protocols.generic.event import EventStatus

    path = _install_accounts(monkeypatch, tmp_path)
    streamed = StubResponse(
        pieces=[
            _sse(
                [
                    _MESSAGE_START,
                    _PARTIAL_TEXT,
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 5},
                    },
                    {"type": "message_stop"},
                ]
            )
        ]
    )
    model, transport = _model(monkeypatch, [streamed, _response(), _response()])
    streamer, first, second = uuid4(), uuid4(), uuid4()

    with _branch_routing_context(streamer, model):
        async for _ in model.stream(**REQUEST):
            pass
    assert len(model.executor.pending) == 0

    with _branch_routing_context(first, model):
        first_call = await model.invoke(
            **{**REQUEST, "messages": [{"role": "user", "content": "first"}]}
        )
    with _branch_routing_context(second, model):
        second_call = await model.invoke(
            **{**REQUEST, "messages": [{"role": "user", "content": "second"}]}
        )

    assert first_call.status is EventStatus.COMPLETED
    assert second_call.status is EventStatus.COMPLETED
    assert _sent_contents(transport) == ["A short request", "first", "second"]
    assert _attempt_scopes(path) == [str(streamer), str(first), str(second)]


@pytest.mark.parametrize("path_kind", ["call", "stream"])
@pytest.mark.asyncio
async def test_a_service_tier_given_as_a_call_keyword_is_refused_before_any_reservation(
    monkeypatch, tmp_path, path_kind
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    with pytest.raises(AccountRoutingError) as caught:
        if path_kind == "call":
            await model.endpoint.call(REQUEST, service_tier="priority")
        else:
            async for _ in model.endpoint.stream(REQUEST, service_tier="priority"):
                pass
    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


@pytest.mark.parametrize(
    "name", ["serviceTier", "service_tier ", "\u017fervice_tier", "ssl", "proxy"]
)
@pytest.mark.parametrize("path_kind", ["call", "stream"])
@pytest.mark.asyncio
async def test_any_other_request_keyword_is_refused_before_any_reservation(
    monkeypatch, tmp_path, path_kind, name
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])

    with pytest.raises(AccountRoutingError) as caught:
        if path_kind == "call":
            await model.endpoint.call(REQUEST, **{name: "priority"})
        else:
            async for _ in model.endpoint.stream(REQUEST, **{name: "priority"}):
                pass
    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not transport.calls
    assert not path.exists() or not _attempts(path)


def _redacted(events, key=CANARY) -> list[tuple[str, list[str]]]:
    """Each event's pieces as sent, for events given as ``(name, [(kind, text)])``."""
    from pydantic import SecretStr

    from lionagi.service.accounts._router import StreamTextRedactor

    redactor = StreamTextRedactor(SecretStr(key))
    out = []
    for name, pieces in events:
        out += redactor.push(name, pieces)
    out += redactor.finish()
    assert [name for name, _ in out] == [name for name, _ in events]
    return out


def test_the_text_redactor_masks_a_key_split_across_pieces():
    out = _redacted([("a", [("text", "ab" + CANARY[:5])]), ("b", [("text", CANARY[5:] + "cd")])])
    assert out == [("a", ["ab[REDACTED]"]), ("b", ["cd"])]


def _delta(delta_type: str, field: str, text: str) -> dict:
    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": delta_type, field: text},
    }


def _raw_delta_text(chunks, field: str) -> str:
    joined = ""
    for chunk in chunks:
        raw = (chunk.metadata or {}).get("raw")
        if isinstance(raw, dict) and raw.get("type") == "content_block_delta":
            joined += raw.get("delta", {}).get(field) or ""
    return joined


@pytest.mark.parametrize(
    ("block", "delta_type", "field", "chunk_type"),
    [
        ({"type": "text", "text": ""}, "text_delta", "text", "text"),
        ({"type": "thinking", "thinking": ""}, "thinking_delta", "thinking", "thinking"),
        (
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}},
            "input_json_delta",
            "partial_json",
            None,
        ),
        (
            {"type": "thinking", "thinking": "", "signature": ""},
            "signature_delta",
            "signature",
            None,
        ),
    ],
    ids=["text", "thinking", "tool_input", "signature"],
)
@pytest.mark.parametrize("ending", ["block_stop", "message_stop_only", "truncated"])
@pytest.mark.asyncio
async def test_a_key_split_across_two_deltas_never_reaches_the_joined_text(
    monkeypatch, tmp_path, block, delta_type, field, chunk_type, ending
):
    _install_accounts(monkeypatch, tmp_path)
    events = [
        _MESSAGE_START,
        {"type": "content_block_start", "index": 0, "content_block": block},
        _delta(delta_type, field, "ab" + CANARY[:5]),
        _delta(delta_type, field, CANARY[5:] + "cd"),
    ]
    if ending == "block_stop":
        events.append({"type": "content_block_stop", "index": 0})
    if ending != "truncated":
        events += [
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        ]
    # One piece per event, so each delta arrives in its own read.
    model, transport = _model(
        monkeypatch, [StubResponse(pieces=[_sse([event]) for event in events])]
    )

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    # A consumer may join the chunk text or the raw events' delta text; neither
    # holds the key, and the held rest arrives before the event that ends its block.
    assert _raw_delta_text(chunks, field) == "ab[REDACTED]cd"
    if chunk_type is not None:
        joined = "".join(chunk.content or "" for chunk in chunks if chunk.type == chunk_type)
        assert joined == "ab[REDACTED]cd"
    assert all(CANARY not in repr(chunk) for chunk in chunks)
    if ending != "truncated":
        kinds = [(chunk.metadata or {}).get("raw", {}).get("type") for chunk in chunks]
        last_delta = max(i for i, kind in enumerate(kinds) if kind == "content_block_delta")
        ends_block = "content_block_stop" if ending == "block_stop" else "message_delta"
        assert last_delta < kinds.index(ends_block)


@pytest.mark.asyncio
async def test_held_thinking_reaches_the_consumer_before_the_blocks_signature(
    monkeypatch, tmp_path
):
    _install_accounts(monkeypatch, tmp_path)
    events = [
        _MESSAGE_START,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        _delta("thinking_delta", "thinking", "ab" + CANARY[:5]),
        _delta("signature_delta", "signature", "sig"),
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ]
    model, transport = _model(
        monkeypatch, [StubResponse(pieces=[_sse([event]) for event in events])]
    )

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    deltas = [
        (chunk.metadata or {}).get("raw", {}).get("delta", {}).get("type")
        for chunk in chunks
        if (chunk.metadata or {}).get("raw", {}).get("type") == "content_block_delta"
    ]
    last_thinking = max(i for i, kind in enumerate(deltas) if kind == "thinking_delta")
    assert last_thinking < deltas.index("signature_delta")
    assert _raw_delta_text(chunks, "thinking") == "ab" + CANARY[:5]
    assert _raw_delta_text(chunks, "signature") == "sig"


@pytest.mark.parametrize(
    ("pieces", "reason"),
    [
        ([_sse([_MESSAGE_START]), _sse([_PARTIAL_TEXT])], "stream_truncated"),
        (
            [
                _sse([_MESSAGE_START]),
                b"event: error\ndata: "
                + json.dumps({"type": "error", "error": {"type": "overloaded_error"}}).encode()
                + b"\n\n",
            ],
            "stream_error",
        ),
        (
            [_sse([_MESSAGE_START]), b'event: error\ndata: {"message":"upstream"}\n\n'],
            "stream_error",
        ),
        (
            [
                b"event: message_start\ndata: "
                + json.dumps(_MESSAGE_START).encode()
                + b"\nevent: error\ndata: "
                + json.dumps({"type": "error", "error": {"type": "overloaded_error"}}).encode()
                + b"\n"
            ],
            "stream_error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_stream_ending_before_message_stop_records_why(
    monkeypatch, tmp_path, pieces, reason
):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(pieces=pieces)])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert chunks
    row = _attempts(path)[0]
    assert row[2:] == ("open", None, reason)


@pytest.mark.parametrize(
    "wire",
    [
        pytest.param(b'event: error\ndata: {"message":"upstream"}\n\n', id="named-only-by-sse"),
        pytest.param(b"event: error\ndata: upstream gateway error\n\n", id="plain-text"),
        pytest.param(
            b"event: message_start\ndata: "
            + json.dumps(_MESSAGE_START).encode()
            + b"\nevent: error\ndata: "
            + json.dumps(
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
            ).encode()
            + b"\n",
            id="no-blank-lines",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_routed_stream_error_named_by_its_sse_event_fails_the_call(
    monkeypatch, tmp_path, wire
):
    from lionagi.protocols.generic.event import EventStatus

    _install_accounts(monkeypatch, tmp_path)
    model, _ = _model(monkeypatch, [StubResponse(pieces=[wire])])
    call = await model.create_event(**REQUEST, stream=True)
    chunks = []
    try:
        with pytest.raises(ValueError):
            async for chunk in model.stream(api_call=call):
                chunks.append(chunk)
    finally:
        await model.close()

    assert call.status is EventStatus.FAILED
    failed = [chunk for chunk in chunks if chunk.metadata.get("call_failed")]
    assert len(failed) == 1
    assert chunks[-1] is failed[0]


@pytest.mark.asyncio
async def test_a_failed_routed_stream_reports_the_error_text_the_consumer_was_shown(
    monkeypatch, tmp_path
):
    from lionagi.protocols.generic.event import EventStatus

    _install_accounts(monkeypatch, tmp_path)
    # The key's run starts in a text delta and ends in the error event, so only the
    # stream's text redaction, which joins the two, can see it.
    wire = _sse(
        [_delta("text_delta", "text", CANARY[13:20]), {"type": "error", "error": CANARY[20:24]}]
    )
    model, _ = _model(monkeypatch, [StubResponse(pieces=[wire])])
    call = await model.create_event(**REQUEST, stream=True)
    chunks = []
    try:
        with pytest.raises(ValueError) as caught:
            async for chunk in model.stream(api_call=call):
                chunks.append(chunk)
    finally:
        await model.close()

    assert call.status is EventStatus.FAILED
    assert chunks[-1].type == "error"
    assert CANARY[20:24] not in chunks[-1].content
    assert chunks[-1].content in str(caught.value)
    assert CANARY[20:24] not in str(caught.value)
    assert CANARY[20:24] not in str(call.execution.error)


@pytest.mark.parametrize(
    ("ending", "row"),
    [
        ([], ("open", None, "usage_incomplete")),
        (
            [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}],
            ("open", None, "usage_incomplete"),
        ),
        (
            [
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 5},
                }
            ],
            ("settled", 250_000, "complete"),
        ),
        (
            [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}],
            ("open", None, "usage_incomplete"),
        ),
        (
            [{"type": "message_delta", "usage": {"input_tokens": 100}}],
            ("open", None, "usage_incomplete"),
        ),
        (
            [{"type": "message_delta", "usage": {"output_tokens": True}}],
            ("open", None, "usage_incomplete"),
        ),
        (
            [{"type": "message_delta", "usage": {"output_tokens": 0}}],
            ("settled", 200_000, "complete"),
        ),
        (
            [{"type": "message_delta", "usage": {"output_tokens": -1}}],
            ("open", None, "usage_incomplete"),
        ),
        (
            [{"type": "message_delta", "usage": {"output_tokens": 5.0}}],
            ("open", None, "usage_incomplete"),
        ),
        (
            [
                {"type": "message_delta", "usage": {"output_tokens": 5}},
                {"type": "message_delta", "usage": {}},
            ],
            ("settled", 250_000, "complete"),
        ),
        (
            [
                {"type": "message_delta", "usage": {"output_tokens": 5}},
                {"type": "message_delta", "usage": {"output_tokens": 7}},
            ],
            ("settled", 270_000, "complete"),
        ),
        (
            [{"type": "message_delta", "usage": {"output_tokens": 5, "speed": "fast"}}],
            ("open", None, "unpriced_speed"),
        ),
        (
            [{"type": "message_delta", "usage": {"output_tokens": 5, "cache_storage_hours": 1}}],
            ("open", None, "unknown_usage_key_cache_storage_hours"),
        ),
    ],
    ids=[
        "no_message_delta",
        "delta_without_usage",
        "delta_with_usage",
        "delta_with_empty_usage",
        "delta_usage_without_output_count",
        "delta_with_boolean_output_count",
        "delta_with_zero_output_count",
        "delta_with_negative_output_count",
        "delta_with_float_output_count",
        "count_then_empty_usage",
        "two_counts_the_last_one_kept",
        "fast_speed_named",
        "unknown_field_named",
    ],
)
@pytest.mark.asyncio
async def test_a_stream_settles_at_message_stop_only_with_a_message_deltas_usage(
    monkeypatch, tmp_path, ending, row
):
    # message_start carries a placeholder output count; settling on it would record
    # less than the call cost.
    path = _install_accounts(monkeypatch, tmp_path)
    start = copy.deepcopy(_MESSAGE_START)
    start["message"]["usage"]["output_tokens"] = 1
    events = [start, _PARTIAL_TEXT, *ending, {"type": "message_stop"}]
    model, transport = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert chunks
    assert _attempts(path)[0][2:] == row


@pytest.mark.asyncio
async def test_a_second_message_stop_settles_nothing(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    start = copy.deepcopy(_MESSAGE_START)
    start["message"]["usage"]["output_tokens"] = 1
    final = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 5},
    }
    later = {"type": "message_delta", "usage": {"output_tokens": 500}}
    events = [start, _PARTIAL_TEXT, final, {"type": "message_stop"}, later]
    events.append({"type": "message_stop"})
    model, transport = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert chunks
    assert _attempts(path)[0][2:] == ("settled", 250_000, "complete")
    with sqlite3.connect(path) as connection:
        reason = connection.execute(
            "SELECT block_reason FROM account WHERE name = 'primary'"
        ).fetchone()[0]
    assert reason is None


@pytest.mark.asyncio
async def test_a_consumer_closing_the_stream_early_records_why(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(
        monkeypatch, [StubResponse(pieces=[_sse([_MESSAGE_START, _PARTIAL_TEXT])])]
    )

    stream = model.endpoint.stream(REQUEST)
    await stream.__anext__()
    await stream.aclose()

    row = _attempts(path)[0]
    assert row[2:] == ("open", None, "stream_closed")


@pytest.mark.asyncio
async def test_a_stream_cancelled_after_its_request_was_sent_records_why(monkeypatch, tmp_path):
    path = _install_accounts(monkeypatch, tmp_path)
    first_chunk = asyncio.Event()

    class HangingStream(ByteStream):
        async def __anext__(self):
            piece = next(self._pieces, None)
            if piece is None:
                await asyncio.Event().wait()
            return piece

    response = StubResponse()
    response.content = HangingStream([_sse([_MESSAGE_START, _PARTIAL_TEXT])])
    model, transport = _model(monkeypatch, [response])

    async def consume():
        async for _ in model.endpoint.stream(REQUEST):
            first_chunk.set()

    task = asyncio.create_task(consume())
    await first_chunk.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = _attempts(path)[0]
    assert row[2:] == ("open", None, "cancelled")


def _text_block(index) -> dict:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }


def _text(index, text: str) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def _citation(index, cited_text: str) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {
            "type": "citations_delta",
            "citation": {
                "type": "char_location",
                "cited_text": cited_text,
                "document_index": 0,
                "start_char_index": 0,
                "end_char_index": len(cited_text),
            },
        },
    }


_MESSAGE_END = [
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
    {"type": "message_stop"},
]


async def _stream_events(monkeypatch, tmp_path, events, key=CANARY) -> list:
    _install_accounts(monkeypatch, tmp_path, key=key)
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse([event]) for event in events])])
    return [chunk async for chunk in model.endpoint.stream(REQUEST)]


def _raw_kinds(chunks) -> list:
    return [(chunk.metadata or {}).get("raw", {}).get("type") for chunk in chunks]


# What comes between the two halves of the key, and the block the second half is in.
# The quoted text begins with a character no test key has, so it never continues one.
_BETWEEN_TWO_TEXT_DELTAS = {
    "citation": ([_citation(0, "quoted")], 0),
    "error_event": ([{"type": "error", "error": {"type": "overloaded_error", "message": "x"}}], 0),
    "next_block": ([{"type": "content_block_stop", "index": 0}, _text_block(1)], 1),
    "block_start_without_stop": ([_text_block(1)], 1),
    "thinking_block": (
        [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            _delta("thinking_delta", "thinking", "hmm"),
            {"type": "content_block_stop", "index": 1},
            _text_block(2),
        ],
        2,
    ),
}


@pytest.mark.parametrize("cut", range(1, len(CANARY)))
@pytest.mark.parametrize("between", sorted(_BETWEEN_TWO_TEXT_DELTAS))
@pytest.mark.parametrize("key", [CANARY, OTHER_KEY], ids=["sk-ant", "other"])
@pytest.mark.asyncio
async def test_a_key_split_around_another_event_never_reaches_the_joined_text(
    monkeypatch, tmp_path, key, between, cut
):
    """A consumer joins text across citations, blocks and error events. A key beginning
    of MIN_RUN characters or more is masked at once; one within the public prefix goes
    out before the event between, and the rest of the key is masked after it. Nothing
    of a key without that prefix goes out."""
    middle, second = _BETWEEN_TWO_TEXT_DELTAS[between]
    events = [
        _MESSAGE_START,
        _text_block(0),
        _text(0, "ab" + key[:cut]),
        *middle,
        _text(second, key[cut:] + "cd"),
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events, key=key)

    shown = key[:cut] if key.startswith("sk-ant-") and cut <= len("sk-ant-") else ""
    expected = "ab" + shown + "[REDACTED]cd"
    assert "".join(chunk.content or "" for chunk in chunks if chunk.type == "text") == expected
    assert _raw_delta_text(chunks, "text") == expected
    assert all(key not in repr(chunk) for chunk in chunks)


@pytest.mark.asyncio
async def test_a_quote_that_continues_the_key_masks_the_held_public_prefix(monkeypatch, tmp_path):
    """The join of every piece runs held text into the next event's pieces, so a
    citation whose quote continues the key makes a long beginning of it there, and
    the held public prefix is masked rather than sent."""
    events = [
        _MESSAGE_START,
        _text_block(0),
        _text(0, "ab" + CANARY[:7]),
        _citation(0, CANARY[7:9] + " quoted"),
        _text(0, "cd"),
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    assert _raw_delta_text(chunks, "text") == "ab[REDACTED]cd"
    assert all(CANARY[:8] not in repr(chunk) for chunk in chunks)


@pytest.mark.parametrize("cut", range(1, len(CANARY)))
@pytest.mark.asyncio
async def test_a_key_split_around_a_signature_never_reaches_the_joined_thinking(
    monkeypatch, tmp_path, cut
):
    from lionagi.service.accounts._router import StreamTextRedactor

    events = [
        _MESSAGE_START,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        _delta("thinking_delta", "thinking", "ab" + CANARY[:cut]),
        _delta("signature_delta", "signature", "sig"),
        _delta("thinking_delta", "thinking", CANARY[cut:] + "cd"),
        {"type": "content_block_stop", "index": 0},
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    shown = CANARY[:cut] if cut < StreamTextRedactor.MIN_RUN else ""
    assert _raw_delta_text(chunks, "thinking") == "ab" + shown + "[REDACTED]cd"
    assert _raw_delta_text(chunks, "signature") == "sig"
    assert all(CANARY not in repr(chunk) for chunk in chunks)


@pytest.mark.parametrize("cut", range(1, len(CANARY)))
@pytest.mark.asyncio
async def test_a_key_split_across_two_citations_never_reaches_their_joined_quotes(
    monkeypatch, tmp_path, cut
):
    """A citation whose quote ends in a beginning of the key waits for the next one, so
    the key is masked across both at every cut."""
    events = [
        _MESSAGE_START,
        _text_block(0),
        _citation(0, "ab" + CANARY[:cut]),
        _citation(0, CANARY[cut:] + "cd"),
        {"type": "content_block_stop", "index": 0},
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    quotes = [
        raw["delta"]["citation"]["cited_text"]
        for raw in ((chunk.metadata or {}).get("raw") for chunk in chunks)
        if isinstance(raw, dict) and raw.get("delta", {}).get("type") == "citations_delta"
    ]
    assert quotes == ["ab[REDACTED]", "cd"]
    assert all(CANARY not in repr(chunk) for chunk in chunks)


@pytest.mark.parametrize("length", range(1, 8))
@pytest.mark.asyncio
async def test_text_that_only_begins_like_the_key_keeps_its_place(monkeypatch, tmp_path, length):
    """A text that ends with fewer than eight characters of the key reaches the consumer
    before its block's stop, so ordinary text is never moved."""
    ending = CANARY[:length]
    events = [
        _MESSAGE_START,
        _text_block(0),
        _text(0, "the end of " + ending),
        {"type": "content_block_stop", "index": 0},
        _text_block(1),
        _text(1, " after"),
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    kinds = _raw_kinds(chunks)
    before_stop = chunks[: kinds.index("content_block_stop")]
    assert _raw_delta_text(before_stop, "text") == "the end of " + ending
    assert _raw_delta_text(chunks, "text") == "the end of " + ending + " after"


@pytest.mark.asyncio
async def test_a_long_key_beginning_is_masked_even_when_the_key_never_completes(
    monkeypatch, tmp_path
):
    from lionagi.service.accounts._router import StreamTextRedactor

    beginning = CANARY[: StreamTextRedactor.MIN_RUN]
    events = [
        _MESSAGE_START,
        _text_block(0),
        _text(0, "ab" + beginning),
        {"type": "content_block_stop", "index": 0},
        _text_block(1),
        _text(1, "zz"),
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    assert _raw_delta_text(chunks, "text") == "ab[REDACTED]zz"
    kinds = _raw_kinds(chunks)
    assert max(i for i, kind in enumerate(kinds) if kind == "content_block_delta") < kinds.index(
        "message_delta"
    )


@pytest.mark.parametrize("index", [0, "0"])
@pytest.mark.asyncio
async def test_held_text_reaches_the_consumer_before_its_blocks_stop(monkeypatch, tmp_path, index):
    events = [
        _MESSAGE_START,
        _text_block(index),
        _text(index, "hello"),
        {"type": "content_block_stop", "index": index},
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    kinds = _raw_kinds(chunks)
    last_delta = max(i for i, kind in enumerate(kinds) if kind == "content_block_delta")
    assert last_delta < kinds.index("content_block_stop")
    assert _raw_delta_text(chunks, "text") == "hello"


@pytest.mark.asyncio
async def test_held_text_reaches_the_consumer_before_the_next_block_starts(monkeypatch, tmp_path):
    """Block 0 sends no stop; its held text still precedes block 1's start."""
    events = [
        _MESSAGE_START,
        _text_block(0),
        _text(0, "hello"),
        _text_block(1),
        _text(1, " world"),
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    raws = [(chunk.metadata or {}).get("raw", {}) for chunk in chunks]
    start_1 = next(
        i
        for i, raw in enumerate(raws)
        if raw.get("type") == "content_block_start" and raw.get("index") == 1
    )
    assert _raw_delta_text(chunks[:start_1], "text") == "hello"
    assert _raw_delta_text(chunks, "text") == "hello world"


def test_a_beginning_of_the_key_waits_only_for_the_next_event():
    """Held while the next event may continue the key; decided at an event with no text."""
    from lionagi.service.accounts._router import StreamTextRedactor

    run = StreamTextRedactor.MIN_RUN
    out = _redacted(
        [
            ("a", [("text", "ab" + CANARY[: run - 1])]),
            ("stop", []),
            ("b", [("text", CANARY[run - 1 :] + "cd")]),
        ]
    )
    assert out == [("a", ["ab" + CANARY[: run - 1]]), ("stop", []), ("b", ["[REDACTED]cd"])]

    out = _redacted([("a", [("text", "ab" + CANARY[:run])]), ("stop", []), ("b", [("text", "zz")])])
    assert out == [("a", ["ab[REDACTED]"]), ("stop", []), ("b", ["zz"])]


def test_a_key_split_across_kinds_is_masked_in_the_join_of_every_piece():
    out = _redacted(
        [("a", [("text_delta", "ab" + CANARY[:5])]), ("b", [("thinking_delta", CANARY[5:] + "zz")])]
    )
    assert out == [("a", ["ab[REDACTED]"]), ("b", ["[REDACTED]zz"])]


@pytest.mark.parametrize("prefix", ["sk-ant-", "k"])
def test_a_key_continued_after_a_block_stop_shows_only_its_public_beginning(prefix):
    long_key = prefix + "".join(chr(ord("A") + (i * 7) % 26) for i in range(112))
    out = _redacted(
        [
            ("a", [("text_delta", "ab" + long_key[:5])]),
            ("stop", []),
            ("b", [("text_delta", long_key[5:100])]),
            ("stop2", []),
            ("c", [("text_delta", long_key[100:] + "zz")]),
        ],
        key=long_key,
    )
    joined = "".join(text for _, texts in out for text in texts)
    shown = long_key[:5] if prefix == "sk-ant-" else ""
    assert joined == "ab" + shown + "[REDACTED]zz"


# Past the public prefix this key holds only capitals, which no other text below uses.
_PREFIXED_KEY = "sk-ant-" + "QWXVJKPZ" * 4
_UNPREFIXED_KEY = "QWXVJKPZ" * 4


def _sent(out) -> str:
    return "".join(text for _, texts in out for text in texts)


@pytest.mark.parametrize("cut", range(1, 9))
def test_an_anthropic_key_split_at_a_pause_sends_nothing_past_its_public_prefix(cut):
    """The key's first eight characters, split at every position with a textless event
    between: what goes out before the pause is the public prefix at most."""
    key = _PREFIXED_KEY
    out = _redacted(
        [("a", [("text", "ab" + key[:cut])]), ("pause", []), ("b", [("text", key[cut:8] + "zz")])],
        key=key,
    )
    sent = _sent(out)
    assert not set(key[7:]) & set(sent.replace("[REDACTED]", ""))
    assert sent == "ab" + (key[:cut] if cut <= 7 else "") + "[REDACTED]zz"


@pytest.mark.parametrize("cut", range(1, 9))
def test_a_key_without_the_public_prefix_is_masked_from_its_first_character_at_a_pause(cut):
    key = _UNPREFIXED_KEY
    out = _redacted(
        [("a", [("text", "ab" + key[:cut])]), ("pause", []), ("b", [("text", key[cut:8] + "zz")])],
        key=key,
    )
    sent = _sent(out)
    assert not set(key) & set(sent.replace("[REDACTED]", ""))
    assert sent == "ab[REDACTED]zz"


@pytest.mark.parametrize("cut", range(1, 9))
@pytest.mark.parametrize("key", [_PREFIXED_KEY, _UNPREFIXED_KEY], ids=["sk-ant", "other"])
def test_a_beginning_of_the_key_held_when_the_stream_ends_is_decided_the_same_way(key, cut):
    sent = _sent(_redacted([("a", [("text", "ab" + key[:cut])])], key=key))
    public = key.startswith("sk-ant-") and cut <= 7
    assert sent == "ab" + (key[:cut] if public else "[REDACTED]")


def test_two_long_beginnings_in_one_piece_are_masked():
    out = _redacted([("a", [("text_delta", CANARY[:20] + CANARY[:8] + "zz")])])
    assert out == [("a", ["[REDACTED]zz"])]


@pytest.mark.parametrize("seed", range(40))
def test_no_join_ever_holds_more_than_a_short_beginning_of_the_key(seed):
    """Random splits of keys across kinds, citations and textless events: in the join
    of each kind and of every piece, at most MIN_RUN - 1 characters that begin the
    key appear before a mask, and the key never does."""
    import random

    from lionagi.service.accounts._router import StreamTextRedactor

    rng = random.Random(seed)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    key = "sk-ant-" + "".join(rng.choice(alphabet) for _ in range(rng.randint(20, 110)))
    kinds = ["text_delta", "thinking_delta", "input_json_delta", "citations_delta"]
    text = ""
    for _ in range(rng.randint(1, 4)):
        filler = "".join(rng.choice(alphabet + " .\n") for _ in range(rng.randint(0, 12)))
        text += filler + key[: rng.choice([len(key), rng.randint(1, len(key))])]
    events = []
    position = 0
    while position < len(text):
        size = rng.randint(1, 15)
        events.append((len(events), [(rng.choice(kinds), text[position : position + size])]))
        position += size
        if rng.random() < 0.3:
            events.append((len(events), []))
    out = _redacted(events, key=key)
    joins = {"": ""}
    for (_, pieces), (_, texts) in zip(events, out, strict=True):
        for (kind, _), sent in zip(pieces, texts, strict=True):
            joins[kind] = joins.get(kind, "") + sent
            joins[""] += sent
    for joined in joins.values():
        assert key not in joined
        for length in range(StreamTextRedactor.MIN_RUN, len(key) + 1):
            assert key[:length] not in joined


@pytest.mark.asyncio
async def test_a_long_key_whose_held_bytes_hold_the_last_events_still_settles(
    monkeypatch, tmp_path
):
    """The byte redactor holds the key's length less one at the end of the stream; the
    events in that tail are split into lines like any others."""
    long_key = "sk-ant-" + "x7" * 50
    path = _install_accounts(monkeypatch, tmp_path, key=long_key)
    events = [_MESSAGE_START, _text_block(0), _text(0, "hi"), *_MESSAGE_END]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert "message_stop" in _raw_kinds(chunks)
    assert _raw_delta_text(chunks, "text") == "hi"
    row = _attempts(path)[0]
    assert row[2] == "settled", row


# Past the public prefix this key holds only capitals; the filler below uses none.
_RUN_KEY = "sk-ant-" + "QWXVJKPZBMHTRLYC" * 2


def _no_run_of_the_key(joined: str, key: str) -> bool:
    from lionagi.service.accounts._router import StreamTextRedactor

    run = min(StreamTextRedactor.MIN_RUN, len(key))
    return not any(
        segment[start : start + run] in key
        for segment in joined.split("[REDACTED]")
        for start in range(len(segment) - run + 1)
    )


@pytest.mark.parametrize("start", [1, 7, 13, 20])
def test_a_long_run_from_inside_the_key_is_masked(start):
    out = _redacted([("a", [("text", "ab" + _RUN_KEY[start:] + "zz")])], key=_RUN_KEY)
    assert _sent(out) == "ab[REDACTED]zz"


@pytest.mark.parametrize("cut", range(1, 12))
def test_a_run_from_inside_the_key_split_across_events_is_masked_whole(cut):
    span = _RUN_KEY[13:30]
    out = _redacted(
        [("a", [("text", "ab" + span[:cut])]), ("b", [("text", span[cut:] + "zz")])], key=_RUN_KEY
    )
    assert _sent(out) == "ab[REDACTED]zz"


@pytest.mark.parametrize("cut", range(1, 8))
def test_a_run_from_inside_the_key_sends_fewer_than_min_run_before_a_pause(cut):
    """Fewer than MIN_RUN characters cannot be told from other text, so a pause inside
    a run lets them out; the run is masked from there."""
    span = _RUN_KEY[13:30]
    out = _redacted(
        [("a", [("text", "ab" + span[:cut])]), ("pause", []), ("b", [("text", span[cut:] + "zz")])],
        key=_RUN_KEY,
    )
    assert _sent(out) == "ab" + span[:cut] + "[REDACTED]zz"


@pytest.mark.parametrize("cut", range(1, 8))
@pytest.mark.parametrize("key", [_PREFIXED_KEY, _UNPREFIXED_KEY], ids=["sk-ant", "other"])
def test_a_beginning_broken_by_the_next_character_is_decided_as_at_a_pause(key, cut):
    sent = _sent(_redacted([("a", [("text", "ab" + key[:cut])]), ("b", [("text", "zz")])], key=key))
    public = key.startswith("sk-ant-")
    assert sent == "ab" + (key[:cut] if public else "[REDACTED]") + "zz"


def test_a_beginning_inside_a_longer_run_from_inside_the_key_is_still_masked():
    """The beginning breaks while the run around it goes on: its characters stay masked."""
    key = "ABCDEFGH" + "XABCY" + "MNOPQRST"
    sent = _sent(_redacted([("a", [("text", "zz" + "XABCY" + "zz")])], key=key))
    assert sent == "zzX[REDACTED]Yzz"


@pytest.mark.parametrize("seed", range(60))
@pytest.mark.parametrize("prefixed", [True, False], ids=["sk-ant", "other"])
def test_no_join_ever_shows_a_run_of_the_key_or_a_secret_beginning(seed, prefixed):
    """Random texts holding whole keys, beginnings and runs from inside the key, split
    across kinds, citations, pauses and empty deltas: no join shows MIN_RUN characters
    in a row from anywhere in the key, which rules out a beginning past the
    seven-character public prefix, and for a key without the prefix none shows its
    first character."""
    import random

    rng = random.Random(seed)
    body = "".join(rng.choice("QWXVJKPZBMHTRLYCGDNF") for _ in range(rng.randint(20, 60)))
    key = ("sk-ant-" if prefixed else "") + body
    filler_chars = "abcdefghijklmnopqrstuvwxyz0123456789 .\n"
    kinds = ["text_delta", "thinking_delta", "input_json_delta", "citations_delta"]
    text = ""
    for _ in range(rng.randint(1, 4)):
        text += "".join(rng.choice(filler_chars) for _ in range(rng.randint(0, 12)))
        roll = rng.random()
        if roll < 0.4:
            text += key[: rng.randint(1, len(key))]
        elif roll < 0.6:
            text += key
        else:
            start = rng.randrange(len(key))
            text += key[start : rng.randint(start + 1, len(key))]
    events = []
    position = 0
    while position < len(text):
        size = rng.randint(1, 15)
        events.append((len(events), [(rng.choice(kinds), text[position : position + size])]))
        position += size
        if rng.random() < 0.3:
            events.append((len(events), []))
        if rng.random() < 0.1:
            events.append((len(events), [(rng.choice(kinds), "")]))
    out = _redacted(events, key=key)
    joins = {"": ""}
    for (_, pieces), (_, texts) in zip(events, out, strict=True):
        for (kind, _), sent in zip(pieces, texts, strict=True):
            joins[kind] = joins.get(kind, "") + sent
            joins[""] += sent
    for joined in joins.values():
        assert _no_run_of_the_key(joined, key)
        # The filler has no capitals, so a capital is a key character; the public prefix
        # has none. Any shown must come from a run from inside the key, never a beginning.
        shown = joined.replace("[REDACTED]", "")
        if not prefixed:
            assert key[0] not in shown


def test_mask_key_runs_masks_each_long_run_and_keeps_short_ones():
    from pydantic import SecretStr

    from lionagi.service.accounts._router import mask_key_runs

    key = SecretStr(_RUN_KEY)
    assert mask_key_runs("x " + _RUN_KEY[10:20] + " y", key) == "x [REDACTED] y"
    assert mask_key_runs(_RUN_KEY[10:17] + " " + _RUN_KEY[3:10], key) == (
        _RUN_KEY[10:17] + " " + _RUN_KEY[3:10]
    )
    assert mask_key_runs("", key) == ""


def _seen(chunks) -> str:
    return " ".join(
        repr((chunk.content, chunk.tool_input, chunk.tool_output, chunk.metadata))
        for chunk in chunks
    )


@pytest.mark.parametrize(
    "data",
    [CANARY[:20], json.dumps(CANARY[7:]), json.dumps([CANARY[7:]])],
    ids=["bare", "json-string", "json-array"],
)
@pytest.mark.asyncio
async def test_a_line_that_is_not_a_json_object_is_masked(monkeypatch, tmp_path, data):
    _install_accounts(monkeypatch, tmp_path)
    body = _sse([_MESSAGE_START]) + b"data: " + data.encode() + b"\n\n" + _sse(_MESSAGE_END)
    model, _ = _model(monkeypatch, [StubResponse(pieces=[body])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    seen = _seen(chunks)
    assert "[REDACTED]" in seen
    assert _no_run_of_the_key(seen, CANARY)


def _text_chunks(chunks) -> str:
    """The text a consumer joins out of the stream's text chunks, as ``Branch.run`` does."""
    return "".join(chunk.content or "" for chunk in chunks if chunk.type == "text")


def _thinking(index, thinking: str) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "thinking_delta", "thinking": thinking},
    }


# Each middle carries part of the key in an event a consumer receives as a text chunk
# without a text delta's text field: the chunk text is joined with the text deltas'.
_TEXT_CHUNK_MIDDLES = {
    "bare": (13, 20, 27, lambda part: b"data: " + part.encode() + b"\n\n"),
    "json-number": (14, 21, 28, lambda part: b"data: " + part.encode() + b"\n\n"),
    "json-string": (13, 20, 27, lambda part: _sse([part])),
    "data-prefixed": (13, 20, 27, lambda part: b"data: data: " + part.encode() + b"\n\n"),
    "thinking-between": (
        13,
        20,
        27,
        lambda part: _sse([_thinking(1, "zz")]) + b"data: " + part.encode() + b"\n\n",
    ),
    "text-delta-without-text": (
        13,
        20,
        27,
        lambda part: _sse(
            [
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "", "thinking": part},
                }
            ]
        ),
    ),
    "output-text-delta": (
        13,
        20,
        27,
        lambda part: _sse([{"type": "response.output_text.delta", "delta": part}]),
    ),
}


@pytest.mark.parametrize("middle", list(_TEXT_CHUNK_MIDDLES))
@pytest.mark.asyncio
async def test_the_middle_of_a_key_across_text_chunks_of_every_shape_is_masked_when_joined(
    monkeypatch, tmp_path, middle
):
    """Text deltas around another event whose chunk is text: a consumer that joins the
    text chunks sees the key's end whole, so the join is matched as one and the key is
    masked, the middle chunk's text included. With a thinking delta between, the text
    join pauses there, so the seven held characters before it are sent."""
    start, cut, end, render = _TEXT_CHUNK_MIDDLES[middle]
    _install_accounts(monkeypatch, tmp_path)
    body = (
        _sse([_MESSAGE_START, _text_block(0), _text(0, CANARY[start:cut])])
        + render(CANARY[cut:end])
        + _sse([_text(0, CANARY[end:]), *_MESSAGE_END])
    )
    model, _ = _model(monkeypatch, [StubResponse(pieces=[body])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    joined = _text_chunks(chunks)
    if middle == "thinking-between":
        # The thinking event carries no text into the text join, so it is a pause: the
        # held run of seven is sent, and the rest of the key after it is masked.
        assert joined == CANARY[start:cut] + "[REDACTED]"
    else:
        assert joined == "[REDACTED]"
    assert _no_run_of_the_key(_seen(chunks), CANARY)


@pytest.mark.parametrize("cut", [1, 4, 9])
def test_a_chunk_join_links_a_piece_and_takes_text_no_piece_holds(cut):
    """The chunk join holds the linked piece's own characters and standalone text, in
    order: a key split between them is masked in both the piece and the chunk."""
    from pydantic import SecretStr

    from lionagi.service.accounts._router import StreamTextRedactor

    redactor = StreamTextRedactor(SecretStr(_RUN_KEY))
    join = "text"
    run = _RUN_KEY[10:22]
    out = redactor.push("a", [("text_delta", "ab" + run[:cut])], (join, 0))
    out += redactor.push("b", [(StreamTextRedactor.RAW, "x")], (join, run[cut:] + "zz"))
    out += redactor.finish()

    assert [name for name, _ in out] == ["a", "b"]
    (_, (piece, linked)), (_, (raw, standalone)) = out
    assert piece == linked == "ab[REDACTED]"
    assert raw == "x"
    assert standalone == "zz"


def _all_chunk_text(chunks) -> str:
    """The text of every chunk a consumer receives, joined in order across types."""
    return "".join(chunk.content for chunk in chunks if isinstance(chunk.content, str))


def _signature(index, signature: str) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "signature_delta", "signature": signature},
    }


# Two chunk texts that hold the middle of the key, with what sits between them.
_ACROSS_CHUNK_TYPES = {
    "thinking-signature-text": [
        _thinking(0, CANARY[13:20]),
        _signature(0, "sig"),
        _text(1, CANARY[20:27]),
    ],
    "thinking-citation-text": [
        _thinking(0, CANARY[13:20]),
        _citation(1, "quoted"),
        _text(1, CANARY[20:27]),
    ],
    "text-error-text": [
        _text(0, CANARY[13:20]),
        {"type": "error", "error": CANARY[20:21]},
        _text(0, CANARY[21:27]),
    ],
}


@pytest.mark.parametrize("between", list(_ACROSS_CHUNK_TYPES))
@pytest.mark.asyncio
async def test_a_key_across_chunks_of_different_types_never_reaches_their_joined_text(
    monkeypatch, tmp_path, between
):
    """A consumer that joins the text of every chunk it receives, whatever the type, sees
    the key's middle whole: that join is matched too."""
    _install_accounts(monkeypatch, tmp_path)
    body = _sse([_MESSAGE_START, *_ACROSS_CHUNK_TYPES[between], *_MESSAGE_END])
    model, _ = _model(monkeypatch, [StubResponse(pieces=[body])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    joined = _all_chunk_text(chunks)
    assert "[REDACTED]" in joined
    assert _no_run_of_the_key(joined, CANARY)
    assert _no_run_of_the_key(_seen(chunks), CANARY)


@pytest.mark.parametrize("delta_type", ["\x00text chunks", "text", "chunks", "all", "data"])
@pytest.mark.asyncio
async def test_no_delta_type_names_a_chunk_join(monkeypatch, tmp_path, delta_type):
    """A textless delta is a pause for the text chunks' join, whatever type it names. A
    thinking chunk breaks the join of all chunks and the tail is no text delta, so only
    the text chunks' join can see the key's middle: the held run before the pause is
    sent and the rest of the key masked."""
    _install_accounts(monkeypatch, tmp_path)
    middle = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": delta_type, "future": "Z"},
    }
    body = _sse(
        [
            _MESSAGE_START,
            _text_block(0),
            _text(0, CANARY[13:20]),
            _thinking(1, "zz"),
            middle,
            {"type": "response.output_text.delta", "delta": CANARY[20:27]},
            *_MESSAGE_END,
        ]
    )
    model, _ = _model(monkeypatch, [StubResponse(pieces=[body])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    assert _text_chunks(chunks) == CANARY[13:20] + "[REDACTED]"


@pytest.mark.parametrize("chunk_type", ["text", "thinking"])
@pytest.mark.asyncio
async def test_a_chunk_types_join_holds_its_delta_and_its_standalone_text(
    monkeypatch, tmp_path, chunk_type
):
    """A delta's chunk and a chunk no delta field holds, of one type, with a chunk of the
    other type between: only that type's chunk join sees the key's middle whole."""
    _install_accounts(monkeypatch, tmp_path)
    other = "thinking" if chunk_type == "text" else "text"
    deltas = {"text": _text, "thinking": _thinking}
    if chunk_type == "text":
        # A bare data line is a text chunk whose text no delta field holds.
        tail = b"data: " + CANARY[20:27].encode() + b"\n\n"
    else:
        # A thinking delta whose chunk shows a field its type does not name, so no
        # piece holds the text.
        tail = _sse(
            [
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "text": CANARY[20:27]},
                }
            ]
        )
    body = (
        _sse([_MESSAGE_START, deltas[chunk_type](0, CANARY[13:20]), deltas[other](1, "zz")])
        + tail
        + _sse(_MESSAGE_END)
    )
    model, _ = _model(monkeypatch, [StubResponse(pieces=[body])])

    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]

    joined = "".join(chunk.content or "" for chunk in chunks if chunk.type == chunk_type)
    assert joined == CANARY[13:20] + "[REDACTED]"


def test_an_events_raw_data_is_joined_only_with_other_raw_data():
    """The data of an event with no known text field is not joined with the pieces: its
    chunk's text is, once, so the data is not matched twice in a row."""
    from pydantic import SecretStr

    from lionagi.service.accounts._router import StreamTextRedactor

    key = "sk-ant-QWXVJKPZ" + "wxabcdabcd" + "MNHTRLYC"
    redactor = StreamTextRedactor(SecretStr(key))
    out = redactor.push("a", [("text_delta", "wx")])
    out += redactor.push("b", [(StreamTextRedactor.RAW, "abcd")], ("text", "abcd"))
    out += redactor.finish()

    assert _sent(out) == "wx" + "abcd" + "abcd"


@pytest.mark.parametrize("cut", [1, 5, 12, 19])
@pytest.mark.asyncio
async def test_an_unknown_delta_type_is_masked_and_joined_like_a_known_one(
    monkeypatch, tmp_path, cut
):
    events = [
        _MESSAGE_START,
        _text_block(0),
        _delta("future_delta", "text", "ab" + CANARY[:cut]),
        _delta("future_delta", "text", CANARY[cut:] + "cd"),
        {"type": "content_block_stop", "index": 0},
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    assert _raw_delta_text(chunks, "text") == "ab[REDACTED]cd"


@pytest.mark.asyncio
async def test_a_run_of_the_key_in_any_other_string_of_an_event_is_masked(monkeypatch, tmp_path):
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": ""}}
    error["error"]["message"] = "bad key " + CANARY[3:25]
    events = [_MESSAGE_START, error]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    seen = _seen(chunks)
    assert "bad key [REDACTED]" in seen
    assert _no_run_of_the_key(seen, CANARY)


def _every_linked_error(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    todo = [error]
    while todo:
        current = todo.pop()
        if current is None or any(current is seen for seen in found):
            continue
        found.append(current)
        todo += [current.__cause__, current.__context__]
    return found


def _library_frame_locals(errors: list[BaseException]) -> list[object]:
    found: list[object] = []
    for error in errors:
        tb = error.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if frame.f_globals.get("__name__", "").startswith("lionagi."):
                found += list(frame.f_locals.values())
            tb = tb.tb_next
    return found


class _UnreadableResponse(StubResponse):
    def json(self):
        raise ValueError("cannot decode " + CANARY)


class _FailingResponse:
    """A response whose status read fails the way a dropped connection does."""

    closed = False

    def __init__(self, error: BaseException) -> None:
        self.error = error

    @property
    def status_code(self):
        raise self.error

    def release(self):
        self.closed = True


# --- a failed attempt clears the account's warm rows; a pending cancellation never decides that

HOUR_REQUEST = {
    **REQUEST,
    "system": [
        {
            "type": "text",
            "text": "Stable instructions that every call of this test repeats. " * 40,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ],
}
# The transport answers after the caller's deadline and without yielding to the event loop, so
# the cancellation is still pending when the attempt's outcome is recorded.
DEADLINE = 0.25
TRANSPORT_DELAY = 0.35


class _LateSession(StubSession):
    def _request(self, method, url, **kwargs):
        time.sleep(TRANSPORT_DELAY)
        return super()._request(method, url, **kwargs)


class _HangingEntry:
    """A session that never finishes opening: the request has not entered when it is cancelled."""

    async def __aenter__(self):
        await anyio.sleep_forever()

    async def __aexit__(self, *_):
        return False


def _hour_write_usage(write: int = 2000) -> dict:
    return {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": write,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": write},
    }


def _warm_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT account, cert FROM warm_prefix ORDER BY account"
        ).fetchall()


def _block_reason(path, account):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT block_reason FROM account WHERE provider = 'anthropic' AND name = ?",
            (account,),
        ).fetchone()[0]


async def _prime(monkeypatch, tmp_path, responses):
    """A routed model whose first call writes a one-hour warm row; later calls take ``responses``."""
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response(usage=_hour_write_usage()), *responses])
    model.endpoint.config.max_retries = 0
    await model.endpoint.call(HOUR_REQUEST)
    rows = _warm_rows(path)
    assert len(rows) == 1, rows
    return path, model, transport, rows


def _answer_late(model, transport):
    model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: _LateSession(
        transport, timeout=timeout
    )


class _ShieldedLateSession(StubSession):
    """Answers after the caller's deadline, from a shielded wait: the deadline passes while the
    request is in flight and its cancellation is still pending when the response arrives."""

    async def request(self, method, url, **kwargs):
        with anyio.CancelScope(shield=True):
            await anyio.sleep(TRANSPORT_DELAY)
        return await super().request(method, url, **kwargs)


def _answer_late_shielded(model, transport):
    model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: (
        _ShieldedLateSession(transport, timeout=timeout)
    )


async def _send(model, streamed):
    if streamed:
        async for _ in model.endpoint.stream(HOUR_REQUEST):
            pass
    else:
        await model.endpoint.call(HOUR_REQUEST)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["call", "stream"])
@pytest.mark.parametrize(
    ("status", "outcome", "blocked"),
    [
        (429, "rate_limited", None),
        (401, "auth_required", "auth_required"),
        (500, "provider_error", None),
        (400, "request_rejected", None),
    ],
    ids=["429", "401", "500", "400"],
)
async def test_a_pending_cancellation_is_delivered_after_a_failed_status_is_recorded(
    monkeypatch, tmp_path, streamed, status, outcome, blocked
):
    path, model, transport, _ = await _prime(
        monkeypatch, tmp_path, [StubResponse(status, {"error": CANARY})]
    )
    _answer_late(model, transport)

    with pytest.raises(TimeoutError):
        with anyio.fail_after(DEADLINE):
            await _send(model, streamed)

    attempt = _attempts(path)[-1]
    assert attempt[4] == outcome, attempt
    assert _block_reason(path, attempt[1]) == blocked
    assert _warm_rows(path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["call", "stream"])
async def test_a_pending_cancellation_is_delivered_after_a_transport_failure_is_recorded(
    monkeypatch, tmp_path, streamed
):
    failing = _FailingResponse(httpx.ConnectError("connection dropped"))
    path, model, transport, _ = await _prime(monkeypatch, tmp_path, [failing])
    _answer_late(model, transport)

    with pytest.raises(TimeoutError):
        with anyio.fail_after(DEADLINE):
            await _send(model, streamed)

    attempt = _attempts(path)[-1]
    assert attempt[4] == "transport_error", attempt
    assert _warm_rows(path) == []


@pytest.mark.asyncio
async def test_a_pending_cancellation_is_delivered_when_the_ledger_refuses_a_settlement(
    monkeypatch, tmp_path
):
    """A ledger refusal on the call path is raised as a routed error, but a cancellation
    already pending when it happens reaches the caller instead."""
    path, model, transport, _ = await _prime(
        monkeypatch, tmp_path, [_response(usage=_hour_write_usage())]
    )
    _answer_late(model, transport)

    async def refuse(self, *args, **kwargs):
        # Like the real write, the refusal runs shielded: the deadline passes while it runs
        # and the cancellation is left pending when the error is raised.
        with anyio.CancelScope(shield=True):
            await anyio.sleep(0.05)
        raise AccountRoutingError(RouterErrorCode.LEDGER_BUSY)

    monkeypatch.setattr(router_module.AccountRouter, "settle_response", refuse)

    with pytest.raises(TimeoutError):
        with anyio.fail_after(DEADLINE):
            await model.endpoint.call(HOUR_REQUEST)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["call", "stream"])
async def test_a_cancellation_before_the_request_enters_leaves_the_warm_rows(
    monkeypatch, tmp_path, streamed
):
    path, model, _, rows = await _prime(monkeypatch, tmp_path, [])
    model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: _HangingEntry()

    with anyio.move_on_after(DEADLINE):
        await _send(model, streamed)

    assert len(_attempts(path)) == 2
    assert _attempts(path)[-1][4] != "transport_error"
    assert _warm_rows(path) == rows


@pytest.mark.asyncio
async def test_an_unreadable_body_is_not_chained_to_the_error(monkeypatch, tmp_path):
    from lionagi.service.connections.endpoint import _RoutedNoRetryError

    _install_accounts(monkeypatch, tmp_path)
    model, _ = _model(monkeypatch, [_UnreadableResponse(), _UnreadableResponse()])

    with pytest.raises(AccountRoutingError) as caught:
        await model.endpoint.call(REQUEST)
    # The request layer's own error, before a retry layer replaces its context.
    payload, headers = model.endpoint.create_payload(REQUEST)
    with pytest.raises(_RoutedNoRetryError) as attempt:
        await model.endpoint._request_attempt(
            payload=payload,
            headers=headers,
            body_kwargs={"json": payload},
            request_kwargs={},
            response_mode="json",
            error_context="Request",
        )

    for error in (caught.value, attempt.value):
        linked = _every_linked_error(error)
        assert not any(isinstance(item, ValueError) for item in linked)
        assert not any(CANARY in repr(item.args) for item in linked)


@pytest.mark.parametrize("ledger", ["up", "down"])
@pytest.mark.parametrize("streamed", [True, False], ids=["stream", "call"])
@pytest.mark.asyncio
async def test_a_transport_error_is_not_chained_to_the_error(
    monkeypatch, tmp_path, streamed, ledger
):
    """With the ledger down, its own error is what the caller gets, and it chains the
    transport error no more than the routed error does."""
    import httpx

    _install_accounts(monkeypatch, tmp_path)
    if ledger == "down":

        async def settle_unknown(self, *args, **kwargs):
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(router_module.AccountRouter, "settle_unknown", settle_unknown)
    failing = httpx.ConnectError("x-api-key: " + CANARY)
    response = StubResponse(pieces=[failing]) if streamed else _FailingResponse(failing)
    model, _ = _model(monkeypatch, [response])
    model.endpoint.config.max_retries = 0

    expected = AccountRoutingError if ledger == "up" else RuntimeError
    with pytest.raises(expected) as caught:
        if streamed:
            async for _ in model.endpoint.stream(REQUEST):
                pass
        else:
            await model.endpoint.call(REQUEST)

    linked = _every_linked_error(caught.value)
    assert not any(isinstance(error, httpx.RequestError) for error in linked)


def _chunk_raw_dicts(chunks) -> list[dict]:
    raw = []
    for chunk in chunks:
        metadata = chunk.metadata
        if isinstance(metadata, dict) and isinstance(metadata.get("raw"), dict):
            raw.append(metadata["raw"])
    return raw


@pytest.mark.asyncio
async def test_an_unknown_delta_types_field_name_is_masked_in_the_consumer_chunk(
    monkeypatch, tmp_path
):
    """A field name of a delta this module does not know holds a run of the key: the name
    the consumer's chunk carries is the masked one, and the value stays under it."""
    run = CANARY[4:20]
    events = [
        _MESSAGE_START,
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "future_delta", run: "x"},
        },
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    seen = _seen(chunks)
    assert _no_run_of_the_key(seen, CANARY)
    deltas = [
        raw.get("delta")
        for raw in _chunk_raw_dicts(chunks)
        if raw.get("type") == "content_block_delta"
    ]
    assert any(
        isinstance(delta, dict)
        and delta.get("type") == "future_delta"
        and delta.get("[REDACTED]") == "x"
        and run not in delta
        for delta in deltas
    )


@pytest.mark.asyncio
async def test_a_run_in_a_name_outside_an_unknown_delta_is_still_masked(monkeypatch, tmp_path):
    """A name holding a run in a delta this module knows, and in an event with no delta,
    is masked where it stands."""
    run = CANARY[4:20]
    events = [
        _MESSAGE_START,
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi", run: "x"},
        },
        {"type": "ping", run: "x"},
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    seen = _seen(chunks)
    assert _no_run_of_the_key(seen, CANARY)
    raw = _chunk_raw_dicts(chunks)
    delta = next(item["delta"] for item in raw if item.get("type") == "content_block_delta")
    assert delta == {"type": "text_delta", "text": "hi", "[REDACTED]": "x"}
    ping = next(item for item in raw if item.get("type") == "ping")
    assert ping == {"type": "ping", "[REDACTED]": "x"}


@pytest.mark.asyncio
async def test_a_whole_key_as_an_unknown_delta_field_name_is_masked(monkeypatch, tmp_path):
    """A field name of an unknown delta that is the whole key is replaced by the mask,
    and the value stays under it."""
    events = [
        _MESSAGE_START,
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "future_delta", CANARY: "x"},
        },
        *_MESSAGE_END,
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    assert _no_run_of_the_key(_seen(chunks), CANARY)
    delta = next(
        item["delta"]
        for item in _chunk_raw_dicts(chunks)
        if item.get("type") == "content_block_delta"
    )
    assert delta == {"type": "future_delta", "[REDACTED]": "x"}


@pytest.mark.asyncio
async def test_an_unknown_delta_types_renamed_field_name_keeps_the_taken_masked_name(
    monkeypatch, tmp_path
):
    """A name that masks to one the delta already has is numbered, so both values stay
    reachable under the mask."""
    run = CANARY[4:20]
    events = [
        _MESSAGE_START,
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "future_delta", "[REDACTED]": "y", run: "x"},
        },
    ]

    chunks = await _stream_events(monkeypatch, tmp_path, events)

    seen = _seen(chunks)
    assert _no_run_of_the_key(seen, CANARY)
    deltas = [
        raw.get("delta")
        for raw in _chunk_raw_dicts(chunks)
        if raw.get("type") == "content_block_delta"
    ]
    assert any(
        isinstance(delta, dict)
        and delta.get("type") == "future_delta"
        and delta.get("[REDACTED]") == "y"
        and delta.get("[REDACTED] (2)") == "x"
        and run not in delta
        for delta in deltas
    )


@pytest.mark.asyncio
async def test_a_streamed_input_count_holding_a_run_still_settles(monkeypatch, tmp_path):
    """A usage count whose digits hold a run of the key is masked in what the consumer
    receives, but the attempt is priced from the provider's own count, exactly as the same
    count settles on a non-streamed body."""
    key = "sk-ant-api03-ab34567890123cd"
    # The count's digits contain an eight-character run of the key.
    count = 345678901
    path = _install_accounts(monkeypatch, tmp_path, key=key)

    stream_events = [copy.deepcopy(_MESSAGE_START)]
    stream_events[0]["message"]["usage"]["input_tokens"] = count
    stream_events += [
        _PARTIAL_TEXT,
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(stream_events)])])
    chunks = [chunk async for chunk in model.endpoint.stream(REQUEST)]
    streamed = _attempts(path)[0]

    body = _response(usage={"input_tokens": count, "output_tokens": 5}).body
    body["id"] = "msg_body"
    plain_model, _ = _model(monkeypatch, [StubResponse(body=body)])
    await plain_model.endpoint.call(REQUEST)
    plain = _attempts(path)[-1]

    assert _no_run_of_the_key(_seen(chunks), key)
    assert streamed[2:] == ("settled", plain[3], "complete"), (streamed, plain)


def _request_ids(path) -> list:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT request_id FROM attempt ORDER BY rowid").fetchall()
    return [row[0] for row in rows]


def _stream_with_id(message_id: str) -> list[bytes]:
    start = copy.deepcopy(_MESSAGE_START)
    start["message"]["id"] = message_id
    return [
        _sse(
            [
                start,
                {"type": "message_delta", "usage": {"output_tokens": 5}},
                {"type": "message_stop"},
            ]
        )
    ]


@pytest.mark.asyncio
async def test_a_message_id_holding_a_run_of_the_key_is_not_recorded(monkeypatch, tmp_path):
    """The attempt keeps the provider's message id only when the id holds no run of the
    key, streamed or not; the attempt settles either way."""
    key = "sk-ant-api03-ab34567890123cd"
    held = "msg_34567890123c"
    path = _install_accounts(monkeypatch, tmp_path, key=key)

    for message_id in (held, "msg_clean"):
        model, _ = _model(monkeypatch, [StubResponse(pieces=_stream_with_id(message_id))])
        assert [chunk async for chunk in model.endpoint.stream(REQUEST)]
    for message_id in (held, "msg_body"):
        body = _response().body
        body["id"] = message_id
        model, _ = _model(monkeypatch, [StubResponse(body=body)])
        await model.endpoint.call(REQUEST)

    assert _request_ids(path) == [None, "msg_clean", None, "msg_body"]
    assert [row[2:] for row in _attempts(path)] == [
        ("settled", row[3], "complete") for row in _attempts(path)
    ]


@pytest.mark.asyncio
async def test_a_message_start_whose_message_is_not_an_object_changes_nothing(
    monkeypatch, tmp_path
):
    """A second message_start carrying no message object leaves the model, id and usage
    the first one gave, so the attempt still settles complete."""
    path = _install_accounts(monkeypatch, tmp_path)
    events = [
        copy.deepcopy(_MESSAGE_START),
        {"type": "message_start", "message": "junk"},
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    control_model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events[:1] + events[2:])])])
    assert [chunk async for chunk in control_model.endpoint.stream(REQUEST)]

    junk, control = _attempts(path)
    assert junk[2:] == control[2:] == ("settled", control[3], "complete"), (junk, control)
    assert _request_ids(path) == ["msg_s", "msg_s"]


def test_renaming_many_masked_names_rebuilds_the_object_once():
    """Every name holding a run is renamed in its place and numbered as one rename after
    another would number it, and the object is rebuilt once, not once per name."""
    from pydantic import SecretStr

    from lionagi.providers.anthropic.messages import _rename_masked_names
    from lionagi.service.accounts._router import mask_key_runs

    key = SecretStr("sk-ant-api03-ab34567890123cd")
    run = "34567890123c"

    def sequential(value: dict, names: list) -> None:
        for name in names:
            masked = mask_key_runs(name, key)
            if masked == name:
                continue
            taken, suffix = masked, 2
            while taken in value:
                taken, suffix = f"{masked} ({suffix})", suffix + 1
            items = [(taken if old == name else old, item) for old, item in value.items()]
            value.clear()
            value.update(items)

    class Counting(dict):
        clears = 0

        def clear(self):
            self.clears += 1
            super().clear()

    names = [mask_key_runs(f"a{run}", key)]
    for index in range(2000):
        names += [f"a{run}", f"clean{index}"] if index == 0 else [f"{run}{index}", f"clean{index}"]
    names.append(f"b{run}")
    names.append(mask_key_runs(f"b{run}", key) + " (2)")
    entries = [(name, position) for position, name in enumerate(names)]

    expected = dict(entries)
    sequential(expected, list(expected))
    value = Counting(entries)
    renames = _rename_masked_names(value, list(value), key)

    assert list(value.items()) == list(expected.items())
    assert value.clears == 1
    assert len(renames) == sum(mask_key_runs(name, key) != name for name in names) == 2001


@pytest.mark.asyncio
async def test_a_later_message_starts_placeholder_count_does_not_undo_a_final_one(
    monkeypatch, tmp_path
):
    """A message_delta's output count is the final one for the attempt; a message_start
    after it carries a placeholder count and must not replace it, so the attempt settles
    at the same cost as a stream that ends where the delta does."""
    path = _install_accounts(monkeypatch, tmp_path)
    start = copy.deepcopy(_MESSAGE_START)
    start["message"]["usage"]["output_tokens"] = 1
    final = {"type": "message_delta", "usage": {"output_tokens": 5}}
    events = [start, _PARTIAL_TEXT, final, copy.deepcopy(start), {"type": "message_stop"}]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    control_model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events[:3] + events[4:])])])
    assert [chunk async for chunk in control_model.endpoint.stream(REQUEST)]

    late, control = _attempts(path)
    assert late[2:] == control[2:] == ("settled", 250_000, "complete"), (late, control)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_model", ["claude-opus-5-5", "claude-not-priced"])
async def test_a_later_message_start_does_not_reprice_a_final_count(
    monkeypatch, tmp_path, late_model
):
    """A message_start after the final count names no model for that count: the attempt
    settles at the price of the model that produced it, and a start naming an unpriced
    model does not leave the attempt open."""
    path = _install_accounts(monkeypatch, tmp_path)
    start = copy.deepcopy(_MESSAGE_START)
    start["message"]["usage"]["output_tokens"] = 1
    final = {"type": "message_delta", "usage": {"output_tokens": 5}}
    late = {"type": "message_start", "message": {"id": "msg_s", "model": late_model}}
    events = [start, _PARTIAL_TEXT, final, late, {"type": "message_stop"}]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    control_model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events[:3] + events[4:])])])
    assert [chunk async for chunk in control_model.endpoint.stream(REQUEST)]

    late_row, control = _attempts(path)
    assert late_row[2:] == control[2:] == ("settled", 250_000, "complete"), (late_row, control)


def test_a_masked_name_taking_a_recorded_place_still_masks_its_value():
    """A name renamed onto one that a recorded place names, though the object carries no
    such name, is no exemption: the value of the renamed entry is still masked."""
    from pydantic import SecretStr

    from lionagi.providers.anthropic.messages import _mask_key_runs_in_strings
    from lionagi.service.accounts._router import mask_key_runs

    key = SecretStr(CANARY)
    run = CANARY[4:20]
    value = {run: CANARY}
    assert mask_key_runs(run, key) == "[REDACTED]"

    changed = _mask_key_runs_in_strings(value, key, {(id(value), "[REDACTED]")})

    assert changed is True
    assert value == {"[REDACTED]": "[REDACTED]"}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, RouterErrorCode.AUTH_FAILED),
        (403, RouterErrorCode.AUTH_FAILED),
        (400, RouterErrorCode.REQUEST_REJECTED),
    ],
)
@pytest.mark.parametrize("streamed", [False, True], ids=["call", "stream"])
@pytest.mark.asyncio
async def test_a_routed_refusal_keeps_an_acyclic_cause_chain_without_key_text(
    monkeypatch, tmp_path, streamed, status, code
):
    """A routed 401, 403 or 400 reaches the caller as the public routed error,
    carrying no key text in its linked errors, and a walk of ``__cause__`` from it ends
    instead of looping."""
    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [StubResponse(status, {"error": CANARY})])

    with pytest.raises(AccountRoutingError) as caught:
        if streamed:
            async for _ in model.endpoint.stream(REQUEST):
                pass
        else:
            await model.endpoint.call(REQUEST)

    assert type(caught.value) is AccountRoutingError
    assert caught.value.code is code
    assert len(transport.calls) == 1

    seen, cause = set(), caught.value
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        assert cause.__cause__ is not cause
        cause = cause.__cause__
    assert len(seen) <= 4

    linked = _every_linked_error(caught.value)
    assert not any(CANARY in repr(item.args) for item in linked)


@pytest.mark.asyncio
async def test_a_later_message_start_that_omits_a_field_keeps_what_the_first_one_said(
    monkeypatch, tmp_path
):
    """A second message_start that leaves out the model, the id or the usage says nothing
    about that field, so the attempt keeps the first one's values and settles complete;
    its own clean id still takes the recorded id's place."""
    path = _install_accounts(monkeypatch, tmp_path)
    final = {"type": "message_delta", "usage": {"output_tokens": 5}}
    omitted = {"type": "message_start", "message": {"id": "msg_b"}}
    events = [
        copy.deepcopy(_MESSAGE_START),
        _PARTIAL_TEXT,
        final,
        omitted,
        {"type": "message_stop"},
    ]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    (row,) = _attempts(path)
    assert row[2:] == ("settled", 250_000, "complete"), row
    assert _request_ids(path) == ["msg_b"]


@pytest.mark.asyncio
async def test_a_later_message_starts_id_that_holds_a_run_keeps_the_recorded_one(
    monkeypatch, tmp_path
):
    """An id holding a run is not recorded, so it leaves a clean id an earlier
    message_start gave where it was rather than clearing it; a clean one still takes
    that place."""
    key = "sk-ant-api03-ab34567890123cd"
    path = _install_accounts(monkeypatch, tmp_path, key=key)
    held = {"type": "message_start", "message": {"id": "msg_34567890123c"}}
    events = [
        copy.deepcopy(_MESSAGE_START),
        _PARTIAL_TEXT,
        held,
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    (row,) = _attempts(path)
    assert row[2:] == ("settled", row[3], "complete"), row
    assert _request_ids(path) == ["msg_s"]


@pytest.mark.asyncio
@pytest.mark.parametrize("late_model", ["claude-opus-5-5", "claude-not-priced"])
@pytest.mark.parametrize("late_delta_first", [False, True], ids=["start-then-count", "count-first"])
async def test_a_count_after_starts_naming_two_models_leaves_the_attempt_unpriced(
    monkeypatch, tmp_path, late_model, late_delta_first
):
    """A count that arrives after message_starts naming two models cannot be tied to one
    of them, so the attempt is not priced at either: it stays open with its ceiling held.
    ``count-first`` has a final count before the second start and another after it;
    ``start-then-count`` has both starts before the only count."""
    path = _install_accounts(monkeypatch, tmp_path)
    start = copy.deepcopy(_MESSAGE_START)
    start["message"]["usage"]["output_tokens"] = 1
    late = {"type": "message_start", "message": {"id": "msg_s", "model": late_model}}
    first = {"type": "message_delta", "usage": {"output_tokens": 5}}
    second = {"type": "message_delta", "usage": {"output_tokens": 7}}
    if late_delta_first:
        events = [start, _PARTIAL_TEXT, first, late, second, {"type": "message_stop"}]
    else:
        events = [start, late, _PARTIAL_TEXT, first, {"type": "message_stop"}]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    (row,) = _attempts(path)
    assert row[2:] == ("open", None, "usage_incomplete"), row


@pytest.mark.asyncio
async def test_usage_after_starts_naming_two_models_leaves_the_attempt_unpriced(
    monkeypatch, tmp_path
):
    """Usage that arrives after a second message_start names another model has no one model
    to price it at, even when it carries no output count, so the attempt stays open with its
    ceiling held instead of settling at the first model's price."""
    path = _install_accounts(monkeypatch, tmp_path)
    start = copy.deepcopy(_MESSAGE_START)
    start["message"]["usage"]["output_tokens"] = 1
    late = {"type": "message_start", "message": {"id": "msg_s", "model": "claude-opus-5-5"}}
    events = [
        start,
        _PARTIAL_TEXT,
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        late,
        {"type": "message_delta", "usage": {"input_tokens": 120}},
        {"type": "message_stop"},
    ]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    (row,) = _attempts(path)
    assert row[2:] == ("open", None, "usage_incomplete"), row


@pytest.mark.asyncio
async def test_a_message_start_whose_model_is_not_a_string_keeps_the_earlier_model(
    monkeypatch, tmp_path
):
    """A message_start whose model is not a string says nothing about the model, so the
    earlier start's model prices the attempt."""
    path = _install_accounts(monkeypatch, tmp_path)
    events = [
        copy.deepcopy(_MESSAGE_START),
        _PARTIAL_TEXT,
        {"type": "message_start", "message": {"id": "msg_b", "model": 5}},
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    model, _ = _model(monkeypatch, [StubResponse(pieces=[_sse(events)])])
    assert [chunk async for chunk in model.endpoint.stream(REQUEST)]

    (row,) = _attempts(path)
    assert row[2:] == ("settled", 250_000, "complete"), row


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["call", "stream"])
async def test_a_cancel_while_an_auth_failure_is_recorded_keeps_the_account_blocked(
    monkeypatch, tmp_path, streamed
):
    """A native cancel that arrives while a 401's attempt is being written returns before
    that write commits and stops any write after it, so the account block is written first:
    the refused key is not reserved again."""
    path = _install_accounts(monkeypatch, tmp_path)
    router = router_module.get_router()
    in_write = threading.Event()
    write_done = threading.Event()
    original = router.ledger._settle_sync

    def slow_auth_settle(attempt_id, evidence, *rest):
        auth = getattr(evidence, "reason_code", None) == "auth_required"
        try:
            if auth:
                in_write.set()
                time.sleep(0.2)
            return original(attempt_id, evidence, *rest)
        finally:
            if auth:
                write_done.set()

    monkeypatch.setattr(router.ledger, "_settle_sync", slow_auth_settle)
    model, _ = _model(monkeypatch, [StubResponse(401, {"error": CANARY})])

    async def consume():
        if streamed:
            async for _ in model.endpoint.stream(REQUEST):
                pass
        else:
            await model.endpoint.call(REQUEST)

    task = asyncio.create_task(consume())
    for _ in range(1000):
        if in_write.is_set() or task.done():
            break
        await asyncio.sleep(0.005)
    assert in_write.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(write_done.wait, 5)

    with sqlite3.connect(path) as connection:
        reason = connection.execute(
            "SELECT block_reason FROM account WHERE name = 'primary'"
        ).fetchone()[0]
    assert reason == "auth_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refused", "missing-row"])
@pytest.mark.parametrize("streamed", [False, True], ids=["call", "stream"])
async def test_a_failed_ledger_write_does_not_replace_the_cancellation(
    monkeypatch, tmp_path, streamed, failure
):
    """When the cancelled attempt's write fails, refused by the ledger or for any other
    reason, the caller still gets CancelledError, not the write's error."""
    _install_accounts(monkeypatch, tmp_path)

    async def refuse(self, attempt, usage, *, reason="usage_incomplete"):
        if failure == "refused":
            raise AccountRoutingError(RouterErrorCode.LEDGER_BUSY)
        raise LookupError("no attempt row")

    monkeypatch.setattr(router_module.AccountRouter, "settle_stream_end", refuse)
    model = iModel(provider="anthropic", model=MODEL)
    model.endpoint._assert_ssrf_safe_url = lambda: None
    entered_request = asyncio.Event()
    never_finish = asyncio.Event()

    class SendingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def request(self, **_):
            entered_request.set()
            await never_finish.wait()

        @asynccontextmanager
        async def stream(self, **_):
            entered_request.set()
            await never_finish.wait()
            yield

    model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: SendingSession()

    async def consume():
        if streamed:
            async for _ in model.endpoint.stream(REQUEST):
                pass
        else:
            await model.endpoint.call(REQUEST)

    task = asyncio.create_task(consume())
    await entered_request.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- a pending cancellation at a 200, a consumer that closes the stream, a stream already settled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "outcome", "blocked"),
    [
        ({"input_tokens": 10, "output_tokens": 5}, "usage_incomplete", None),
        (
            {**_hour_write_usage(), "output_tokens": 10_000_000},
            "complete",
            "ceiling_exceeded",
        ),
    ],
    ids=["usage_incomplete", "over_the_ceiling"],
)
async def test_a_pending_cancellation_is_delivered_after_a_200_is_settled(
    monkeypatch, tmp_path, usage, outcome, blocked
):
    """A 200 whose usage cannot be priced, or prices over the ceiling, still clears the warm
    rows (and blocks the account in the second case) when the deadline has already passed:
    the response arrives after the deadline, so the cancellation is pending while it is
    settled. The call returns the response; the caller's next checkpoint raises the timeout."""
    path, model, transport, _ = await _prime(monkeypatch, tmp_path, [_response(usage=usage)])
    _answer_late_shielded(model, transport)

    started = anyio.current_time()
    returned_after: list[float] = []
    with pytest.raises(TimeoutError):
        with anyio.fail_after(DEADLINE):
            await model.endpoint.call(HOUR_REQUEST)
            returned_after.append(anyio.current_time() - started)
            await anyio.sleep(0)
    assert returned_after and returned_after[0] > DEADLINE

    attempt = _attempts(path)[-1]
    assert attempt[4] == outcome, attempt
    assert _block_reason(path, attempt[1]) == blocked
    assert _warm_rows(path) == []


_ERROR_EVENT = {"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
_HOUR_MESSAGE_START = {
    "type": "message_start",
    "message": {
        "id": "msg_s",
        "model": MODEL,
        "usage": {**_hour_write_usage(), "output_tokens": 0},
    },
}
_FINAL_COUNT = {
    "type": "message_delta",
    "delta": {"stop_reason": "end_turn"},
    "usage": {"output_tokens": 5},
}
_MESSAGE_STOP = {"type": "message_stop"}


@pytest.mark.asyncio
async def test_a_consumer_closing_the_stream_after_an_error_event_records_stream_error(
    monkeypatch, tmp_path
):
    path, model, _, _ = await _prime(
        monkeypatch,
        tmp_path,
        [StubResponse(pieces=[_sse([_HOUR_MESSAGE_START, _ERROR_EVENT, _PARTIAL_TEXT])])],
    )

    stream = model.endpoint.stream(HOUR_REQUEST)
    async for chunk in stream:
        if chunk.type == "error":
            break
    else:
        raise AssertionError("the stream ended without its error chunk")
    await stream.aclose()

    attempt = _attempts(path)[-1]
    assert attempt[2:] == ("open", None, "stream_error"), attempt
    assert _warm_rows(path) == []


@pytest.mark.asyncio
async def test_a_consumer_closing_the_stream_without_an_error_event_keeps_the_warm_rows(
    monkeypatch, tmp_path
):
    path, model, _, rows = await _prime(
        monkeypatch,
        tmp_path,
        [StubResponse(pieces=[_sse([_HOUR_MESSAGE_START, _PARTIAL_TEXT])])],
    )

    stream = model.endpoint.stream(HOUR_REQUEST)
    await stream.__anext__()
    await stream.aclose()

    attempt = _attempts(path)[-1]
    assert attempt[2:] == ("open", None, "stream_closed"), attempt
    assert _warm_rows(path) == rows


def _count_ledger_settlements(monkeypatch) -> list:
    """Record the evidence of every settlement the ledger is asked to write from here on."""
    written: list = []
    original = CredentialLedger.settle

    async def settle(self, attempt_id, evidence, **kwargs):
        written.append(evidence)
        return await original(self, attempt_id, evidence, **kwargs)

    monkeypatch.setattr(CredentialLedger, "settle", settle)
    return written


@pytest.mark.asyncio
async def test_a_transport_failure_after_complete_usage_settled_clears_nothing(
    monkeypatch, tmp_path
):
    pieces = [
        _sse([_HOUR_MESSAGE_START, _FINAL_COUNT, _MESSAGE_STOP]),
        httpx.ReadError("connection dropped"),
    ]
    path, model, _, rows = await _prime(monkeypatch, tmp_path, [StubResponse(pieces=pieces)])
    written = _count_ledger_settlements(monkeypatch)

    with pytest.raises(Exception):
        async for _ in model.endpoint.stream(HOUR_REQUEST):
            pass

    attempt = _attempts(path)[-1]
    assert attempt[2] == "settled" and attempt[4] == "complete", attempt
    assert len(written) == 1, written
    assert _block_reason(path, attempt[1]) is None
    assert _warm_rows(path) == rows


@pytest.mark.asyncio
async def test_a_transport_failure_inside_an_unterminated_event_does_not_dispatch_it(
    monkeypatch, tmp_path
):
    """An event cut off before its blank line is a truncated event: the message_stop in it is
    never seen, so the attempt settles as a transport failure."""
    unterminated = _sse([_HOUR_MESSAGE_START, _FINAL_COUNT, _MESSAGE_STOP])[:-2]
    assert not unterminated.endswith(b"\n\n")
    pieces = [unterminated, httpx.ReadError("connection dropped")]
    path, model, _, _ = await _prime(monkeypatch, tmp_path, [StubResponse(pieces=pieces)])
    written = _count_ledger_settlements(monkeypatch)

    with pytest.raises(Exception):
        async for _ in model.endpoint.stream(HOUR_REQUEST):
            pass

    attempt = _attempts(path)[-1]
    assert attempt[2:] == ("open", None, "transport_error"), attempt
    assert len(written) == 1, written


@pytest.mark.asyncio
async def test_a_key_prefix_held_when_the_transport_fails_is_never_delivered_or_stored(
    monkeypatch, tmp_path
):
    """The first half of the key is the last thing received before the connection drops: it
    sits in the redactor's held tail, and nothing the consumer or the store gets completes it."""
    half = len(CANARY) // 2
    cut_event = (
        b'data: {"type": "content_block_delta", "index": 0, '
        b'"delta": {"type": "text_delta", "text": "' + CANARY[:half].encode()
    )
    pieces = [
        _sse([_HOUR_MESSAGE_START, _PARTIAL_TEXT]),
        cut_event,
        httpx.ReadError("connection dropped"),
    ]
    path, model, _, _ = await _prime(monkeypatch, tmp_path, [StubResponse(pieces=pieces)])

    chunks = []
    with pytest.raises(Exception):
        async for chunk in model.endpoint.stream(HOUR_REQUEST):
            chunks.append(chunk)

    received = _all_chunk_text(chunks)
    assert "partial" in received
    assert CANARY not in received
    assert CANARY[:half] not in received
    with sqlite3.connect(path) as connection:
        stored = "\n".join(connection.iterdump())
    assert CANARY not in stored
    assert CANARY[:half] not in stored
    attempt = _attempts(path)[-1]
    assert attempt[2:] == ("open", None, "transport_error"), attempt


class _UnwindFailingSession(StubSession):
    """A session whose stream fails to close, the way a dropped connection can."""

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        response = self._request(method, url, **kwargs)
        try:
            yield response
        finally:
            response.closed = True
            raise RuntimeError("closing the connection failed")


async def _stream_past_settlement(model):
    """A suspended stream that has already yielded content sent after its message_stop."""
    stream = model.endpoint.stream(HOUR_REQUEST)
    async for chunk in stream:
        if "partial" in str(chunk.content or ""):
            return stream
    raise AssertionError("the stream ended before the content that follows its settlement")


@pytest.mark.asyncio
@pytest.mark.parametrize("unwind", ["close", "raise"])
@pytest.mark.parametrize(
    ("events", "state", "outcome"),
    [
        ([_HOUR_MESSAGE_START, _MESSAGE_STOP, _PARTIAL_TEXT], "open", "usage_incomplete"),
        ([_HOUR_MESSAGE_START, _FINAL_COUNT, _MESSAGE_STOP, _PARTIAL_TEXT], "settled", "complete"),
    ],
    ids=["after_unknown_usage", "after_complete_usage"],
)
async def test_unwinding_a_stream_that_already_settled_settles_nothing_more(
    monkeypatch, tmp_path, unwind, events, state, outcome
):
    """The consumer closing the stream, or the close itself failing, after message_stop has
    settled the attempt leaves its outcome and the account's warm rows as they were."""
    path, model, transport, _ = await _prime(
        monkeypatch, tmp_path, [StubResponse(pieces=[_sse(events)])]
    )
    if unwind == "raise":
        model.endpoint._create_http_session = lambda client_kwargs=None, timeout=None: (
            _UnwindFailingSession(transport, timeout=timeout)
        )
    written = _count_ledger_settlements(monkeypatch)

    stream = await _stream_past_settlement(model)
    settled_row = _attempts(path)[-1]
    settled_rows = _warm_rows(path)
    assert settled_row[2] == state and settled_row[4] == outcome, settled_row
    settlements = len(written)

    if unwind == "close":
        await stream.aclose()
    else:
        try:
            await stream.aclose()
        except Exception:  # noqa: S110 - only what is recorded afterwards is under test
            pass

    assert len(written) == settlements, written
    assert _attempts(path)[-1] == settled_row
    assert _warm_rows(path) == settled_rows


# --- the warm row is keyed by the body that was sent, not the body the marks were placed on

REWRITTEN_SYSTEM = "Replacement instructions written after the marks were placed. " * 40


def _system_certificate(body) -> str:
    """The certificate of the last one-hour system block in a request body."""
    from lionagi.providers.anthropic._prompt_cache import points

    blocks, _ = points(body)
    keyed = [p for p in blocks if p.tier == "system" and p.markable and p.mark == "1h"]
    assert keyed, body
    return keyed[-1].cert


def _rewrite_system(payload) -> None:
    payload["system"][0]["text"] = REWRITTEN_SYSTEM


def _stored_certificates(path) -> set[str]:
    return {cert for _, cert in _warm_rows(path)}


@pytest.mark.asyncio
async def test_a_pre_invoke_hook_that_rewrites_the_system_text_is_keyed_by_the_body_sent(
    monkeypatch, tmp_path
):
    """Reached through ``iModel.invoke`` with a pre-invoke hook registered on the model: the
    hook runs after the marks are placed and changes the system text the request sends."""
    from lionagi.service.hooks import HookRegistry
    from lionagi.service.hooks._types import HookEventTypes

    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response(usage=_hour_write_usage())])
    model.endpoint.config.max_retries = 0

    async def rewrite(event, **_):
        _rewrite_system(event.payload)

    model.hook_registry = HookRegistry(hooks={HookEventTypes.PreInvocation: rewrite})
    await model.invoke(**copy.deepcopy(HOUR_REQUEST))

    assert len(transport.calls) == 1
    sent = transport.calls[0]["json"]
    assert sent["system"][0]["text"] == REWRITTEN_SYSTEM
    sent_certificate = _system_certificate(sent)
    assert sent_certificate != _system_certificate(HOUR_REQUEST)
    assert _stored_certificates(path) == {sent_certificate}


@pytest.mark.asyncio
async def test_a_direct_endpoint_call_with_a_prebuilt_request_is_keyed_by_the_body_sent(
    monkeypatch, tmp_path
):
    """Reached through ``Endpoint.call`` with ``skip_payload_creation``: the payload was built
    by ``create_payload`` and its system text changed before it was handed in."""
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response(usage=_hour_write_usage())])
    model.endpoint.config.max_retries = 0

    payload, headers = model.endpoint.create_payload(request=copy.deepcopy(HOUR_REQUEST))
    _rewrite_system(payload)
    await model.endpoint.call(payload, skip_payload_creation=True, extra_headers=headers)

    assert len(transport.calls) == 1
    sent = transport.calls[0]["json"]
    assert sent["system"][0]["text"] == REWRITTEN_SYSTEM
    sent_certificate = _system_certificate(sent)
    assert sent_certificate != _system_certificate(HOUR_REQUEST)
    assert _stored_certificates(path) == {sent_certificate}


@pytest.mark.asyncio
async def test_a_prebuilt_event_sent_twice_is_keyed_by_the_body_of_each_send(monkeypatch, tmp_path):
    """Reached through an event from ``iModel.create_api_calling``. A second ``invoke`` on a
    finished event sends nothing; its payload is sent again through ``Endpoint.call``."""
    path = _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(
        monkeypatch,
        [_response(usage=_hour_write_usage()), _response(usage=_hour_write_usage())],
    )
    model.endpoint.config.max_retries = 0
    event = model.create_api_calling(**copy.deepcopy(HOUR_REQUEST))

    await event.invoke()
    await event.invoke()
    assert len(transport.calls) == 1
    await model.endpoint.call(
        event.payload,
        skip_payload_creation=True,
        extra_headers=event.headers,
        _routing_scope=event._routing_scope,
    )

    assert len(transport.calls) == 2
    certificates = {_system_certificate(call["json"]) for call in transport.calls}
    assert len(certificates) == 1
    assert _stored_certificates(path) == certificates


@pytest.mark.asyncio
async def test_a_second_call_with_the_same_stable_prefix_binds_the_account_that_wrote_it(
    monkeypatch, tmp_path
):
    """Two calls with the same stable prefix on a fresh ledger with two accounts, each call on a
    new conversation. Without a one-hour cache write nothing is warm, so the second call is placed
    by pace and lands on the second account. When the first call writes a one-hour cache entry for
    the prefix on the first account, the second call is bound to that account although the second
    account is first by pace."""

    async def run_two_calls(directory, response_options):
        directory.mkdir()
        path = _install_accounts(monkeypatch, directory)
        warm_after_first = None
        for turn in range(2):
            model, _ = _model(monkeypatch, [_response(**response_options)])
            model.endpoint.config.max_retries = 0
            await model.endpoint.call(copy.deepcopy(HOUR_REQUEST))
            if turn == 0:
                warm_after_first = [row[0] for row in _warm_rows(path)]
        return warm_after_first, [row[1] for row in _attempts(path)]

    warm, accounts = await run_two_calls(tmp_path / "cold", {})
    assert warm == []
    assert accounts == ["primary", "secondary"]

    warm, accounts = await run_two_calls(tmp_path / "warm", {"usage": _hour_write_usage()})
    assert warm == ["primary"]
    assert accounts == ["primary", "primary"]
