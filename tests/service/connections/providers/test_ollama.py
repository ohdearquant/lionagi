"""Tests for lionagi.providers.ollama.chat.endpoint module."""

import contextlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

# Create mock ollama module — module-level so test methods can reference it by name.
# NOT installed into sys.modules here; the fixture below owns that lifetime.
mock_ollama = MagicMock()
mock_ollama.__spec__ = MagicMock()  # Required for importlib.util.find_spec

from lionagi.service.connections.endpoint_config import EndpointConfig


@pytest.fixture(autouse=True, scope="module")
def _patch_ollama_module():
    """Install the ollama mock into sys.modules for the duration of this module.

    Captures any pre-existing real 'ollama' entry and restores it on teardown
    so the mock never leaks to other test modules collected in the same session.
    """
    _prior = sys.modules.get("ollama")
    sys.modules["ollama"] = mock_ollama
    yield mock_ollama
    if _prior is None:
        sys.modules.pop("ollama", None)
    else:
        sys.modules["ollama"] = _prior


def _get_ollama_config(
    name: str = "ollama_chat",
    base_url: str = "http://localhost:11434/v1",
    **overrides,
) -> EndpointConfig:
    """Create an Ollama chat endpoint config for testing.

    Replacement for the removed _get_ollama_config helper.
    """
    defaults = dict(
        name=name,
        provider="ollama",
        base_url=base_url,
        endpoint="chat/completions",
        api_key=None,
        auth_type="none",
        content_type="application/json",
        method="POST",
        openai_compatible=False,
    )
    defaults.update(overrides)
    return EndpointConfig(**defaults)


# Module-level config constant (was exported from the old module)
OLLAMA_CHAT_ENDPOINT_CONFIG = _get_ollama_config()


class TestOllamaEndpointConfiguration:
    """Test Ollama endpoint configuration and initialization."""

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_ollama_chat_endpoint_init_success(self):
        """Test successful OllamaChatEndpoint initialization."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        # Reset mock for this test
        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock()

        endpoint = OllamaChatEndpoint()

        assert endpoint is not None
        assert endpoint._pull is not None
        assert endpoint._list is not None

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", False)
    def test_ollama_chat_endpoint_init_missing_package(self):
        """Test that OllamaChatEndpoint raises error when ollama not installed."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        with pytest.raises(ModuleNotFoundError, match="ollama is not installed"):
            OllamaChatEndpoint()

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_ollama_chat_endpoint_removes_api_key(self):
        """Test that OllamaChatEndpoint removes api_key from kwargs."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock()

        # api_key should be removed
        endpoint = OllamaChatEndpoint(api_key="should_be_removed")

        # Should not raise error
        assert endpoint is not None

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_ollama_chat_endpoint_custom_config(self):
        """Test OllamaChatEndpoint with custom configuration."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock()

        custom_config = _get_ollama_config(base_url="http://custom:8080/v1")
        endpoint = OllamaChatEndpoint(config=custom_config)

        assert endpoint is not None
        assert endpoint.config.base_url == "http://custom:8080/v1"


class TestOllamaPayloadCreation:
    """Test Ollama payload creation and handling."""

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_create_payload_removes_reasoning_effort(self):
        """Test that create_payload removes reasoning_effort parameter."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock()

        endpoint = OllamaChatEndpoint()

        request = {
            "model": "llama2",
            "messages": [{"role": "user", "content": "test"}],
            "reasoning_effort": "high",  # Should be removed
        }

        payload, headers = endpoint.create_payload(request)

        assert "reasoning_effort" not in payload
        assert "model" in payload
        assert "messages" in payload

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_create_payload_with_basemodel(self):
        """Test create_payload with Pydantic BaseModel request."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock()

        class TestRequest(BaseModel):
            model: str
            messages: list
            reasoning_effort: str = "medium"

        endpoint = OllamaChatEndpoint()
        request = TestRequest(model="llama2", messages=[{"role": "user", "content": "test"}])

        payload, headers = endpoint.create_payload(request)

        assert "reasoning_effort" not in payload
        assert "model" in payload


class TestOllamaModelManagement:
    """Test Ollama model checking and pulling."""

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_check_model_already_available(self, caplog):
        """Test _check_model when model is already available locally."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        # Mock model list
        mock_model = MagicMock()
        mock_model.model = "llama2"
        mock_models_response = MagicMock()
        mock_models_response.models = [mock_model]

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock(return_value=mock_models_response)
        mock_ollama.pull = MagicMock()

        endpoint = OllamaChatEndpoint()
        with caplog.at_level("DEBUG", logger="lionagi.providers.ollama.chat.endpoint"):
            endpoint._check_model("llama2")

        # Verify output shows no pulling occurred
        assert "not found locally" not in caplog.text

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_check_model_not_available_pulls(self, caplog):
        """Test _check_model pulls model when not available locally."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        # Mock empty model list
        mock_models_response = MagicMock()
        mock_models_response.models = []

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock(return_value=mock_models_response)
        mock_ollama.pull = MagicMock(
            return_value=iter([{"status": "pulling manifest"}, {"status": "success"}])
        )

        endpoint = OllamaChatEndpoint()
        with caplog.at_level("DEBUG", logger="lionagi.providers.ollama.chat.endpoint"):
            endpoint._check_model("mistral")

        assert "not found locally" in caplog.text
        assert "successfully pulled" in caplog.text

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_check_model_handles_exception(self, caplog):
        """Test _check_model handles exceptions gracefully."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock(side_effect=ConnectionError("Connection failed"))
        mock_ollama.pull = MagicMock()

        endpoint = OllamaChatEndpoint()

        # Should not raise, but log warning
        with caplog.at_level("DEBUG", logger="lionagi.providers.ollama.chat.endpoint"):
            endpoint._check_model("llama2")

        assert "Connection failed" in caplog.text

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    @patch("tqdm.tqdm")
    def test_pull_model_with_progress(self, mock_tqdm):
        """Test _pull_model displays progress bars correctly."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        # Mock progress stream
        progress_data = [
            {"digest": "sha256:abc123", "total": 1000, "completed": 250},
            {"digest": "sha256:abc123", "total": 1000, "completed": 500},
            {"digest": "sha256:abc123", "total": 1000, "completed": 1000},
        ]

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock(return_value=iter(progress_data))

        mock_progress_bar = MagicMock()
        mock_progress_bar.n = 0
        mock_tqdm.return_value = mock_progress_bar

        endpoint = OllamaChatEndpoint()
        endpoint._pull_model("llama2")

        # Progress bar should be created and updated
        assert mock_tqdm.called
        assert mock_progress_bar.update.call_count == 3

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_pull_model_status_messages(self, caplog):
        """Test _pull_model logs status messages without digest."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        progress_data = [
            {"status": "pulling manifest"},
            {"status": "verifying sha256 digest"},
        ]

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock(return_value=iter(progress_data))

        endpoint = OllamaChatEndpoint()
        with caplog.at_level("DEBUG", logger="lionagi.providers.ollama.chat.endpoint"):
            endpoint._pull_model("llama2")

        assert "pulling manifest" in caplog.text
        assert "verifying" in caplog.text


class TestOllamaCall:
    """Test Ollama call method and integration."""

    @pytest.mark.asyncio
    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    async def test_call_checks_model_before_request(self):
        """Test that call() checks model availability before making request."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        # Mock available model
        mock_model = MagicMock()
        mock_model.model = "llama2"
        mock_models_response = MagicMock()
        mock_models_response.models = [mock_model]

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock(return_value=mock_models_response)
        mock_ollama.pull = MagicMock()

        endpoint = OllamaChatEndpoint()

        # Mock parent call method
        with patch.object(
            endpoint.__class__.__bases__[0], "call", new_callable=AsyncMock
        ) as mock_super_call:
            mock_super_call.return_value = {"response": "test"}

            request = {
                "model": "llama2",
                "messages": [{"role": "user", "content": "hello"}],
            }

            await endpoint.call(request)

            # Verify super().call() was invoked
            mock_super_call.assert_called_once()

    @pytest.mark.asyncio
    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    async def test_call_pulls_missing_model(self, caplog):
        """Test that call() pulls model if not available."""
        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        # Mock empty model list initially
        mock_models_response = MagicMock()
        mock_models_response.models = []

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock(return_value=mock_models_response)
        mock_ollama.pull = MagicMock(
            return_value=iter([{"status": "pulling"}, {"status": "success"}])
        )

        endpoint = OllamaChatEndpoint()

        # Mock parent call method
        with patch.object(
            endpoint.__class__.__bases__[0], "call", new_callable=AsyncMock
        ) as mock_super_call:
            mock_super_call.return_value = {"response": "test"}

            request = {
                "model": "mistral",
                "messages": [{"role": "user", "content": "hello"}],
            }

            with caplog.at_level("DEBUG", logger="lionagi.providers.ollama.chat.endpoint"):
                await endpoint.call(request)

            assert "not found locally" in caplog.text


class TestOllamaConfig:
    """Test Ollama configuration generation."""

    def test_get_ollama_config_defaults(self):
        """Test _get_ollama_config returns correct defaults."""
        # _get_ollama_config is defined at the module level in this test file

        config = _get_ollama_config()

        assert config.name == "ollama_chat"
        assert config.provider == "ollama"
        assert config.base_url == "http://localhost:11434/v1"
        assert config.endpoint == "chat/completions"
        assert config.api_key is None
        assert config.auth_type == "none"
        assert config.openai_compatible is False

    def test_get_ollama_config_custom_overrides(self):
        """Test _get_ollama_config with custom parameters."""
        # _get_ollama_config is defined at the module level in this test file

        config = _get_ollama_config(base_url="http://custom-host:9999/v1", name="custom_ollama")

        assert config.base_url == "http://custom-host:9999/v1"
        assert config.name == "custom_ollama"
        # Other defaults should still apply
        assert config.auth_type == "none"

    def test_ollama_chat_endpoint_config_module_level(self):
        """Test that OLLAMA_CHAT_ENDPOINT_CONFIG is properly initialized."""
        # OLLAMA_CHAT_ENDPOINT_CONFIG is defined at the module level in this test file

        assert OLLAMA_CHAT_ENDPOINT_CONFIG is not None
        assert OLLAMA_CHAT_ENDPOINT_CONFIG.provider == "ollama"


class TestOllamaPublicSurface:
    """AUDIT-002 regression: __all__ must not export undefined names (F822).

    The stale ``OLLAMA_CHAT_ENDPOINT_CONFIG`` entry was removed from ``__all__``
    because no such symbol is defined in the module.  A star-import must succeed
    and the resulting namespace must only contain names that actually exist.
    """

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_star_import_succeeds(self):
        """``from lionagi.providers.ollama.chat.endpoint import *`` must not raise."""
        import importlib

        mod = importlib.import_module("lionagi.providers.ollama.chat.endpoint")
        namespace = {}
        # Simulate star-import by executing each exported name.
        for name in mod.__all__:
            assert hasattr(mod, name), (
                f"__all__ exports {name!r} but module has no such attribute — "
                "remove the stale name or define the symbol (AUDIT-002 / F822)"
            )
            namespace[name] = getattr(mod, name)

    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    def test_all_does_not_contain_ollama_chat_endpoint_config(self):
        """Stale OLLAMA_CHAT_ENDPOINT_CONFIG must be absent from __all__."""
        import importlib

        mod = importlib.import_module("lionagi.providers.ollama.chat.endpoint")
        assert "OLLAMA_CHAT_ENDPOINT_CONFIG" not in mod.__all__, (
            "OLLAMA_CHAT_ENDPOINT_CONFIG is in __all__ but not defined in the module; "
            "remove it from __all__ to fix AUDIT-002 / F822"
        )


class TestOllamaAsyncCheckModel:
    """AUDIT-003 regression: _check_model must not block the event loop.

    The fix wraps the synchronous Ollama SDK calls in ``run_sync`` so they run
    in a thread pool.  We verify that calling ``call()`` does NOT block by
    confirming a concurrent ticker task keeps ticking while the (mocked) slow
    model-check executes.
    """

    @pytest.mark.asyncio
    @patch("lionagi.providers.ollama.chat.endpoint._HAS_OLLAMA", True)
    async def test_call_check_model_runs_in_thread_not_blocking(self):
        """A slow _check_model must not block concurrent async tasks (AUDIT-003).

        We measure how many ticks a short-interval counter task completes while
        ``call()`` is waiting on a "slow" (sleep-based) _check_model.  If the
        event loop is blocked, the ticker gets zero ticks; if the fix is correct
        it gets at least a few.
        """
        import asyncio

        from lionagi.providers.ollama.chat.endpoint import OllamaChatEndpoint

        mock_ollama.reset_mock()
        mock_ollama.list = MagicMock()
        mock_ollama.pull = MagicMock()

        endpoint = OllamaChatEndpoint()

        tick_count = 0

        async def ticker():
            nonlocal tick_count
            while True:
                await asyncio.sleep(0.01)
                tick_count += 1

        def slow_check(model: str) -> None:
            """Simulates a slow sync check — sleeps in the current thread."""
            import time

            time.sleep(0.1)

        with (
            patch.object(endpoint, "_check_model", side_effect=slow_check),
            patch.object(
                endpoint.__class__.__bases__[0],
                "call",
                new_callable=AsyncMock,
                return_value={"response": "ok"},
            ),
        ):
            ticker_task = asyncio.create_task(ticker())
            try:
                await endpoint.call({"model": "llama2", "messages": []})
            finally:
                ticker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker_task

        # If the event loop was blocked, tick_count would be 0.
        # With run_sync the event loop stays alive, so we get ≥ 2 ticks during
        # the 100 ms wait (10 ms interval).
        assert tick_count >= 2, (
            f"tick_count={tick_count}: event loop appears blocked during _check_model "
            "— ensure _check_model is wrapped in run_sync (AUDIT-003)"
        )
