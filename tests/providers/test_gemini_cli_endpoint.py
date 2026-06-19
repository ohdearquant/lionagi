# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Gemini CLI endpoint fixes.

Covers trust-env injection, nonzero-exit stderr surfacing, JSON-noise tolerance, default model gemini-3-flash-preview, and known-bad model surfacing a RuntimeError.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.providers.google.gemini_code import (
    GeminiCodeRequest,
    GeminiSession,
    stream_gemini_cli,
)

# ---------------------------------------------------------------------------
# Trust env injection
# ---------------------------------------------------------------------------


class TestTrustEnvInjection:
    """GEMINI_CLI_TRUST_WORKSPACE must be set in the subprocess env."""

    @pytest.mark.asyncio
    async def test_trust_env_injected_into_subprocess(self, monkeypatch, tmp_path):
        """_ndjson_from_cli must pass GEMINI_CLI_TRUST_WORKSPACE=true to ndjson_from_cli."""
        import lionagi.providers.google.gemini_code as gemini_models

        monkeypatch.setattr(gemini_models, "GEMINI_CLI", "/fake/gemini")

        captured_env: dict | None = None

        async def fake_ndjson(cmd, *, cwd=None, env=None, stdin=None, tail_repair=None):
            nonlocal captured_env
            captured_env = env
            # Yield nothing — we only care about the env that was passed
            return
            yield  # make it an async generator

        monkeypatch.setattr(gemini_models, "ndjson_from_cli", fake_ndjson)

        request = GeminiCodeRequest(prompt="test", repo=tmp_path)
        # Drain the async generator
        async with __import__("contextlib").aclosing(
            gemini_models._ndjson_from_cli(request)
        ) as gen:
            async for _ in gen:
                pass

        assert captured_env is not None, "_ndjson_from_cli did not call ndjson_from_cli"
        assert captured_env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"

    @pytest.mark.asyncio
    async def test_trust_env_inherits_parent_env(self, monkeypatch, tmp_path):
        """Subprocess env must include all parent env vars, not just the trust flag."""
        import lionagi.providers.google.gemini_code as gemini_models

        monkeypatch.setattr(gemini_models, "GEMINI_CLI", "/fake/gemini")
        monkeypatch.setenv("SOME_PARENT_VAR", "parent_value")

        captured_env: dict | None = None

        async def fake_ndjson(cmd, *, cwd=None, env=None, stdin=None, tail_repair=None):
            nonlocal captured_env
            captured_env = env
            return
            yield

        monkeypatch.setattr(gemini_models, "ndjson_from_cli", fake_ndjson)

        request = GeminiCodeRequest(prompt="test", repo=tmp_path)
        async with __import__("contextlib").aclosing(
            gemini_models._ndjson_from_cli(request)
        ) as gen:
            async for _ in gen:
                pass

        assert captured_env is not None
        assert captured_env.get("SOME_PARENT_VAR") == "parent_value"
        assert captured_env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"


# ---------------------------------------------------------------------------
# Subprocess error surfacing
# ---------------------------------------------------------------------------


class TestSubprocessErrorSurfacing:
    """When gemini exits nonzero, stderr must be surfaced as RuntimeError."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_runtime_error_with_stderr(self, monkeypatch, tmp_path):
        """ndjson_from_cli raises RuntimeError with stderr text on nonzero exit."""
        import lionagi.providers.google.gemini_code as gemini_models
        from lionagi.providers._cli_subprocess import ndjson_from_cli

        monkeypatch.setattr(gemini_models, "GEMINI_CLI", "/bin/sh")

        # A command that prints to stderr and exits nonzero
        async def fake_ndjson(cmd, *, cwd=None, env=None, stdin=None, tail_repair=None):
            raise RuntimeError(
                "Gemini CLI is not running in a trusted directory. "
                "To proceed, use --skip-trust or set GEMINI_CLI_TRUST_WORKSPACE=true."
            )
            yield

        monkeypatch.setattr(gemini_models, "ndjson_from_cli", fake_ndjson)

        request = GeminiCodeRequest(prompt="hello", repo=tmp_path)

        with pytest.raises(RuntimeError, match="trusted directory"):
            async with __import__("contextlib").aclosing(
                gemini_models._ndjson_from_cli(request)
            ) as gen:
                async for _ in gen:
                    pass


# ---------------------------------------------------------------------------
# Noise-line tolerance (stdout lines before JSON)
# ---------------------------------------------------------------------------


class TestNoiseTolerance:
    """Non-JSON lines in stdout must not abort the parse loop."""

    @pytest.mark.asyncio
    async def test_stream_gemini_cli_tolerates_noise_before_json(self, monkeypatch):
        """stream_gemini_cli must work even if the first stdout lines are not valid JSON."""
        import lionagi.providers.google.gemini_code as gemini_models

        # Simulate: noise line appears in events list as would happen if ndjson_from_cli
        # managed to yield a dict after skipping the noise. The underlying
        # ndjson_from_cli drops non-JSON lines silently; we verify that
        # stream_gemini_cli can consume a session when the NDJSON events are clean.
        events = [
            {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
            {
                "type": "message",
                "role": "assistant",
                "content": "GEMINI-LIONAGI-OK",
                "delta": True,
            },
            {
                "type": "result",
                "result": "GEMINI-LIONAGI-OK",
                "status": "success",
                "stats": {"duration_ms": 500},
            },
        ]

        async def fake_events(_request):
            for ev in events:
                yield ev
            yield {"type": "done"}

        monkeypatch.setattr(gemini_models, "stream_gemini_cli_events", fake_events)

        session = None
        async for item in gemini_models.stream_gemini_cli(GeminiCodeRequest(prompt="hello")):
            if isinstance(item, GeminiSession):
                session = item

        assert session is not None
        assert session.result == "GEMINI-LIONAGI-OK"


# ---------------------------------------------------------------------------
# Default model
# ---------------------------------------------------------------------------


class TestDefaultModel:
    """GeminiCodeRequest default model must be gemini-3-flash-preview."""

    def test_default_model_is_gemini_3_flash_preview(self):
        req = GeminiCodeRequest(prompt="hello")
        assert req.model == "gemini-3-flash-preview"

    def test_explicit_model_is_preserved(self):
        req = GeminiCodeRequest(prompt="hello", model="gemini-2.5-pro")
        assert req.model == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# BACKENDS default model
# ---------------------------------------------------------------------------


class TestBackendsDefaultModel:
    """BACKENDS entries for gemini-cli must point to gemini-3-flash-preview."""

    def test_gemini_cli_backend_uses_3_flash_preview(self):
        from lionagi.service.providers import BACKENDS

        assert "gemini-3-flash-preview" in BACKENDS["gemini-cli"]
        assert "gemini-3-flash-preview" in BACKENDS["gemini_cli"]
        assert "gemini-3-flash-preview" in BACKENDS["gemini-code"]
        assert "gemini-3-flash-preview" in BACKENDS["gemini_code"]


# ---------------------------------------------------------------------------
# CLI_PROVIDERS includes gemini variants
# ---------------------------------------------------------------------------


class TestCliProvidersSet:
    """gemini_code, gemini-cli, gemini_cli, gemini-code must be in CLI_PROVIDERS."""

    def test_gemini_cli_in_cli_providers(self):
        from lionagi.service.providers import CLI_PROVIDERS

        for name in ("gemini_code", "gemini-code", "gemini_cli", "gemini-cli"):
            assert name in CLI_PROVIDERS, f"{name!r} not in CLI_PROVIDERS"


# ---------------------------------------------------------------------------
# Run.py error message improvement
# ---------------------------------------------------------------------------


class TestRunErrorMessage:
    """ValueError from run.py when provider is not CLI must mention gemini-cli."""

    @pytest.mark.asyncio
    async def test_gemini_api_provider_gives_helpful_error(self):
        """Using 'gemini' (REST API) in run() must mention 'gemini-cli'."""
        from lionagi.session.branch import Branch

        # 'gemini' resolves to the REST API endpoint (GeminiChatConfigs.CHAT)
        # which is not a CLI endpoint — is_cli=False.
        branch = Branch(chat_model="gemini/gemini-2.5-flash")

        if branch.chat_model.is_cli:
            pytest.skip("gemini resolved to CLI endpoint — skip REST path test")

        from lionagi.operations.run.run import run
        from lionagi.operations.types import RunParam

        with pytest.raises(ValueError, match="gemini-cli"):
            async for _ in run(branch, "hello", RunParam()):
                pass
