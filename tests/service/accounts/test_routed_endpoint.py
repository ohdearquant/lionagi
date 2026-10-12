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
CANARY = "test-canary-key-0123456789"
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


def _install_accounts(monkeypatch, tmp_path, *, accounts=None):
    declarations = accounts or [
        {"name": "primary", "key_env": KEY_NAME, "budget_usd": "20"},
        {"name": "secondary", "key_env": "LIONAGI_ROUTER_SECONDARY", "budget_usd": "20"},
    ]
    settings = AppSettings(_env_file=None, ANTHROPIC_ACCOUNTS=declarations)
    monkeypatch.setattr(config_module, "settings", settings)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for item in declarations:
        monkeypatch.setenv(item["key_env"], CANARY)
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
    pieces = [
        b"data: " + json.dumps(first).encode() + b"\n\n",
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


@pytest.mark.parametrize("hand_over", ["stream", "invoke"])
@pytest.mark.asyncio
async def test_a_refused_prebuilt_call_does_not_stay_queued_for_the_next_invoke(
    monkeypatch, tmp_path, hand_over
):
    _install_accounts(monkeypatch, tmp_path)
    model, transport = _model(monkeypatch, [_response()])
    queued_request = {**REQUEST, "messages": [{"role": "user", "content": "queued elsewhere"}]}
    with _branch_routing_context(uuid4(), model):
        queued = await model.create_event(stream=hand_over == "stream", **queued_request)
    await model.executor.append(queued)

    with pytest.raises(AccountRoutingError) as caught:
        if hand_over == "stream":
            async for _ in model.stream(api_call=queued):
                pass
        else:
            await model.invoke(api_call=queued)
    assert caught.value.code is RouterErrorCode.ACCOUNT_CONFIG_CONFLICT
    assert queued.id not in model.executor.pending
    assert queued.id not in model.executor.pile

    with _branch_routing_context(uuid4(), model):
        await model.invoke(**REQUEST)
    sent = [json.loads(call["data"])["messages"][0]["content"] for call in transport.calls]
    assert sent == ["A short request"]


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
    # One feed holding the whole key, ending two bytes past it: the held tail is the
    # key's length minus one, so the cut falls inside the key.
    out = redactor.feed(b"ab" + CANARY.encode() + b"cd") + redactor.feed(b"", final=True)
    assert out == b"ab[REDACTED]cd"

    redactor = StreamingSecretRedactor(SecretStr(CANARY))
    key = CANARY.encode()
    out = redactor.feed(b"ab" + key[:5]) + redactor.feed(key[5:] + b"cd")
    out += redactor.feed(b"", final=True)
    assert out == b"ab[REDACTED]cd"


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
