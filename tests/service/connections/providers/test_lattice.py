# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lattice provider: how it resolves, what its requests carry, and its HTTP contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from lionagi._errors import ProviderStreamError
from lionagi.providers.lattice._config import LATTICE_DEFAULT_BASE_URL
from lionagi.providers.lattice.chat import (
    LatticeChatCompletionsRequest,
    LatticeChatEndpoint,
    LatticeLoraSelection,
)
from lionagi.providers.lattice.embed import LatticeEmbedEndpoint, LatticeEmbedRequest
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.imodel import iModel
from lionagi.service.resilience import APIClientError

MODEL = "qwen3.5-0.8b"
REPO_ROOT = Path(__file__).resolve().parents[4]


def _chat_reply(content: str) -> dict:
    return {
        "id": "chatcmpl-1-0",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


@pytest.fixture
def lattice_server(monkeypatch):
    async def _start(
        reply: str = "Hello there",
        stream_events: list[dict] | None = None,
        redirect: str | None = None,
    ) -> tuple[str, list[dict]]:
        received: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            raw = request.content
            entry = {
                "path": request.url.path,
                "headers": dict(request.headers),
                "raw": raw,
                "body": json.loads(raw) if raw else {},
            }
            received.append(entry)
            if redirect:
                return httpx.Response(307, headers={"location": redirect}, request=request)
            if request.url.path.endswith("/embeddings"):
                items = entry["body"]["input"]
                items = items if isinstance(items, list) else [items]
                body = {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [0.6, 0.8]}
                        for i in range(len(items))
                    ],
                    "model": MODEL,
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                }
                return httpx.Response(200, json=body, request=request)
            if not entry["body"].get("stream"):
                return httpx.Response(200, json=_chat_reply(reply), request=request)
            content = (
                b"".join(
                    b"data: " + json.dumps(event).encode() + b"\n\n"
                    for event in (stream_events or ())
                )
                + b"data: [DONE]\n\n"
            )
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
                request=request,
            )

        def create_client(self, *args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(Endpoint, "_create_http_session", create_client)
        monkeypatch.setattr(Endpoint, "_assert_ssrf_safe_url", lambda self: None)
        return "https://lattice.example.test/v1", received

    return _start


def test_lattice_resolves_to_its_own_keyless_loopback_endpoints(monkeypatch):
    for name in ("OPENAI_API_KEY", "LATTICE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    chat = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)
    embed = match_endpoint(provider="lattice", endpoint="embed", model=MODEL)

    assert isinstance(chat, LatticeChatEndpoint)
    assert isinstance(embed, LatticeEmbedEndpoint)
    for endpoint, path in ((chat, "chat/completions"), (embed, "embeddings")):
        assert endpoint.config.provider == "lattice"
        assert endpoint.config.base_url == "http://127.0.0.1:8080/v1"
        assert endpoint.config.endpoint == path
        assert endpoint.config.auth_type == "none"
        assert endpoint.config.allow_local_network is True


def test_a_fresh_process_resolves_lattice_without_importing_it_first():
    """The registry loads the provider itself; nothing has to import it beforehand."""
    repo_root = Path(__file__).resolve().parents[4]
    script = """
import sys
from lionagi.service.connections.match_endpoint import match_endpoint

assert not any(name.startswith("lionagi.providers.lattice") for name in sys.modules)
for endpoint, cls in (("chat", "LatticeChatEndpoint"), ("embed", "LatticeEmbedEndpoint")):
    resolved = match_endpoint(provider="lattice", endpoint=endpoint, model="m")
    assert type(resolved).__name__ == cls, (endpoint, type(resolved))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(repo_root), env.get("PYTHONPATH"))))
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


def test_a_chat_payload_keeps_the_lattice_fields_and_carries_no_credential():
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)
    payload, headers = endpoint.create_payload(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_budget": 64,
            "top_k": 20,
            "repetition_penalty": 1.1,
            "lora": [{"id": 2, "scale": 0.5}],
        }
    )

    assert payload["reasoning_budget"] == 64
    assert payload["top_k"] == 20
    assert payload["repetition_penalty"] == 1.1
    assert payload["lora"] == [{"id": 2, "scale": 0.5}]
    assert not {"authorization", "x-api-key"} & {name.lower() for name in headers}


def test_an_omitted_adapter_list_stays_absent_and_an_empty_one_is_sent():
    """An absent `lora` and `[]` both select the base model on today's servers; `[]` pins it,
    so it keeps selecting the base model if the server later enables adapter routing."""
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)
    messages = [{"role": "user", "content": "hi"}]

    omitted, _ = endpoint.create_payload({"model": MODEL, "messages": messages})
    empty, _ = endpoint.create_payload({"model": MODEL, "messages": messages, "lora": []})

    assert "lora" not in omitted
    assert empty["lora"] == []


def test_request_models_refuse_values_the_server_would_refuse():
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(ValidationError):
        LatticeChatCompletionsRequest(model=MODEL, messages=messages, reasoning_budget=-1)
    with pytest.raises(ValidationError):
        LatticeChatCompletionsRequest(
            model=MODEL, messages=messages, lora=[{"id": 1, "scale": 1.0, "weight": 2}]
        )
    with pytest.raises(ValidationError):
        LatticeChatCompletionsRequest(
            model=MODEL, messages=messages, lora=[{"id": 1, "scale": float("nan")}]
        )
    with pytest.raises(ValidationError):
        LatticeChatCompletionsRequest(
            model=MODEL, messages=messages, lora=[{"id": 1, "scale": float("inf")}]
        )
    with pytest.raises(ValidationError):
        LatticeEmbedRequest(model=MODEL, input="x", pooling="max")
    # Bounds the server states: `top_p` is `(0.0, 1.0]` there, and `seed` is an unsigned
    # integer, so a zero probability and a negative seed are refused before the request.
    with pytest.raises(ValidationError):
        LatticeChatCompletionsRequest(model=MODEL, messages=messages, top_p=0.0)
    with pytest.raises(ValidationError):
        LatticeChatCompletionsRequest(model=MODEL, messages=messages, seed=-1)
    with pytest.raises(ValidationError):
        LatticeChatCompletionsRequest(model=MODEL, messages=messages, seed=1 << 64)
    admitted = LatticeChatCompletionsRequest(model=MODEL, messages=messages, top_p=1.0, seed=0)
    assert (admitted.top_p, admitted.seed) == (1.0, 0)
    assert (
        LatticeChatCompletionsRequest(model=MODEL, messages=messages, seed=(1 << 64) - 1).seed
        == (1 << 64) - 1
    )


@pytest.mark.asyncio
async def test_imodel_chat_round_trip_against_a_lattice_shaped_server(lattice_server):
    base_url, received = await lattice_server()
    model = iModel(provider="lattice", model=MODEL, base_url=base_url)

    api_call = await model.invoke(
        messages=[{"role": "user", "content": "hi"}],
        reasoning_budget=32,
        lora=[{"id": 1, "scale": 1.0}],
    )

    assert api_call.response["choices"][0]["message"]["content"] == "Hello there"
    (request,) = received
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == MODEL
    assert request["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert request["body"]["reasoning_budget"] == 32
    assert request["body"]["lora"] == [{"id": 1, "scale": 1.0}]
    assert "authorization" not in {name.lower() for name in request["headers"]}


@pytest.mark.asyncio
async def test_imodel_embed_round_trip_sends_only_the_fields_set(lattice_server):
    base_url, received = await lattice_server()
    model = iModel(provider="lattice", endpoint="embed", model=MODEL, base_url=base_url)

    api_call = await model.invoke(input=["a", "b"])

    assert [item["index"] for item in api_call.response["data"]] == [0, 1]
    (request,) = received
    assert request["path"] == "/v1/embeddings"
    assert request["body"] == {"model": MODEL, "input": ["a", "b"]}
    assert "authorization" not in {name.lower() for name in request["headers"]}
    assert "pooling" not in request["body"]


@pytest.mark.asyncio
async def test_a_set_pooling_is_sent_and_an_unset_one_is_not(lattice_server):
    """`pooling` may only appear when the caller set it, so the server's own default keeps
    applying to a request that never named one."""
    base_url, received = await lattice_server()
    model = iModel(provider="lattice", endpoint="embed", model=MODEL, base_url=base_url)

    await model.invoke(input="a", pooling="last_token")

    (request,) = received
    assert request["body"] == {"model": MODEL, "input": "a", "pooling": "last_token"}


class Answer(BaseModel):
    answer: int


@pytest.mark.asyncio
async def test_a_leading_think_block_moves_out_of_content(lattice_server):
    """The server returns the model's reasoning inline, as a prefix of `content`."""
    base_url, _ = await lattice_server(reply='<think>\nadd them\n</think>\n\n{"answer": 42}')
    model = iModel(provider="lattice", model=MODEL, base_url=base_url)

    api_call = await model.invoke(messages=[{"role": "user", "content": "hi"}])

    message = api_call.response["choices"][0]["message"]
    assert message["content"] == '{"answer": 42}'
    assert message["reasoning_content"] == "add them"


@pytest.mark.asyncio
async def test_a_think_block_without_its_closing_tag_is_left_in_content(lattice_server):
    base_url, _ = await lattice_server(reply="<think>\nstill reasoning when the reply ends")
    model = iModel(provider="lattice", model=MODEL, base_url=base_url)

    api_call = await model.invoke(messages=[{"role": "user", "content": "hi"}])

    message = api_call.response["choices"][0]["message"]
    assert message["content"] == "<think>\nstill reasoning when the reply ends"
    assert "reasoning_content" not in message


@pytest.mark.asyncio
async def test_structured_output_parses_a_reply_carrying_the_usual_empty_think_block(
    lattice_server,
):
    from lionagi import Branch

    base_url, received = await lattice_server(reply='<think>\n\n</think>\n\n{"answer": 42}')
    branch = Branch(chat_model=iModel(provider="lattice", model=MODEL, base_url=base_url))

    result = await branch.communicate("What is 40 + 2?", response_format=Answer)

    assert result.answer == 42
    assert len(received) == 1  # no reformat round trip: the first reply parsed


def _stream_events() -> tuple[list[dict], dict]:
    delta = {
        "id": "chatcmpl-1-1",
        "object": "chat.completion.chunk",
        "model": MODEL,
        "choices": [{"index": 0, "delta": {"content": "Here"}}],
    }
    failure = {"error": {"message": "inference failed", "type": "server_error"}}
    return [delta, failure], failure


@pytest.mark.asyncio
async def test_a_failure_after_the_stream_starts_arrives_as_an_error_chunk_and_fails_the_call(
    lattice_server,
):
    """The server ends a failed generation with `{"error": {...}}` and `[DONE]`; read as
    an ordinary event it would look like a normal end. The error chunk still reaches the
    consumer, and the call fails once the stream ends."""
    events, _ = _stream_events()
    base_url, _ = await lattice_server(stream_events=events)
    model = iModel(provider="lattice", model=MODEL, base_url=base_url)

    chunks = []
    with pytest.raises(ValueError, match="inference failed") as excinfo:
        async for chunk in model.stream(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    assert isinstance(excinfo.value.__cause__, ProviderStreamError)
    errors = [chunk for chunk in chunks if chunk.is_error]
    assert [chunk.content for chunk in chunks if chunk.type == "text"] == ["Here"]
    assert len(errors) == 1
    assert errors[0].type == "error"
    assert "inference failed" in errors[0].content
    assert "Here" not in errors[0].content


@pytest.mark.asyncio
async def test_the_stream_failure_and_its_evidence_reach_the_endpoint_contract(lattice_server):
    events, failure = _stream_events()
    base_url, _ = await lattice_server(stream_events=events)
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL, base_url=base_url)

    chunks = [
        chunk
        async for chunk in endpoint.stream(
            {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}
        )
    ]

    (error,) = [chunk for chunk in chunks if chunk.is_error]
    assert error.metadata["raw"] == {"error": failure["error"]}


@pytest.mark.asyncio
async def test_a_lattice_request_never_follows_a_redirect(lattice_server):
    """The address guard admits one host, once, before the first request. A redirect the
    client followed would reach a host the guard never admitted, so none is followed: the
    redirect comes back as the failed response, on a call, an embedding and a stream."""
    base_url, received = await lattice_server(redirect="/v1/elsewhere")
    chat = match_endpoint(provider="lattice", endpoint="chat", model=MODEL, base_url=base_url)
    embed = match_endpoint(provider="lattice", endpoint="embed", model=MODEL, base_url=base_url)
    request = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}

    with pytest.raises(APIClientError) as called:
        await chat.call(request)
    with pytest.raises(APIClientError) as embedded:
        await embed.call({"model": MODEL, "input": "a"})
    with pytest.raises(APIClientError) as streamed:
        async for _ in chat.stream(request):
            pass

    assert (
        called.value.status_code == embedded.value.status_code == streamed.value.status_code == 307
    )
    assert [entry["path"] for entry in received] == [
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/chat/completions",
    ]


def _lattice_doc_paragraph() -> str:
    doc = (REPO_ROOT / "docs" / "reference" / "providers.md").read_text()
    start = doc.index("Lattice is a local inference server")
    end = doc.index("```python", start)
    return doc[start:end]


def test_the_documented_server_rules_are_the_ones_the_server_has():
    """The reference has to state the rules a caller cannot see from lionagi: the token
    cap a request without `max_tokens` gets, the checkpoint the embeddings route needs,
    and the sampling extensions `lattice serve` accepts without applying."""
    paragraph = _lattice_doc_paragraph()

    assert "256" in paragraph
    assert "vision-language checkpoint" in paragraph
    assert "ignored" in paragraph and "`lattice serve`" in paragraph
    for field in ("frequency_penalty", "presence_penalty", "logit_bias", "user"):
        assert field in paragraph


def test_the_reference_states_the_limits_and_refusals_lionagi_does_not_check():
    """A caller cannot see these from lionagi: the flag the server does not have, the
    hardcoded 4096-token cap, and the values the server refuses."""
    paragraph = _lattice_doc_paragraph()

    assert "--max-tokens" not in paragraph
    assert "4096" in paragraph
    assert "default of 256" in paragraph
    assert 'finish_reason: "length"' in paragraph
    for named in ("top_p", "seed", "response_format", "top_logprobs", "`n` above 1"):
        assert named in paragraph


def test_the_reference_states_the_stream_error_chunk():
    """A mid-stream failure arrives as an error chunk before the call fails."""
    paragraph = _lattice_doc_paragraph()

    assert "is_error" in paragraph and "iModel.stream" in paragraph


@pytest.mark.asyncio
async def test_a_model_restored_from_its_dict_validates_its_payload(lattice_server):
    """A default snapshot omits the request model, which is recovered on restore."""
    base_url, received = await lattice_server()
    model = iModel(provider="lattice", model=MODEL, base_url=base_url)

    restored = iModel.from_dict(model.to_dict())
    fresh, _ = model.endpoint.create_payload(
        {"messages": [{"role": "user", "content": "hi"}], "temperature": "0.5"}
    )
    await restored.invoke(messages=[{"role": "user", "content": "hi"}], temperature="0.5")

    assert restored.endpoint.config.request_options is LatticeChatCompletionsRequest
    (request,) = received
    assert request["body"]["temperature"] == fresh["temperature"] == 0.5


def test_the_request_model_records_what_the_server_does_not_apply():
    docstring = LatticeChatCompletionsRequest.__doc__
    for field in ("frequency_penalty", "presence_penalty", "logit_bias", "user"):
        assert field in docstring
    for name in ("top_k", "repetition_penalty"):
        assert "ignored" in LatticeChatCompletionsRequest.model_fields[name].description


def test_the_documented_local_addresses_are_the_ones_the_guard_admits():
    """The docs and the endpoint's address guard have to say the same thing: the port may
    change, the host may not move off loopback."""
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL, base_url=None)
    for admitted in (LATTICE_DEFAULT_BASE_URL, "http://localhost:9999/v1", "http://[::1]:8080/v1"):
        endpoint.config.base_url = admitted
        endpoint._assert_ssrf_safe_url()

    endpoint.config.base_url = "http://192.168.1.50:8080/v1"
    with pytest.raises(PermissionError):
        endpoint._assert_ssrf_safe_url()

    paragraph = _lattice_doc_paragraph()
    for host in ("localhost", "127.0.0.1", "::1"):
        assert host in paragraph
    assert "loopback" in paragraph
    assert "names another address" not in paragraph


def test_a_supplied_api_key_is_dropped_like_the_other_keyless_endpoint():
    endpoint = match_endpoint(
        provider="lattice", endpoint="chat", model=MODEL, api_key="sk-not-a-lattice-key"
    )

    assert endpoint.config.api_key is None
    assert endpoint.config._api_key is None
    _, headers = endpoint.create_payload(
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
    )
    assert not {"authorization", "x-api-key"} & {name.lower() for name in headers}


def test_an_endpoint_rebuilds_from_its_own_serialized_config():
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)

    rebuilt = LatticeChatEndpoint(config=endpoint.to_dict()["config"])

    assert rebuilt.config.allow_local_network is True
    assert rebuilt.config.serialize_by_alias is True
    assert rebuilt.request_options is LatticeChatCompletionsRequest


@pytest.mark.asyncio
async def test_a_rebuilt_endpoint_sends_plain_json_on_the_wire(lattice_server):
    """A request model survives serialization as a JSON schema, and the model rebuilt from
    that schema holds `const` and `enum` fields as Enum members that `json` cannot encode, so
    an endpoint rebuilt from its own config used to fail before its request left the client.
    The server's own `body` proves the bytes on the wire were valid JSON."""
    base_url, received = await lattice_server()

    chat = LatticeChatEndpoint(
        config=match_endpoint(
            provider="lattice", endpoint="chat", model=MODEL, base_url=base_url
        ).to_dict()["config"]
    )
    await chat.call({"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})

    embed = LatticeEmbedEndpoint(
        config=match_endpoint(
            provider="lattice", endpoint="embed", model=MODEL, base_url=base_url
        ).to_dict()["config"]
    )
    await embed.call({"model": MODEL, "input": "a", "pooling": "last_token"})

    assert chat.request_options is LatticeChatCompletionsRequest
    assert embed.request_options is LatticeEmbedRequest
    chat_request, embed_request = received
    assert chat_request["path"] == "/v1/chat/completions"
    assert chat_request["body"]["messages"][0]["role"] == "user"
    assert type(chat_request["body"]["messages"][0]["role"]) is str
    assert embed_request["path"] == "/v1/embeddings"
    assert embed_request["body"] == {"model": MODEL, "input": "a", "pooling": "last_token"}
    assert type(embed_request["body"]["pooling"]) is str
    for request in (chat_request, embed_request):
        assert json.loads(request["raw"]) == request["body"]


def test_a_config_dict_api_key_is_dropped_like_the_keyword_one():
    endpoint = LatticeChatEndpoint(
        config={
            "name": "lattice_chat",
            "provider": "lattice",
            "endpoint": "chat/completions",
            "base_url": LATTICE_DEFAULT_BASE_URL,
            "auth_type": "none",
            "api_key": "sk-not-a-lattice-key",
        }
    )

    assert endpoint.config.api_key is None
    assert endpoint.config._api_key is None
    _, headers = endpoint.create_payload(
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
    )
    assert not {"authorization", "x-api-key"} & {name.lower() for name in headers}


def test_a_caller_supplied_local_network_setting_survives_every_construction_path():
    config = EndpointConfig(
        provider="lattice",
        endpoint="chat/completions",
        name="lattice_chat",
        base_url=LATTICE_DEFAULT_BASE_URL,
        auth_type="none",
        allow_local_network=False,
    )
    direct = LatticeChatEndpoint(config=config)

    assert direct.config.allow_local_network is False
    assert (
        LatticeChatEndpoint(config=direct.config.model_copy(deep=True)).config.allow_local_network
        is False
    )
    assert (
        match_endpoint(
            provider="lattice", endpoint="chat", model=MODEL, allow_local_network=False
        ).config.allow_local_network
        is False
    )


def test_an_adapter_instance_is_serialized_and_a_null_adapter_list_is_omitted():
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)
    messages = [{"role": "user", "content": "hi"}]

    payload, _ = endpoint.create_payload(
        {"model": MODEL, "messages": messages, "lora": [LatticeLoraSelection(id=3, scale=0.25)]}
    )
    assert payload["lora"] == [{"id": 3, "scale": 0.25}]
    assert json.dumps(payload)  # a selection instance here raised TypeError before

    null_payload, _ = endpoint.create_payload({"model": MODEL, "messages": messages, "lora": None})
    assert "lora" not in null_payload


def test_the_openai_fields_the_server_refuses_are_forwarded_unchanged():
    """lionagi narrows nothing here; the server is what rejects these."""
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)
    request = LatticeChatCompletionsRequest(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        n=2,
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
    )

    payload, _ = endpoint.create_payload(request)

    assert payload["n"] == 2
    assert payload["tools"] == [{"type": "function", "function": {"name": "f", "parameters": {}}}]


def test_the_provider_inventories_name_lattice():
    imodel_doc = (REPO_ROOT / "docs" / "api" / "imodel.md").read_text()
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text()

    assert imodel_doc.count('"lattice"') >= 3  # chat table, embed table, resolution table
    assert "`lattice`" in claude_md


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "embed"])
@pytest.mark.parametrize("rebuild", ["schema-config", "json-config", "endpoint-dict"])
async def test_endpoint_config_rebuilds_send_plain_json(lattice_server, kind, rebuild):
    base_url, received = await lattice_server()
    original = match_endpoint(provider="lattice", endpoint=kind, model=MODEL, base_url=base_url)
    serialized = original.to_dict()
    config = serialized["config"]
    if rebuild == "endpoint-dict":
        rebuilt = type(original).from_dict(serialized)
    else:
        if rebuild == "json-config":
            config = {**config, "request_options": json.dumps(config["request_options"])}
        rebuilt = type(original)(config=EndpointConfig(**config))

    if kind == "chat":
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
        expected_model = LatticeChatCompletionsRequest
    else:
        payload = {"model": MODEL, "input": "a", "pooling": "last_token"}
        expected_model = LatticeEmbedRequest
    await rebuilt.call(payload)

    assert rebuilt.request_options is expected_model
    (request,) = received
    assert request["path"] == f"/v1/{original.config.endpoint}"
    assert request["body"] == expected_model.model_validate(payload).model_dump(
        by_alias=True, exclude_none=True
    )
    assert json.loads(request["raw"]) == request["body"]


@pytest.mark.parametrize("kind", ["chat", "embed"])
def test_an_absent_endpoint_config_request_model_gets_the_provider_default(kind):
    original = match_endpoint(provider="lattice", endpoint=kind, model=MODEL)
    config = EndpointConfig(**{**original.to_dict()["config"], "request_options": None})

    rebuilt = type(original)(config=config)

    assert rebuilt.request_options is original.request_options
    assert config.request_options is None


@pytest.mark.parametrize("kind", ["chat", "embed"])
@pytest.mark.parametrize("construction", ["keyword", "dict", "config"])
def test_a_caller_request_model_survives_lattice_configuration(kind, construction):
    class CallerRequest(BaseModel):
        model: str
        custom: str

    original = match_endpoint(provider="lattice", endpoint=kind, model=MODEL)
    config = {**original.to_dict()["config"], "request_options": CallerRequest}
    if construction == "keyword":
        endpoint = type(original)(request_options=CallerRequest)
    elif construction == "dict":
        endpoint = type(original)(config=config)
    else:
        endpoint = type(original)(config=EndpointConfig(**config))

    assert endpoint.request_options is CallerRequest
    payload, _ = endpoint.create_payload({"model": MODEL, "custom": "kept"})
    assert payload == {"model": MODEL, "custom": "kept"}


@pytest.mark.parametrize("kind", ["chat", "embed"])
@pytest.mark.parametrize("private_only", [False, True], ids=["both-key-fields", "private-key-only"])
def test_a_config_object_key_is_dropped_without_changing_the_caller(kind, private_only):
    original = match_endpoint(provider="lattice", endpoint=kind, model=MODEL)
    config = EndpointConfig(
        name=original.config.name,
        provider="lattice",
        endpoint=original.config.endpoint,
        base_url=LATTICE_DEFAULT_BASE_URL,
        auth_type="none",
        api_key="sk-not-a-lattice-key",
    )
    if private_only:
        config.api_key = None
    public_key, private_key = config.api_key, config._api_key

    endpoint = type(original)(config=config)

    assert endpoint.config.api_key is None
    assert endpoint.config._api_key is None
    assert config.api_key == public_key
    assert config._api_key == private_key
    assert endpoint.config is not config
