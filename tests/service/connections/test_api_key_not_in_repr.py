# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An API key never reaches repr() or str() of the objects that carry it.

Both fields were already excluded from model dumps; the default pydantic repr still
rendered them, so a log line or traceback formatting the object printed the key.
"""

import pytest
from pydantic import SecretStr

from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel

CANARY = "sk-ant-CANARYabcdef0123456789"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_a_built_call_carries_the_key_in_its_headers_but_not_in_its_repr(provider):
    model = iModel(provider=provider, model="any-model", api_key=CANARY)
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}], max_tokens=8)

    assert any(CANARY in str(value) for value in call.headers.values())
    assert "hello" in repr(call)
    assert CANARY not in repr(call)
    assert CANARY not in str(call)
    assert CANARY not in str(call.model_dump())


@pytest.mark.parametrize("api_key", [CANARY, SecretStr(CANARY)], ids=["str", "SecretStr"])
def test_an_endpoint_config_resolves_the_key_but_does_not_render_it(api_key):
    config = EndpointConfig(
        name="chat",
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
        endpoint="messages",
        api_key=api_key,
    )

    assert config._api_key == CANARY
    assert "anthropic" in repr(config)
    assert CANARY not in repr(config)
    assert CANARY not in str(config)
    assert CANARY not in str(config.model_dump())
