# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lattice provider: how it resolves, what its requests carry, and the wire against a local
server shaped like `lattice serve`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from lionagi.providers.lattice._config import LATTICE_DEFAULT_BASE_URL
from lionagi.providers.lattice.chat import (
    LatticeChatCompletionsRequest,
    LatticeChatEndpoint,
    LatticeLoraSelection,
)
from lionagi.providers.lattice.embed import LatticeEmbedEndpoint, LatticeEmbedRequest
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.imodel import iModel

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


@pytest_asyncio.fixture
async def lattice_server():
    """Starts a server shaped like `lattice serve` from inside the test, so it is served by
    the test's own event loop."""
    servers: list[TestServer] = []

    async def _start() -> tuple[str, list[dict]]:
        received: list[dict] = []

        async def record(request: web.Request) -> dict:
            entry = {
                "path": request.path,
                "headers": dict(request.headers),
                "body": await request.json(),
            }
            received.append(entry)
            return entry

        async def chat(request: web.Request):
            await record(request)
            return web.json_response(_chat_reply("Hello there"))

        async def embeddings(request: web.Request):
            entry = await record(request)
            items = entry["body"]["input"]
            items = items if isinstance(items, list) else [items]
            return web.json_response(
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [0.6, 0.8]}
                        for i in range(len(items))
                    ],
                    "model": MODEL,
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        app.router.add_post("/v1/embeddings", embeddings)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        servers.append(server)
        return str(server.make_url("/v1")), received

    yield _start

    for server in servers:
        await server.close()


def test_lattice_resolves_to_its_own_keyless_loopback_endpoints(monkeypatch):
    for name in ("OPENAI_API_KEY", "LATTICE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    chat = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)
    embed = match_endpoint(provider="lattice", endpoint="embed", model=MODEL)

    assert isinstance(chat, LatticeChatEndpoint)
    assert isinstance(embed, LatticeEmbedEndpoint)
    for endpoint, path in ((chat, "chat/completions"), (embed, "embeddings")):
        assert endpoint.config.provider == "lattice"
        assert endpoint.config.base_url == LATTICE_DEFAULT_BASE_URL
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
    assert not {"Authorization", "x-api-key"} & set(headers)


def test_an_omitted_adapter_list_stays_absent_and_an_empty_one_is_sent():
    """The server reads an absent `lora` as "choose for me" and `[]` as "apply none"."""
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
        LatticeEmbedRequest(model=MODEL, input="x", pooling="max")


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
    assert "Authorization" not in request["headers"]


@pytest.mark.asyncio
async def test_imodel_embed_round_trip_sends_only_the_fields_set(lattice_server):
    base_url, received = await lattice_server()
    model = iModel(provider="lattice", endpoint="embed", model=MODEL, base_url=base_url)

    api_call = await model.invoke(input=["a", "b"])

    assert [item["index"] for item in api_call.response["data"]] == [0, 1]
    (request,) = received
    assert request["path"] == "/v1/embeddings"
    assert request["body"] == {"model": MODEL, "input": ["a", "b"]}
    assert "Authorization" not in request["headers"]


def _lattice_doc_paragraph() -> str:
    doc = (REPO_ROOT / "docs" / "reference" / "providers.md").read_text()
    start = doc.index("Lattice is a local inference server")
    end = doc.index("```python", start)
    return doc[start:end]


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
    assert not {"Authorization", "x-api-key"} & set(headers)


def test_an_endpoint_rebuilds_from_its_own_serialized_config():
    endpoint = match_endpoint(provider="lattice", endpoint="chat", model=MODEL)

    rebuilt = LatticeChatEndpoint(config=endpoint.to_dict()["config"])

    assert rebuilt.config.allow_local_network is True
    assert rebuilt.config.serialize_by_alias is True


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
