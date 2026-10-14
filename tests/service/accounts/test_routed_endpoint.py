# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Routed Anthropic request-path behavior."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from collections import deque
from uuid import uuid4

import pytest

import lionagi.config as config_module
import lionagi.service.accounts._router as router_module
from lionagi.config import AppSettings
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionagi.service.accounts._credentials import CredentialSources
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
        self.status = status
        self.body = body or {}
        self.content = ByteStream(list(pieces or []))
        self.closed = False
        self.headers = {"content-type": "application/json"}

    async def json(self):
        return self.body

    async def read(self):
        return json.dumps(self.body).encode()

    def release(self):
        self.closed = True


class StubTransport:
    def __init__(self, responses) -> None:
        self.responses = deque(responses)
        self.calls: list[dict] = []

    def session(self):
        return StubSession(self)


class StubSession:
    def __init__(self, transport: StubTransport) -> None:
        self.transport = transport

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def request(self, **kwargs):
        self.transport.calls.append(kwargs)
        return self.transport.responses.popleft()


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
    with pytest.raises(AccountRoutingError) as caught:
        models[1].endpoint.create_payload(REQUEST, extra_headers={"Authorization": "Bearer x"})
    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    models.append(iModel(provider="anthropic", model=MODEL))
    models[-1].endpoint.config.client_kwargs = {"allow_redirects": True}
    with pytest.raises(AccountRoutingError) as caught:
        models[-1].endpoint.create_payload(REQUEST)
    assert caught.value.code is RouterErrorCode.CREDENTIAL_CONFLICT
    assert not lookups
    assert not path.exists()


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


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_yielding_a_chunk(monkeypatch, tmp_path):
    from aiohttp import ClientError

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
        ClientError("offline"),
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

    model.endpoint._create_http_session = WaitingSession
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

    model.endpoint._create_http_session = SendingSession
    task = asyncio.create_task(model.endpoint.call(REQUEST))
    await entered_request.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = _attempts(path)[0]
    assert row[2] == "open" and row[3] is None


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

    model.endpoint._create_http_session = HeldSession
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
    # aiohttp's own raise would skip the 401/403 block and keep the sent headers,
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
    """Held while the next event may continue the key; sent when it cannot."""
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
