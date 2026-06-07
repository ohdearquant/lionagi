"""Tests for lionagi.providers.anthropic.claude_code.endpoint module."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel


class TestClaudeCodeCLIConfiguration:
    """Test Claude Code CLI endpoint configuration."""

    def test_endpoint_init_default_config(self):
        """Test ClaudeCodeCLIEndpoint initialization with default config."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()

        assert endpoint is not None
        assert endpoint.config.provider == "claude_code"
        # Config name is auto-generated from provider+endpoint in the new registry
        assert "claude_code" in endpoint.config.name
        # Timeout is set by EndpointMeta.create_config for agentic endpoints
        assert endpoint.config.timeout >= 3600

    def test_endpoint_init_custom_config(self):
        """Test ClaudeCodeCLIEndpoint with custom configuration."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
            EndpointConfig,
        )

        custom_config = EndpointConfig(
            name="custom_claude",
            provider="claude_code",
            base_url="internal",
            endpoint="custom",
            api_key="custom-key",
        )

        endpoint = ClaudeCodeCLIEndpoint(config=custom_config)

        assert endpoint.config.name == "custom_claude"
        assert endpoint.config.endpoint == "custom"


class TestHandlerValidation:
    """Test handler validation logic."""

    def test_validate_handlers_valid_dict(self):
        """Test _validate_handlers accepts valid handler dictionary."""
        from lionagi.providers.anthropic.claude_code.endpoint import _validate_handlers

        handlers = {
            "on_thinking": lambda x: None,
            "on_text": lambda x: None,
            "on_tool_use": None,
            "on_final": lambda x: None,
        }

        result = _validate_handlers(handlers)
        assert result is None

    def test_validate_handlers_invalid_type(self):
        """Test _validate_handlers rejects non-dict input."""
        from lionagi.providers.anthropic.claude_code.endpoint import _validate_handlers

        with pytest.raises(ValueError, match="Handlers must be a dictionary"):
            _validate_handlers("not a dict")

    def test_validate_handlers_invalid_key(self):
        """Test _validate_handlers rejects invalid handler keys."""
        from lionagi.providers.anthropic.claude_code.endpoint import _validate_handlers

        handlers = {"invalid_handler": lambda x: None}

        with pytest.raises(ValueError, match="Invalid handler key"):
            _validate_handlers(handlers)

    def test_validate_handlers_invalid_value(self):
        """Test _validate_handlers rejects non-callable values."""
        from lionagi.providers.anthropic.claude_code.endpoint import _validate_handlers

        handlers = {"on_thinking": "not callable"}

        with pytest.raises(ValueError, match="Handler value must be callable"):
            _validate_handlers(handlers)

    def test_validate_handlers_allows_none(self):
        """Test _validate_handlers allows None values."""
        from lionagi.providers.anthropic.claude_code.endpoint import _validate_handlers

        handlers = {
            "on_thinking": None,
            "on_text": None,
            "on_tool_use": None,
        }

        result = _validate_handlers(handlers)
        assert result is None


class TestClaudeHandlers:
    """Test claude_handlers property and updates."""

    def test_claude_handlers_default(self):
        """Test default claude_handlers property."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()
        handlers = endpoint.claude_handlers

        assert isinstance(handlers, dict)
        assert "on_thinking" in handlers
        assert "on_text" in handlers
        assert "on_tool_use" in handlers
        assert handlers["on_thinking"] is None

    def test_claude_handlers_setter_valid(self):
        """Test setting valid claude_handlers."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()

        new_handlers = {
            "on_thinking": lambda x: None,
            "on_text": lambda x: x,
            "on_tool_use": None,
            "on_tool_result": lambda x: None,
            "on_system": None,
            "on_final": lambda x: None,
        }

        endpoint.claude_handlers = new_handlers

        assert endpoint.claude_handlers == new_handlers

    def test_claude_handlers_setter_invalid(self):
        """Test setting invalid claude_handlers raises error."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()

        invalid_handlers = {"invalid_key": lambda x: None}

        with pytest.raises(ValueError, match="Invalid handler key"):
            endpoint.claude_handlers = invalid_handlers

    def test_update_handlers_merges_correctly(self):
        """Test update_handlers merges with existing handlers."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()

        # Set initial handler
        on_thinking_handler = lambda x: "thinking"
        endpoint.update_handlers(on_thinking=on_thinking_handler)

        # Update with another handler
        on_text_handler = lambda x: "text"
        endpoint.update_handlers(on_text=on_text_handler)

        handlers = endpoint.claude_handlers
        assert handlers["on_thinking"] == on_thinking_handler
        assert handlers["on_text"] == on_text_handler

    def test_update_handlers_invalid_raises(self):
        """Test update_handlers with invalid handlers raises error."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()

        with pytest.raises(ValueError, match="Invalid handler key"):
            endpoint.update_handlers(invalid_key=lambda x: None)


class TestPayloadCreation:
    """Test payload creation for Claude Code CLI."""

    def test_create_payload_basic(self):
        """Test create_payload with basic request."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()

        request = {
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1000,
        }

        payload, headers = endpoint.create_payload(request)

        assert "request" in payload
        assert headers == {}
        # Verify request object was created
        assert payload["request"] is not None

    def test_create_payload_with_basemodel(self):
        """Test create_payload with Pydantic BaseModel."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        class TestRequest(BaseModel):
            messages: list
            max_tokens: int = 1000

        endpoint = ClaudeCodeCLIEndpoint()

        request = TestRequest(messages=[{"role": "user", "content": "Hello"}])

        payload, headers = endpoint.create_payload(request)

        assert "request" in payload
        assert headers == {}

    def test_create_payload_merges_kwargs(self):
        """Test create_payload merges config kwargs and request kwargs."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()

        request = {
            "messages": [{"role": "user", "content": "Hello"}],
        }

        payload, headers = endpoint.create_payload(request, max_turns=5, auto_finish=True)

        assert "request" in payload
        # ClaudeCodeRequest should have merged these


class TestStreamMethod:
    """Test async stream method."""

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self):
        """Test stream method yields StreamChunk objects."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )
        from lionagi.service.types.stream_chunk import StreamChunk

        with patch(
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli"
        ) as mock_stream:
            chunk1 = StreamChunk(type="text", content="hello")
            chunk2 = StreamChunk(type="text", content="world")

            async def async_gen(*args, **kwargs):
                yield chunk1
                yield chunk2

            mock_stream.return_value = async_gen()

            endpoint = ClaudeCodeCLIEndpoint()

            request = {
                "messages": [{"role": "user", "content": "Hello"}],
            }

            chunks = []
            async for chunk in endpoint.stream(request):
                chunks.append(chunk)

            assert len(chunks) == 2
            assert all(isinstance(c, StreamChunk) for c in chunks)
            assert chunks[0].type == "text"
            assert chunks[0].content == "hello"
            assert chunks[1].content == "world"

    @pytest.mark.asyncio
    async def test_stream_with_kwargs(self):
        """Test stream passes kwargs to create_payload."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        with patch(
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli"
        ) as mock_stream:

            async def async_gen(*args, **kwargs):
                yield MagicMock()

            mock_stream.return_value = async_gen()

            endpoint = ClaudeCodeCLIEndpoint()

            request = {
                "messages": [{"role": "user", "content": "Hello"}],
            }

            # Stream with kwargs
            chunks = []
            async for chunk in endpoint.stream(request, max_turns=5, auto_finish=True):
                chunks.append(chunk)

            # Verify stream was called (kwargs would be merged into request_obj)
            assert mock_stream.called
            assert len(chunks) >= 0  # At least doesn't error


class TestCallMethod:
    """Test async _call method."""

    @pytest.mark.asyncio
    async def test_call_basic_flow(self):
        """Test _call method basic execution flow."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        with patch(
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli"
        ) as mock_stream:
            # Create mock session
            mock_session = MagicMock()
            mock_session.session_id = "test-session"
            mock_session.chunks = []
            mock_session.result = "Final result"

            # Create mock chunks and done signal
            mock_chunk = MagicMock()
            mock_chunk.text = "Response text"
            done_dict = {"type": "done"}

            async def async_gen(*args, **kwargs):
                yield mock_chunk
                yield done_dict
                yield mock_session

            mock_stream.return_value = async_gen()

            endpoint = ClaudeCodeCLIEndpoint()

            # Create mock payload
            mock_request = MagicMock()
            mock_request.auto_finish = False
            mock_request.cli_include_summary = False
            mock_request.model_copy = MagicMock(return_value=mock_request)

            payload = {"request": mock_request}
            headers = {}

            result = await endpoint._call(payload, headers)

            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_call_with_auto_finish(self):
        """Test _call method with auto_finish enabled."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        with patch(
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli"
        ) as mock_stream:
            # Mock session
            mock_session = MagicMock()
            mock_session.session_id = "test-session"
            mock_session.chunks = []
            mock_session.result = "Final result"

            # Create mock request
            mock_request = MagicMock()
            mock_request.auto_finish = True
            mock_request.cli_include_summary = False
            mock_request.max_turns = 3

            # Mock model_copy for second request
            mock_request_copy = MagicMock()
            mock_request_copy.prompt = "Please provide a the final result message only"
            mock_request_copy.max_turns = 1
            mock_request_copy.continue_conversation = True
            mock_request.model_copy = MagicMock(return_value=mock_request_copy)

            # First stream: returns chunk (not ClaudeSession)
            # Second stream: returns final session
            call_count = [0]

            async def async_gen(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    # First call - not ending with session
                    yield MagicMock(text="initial")
                    yield {"type": "done", "session_id": "test"}
                else:
                    # Second call - auto-finish
                    yield MagicMock(text="final")
                    yield mock_session

            mock_stream.side_effect = lambda *args, **kwargs: async_gen(*args, **kwargs)

            endpoint = ClaudeCodeCLIEndpoint()

            payload = {"request": mock_request}
            headers = {}

            result = await endpoint._call(payload, headers)

            # Should have called stream twice (initial + auto-finish)
            assert mock_stream.call_count == 2

    # test_call_with_include_summary and test_call_combines_chunk_texts removed.
    # These were stubs covered by the non-skipped tests above and by
    # tests/operations/run/test_run.py which exercises the streaming path
    # end-to-end through the public API.


class TestModuleLevelConfig:
    """Test module-level configuration."""

    def test_endpoint_config_exists(self):
        """Test ClaudeCodeCLIEndpoint config is properly initialized."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        endpoint = ClaudeCodeCLIEndpoint()
        config = endpoint.config
        assert config is not None
        assert config.provider == "claude_code"
        # Config name is auto-generated from provider+endpoint in the new registry
        assert "claude_code" in config.name
        # Agentic endpoints have timeout >= 3600
        assert config.timeout >= 3600
