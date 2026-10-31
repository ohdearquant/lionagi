# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import os
from unittest.mock import patch

import anyio
import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.hooks import HookRegistry
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.imodel import iModel
from lionagi.service.types import StreamChunk


class TestiModel:
    """Test the iModel class for request validation and parallel calls."""

    def test_imodel_initialization_with_provider(self, base_imodel):
        imodel = base_imodel

        assert imodel.endpoint.config.provider == "openai"
        assert imodel.endpoint.config.kwargs["model"] == "gpt-4.1-mini"
        # The actual API key might be different, so just check it's set
        assert imodel.endpoint.config._api_key is not None

    def test_imodel_initialization_from_model_path(self):
        imodel = iModel(model="openai/gpt-4.1-mini", api_key="test-key")

        assert imodel.endpoint.config.provider == "openai"
        assert imodel.endpoint.config.kwargs["model"] == "gpt-4.1-mini"

    def test_imodel_missing_provider_falls_back_to_settings_default(self):
        """iModel(model=...) without provider falls back to LIONAGI_CHAT_PROVIDER from settings rather than raising."""
        m = iModel(model="gpt-4.1-mini", api_key="test-key")
        assert m.endpoint.config.provider  # truthy — settings default was applied
        assert m.endpoint.config.provider != "gpt-4.1-mini"  # model name is not the provider

    def test_api_key_environment_variable_lookup(self):
        test_cases = [
            ("openai", "OPENAI_API_KEY"),
            ("anthropic", "ANTHROPIC_API_KEY"),
            ("perplexity", "PERPLEXITY_API_KEY"),
        ]

        for provider, env_var in test_cases:
            if env_var:
                # Just verify that an API key is set when provider requires it
                imodel = iModel(
                    provider=provider,
                    model="test-model",
                    api_key=f"test-{provider}-key",
                )
                assert imodel.endpoint.config._api_key is not None

    def test_custom_api_key(self):
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="custom-key")

        # Just verify that an API key was set
        assert imodel.endpoint.config._api_key is not None

    def test_create_api_calling(self, base_imodel):
        imodel = base_imodel

        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}], temperature=0.7
        )

        assert isinstance(api_call, APICalling)
        assert api_call.payload["model"] == "gpt-4.1-mini"
        assert api_call.payload["messages"][0]["content"] == "Hello"
        assert api_call.payload["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_successful_invoke(self, base_imodel, mock_response):
        imodel = base_imodel

        with patch.object(
            imodel.endpoint,
            "call",
            return_value=mock_response.json(),
        ):
            result = await imodel.invoke(messages=[{"role": "user", "content": "Hello"}])

        assert isinstance(result, APICalling)
        assert result.status == EventStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_parallel_invoke_calls(self, base_imodel, mock_response):
        imodel = base_imodel

        async def mock_request_with_id(request, cache_control=False, **kwargs):
            await asyncio.sleep(0.1)  # Simulate network delay
            response = mock_response.json().copy()
            response["id"] = f"response-{request['messages'][0]['content'][-1]}"
            return response

        with patch.object(imodel.endpoint, "call", side_effect=mock_request_with_id):
            # Create multiple concurrent calls
            tasks = []
            for i in range(3):
                task = asyncio.create_task(
                    imodel.invoke(messages=[{"role": "user", "content": f"Message {i}"}])
                )
                tasks.append(task)

            results = await asyncio.gather(*tasks)

        # Verify all calls completed successfully and independently
        assert len(results) == 3
        for i, result in enumerate(results):
            assert result.status == EventStatus.COMPLETED
            assert f"{i}" in result.response["id"]

    @pytest.mark.asyncio
    async def test_streaming_invoke(self, base_imodel, mock_streaming_response):
        imodel = base_imodel

        async def mock_stream():
            chunks = [
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
                {"choices": [{"delta": {}}]},  # End marker
            ]
            for chunk in chunks:
                yield chunk

        # Set streaming_process_func to return chunks
        imodel.streaming_process_func = lambda chunk: chunk

        with patch.object(imodel.endpoint, "stream", return_value=mock_stream()):
            chunks = []
            async for chunk in imodel.stream(messages=[{"role": "user", "content": "Hello"}]):
                if chunk:
                    chunks.append(chunk)

        assert len(chunks) >= 2  # Should have content chunks

    @pytest.mark.asyncio
    async def test_stream_stores_served_model_before_chunk_hook(self, base_imodel):
        async def replace_system_chunk(_event, _chunk_type, _chunk, **_kw):
            return {"handled": True}

        base_imodel.hook_registry = HookRegistry(
            stream_handlers={StreamChunk: replace_system_chunk}
        )

        async def mock_stream():
            yield StreamChunk(
                type="system",
                metadata={"model": "provider-served-model", "session_id": "session-123"},
            )

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            chunks = [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        assert chunks == [{"handled": True}]
        assert api_call.metadata["served_model"] == "provider-served-model"
        assert base_imodel.provider_metadata["served_model"] == "provider-served-model"

    @pytest.mark.asyncio
    async def test_invoke_stores_and_serializes_served_model(self, base_imodel):
        async def mock_call(*_args, **_kwargs):
            return {"model": "provider-served-model", "response": "Hello"}

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"
        with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
            result = await base_imodel.invoke(api_call=api_call)

        assert result is api_call
        assert result.metadata["served_model"] == "provider-served-model"
        assert result.to_dict()["metadata"]["served_model"] == "provider-served-model"
        assert base_imodel.provider_metadata["served_model"] == "provider-served-model"
        restored = iModel.from_dict(base_imodel.to_dict())
        assert restored.provider_metadata["served_model"] == "provider-served-model"

    @pytest.mark.asyncio
    async def test_stream_missing_report_clears_call_and_model_evidence(self, base_imodel):
        base_imodel.provider_metadata["served_model"] = "previous-served-model"

        async def mock_stream():
            yield StreamChunk(type="system", metadata={"session_id": "session-123"})

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            chunks = [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        assert len(chunks) == 1
        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_invoke_missing_report_clears_call_and_model_evidence(self, base_imodel):
        base_imodel.provider_metadata["served_model"] = "previous-served-model"

        async def mock_call(*_args, **_kwargs):
            return {"response": "Hello"}

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"
        with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
            result = await base_imodel.invoke(api_call=api_call)

        assert "served_model" not in result.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_stream_hook_cannot_forge_missing_served_model(self, base_imodel):
        base_imodel.provider_metadata["served_model"] = "previous-served-model"

        async def mock_stream():
            yield StreamChunk(type="system", metadata={"session_id": "session-123"})

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])

        async def poison_metadata(chunk):
            api_call.metadata["served_model"] = "hook-forged-model"
            return chunk

        base_imodel.streaming_process_func = poison_metadata
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            chunks = [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        assert len(chunks) == 1
        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_post_invoke_hook_cannot_forge_missing_served_model(self, base_imodel):
        base_imodel.provider_metadata["served_model"] = "previous-served-model"

        async def poison_metadata(event, **_kwargs):
            event.metadata["served_model"] = "hook-forged-model"

        base_imodel.hook_registry = HookRegistry(
            hooks={HookEventTypes.PostInvocation: poison_metadata}
        )
        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])

        async def mock_call(*_args, **_kwargs):
            return {"response": "Hello"}

        with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
            result = await base_imodel.invoke(api_call=api_call)

        assert "served_model" not in result.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_concurrent_stream_keeps_call_evidence_isolated(self, base_imodel):
        release_unknown = asyncio.Event()
        unknown_started = asyncio.Event()

        async def mock_stream(request, **_kwargs):
            content = request["messages"][0]["content"]
            if content == "unknown":
                unknown_started.set()
                await release_unknown.wait()
                yield StreamChunk(type="system", metadata={"session_id": "unknown"})
                return
            yield StreamChunk(type="system", metadata={"model": "provider-served-model"})

        async def consume(api_call):
            return [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        unknown_call = await base_imodel.create_event(
            messages=[{"role": "user", "content": "unknown"}]
        )
        unknown_call.metadata["served_model"] = "stale-event-evidence"
        reported_call = await base_imodel.create_event(
            messages=[{"role": "user", "content": "reported"}]
        )
        with patch.object(base_imodel.endpoint, "stream", side_effect=mock_stream):
            unknown_task = asyncio.create_task(consume(unknown_call))
            await unknown_started.wait()
            await consume(reported_call)
            assert reported_call.metadata["served_model"] == "provider-served-model"
            assert base_imodel.provider_metadata["served_model"] == "provider-served-model"

            release_unknown.set()
            await unknown_task

        assert "served_model" not in unknown_call.metadata
        assert reported_call.metadata["served_model"] == "provider-served-model"
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_concurrent_invoke_keeps_call_evidence_isolated(self, base_imodel):
        release_unknown = asyncio.Event()
        unknown_started = asyncio.Event()

        async def mock_call(request, **_kwargs):
            content = request["messages"][0]["content"]
            if content == "unknown":
                unknown_started.set()
                await release_unknown.wait()
                return {"response": "unknown"}
            return {"model": "provider-served-model", "response": "reported"}

        unknown_call = await base_imodel.create_event(
            messages=[{"role": "user", "content": "unknown"}]
        )
        unknown_call.metadata["served_model"] = "stale-event-evidence"
        reported_call = await base_imodel.create_event(
            messages=[{"role": "user", "content": "reported"}]
        )

        with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
            unknown_task = asyncio.create_task(base_imodel.invoke(api_call=unknown_call))
            await unknown_started.wait()
            reported_result = await base_imodel.invoke(api_call=reported_call)
            assert reported_result.metadata["served_model"] == "provider-served-model"
            assert base_imodel.provider_metadata["served_model"] == "provider-served-model"

            release_unknown.set()
            unknown_result = await unknown_task

        assert "served_model" not in unknown_result.metadata
        assert reported_result.metadata["served_model"] == "provider-served-model"
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_concurrent_invoke_records_each_report_and_model_keeps_last_completed(
        self, base_imodel
    ):
        release_slow = asyncio.Event()
        slow_started = asyncio.Event()

        async def mock_call(request, **_kwargs):
            content = request["messages"][0]["content"]
            if content == "slow":
                slow_started.set()
                await release_slow.wait()
                return {"model": "slow-served-model", "response": "slow"}
            return {"model": "fast-served-model", "response": "fast"}

        slow_call = await base_imodel.create_event(messages=[{"role": "user", "content": "slow"}])
        fast_call = await base_imodel.create_event(messages=[{"role": "user", "content": "fast"}])
        with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
            slow_task = asyncio.create_task(base_imodel.invoke(api_call=slow_call))
            await slow_started.wait()
            fast_result = await base_imodel.invoke(api_call=fast_call)
            assert fast_result.metadata["served_model"] == "fast-served-model"
            assert base_imodel.provider_metadata["served_model"] == "fast-served-model"

            release_slow.set()
            slow_result = await slow_task

        assert fast_result.metadata["served_model"] == "fast-served-model"
        assert slow_result.metadata["served_model"] == "slow-served-model"
        assert base_imodel.provider_metadata["served_model"] == "slow-served-model"

    @pytest.mark.asyncio
    async def test_early_closed_stream_clears_call_and_model_evidence(self, base_imodel):
        base_imodel.provider_metadata["served_model"] = "previous-served-model"
        never = asyncio.Event()

        async def mock_stream():
            yield StreamChunk(type="text", content="first")
            await never.wait()

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            stream = base_imodel.stream(api_call=api_call)
            assert await stream.__anext__() == StreamChunk(type="text", content="first")
            await stream.aclose()

        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_cancelled_stream_clears_call_and_model_evidence(self, base_imodel):
        base_imodel.provider_metadata["served_model"] = "previous-served-model"
        provider_started = asyncio.Event()
        never = asyncio.Event()

        async def mock_stream():
            provider_started.set()
            await never.wait()
            yield StreamChunk(type="text", content="unreachable")

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            stream = base_imodel.stream(api_call=api_call)
            pending_chunk = asyncio.create_task(stream.__anext__())
            await provider_started.wait()
            pending_chunk.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending_chunk
            await stream.aclose()

        assert api_call.status == EventStatus.CANCELLED
        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_pre_create_hook_replacement_gets_exact_call_evidence(self, base_imodel):
        replacement = base_imodel.create_api_calling(
            messages=[{"role": "user", "content": "replacement payload"}]
        )
        replacement.metadata["served_model"] = "stale-event-evidence"

        async def replace_event(_event_type, **_kwargs):
            return replacement

        async def mock_call(*_args, **_kwargs):
            return {"model": "provider-served-model", "response": "Hello"}

        base_imodel.hook_registry = HookRegistry(
            hooks={HookEventTypes.PreEventCreate: replace_event}
        )
        with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
            result = await base_imodel.invoke(
                messages=[{"role": "user", "content": "original payload"}]
            )

        assert result is replacement
        assert result.metadata["served_model"] == "provider-served-model"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reported_model", [None, "", "   "])
    async def test_invoke_does_not_infer_missing_served_model(self, base_imodel, reported_model):
        base_imodel.provider_metadata["served_model"] = "previous-served-model"

        async def mock_call(*_args, **_kwargs):
            return {"model": reported_model, "response": "Hello"}

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"
        with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
            result = await base_imodel.invoke(api_call=api_call)

        assert base_imodel.model_name == "gpt-4.1-mini"
        assert "served_model" not in result.metadata
        assert "served_model" not in base_imodel.provider_metadata

    def test_model_name_property(self, base_imodel):
        imodel = base_imodel

        assert imodel.model_name == "gpt-4.1-mini"

    def test_request_options_property(self, base_imodel):
        imodel = base_imodel

        # NOTE: request_options removed due to incorrect role literals in generated models
        from lionhub.providers.openai.chat import OpenAIChatCompletionsRequest

        assert imodel.request_options == OpenAIChatCompletionsRequest

    @pytest.mark.asyncio
    async def test_error_handling_in_invoke(self, base_imodel):
        imodel = base_imodel
        imodel.provider_metadata["served_model"] = "previous-served-model"
        api_call = await imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.metadata["served_model"] = "stale-event-evidence"

        with patch.object(imodel.endpoint, "call", side_effect=Exception("API Error")):
            result = await imodel.invoke(api_call=api_call)

            # The invoke method returns a failed APICalling object instead of raising
            assert result.status == EventStatus.FAILED
            assert result.execution.error is not None
            assert "served_model" not in result.metadata
            assert "served_model" not in imodel.provider_metadata

    def test_cache_control_parameter(self, base_imodel):
        imodel = base_imodel

        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}], cache_control=True
        )

        assert api_call.cache_control is True

    def test_include_token_usage_to_model(self, base_imodel):
        imodel = base_imodel

        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}],
            include_token_usage_to_model=True,
        )

        assert api_call.include_token_usage_to_model is True

    def test_to_dict_serialization(self, base_imodel):
        imodel = base_imodel

        data = imodel.to_dict()

        assert "endpoint" in data
        assert "processor_config" in data
        assert imodel.endpoint.config.provider == "openai"

    @pytest.mark.asyncio
    async def test_custom_streaming_process_func(self):

        def custom_process(chunk):
            if hasattr(chunk, "choices") and chunk.choices:
                return f"Processed: {chunk.choices[0].delta.content}"
            return None

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            imodel = iModel(
                provider="openai",
                model="gpt-4.1-mini",
                streaming_process_func=custom_process,
            )

        assert imodel.streaming_process_func == custom_process

    @pytest.mark.asyncio
    async def test_rate_limiting_configuration(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            imodel = iModel(
                provider="openai",
                model="gpt-4.1-mini",
                limit_requests=10,
                limit_tokens=1000,
                queue_capacity=50,
            )

        assert imodel.executor.config["limit_requests"] == 10
        assert imodel.executor.config["limit_tokens"] == 1000
        assert imodel.executor.config["queue_capacity"] == 50

    @pytest.mark.asyncio
    async def test_concurrent_different_models(self):
        imodel1 = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        imodel2 = iModel(provider="openai", model="gpt-4o", api_key="test-key")

        async def mock_request(request, cache_control=False, **kwargs):
            await asyncio.sleep(0.05)
            # The request parameter is the payload dict
            model = request.get("model", "unknown") if isinstance(request, dict) else "unknown"
            return {"model_used": model, "response": "test"}

        with patch(
            "lionagi.service.connections.endpoint.Endpoint.call",
            side_effect=mock_request,
        ):
            task1 = asyncio.create_task(
                imodel1.invoke(messages=[{"role": "user", "content": "Hello"}])
            )
            task2 = asyncio.create_task(
                imodel2.invoke(messages=[{"role": "user", "content": "Hello"}])
            )

            result1, result2 = await asyncio.gather(task1, task2)

        assert result1.response["model_used"] == "gpt-4.1-mini"
        assert result2.response["model_used"] == "gpt-4o"

    def test_imodel_custom_id_with_id_get_id(self):
        from uuid import uuid4

        custom_id = uuid4()
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            id=custom_id,
        )

        assert imodel.id == custom_id

    def test_imodel_custom_id_as_string(self):
        from uuid import uuid4

        custom_id = str(uuid4())
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            id=custom_id,
        )

        # ID.get_id should handle UUID string conversion
        assert str(imodel.id) == custom_id

    def test_imodel_invalid_created_at_type(self):
        with pytest.raises(ValueError, match="created_at must be a float timestamp"):
            iModel(
                provider="openai",
                model="gpt-4.1-mini",
                api_key="test-key",
                created_at="not-a-float",
            )

    def test_imodel_with_endpoint_object(self):
        from lionagi.service.connections.match_endpoint import match_endpoint

        # Create an endpoint object
        endpoint = match_endpoint(
            provider="openai",
            endpoint="chat",
            model="gpt-4.1-mini",
            api_key="test-key",
        )

        # Pass endpoint object directly
        imodel = iModel(endpoint=endpoint, api_key="test-key")

        assert imodel.endpoint == endpoint
        assert imodel.endpoint.config.provider == "openai"

    def test_imodel_hook_registry_as_dict(self):
        from lionagi.service.hooks import HookRegistry

        hook_registry_dict = {}
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            hook_registry=hook_registry_dict,
        )

        assert isinstance(imodel.hook_registry, HookRegistry)

    def test_imodel_claude_code_auto_resume(self):
        imodel = iModel(
            provider="claude_code",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )

        # Set session_id on the CLI endpoint
        imodel.endpoint.session_id = "test-session-123"

        # Create API calling without explicit resume parameter
        api_call = imodel.create_api_calling(messages=[{"role": "user", "content": "Hello"}])

        # Check that resume was auto-injected (in the request object for claude_code)
        assert api_call.payload["request"].resume == "test-session-123"

    def test_imodel_claude_code_no_auto_resume_if_explicit(self):
        imodel = iModel(
            provider="claude_code",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )

        # Set session_id on the CLI endpoint
        imodel.endpoint.session_id = "test-session-123"

        # Create API calling WITH explicit resume parameter
        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}],
            resume="explicit-session",
        )

        # Check that explicit resume was used (in the request object for claude_code)
        assert api_call.payload["request"].resume == "explicit-session"

    @pytest.mark.asyncio
    async def test_imodel_streaming_with_async_process_func(self):

        async def async_process(chunk):
            await asyncio.sleep(0.001)
            if hasattr(chunk, "get") and chunk.get("choices"):
                return f"Async: {chunk['choices'][0]['delta'].get('content', '')}"
            return None

        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            streaming_process_func=async_process,
        )

        async def mock_stream():
            chunks = [
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
            ]
            for chunk in chunks:
                yield chunk

        with patch.object(imodel.endpoint, "stream", return_value=mock_stream()):
            chunks = []
            async for chunk in imodel.stream(messages=[{"role": "user", "content": "Hello"}]):
                if chunk and not isinstance(chunk, APICalling):
                    chunks.append(chunk)

        assert len(chunks) >= 2
        assert any("Async:" in str(chunk) for chunk in chunks)

    @pytest.mark.asyncio
    async def test_process_chunk_uses_string_key_stream_handler(self):
        async def handler(_event, chunk_type, chunk, **_kw):
            assert chunk_type == "dict"
            return {"handled": chunk["value"]}

        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            hook_registry=HookRegistry(stream_handlers={"dict": handler}),
        )

        assert await imodel.process_chunk({"value": "ok"}) == {"handled": "ok"}

    @pytest.mark.asyncio
    async def test_process_chunk_uses_type_key_stream_handler(self):
        async def handler(_event, chunk_type, chunk, **_kw):
            assert chunk_type is dict
            return {"handled": chunk["value"]}

        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            hook_registry=HookRegistry(stream_handlers={dict: handler}),
        )

        assert await imodel.process_chunk({"value": "ok"}) == {"handled": "ok"}

    @pytest.mark.asyncio
    async def test_hook_registry_takes_priority_over_streaming_process_func(self):
        """Hook registry result is returned directly; streaming_process_func is not called."""

        async def handler(_event, _chunk_type, _chunk, **_kw):
            return {"choices": [{"delta": {"content": "hooked"}}]}

        def process(chunk):
            return chunk["choices"][0]["delta"]["content"].upper()

        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            streaming_process_func=process,
            hook_registry=HookRegistry(stream_handlers={"dict": handler}),
        )

        result = await imodel.process_chunk({"value": "raw"})
        # Hook handles the chunk → returns hook result, NOT process(hook_result)
        assert result == {"choices": [{"delta": {"content": "hooked"}}]}

    @pytest.mark.asyncio
    async def test_process_chunk_streaming_hook_exit_policy(self):
        async def failing_handler(_event, _chunk_type, _chunk, **_kw):
            raise RuntimeError("stream hook failed")

        non_exit_imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            hook_registry=HookRegistry(stream_handlers={"str": failing_handler}),
        )
        assert await non_exit_imodel.process_chunk("raw") is None

        exit_imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            hook_registry=HookRegistry(stream_handlers={"str": failing_handler}),
            exit_hook=True,
        )
        with pytest.raises(RuntimeError, match="stream hook failed"):
            await exit_imodel.process_chunk("raw")

    @pytest.mark.asyncio
    async def test_imodel_claude_code_session_id_storage(self, mock_response):
        imodel = iModel(
            provider="claude_code",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )

        # Mock response with session_id
        async def mock_request_with_session(request, cache_control=False, **kwargs):
            return {"session_id": "new-session-456", "response": "test"}

        with patch.object(imodel.endpoint, "call", side_effect=mock_request_with_session):
            result = await imodel.invoke(messages=[{"role": "user", "content": "Hello"}])

        # Check that session_id was stored on the CLI endpoint
        assert imodel.endpoint.session_id == "new-session-456"

    @pytest.mark.parametrize("empty", [None, ""])
    @pytest.mark.asyncio
    async def test_an_invoke_that_returns_no_session_id_keeps_the_confirmed_one(self, empty):
        imodel = iModel(
            provider="claude_code",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )
        imodel.endpoint.session_id = "confirmed-session"

        # a call whose stream carried no id-bearing chunk returns the field empty
        async def no_session(request, cache_control=False, **kwargs):
            return {"session_id": empty, "response": "test"}

        with patch.object(imodel.endpoint, "call", side_effect=no_session):
            completed_call = await imodel.invoke(messages=[{"role": "user", "content": "Hello"}])

        assert completed_call.response == {"session_id": empty, "response": "test"}
        next_call = imodel.create_api_calling(messages=[{"role": "user", "content": "Again"}])
        assert next_call.payload["request"].resume == "confirmed-session"
        assert imodel.endpoint.session_id == "confirmed-session"

    def test_imodel_to_dict(self):
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=10,
        )

        data = imodel.to_dict()

        assert "id" in data
        assert "created_at" in data
        assert "endpoint" in data
        assert "processor_config" in data
        assert "provider_metadata" in data
        assert data["processor_config"]["limit_requests"] == 10

    def test_imodel_from_dict(self):
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=10,
        )

        data = imodel.to_dict()
        restored = iModel.from_dict(data)

        assert restored.id == imodel.id
        assert restored.created_at == imodel.created_at
        assert restored.endpoint.config.provider == "openai"
        assert restored.executor.config["limit_requests"] == 10

    def test_imodel_from_dict_with_match_endpoint(self):
        # Create initial iModel
        imodel = iModel(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )

        # Serialize and deserialize
        data = imodel.to_dict()
        restored = iModel.from_dict(data)

        # Check that match_endpoint was used to properly reconstruct
        assert restored.endpoint.config.provider == "anthropic"
        assert restored.endpoint.config.kwargs["model"] == "claude-3-5-sonnet-20241022"

    @pytest.mark.asyncio
    async def test_imodel_unsupported_event_type(self):
        from lionagi.protocols.types import Event

        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

        # Create a custom event type that's not APICalling
        class CustomEvent(Event):
            pass

        with pytest.raises(
            ValueError,
            match="Unsupported event type.*Only APICalling is supported",
        ):
            await imodel.create_event(create_event_type=CustomEvent)

    @pytest.mark.asyncio
    async def test_imodel_invoke_with_concurrency_limit(self, mock_response):
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=2,
        )

        with patch.object(
            imodel.endpoint,
            "call",
            return_value=mock_response.json(),
        ):
            result = await imodel.invoke(messages=[{"role": "user", "content": "Hello"}])

        assert isinstance(result, APICalling)
        assert result.status == EventStatus.COMPLETED


class TestEffortSuffixIsNotStrippedFromVendorModelIds:
    """iModel strips a trailing effort word off a model name and routes it to
    the provider's effort kwarg, so ``codex/gpt-5.4-max`` means gpt-5.4 at max
    effort. That convenience belongs to lionagi's own ``provider/model-effort``
    grammar, which spends its single slash on the provider.

    A model name that still contains a slash after the provider is known came
    from another vendor's catalogue -- codex reaches OpenRouter models this way
    -- and its trailing token is part of the id. Stripping it produced a model
    id nobody serves, and the provider answered with a 400 naming a model that
    was never requested.
    """

    def test_namespaced_vendor_id_keeps_a_trailing_effort_word(self):
        m = iModel(
            provider="codex",
            model="qwen/qwen3.8-max",
            endpoint="query_cli",
            api_key="dummy",
        )
        assert m.endpoint.config.kwargs["model"] == "qwen/qwen3.8-max"
        assert "reasoning_effort" not in m.endpoint.config.kwargs

    def test_a_bare_model_name_still_gives_up_its_effort_suffix(self):
        """The must-strip arm. Without it a fix that simply deletes the strip
        passes the test above and silently drops effort routing everywhere."""
        m = iModel(
            provider="codex",
            model="gpt-5.4-max",
            endpoint="query_cli",
            api_key="dummy",
        )
        assert m.endpoint.config.kwargs["model"] == "gpt-5.4"
        assert m.endpoint.config.kwargs["reasoning_effort"] == "max"

    def test_a_namespaced_id_with_no_effort_word_is_untouched_either_way(self):
        """Control: the fleet's other OpenRouter ids never collided in the
        first place, so they must read identically before and after."""
        m = iModel(
            provider="codex",
            model="deepseek/deepseek-v4-flash-0731",
            endpoint="query_cli",
            api_key="dummy",
        )
        assert m.endpoint.config.kwargs["model"] == "deepseek/deepseek-v4-flash-0731"


_CREATED = (
    'data: {"type":"response.created","response":{"id":"r1","model":"gpt-served-2",'
    '"status":"in_progress"}}'
)


class TestReportedServedModel:
    """Where a provider's own model name is read from, per chunk shape."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            # Chat completion chunk: model at the event's top level.
            (
                'data: {"id":"c1","model":"gpt-served-1","choices":[{"delta":{"content":"hi"}}]}',
                "gpt-served-1",
            ),
            # Anthropic message_start: model under "message".
            (
                'data: {"type":"message_start","message":{"id":"m1","model":"claude-served-1"}}',
                "claude-served-1",
            ),
            # Responses created event: model under "response".
            (
                'data: {"type":"response.created","response":{"id":"r1","model":"gpt-served-2"}}',
                "gpt-served-2",
            ),
            # The Responses API's own terminal event is read: it is the provider's
            # event, never a CLI run record.
            (
                'data: {"type":"response.completed","response":{"model":"gpt-served-3"}}',
                "gpt-served-3",
            ),
            ('data: {"id":"c2","choices":[{"delta":{"content":"hi"}}]}', None),
            # A failed response, an in-stream error chunk and an error event name a
            # model that produced no output.
            (
                'data: {"type":"response.failed","response":{"id":"r2","model":"gpt-failed-4",'
                '"status":"failed","error":{"code":"server_error","message":"x"}}}',
                None,
            ),
            (
                'data: {"id":"c3","model":"routed-7","choices":[{"delta":{"content":""},'
                '"finish_reason":"error"}],"error":{"message":"upstream"}}',
                None,
            ),
            ('data: {"error":{"message":"x"},"model":"gpt-err-8"}', None),
            # A failure named without an error key, or nested under a live event.
            (
                'data: {"id":"c8","model":"routed-14","choices":[{"delta":{"content":""},'
                '"finish_reason":"error"}]}',
                None,
            ),
            (
                'data: {"type":"response.in_progress","response":{"id":"r6",'
                '"model":"gpt-nested-11","status":"in_progress","error":{"message":"x"}}}',
                None,
            ),
            (
                'data: {"type":"response.created","response":{"id":"r8",'
                '"model":"gpt-status-13","status":"failed"}}',
                None,
            ),
            # An empty error is no error.
            (
                'data: {"id":"c9","model":"gpt-ok-3","error":{},'
                '"choices":[{"delta":{"content":"x"}}]}',
                "gpt-ok-3",
            ),
            # An incomplete response was cut short, not refused: the model it names ran.
            (
                'data: {"type":"response.incomplete","response":{"id":"r3",'
                '"model":"gpt-served-5","status":"incomplete"}}',
                "gpt-served-5",
            ),
        ],
    )
    def test_api_stream_chunk_through_the_converter(self, base_imodel, line, expected):
        chunk = base_imodel.endpoint._line_to_stream_chunk(line)
        assert iModel._reported_served_model(chunk) == expected

    @pytest.mark.parametrize(
        "chunk,expected",
        [
            (StreamChunk(type="system", metadata={"model": "cli-served"}), "cli-served"),
            # A CLI text or result chunk carrying a model is not a report.
            (StreamChunk(type="text", content="x", metadata={"model": "echoed"}), None),
            (StreamChunk(type="result", metadata={"model": "requested"}), None),
            # A result chunk is read only for the Responses terminal event.
            (
                StreamChunk(
                    type="result", metadata={"raw": {"type": "turn.completed", "model": "r"}}
                ),
                None,
            ),
            (StreamChunk(type="system", metadata={"model": "  "}), None),
            (StreamChunk(type="system", metadata={"raw": {"model": 7}}), None),
            (StreamChunk(type="system", metadata={"model": "cli-x", "status": "failed"}), None),
        ],
    )
    def test_chunk_shapes(self, chunk, expected):
        assert iModel._reported_served_model(chunk) == expected

    @pytest.mark.parametrize(
        "chunk,expected",
        [
            (StreamChunk(type="error", content="x", is_error=True), True),
            # A Codex reconnect notice is an error chunk that is not an error.
            (
                StreamChunk(
                    type="error",
                    content="Reconnecting... 1/5",
                    is_error=False,
                    metadata={"reconnect_notice": True},
                ),
                False,
            ),
            # A tool that failed is not the call failing.
            (StreamChunk(type="tool_result", content="x", is_error=True), False),
            (StreamChunk(type="text", content="x"), False),
        ],
    )
    def test_only_a_chunk_marked_as_an_error_reports_a_failed_call(self, chunk, expected):
        assert iModel._reports_failure(chunk) is expected

    @pytest.mark.parametrize(
        "event_type,expected",
        [
            ("response.failed", True),
            ("turn.failed", True),
            ("failed", True),
            ("response.mcp_call.failed", False),
            ("response.mcp_list_tools.failed", False),
            ("item.failed", False),
            ("tool_failed", False),
            ("response.completed", False),
        ],
    )
    def test_only_the_calls_own_failure_event_reports_a_failed_call(self, event_type, expected):
        assert iModel._event_failed({"type": event_type}) is expected

    @pytest.mark.parametrize(
        "carried",
        [
            {"error": {"code": "mcp_error", "message": "x"}},
            {"is_error": True},
            {"status": "failed"},
        ],
        ids=["error", "is_error", "status"],
    )
    @pytest.mark.parametrize("event_type", ["response.mcp_call.failed", "item.failed"])
    def test_an_item_failure_is_not_the_call_failing_whatever_it_carries(self, event_type, carried):
        assert iModel._event_failed({"type": event_type, **carried}) is False
        # Control: the same fields on an event of no failure type do fail the call.
        assert iModel._event_failed({"type": "response.output_item.done", **carried}) is True

    @pytest.mark.parametrize(
        "carried",
        [
            {"error": {"code": "tool_error", "message": "x"}},
            {"is_error": True},
            {"status": "failed"},
        ],
        ids=["error", "is_error", "status"],
    )
    @pytest.mark.parametrize("event_type", ["tool_failed", "turn_failed", "stream_failed"])
    def test_a_failure_name_without_a_dot_is_read_by_what_it_carries(self, event_type, carried):
        assert iModel._event_failed({"type": event_type, **carried}) is True
        assert iModel._event_failed({"type": event_type}) is False

    @pytest.mark.parametrize(
        "response,expected",
        [
            ({"model": "gpt-served-1", "error": None}, "gpt-served-1"),
            ({"model": "gpt-err-8", "error": {"message": "x"}}, None),
            ({"model": "gpt-status-21", "status": "failed", "error": None}, None),
            ({"model": "cli-x", "is_error": True}, None),
            ({"model": "gpt-ok-20", "status": "completed", "error": None}, "gpt-ok-20"),
        ],
    )
    def test_a_response_that_carries_an_error_names_no_served_model(self, response, expected):
        assert iModel._reported_served_model(response) == expected

    def test_a_copy_carries_no_served_model_and_a_restore_keeps_it(self, base_imodel):
        """A copy is a new model that has made no call; a restore is the same model."""
        base_imodel.provider_metadata["served_model"] = "gpt-served-1"
        base_imodel.provider_metadata["session_id"] = "s-1"

        copied = base_imodel.copy()
        restored = iModel.from_dict(base_imodel.to_dict())

        assert copied.id != base_imodel.id
        assert "served_model" not in copied.provider_metadata
        assert copied.provider_metadata["session_id"] == "s-1"
        assert restored.id == base_imodel.id
        assert restored.provider_metadata["served_model"] == "gpt-served-1"
        assert base_imodel.provider_metadata["served_model"] == "gpt-served-1"

    @pytest.mark.asyncio
    async def test_stream_does_not_record_the_model_an_error_chunk_names(self, base_imodel):
        async def mock_stream():
            yield base_imodel.endpoint._line_to_stream_chunk(
                'data: {"id":"c3","model":"routed-7","choices":[{"delta":{"content":""},'
                '"finish_reason":"error"}],"error":{"message":"upstream"}}'
            )

        base_imodel.provider_metadata["served_model"] = "from-an-earlier-call"
        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            with pytest.raises(ValueError, match="failed call inside its stream: upstream"):
                [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        assert api_call.status == EventStatus.FAILED
        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.parametrize(
        "failure",
        [
            'data: {"type":"response.failed","response":{"id":"r1","model":"gpt-failed-4",'
            '"status":"failed","error":{"code":"server_error","message":"x"}}}',
            'data: {"type":"error","code":"server_error","message":"x","model":"gpt-err-6"}',
        ],
    )
    # CLI models stream under a concurrency limit, API models without one by
    # default; the two paths read chunks in separate loops.
    @pytest.mark.parametrize("concurrency_limit", [None, 2])
    @pytest.mark.asyncio
    async def test_a_failure_later_in_the_stream_voids_the_model_read_before_it(
        self, failure, concurrency_limit
    ):
        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=concurrency_limit,
        )

        async def mock_stream():
            yield model.endpoint._line_to_stream_chunk(_CREATED)
            yield model.endpoint._line_to_stream_chunk(failure)

        model.provider_metadata["served_model"] = "from-an-earlier-call"
        api_call = await model.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(model.endpoint, "stream", return_value=mock_stream()):
            with pytest.raises(ValueError, match="failed call inside its stream"):
                [chunk async for chunk in model.stream(api_call=api_call)]

        assert api_call.status == EventStatus.FAILED
        assert "served_model" not in api_call.metadata
        assert "served_model" not in model.provider_metadata

    @pytest.mark.parametrize("concurrency_limit", [None, 2])
    @pytest.mark.asyncio
    async def test_no_model_named_after_a_failure_is_recorded(self, concurrency_limit):
        # A CLI stream's chunks carry no call_failed mark, so the call itself still
        # completes; the model a later chunk names must not stand in for the failed one.
        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=concurrency_limit,
        )

        async def mock_stream():
            yield StreamChunk(type="system", metadata={"model": "cli-first"})
            yield StreamChunk(type="error", content="session failed", is_error=True)
            yield StreamChunk(type="system", metadata={"model": "cli-later"})

        model.provider_metadata["served_model"] = "from-an-earlier-call"
        api_call = await model.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(model.endpoint, "stream", return_value=mock_stream()):
            [chunk async for chunk in model.stream(api_call=api_call)]

        assert api_call.status == EventStatus.COMPLETED
        assert "served_model" not in api_call.metadata
        assert "served_model" not in model.provider_metadata

    @pytest.mark.parametrize("concurrency_limit", [None, 2])
    @pytest.mark.asyncio
    async def test_a_failed_tool_call_inside_a_completed_call_keeps_the_model(
        self, concurrency_limit
    ):
        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=concurrency_limit,
        )

        async def mock_stream():
            yield model.endpoint._line_to_stream_chunk(_CREATED)
            yield model.endpoint._line_to_stream_chunk(
                'data: {"type":"response.mcp_call.failed","item_id":"mcp_1","output_index":0}'
            )
            yield model.endpoint._line_to_stream_chunk(
                'data: {"type":"response.output_text.delta","delta":"hi"}'
            )

        api_call = await model.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(model.endpoint, "stream", return_value=mock_stream()):
            [chunk async for chunk in model.stream(api_call=api_call)]

        assert api_call.metadata["served_model"] == "gpt-served-2"
        assert model.provider_metadata["served_model"] == "gpt-served-2"

    @pytest.mark.parametrize("how", ["cancel", "timeout"])
    @pytest.mark.parametrize("concurrency_limit", [None, 2])
    @pytest.mark.asyncio
    async def test_a_call_cancelled_after_the_model_was_named_keeps_it(
        self, how, concurrency_limit
    ):
        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=concurrency_limit,
        )
        never = asyncio.Event()

        async def mock_stream():
            yield model.endpoint._line_to_stream_chunk(_CREATED)
            await never.wait()
            yield model.endpoint._line_to_stream_chunk(
                'data: {"type":"response.output_text.delta","delta":"unreachable"}'
            )

        model.provider_metadata["served_model"] = "from-an-earlier-call"
        api_call = await model.create_event(messages=[{"role": "user", "content": "Hello"}])
        named = asyncio.Event()

        async def consume():
            async for _chunk in model.stream(api_call=api_call):
                named.set()

        with patch.object(model.endpoint, "stream", return_value=mock_stream()):
            if how == "timeout":
                with pytest.raises(TimeoutError):
                    with anyio.fail_after(0.5):
                        await consume()
            else:
                task = asyncio.create_task(consume())
                await named.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        assert api_call.status == EventStatus.CANCELLED
        assert api_call.metadata["served_model"] == "gpt-served-2"
        assert model.provider_metadata["served_model"] == "gpt-served-2"

    @pytest.mark.asyncio
    async def test_an_invoke_cancelled_while_its_call_is_in_flight_records_no_model(
        self, base_imodel
    ):
        base_imodel.provider_metadata["served_model"] = "from-an-earlier-call"
        started = asyncio.Event()
        never = asyncio.Event()

        async def mock_call(*_args, **_kwargs):
            # Written during the call, after invoke cleared the field at its start.
            api_call.metadata["served_model"] = "written-during-the-call"
            base_imodel.provider_metadata["served_model"] = "written-during-the-call"
            started.set()
            await never.wait()
            return {"model": "unreachable", "response": "x"}

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        try:
            with patch.object(base_imodel.endpoint, "call", side_effect=mock_call):
                task = asyncio.create_task(base_imodel.invoke(api_call=api_call))
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            never.set()

        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.parametrize("failure", ["cancelled", "raised"])
    @pytest.mark.asyncio
    async def test_an_invoke_stopped_before_its_call_exists_keeps_the_last_model(
        self, base_imodel, failure
    ):
        base_imodel.provider_metadata["served_model"] = "from-an-earlier-call"
        started = asyncio.Event()

        async def create_event(**_kwargs):
            started.set()
            if failure == "raised":
                raise RuntimeError("the request could not be built")
            await asyncio.Event().wait()

        with patch.object(base_imodel, "create_event", side_effect=create_event):
            task = asyncio.create_task(
                base_imodel.invoke(messages=[{"role": "user", "content": "Hello"}])
            )
            await started.wait()
            if failure == "cancelled":
                task.cancel()
            with pytest.raises(asyncio.CancelledError if failure == "cancelled" else ValueError):
                await task

        assert base_imodel.provider_metadata["served_model"] == "from-an-earlier-call"

    @pytest.mark.parametrize("failure", ["cancelled", "raised"])
    @pytest.mark.asyncio
    async def test_an_invoke_stopped_before_its_call_is_sent_keeps_the_last_model(
        self, base_imodel, failure
    ):
        base_imodel.provider_metadata["served_model"] = "from-an-earlier-call"
        started = asyncio.Event()
        sent = []

        async def enqueue(_event):
            # invoke sends only its own call: the call exists and is being queued to the
            # processor; nothing has been sent.
            started.set()
            if failure == "raised":
                raise RuntimeError("the call could not be queued")
            await asyncio.Event().wait()

        async def call(*_args, **_kwargs):
            sent.append(True)
            return {"model": "unreachable", "response": "x"}

        await base_imodel.executor.start()
        with (
            patch.object(base_imodel.executor.processor, "enqueue", side_effect=enqueue),
            patch.object(base_imodel.endpoint, "call", side_effect=call),
        ):
            task = asyncio.create_task(
                base_imodel.invoke(messages=[{"role": "user", "content": "Hello"}])
            )
            await started.wait()
            if failure == "cancelled":
                task.cancel()
            with pytest.raises(asyncio.CancelledError if failure == "cancelled" else ValueError):
                await task

        assert sent == []
        assert base_imodel.provider_metadata["served_model"] == "from-an-earlier-call"

    @pytest.mark.asyncio
    async def test_a_pending_call_records_no_model_and_keeps_the_last_one(self, base_imodel):
        base_imodel.provider_metadata["served_model"] = "from-an-earlier-call"
        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        assert api_call.status == EventStatus.PENDING

        base_imodel._store_served_model(api_call, "never-served")

        assert "served_model" not in api_call.metadata
        assert base_imodel.provider_metadata["served_model"] == "from-an-earlier-call"

    @pytest.mark.asyncio
    async def test_a_call_the_queue_sends_after_its_invoke_was_cancelled_records_nothing(self):
        # One request per replenish interval, so the second call waits in the queue.
        model = iModel(
            provider="openai", model="gpt-4.1-mini", api_key="test-key", limit_requests=1
        )
        sent = []

        async def call(request, **_kwargs):
            sent.append(request["messages"][0]["content"])
            return {"model": "gpt-" + request["messages"][0]["content"], "response": "x"}

        try:
            with patch.object(model.endpoint, "call", side_effect=call):
                await model.invoke(messages=[{"role": "user", "content": "A"}])
                queued = await model.create_event(messages=[{"role": "user", "content": "B"}])
                task = asyncio.create_task(model.invoke(api_call=queued))
                await asyncio.sleep(0.3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert sent == ["A"]

                # Room for one more request, so the queue sends the call nobody waits for.
                model.executor.processor._available_requests = 1
                await model.executor.processor.process()
        finally:
            await model.close()

        assert sent == ["A", "B"]
        assert queued.status == EventStatus.COMPLETED
        assert "served_model" not in queued.metadata
        assert model.provider_metadata["served_model"] == "gpt-A"

    @pytest.mark.parametrize("concurrency_limit", [None, 2])
    @pytest.mark.asyncio
    async def test_a_timeout_raised_inside_the_stream_records_no_model(self, concurrency_limit):
        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=concurrency_limit,
        )

        async def mock_stream():
            yield model.endpoint._line_to_stream_chunk(_CREATED)
            raise asyncio.TimeoutError("read timed out")

        model.provider_metadata["served_model"] = "from-an-earlier-call"
        api_call = await model.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(model.endpoint, "stream", return_value=mock_stream()):
            with pytest.raises(ValueError):
                [chunk async for chunk in model.stream(api_call=api_call)]

        assert api_call.status == EventStatus.FAILED
        assert "served_model" not in api_call.metadata
        assert "served_model" not in model.provider_metadata

    @pytest.mark.parametrize("concurrency_limit", [None, 2])
    @pytest.mark.asyncio
    async def test_a_response_with_a_cancelled_status_is_read(self, concurrency_limit):
        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=concurrency_limit,
        )

        async def mock_stream():
            yield model.endpoint._line_to_stream_chunk(
                'data: {"type":"response.incomplete","response":{"id":"r1",'
                '"model":"gpt-cancelled-7","status":"cancelled"}}'
            )

        api_call = await model.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(model.endpoint, "stream", return_value=mock_stream()):
            [chunk async for chunk in model.stream(api_call=api_call)]

        assert api_call.metadata["served_model"] == "gpt-cancelled-7"
        assert model.provider_metadata["served_model"] == "gpt-cancelled-7"

    @pytest.mark.asyncio
    async def test_a_stream_that_fails_in_transport_records_no_model(self, base_imodel):
        async def mock_stream():
            yield base_imodel.endpoint._line_to_stream_chunk(_CREATED)
            raise RuntimeError("connection reset")

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            with pytest.raises(ValueError):
                [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        assert api_call.status == EventStatus.FAILED
        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_a_reconnect_notice_does_not_void_the_model(self, base_imodel):
        async def mock_stream():
            yield base_imodel.endpoint._line_to_stream_chunk(_CREATED)
            yield StreamChunk(
                type="error",
                content="Reconnecting... 1/5",
                is_error=False,
                metadata={"reconnect_notice": True},
            )

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        assert api_call.metadata["served_model"] == "gpt-served-2"

    @pytest.mark.asyncio
    async def test_a_stream_closed_early_keeps_the_model_it_read(self, base_imodel):
        async def mock_stream():
            yield base_imodel.endpoint._line_to_stream_chunk(_CREATED)
            yield base_imodel.endpoint._line_to_stream_chunk(
                'data: {"type":"response.output_text.delta","delta":"hi"}'
            )

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            async with contextlib.aclosing(base_imodel.stream(api_call=api_call)) as stream:
                async for _chunk in stream:
                    break

        assert api_call.status != EventStatus.FAILED
        assert api_call.metadata["served_model"] == "gpt-served-2"

    @pytest.mark.asyncio
    async def test_a_failed_call_records_no_model(self, base_imodel):
        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        api_call.status = EventStatus.FAILED
        base_imodel.provider_metadata["served_model"] = "from-an-earlier-call"

        base_imodel._store_served_model(api_call, "gpt-served-2")

        assert "served_model" not in api_call.metadata
        assert "served_model" not in base_imodel.provider_metadata

    @pytest.mark.asyncio
    async def test_stream_records_the_model_named_in_an_api_text_chunk(self, base_imodel):
        async def mock_stream():
            yield base_imodel.endpoint._line_to_stream_chunk(
                'data: {"id":"c1","model":"gpt-served-1","choices":[{"delta":{"content":"hi"}}]}'
            )

        api_call = await base_imodel.create_event(messages=[{"role": "user", "content": "Hello"}])
        with patch.object(base_imodel.endpoint, "stream", return_value=mock_stream()):
            [chunk async for chunk in base_imodel.stream(api_call=api_call)]

        assert api_call.metadata["served_model"] == "gpt-served-1"
        assert base_imodel.provider_metadata["served_model"] == "gpt-served-1"
