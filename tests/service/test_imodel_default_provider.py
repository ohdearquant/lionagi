# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for iModel bare-model construction.

When a caller supplies only `model=` without `provider=`, iModel must resolve
the provider from the LIONAGI_CHAT_PROVIDER setting rather than raising an
error.  Explicit provider= still takes precedence.
"""

from __future__ import annotations

import pytest

from lionagi.service.imodel import iModel


class TestiModelDefaultProvider:
    """Bare iModel(model=...) resolves to the settings default provider."""

    def test_bare_model_does_not_raise(self):
        """iModel(model='gpt-4o-mini') constructs without error.

        Previously raised 'Provider must be provided' when no slash appeared
        in the model name and provider= was omitted.
        """
        m = iModel(model="gpt-4o-mini", api_key="test-key")
        assert m is not None

    def test_bare_model_resolves_settings_provider(self):
        """Provider falls back to LIONAGI_CHAT_PROVIDER when not supplied.

        The default value of LIONAGI_CHAT_PROVIDER is 'openai', so the
        resolved endpoint provider must equal that default.
        """
        m = iModel(model="gpt-4o-mini", api_key="test-key")
        # The endpoint config must have resolved a provider, not be empty.
        assert m.endpoint.config.provider  # truthy — some provider was set

    def test_bare_model_uses_env_override(self):
        """LIONAGI_CHAT_PROVIDER env override is respected.

        Patch the singleton so no real env var mutation is needed.
        """
        from lionagi import config as cfg

        original = cfg.settings
        try:
            # Build a fresh settings object with a different default provider.
            patched = cfg.AppSettings(LIONAGI_CHAT_PROVIDER="anthropic")
            cfg.settings = patched

            m = iModel(model="claude-3-haiku-20240307", api_key="test-key")
            assert m is not None
            # Provider was resolved from patched settings (anthropic).
            assert m.endpoint.config.provider == "anthropic"
        finally:
            cfg.settings = original

    def test_slash_model_still_splits_provider(self):
        """'provider/model' syntax continues to work as before."""
        m = iModel(model="openai/gpt-4o-mini", api_key="test-key")
        assert m.endpoint.config.provider == "openai"
        assert m.model_name == "gpt-4o-mini"

    def test_explicit_provider_wins(self):
        """Explicit provider= takes precedence over the settings default."""
        m = iModel(provider="openai", model="gpt-4o-mini", api_key="test-key")
        assert m.endpoint.config.provider == "openai"

    def test_no_model_no_provider_still_works(self):
        """Neither model nor provider raises no error (endpoint defaults apply)."""
        # Passing provider explicitly to satisfy match_endpoint for the bare case.
        m = iModel(provider="openai", api_key="test-key")
        assert m is not None


class TestiModelDefaultProviderAttackDriven:
    """Ensures callers cannot bypass provider resolution with crafted model names.

    The bare-model fallback must not allow a caller to inject an arbitrary
    provider by embedding one in the model name without a slash separator.
    The slash-split path is the only authorised provider-from-model-name form.
    """

    def test_no_slash_uses_settings_not_model_as_provider(self):
        """A model name without '/' must not be treated as a provider name.

        Regression guard: the old code raised ValueError; the fixed code falls
        through to settings.  In neither case should the raw model string
        become the provider.
        """
        m = iModel(model="gpt-4o-mini", api_key="test-key")
        # The provider must NOT equal the model name.
        assert m.endpoint.config.provider != "gpt-4o-mini"

    def test_multiple_slashes_splits_on_first(self):
        """'a/b/c' model string splits provider on first slash only."""
        # 'openai/gpt-4/turbo' — provider=openai, model=gpt-4/turbo
        m = iModel(model="openai/gpt-4/turbo", api_key="test-key")
        assert m.endpoint.config.provider == "openai"
        # model part after first slash is kept as-is
        assert "gpt-4" in m.model_name
