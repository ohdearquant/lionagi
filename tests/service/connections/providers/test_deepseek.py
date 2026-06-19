import pytest

from lionagi.providers.deepseek.chat import (
    DeepseekChatCompletionsRequest,
    DeepseekChatEndpoint,
    normalize_deepseek_usage,
)
from lionagi.service.connections.endpoint_config import EndpointConfig


def _get_deepseek_config(**overrides) -> EndpointConfig:
    """Create a DeepSeek endpoint config for testing."""
    defaults = dict(
        name="deepseek_chat/completions",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        endpoint="chat/completions",
        api_key="dummy-key-for-testing",
        request_options=DeepseekChatCompletionsRequest,
        auth_type="bearer",
        content_type="application/json",
        method="POST",
    )
    defaults.update(overrides)
    return EndpointConfig(**defaults)


def test_deepseek_config_uses_deepseek_request_model():
    config = _get_deepseek_config()

    assert config.request_options is DeepseekChatCompletionsRequest
    assert config.provider == "deepseek"


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("low", "high"),
        ("medium", "high"),
        ("high", "high"),
        ("xhigh", "max"),
        ("max", "max"),
    ],
)
def test_deepseek_payload_maps_reasoning_effort(effort, expected):
    endpoint = DeepseekChatEndpoint()

    payload, _ = endpoint.create_payload(
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "think carefully"}],
            "thinking": {"type": "enabled"},
            "reasoning_effort": effort,
        }
    )

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == expected


def test_deepseek_payload_accepts_disabled_thinking():
    endpoint = DeepseekChatEndpoint()

    payload, _ = endpoint.create_payload(
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "answer directly"}],
            "thinking": {"type": "disabled"},
        }
    )

    assert payload["thinking"] == {"type": "disabled"}


def test_deepseek_usage_surfaces_thinking_tokens_alias():
    response = {
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 9,
            "total_tokens": 13,
            "completion_tokens_details": {"reasoning_tokens": 7},
        }
    }

    normalized = normalize_deepseek_usage(response)

    assert normalized["usage"]["thinking_tokens"] == 7
    assert normalized["usage"]["reasoning_tokens"] == 7
    assert normalized["usage"]["completion_tokens_details"]["thinking_tokens"] == 7


def test_deepseek_usage_preserves_zero_thinking_tokens():
    response = {
        "usage": {
            "completion_tokens_details": {"reasoning_tokens": 0},
        }
    }

    normalized = normalize_deepseek_usage(response)

    assert normalized["usage"]["thinking_tokens"] == 0
