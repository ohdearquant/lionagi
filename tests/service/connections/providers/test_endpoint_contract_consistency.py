from lionagi.providers.anthropic.claude_code import ClaudeCodeCLIEndpoint
from lionagi.providers.google.gemini_code import GeminiCLIEndpoint
from lionagi.providers.openai.codex import CodexCLIEndpoint
from lionagi.providers.pi.cli import PiCLIEndpoint
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.registry import EndpointRegistry
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk


def _noop(*_, **__):
    return None


def test_cli_handler_kwargs_do_not_leak_into_endpoint_config():
    cases = [
        (ClaudeCodeCLIEndpoint, "claude_handlers"),
        (CodexCLIEndpoint, "codex_handlers"),
        (GeminiCLIEndpoint, "gemini_handlers"),
        (PiCLIEndpoint, "pi_handlers"),
    ]

    for endpoint_cls, handler_key in cases:
        endpoint = endpoint_cls(**{handler_key: {"on_text": _noop}})

        assert handler_key not in endpoint.config.kwargs
        payload, headers = endpoint.create_payload({"prompt": "hello"})
        assert headers == {}
        assert "request" in payload


def test_imodel_preserves_multipart_transport_kwargs_outside_payload():
    model = iModel(provider="openai", endpoint="stt")
    api_call = model.create_api_calling(
        model="whisper-1",
        file=b"audio",
        filename="audio.wav",
    )

    assert "file" not in api_call.payload
    assert api_call.call_kwargs == {"file": b"audio", "filename": "audio.wav"}

    image_model = iModel(provider="openai", endpoint="image_edit")
    image_call = image_model.create_api_calling(
        prompt="edit this",
        image=b"image",
        image_filename="image.png",
    )

    assert "image" not in image_call.payload
    assert image_call.call_kwargs == {
        "image": b"image",
        "image_filename": "image.png",
    }


def test_request_models_are_registered_for_previous_schema_gaps():
    cases = [
        ("openai", "embed", "OpenAIEmbeddingRequest"),
        ("openai", "response", "OpenAIResponsesRequest"),
        ("nvidia_nim", "chat", "OpenAIChatCompletionsRequest"),
        ("nvidia_nim", "embed", "NvidiaNimEmbeddingRequest"),
    ]

    for provider, endpoint_name, request_model_name in cases:
        endpoint = EndpointRegistry.match(provider, endpoint_name)
        assert endpoint.request_options is not None
        assert endpoint.request_options.__name__ == request_model_name


def test_openai_embed_and_response_payloads_filter_internal_kwargs():
    embed = EndpointRegistry.match("openai", "embed")
    payload, _ = embed.create_payload({"input": "hello", "branch": "drop"})
    assert payload == {"model": "text-embedding-3-small", "input": "hello"}

    response = EndpointRegistry.match("openai", "response")
    payload, _ = response.create_payload({"model": "gpt-5", "input": "hello", "branch": "drop"})
    assert payload == {"model": "gpt-5", "input": "hello"}


def test_openai_response_payload_keeps_current_api_fields_and_prompt_only():
    response = EndpointRegistry.match("openai", "response")

    payload, _ = response.create_payload(
        {
            "model": "gpt-5",
            "input": "hello",
            "service_tier": "flex",
            "stream_options": {"include_usage": True},
            "prompt_cache_key": "cache-key",
            "branch": "drop",
        }
    )
    assert payload == {
        "model": "gpt-5",
        "input": "hello",
        "service_tier": "flex",
        "stream_options": {"include_usage": True},
        "prompt_cache_key": "cache-key",
    }

    prompt_payload, _ = response.create_payload({"model": "gpt-5", "prompt": {"id": "pmpt_123"}})
    assert prompt_payload == {"model": "gpt-5", "prompt": {"id": "pmpt_123"}}


def test_imodel_copy_preserves_endpoint_runtime_handlers():
    model = iModel(provider="codex", codex_handlers={"on_text": _noop})
    copied = model.copy()

    assert copied.endpoint.codex_handlers["on_text"] is _noop
    assert "codex_handlers" not in copied.endpoint.config.kwargs

    api_call = model.create_api_calling(prompt="hello", on_text=_noop)
    assert api_call.call_kwargs == {"on_text": _noop}


def test_api_calling_call_kwargs_are_copyable_but_not_serialized():
    endpoint = Endpoint(
        EndpointConfig(
            name="test",
            provider="test",
            base_url="https://example.test",
            endpoint="chat",
            api_key="dummy-key-test",
        )
    )
    api_call = APICalling(
        payload={"model": "test"},
        endpoint=endpoint,
        call_kwargs={"file": b"audio"},
    )

    assert "call_kwargs" not in api_call.model_dump()
    assert api_call.as_fresh_event().call_kwargs == {"file": b"audio"}


def test_public_lionagi_star_import_does_not_reference_missing_exports():
    import lionagi

    namespace = {name: getattr(lionagi, name) for name in lionagi.__all__}

    assert "Session" in namespace
    assert "ResultCondition" not in lionagi.__all__


def test_base_http_stream_lines_convert_to_stream_chunks():
    endpoint = Endpoint(
        EndpointConfig(
            name="test",
            provider="test",
            base_url="https://example.test",
            endpoint="chat",
            api_key="dummy-key-test",
        )
    )

    text_chunk = endpoint._line_to_stream_chunk('data: {"choices":[{"delta":{"content":"hi"}}]}')
    done_chunk = endpoint._line_to_stream_chunk("data: [DONE]")

    assert isinstance(text_chunk, StreamChunk)
    assert text_chunk.type == "text"
    assert text_chunk.content == "hi"
    assert text_chunk.is_delta is True
    assert done_chunk.type == "result"
