# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.service.token_budget — TokenBudget dataclass and helpers."""

import types

import pytest

from lionagi.service.token_budget import TokenBudget, get_context_window


class TestTokenBudget:
    def test_remaining_clamps_at_zero_when_over_limit(self):
        budget = TokenBudget(used=150, limit=100, model="m")
        assert budget.remaining == 0

    def test_remaining_positive_when_under_limit(self):
        budget = TokenBudget(used=40, limit=100)
        assert budget.remaining == 60

    def test_usage_pct_over_limit(self):
        budget = TokenBudget(used=150, limit=100, model="m")
        assert budget.usage_pct == 1.5

    def test_is_critical_when_over_limit(self):
        budget = TokenBudget(used=150, limit=100, model="m")
        assert budget.is_critical is True

    def test_is_warning_at_70_pct(self):
        """The inclusive 70% warning boundary remains below the critical boundary."""
        budget = TokenBudget(used=70, limit=100)
        assert budget.is_warning is True
        assert budget.is_critical is False

    def test_is_not_warning_below_70_pct(self):
        budget = TokenBudget(used=69, limit=100)
        assert budget.is_warning is False

    def test_zero_limit_does_not_raise(self):
        budget = TokenBudget(used=0, limit=0)
        assert budget.usage_pct == 0.0
        assert budget.remaining == 0


class TestGetContextWindow:
    def test_falls_back_to_default_when_endpoint_access_raises(self):
        class BadConfig:
            @property
            def context_window(self):
                raise AttributeError("no context_window")

        class BadEndpoint:
            config = BadConfig()

        class FakeBranch:
            class chat_model:
                endpoint = BadEndpoint()

        result = get_context_window(FakeBranch())
        assert result == 128_000

    def test_falls_back_to_default_when_chat_model_attribute_missing(self):
        class FakeBranch:
            class chat_model:
                @property
                def endpoint(self):
                    raise AttributeError("no endpoint")

        result = get_context_window(FakeBranch())
        assert result == 128_000

    def test_respects_explicit_context_window_in_config(self):
        class FakeBranch:
            class chat_model:
                endpoint = types.SimpleNamespace(
                    config=types.SimpleNamespace(
                        context_window=32_000,
                        kwargs={},
                        provider="openai",
                    )
                )

        result = get_context_window(FakeBranch())
        assert result == 32_000


class TestTokenBudgetBoundaries:
    """Boundary conditions for is_warning and is_critical."""

    def test_is_critical_exactly_at_90_pct(self):
        budget = TokenBudget(used=90, limit=100)
        assert budget.is_critical is True

    def test_is_critical_false_at_89_pct(self):
        budget = TokenBudget(used=89, limit=100)
        assert budget.is_critical is False

    def test_remaining_exactly_at_limit(self):
        budget = TokenBudget(used=100, limit=100)
        assert budget.remaining == 0

    def test_remaining_one_below_limit(self):
        budget = TokenBudget(used=99, limit=100)
        assert budget.remaining == 1

    def test_model_field_is_none_by_default(self):
        budget = TokenBudget(used=0, limit=100)
        assert budget.model is None

    def test_model_field_stored(self):
        budget = TokenBudget(used=0, limit=100, model="gpt-4")
        assert budget.model == "gpt-4"


import types as _types

from lionagi.service.token_budget import get_token_budget, lookup_context_window


class TestLookupContextWindow:
    def test_known_openai_model_returns_nondefault(self):
        """gpt-4 is in the openai CONTEXT_WINDOWS dict."""
        result = lookup_context_window("gpt-4", provider="openai")
        # Any positive non-default value is acceptable; must be > 0
        assert result > 0

    @pytest.mark.parametrize(
        ("provider", "model", "expected"),
        [
            ("anthropic", "claude-fable-5-1", 1_000_000),
            ("anthropic", "claude-opus-5-5", 1_000_000),
            ("anthropic", "claude-sonnet-5-5", 1_000_000),
            ("anthropic", "claude-haiku-5-5", 1_000_000),
            ("claude_code", "claude-fable-5-1", 1_000_000),
            ("claude_code", "claude-opus-5-5", 1_000_000),
            ("claude_code", "claude-sonnet-5-5", 1_000_000),
            ("claude_code", "claude-haiku-5-5", 1_000_000),
            ("openai", "gpt-6.1-sol", 1_050_000),
            ("openai", "gpt-6.1-terra", 1_050_000),
            ("openai", "gpt-6-astra", 1_050_000),
            ("openai", "gpt-6-sol", 1_050_000),
            ("openai", "gpt-6-luna", 1_050_000),
            ("codex", "gpt-6.1-sol", 1_050_000),
            ("codex", "gpt-6.1-terra", 1_050_000),
            ("codex", "gpt-6-astra", 1_050_000),
            ("codex", "gpt-6-sol", 1_050_000),
            ("codex", "gpt-6-luna", 1_050_000),
            ("gemini", "gemini-3.8-flash", 1_048_576),
            ("gemini_code", "gemini-3.8-flash", 1_048_576),
            ("deepseek", "deepseek-v4-flash", 1_000_000),
            ("openrouter", "google/gemini-3.8-flash", 1_048_576),
            ("openrouter", "anthropic/claude-fable-5.1", 1_000_000),
            ("openrouter", "anthropic/claude-opus-5.5", 1_000_000),
            ("openrouter", "anthropic/claude-sonnet-5.5", 1_000_000),
            ("openrouter", "anthropic/claude-haiku-5.5", 1_000_000),
            ("openrouter", "openai/gpt-6.1-sol", 1_050_000),
            ("openrouter", "openai/gpt-6.1-terra", 1_050_000),
            ("openrouter", "openai/gpt-6-astra", 1_050_000),
            ("openrouter", "openai/gpt-6-sol", 1_050_000),
            ("openrouter", "openai/gpt-6-luna", 1_050_000),
        ],
    )
    def test_current_model_context_windows(self, provider, model, expected):
        from lionagi.service.token_budget import lookup_context_window

        assert lookup_context_window(model, provider=provider) == expected

    def test_unknown_model_and_provider_returns_default(self):
        """Completely unknown model + provider returns 128_000."""
        result = lookup_context_window("totally-made-up-model-xyz", provider="xyz_provider")
        assert result == 128_000

    def test_unknown_model_no_provider_returns_default(self):
        """Unknown model without provider hint falls through to default."""
        result = lookup_context_window("nonexistent-model-zzz")
        assert result == 128_000

    def test_provider_specific_lookup_before_all_providers(self):
        """Provider hint is tried first; result same or consistent as without hint."""
        with_hint = lookup_context_window("gpt-4", provider="openai")
        without_hint = lookup_context_window("gpt-4")
        # Both resolve to same value
        assert with_hint == without_hint


class TestGetContextWindowModelLookup:
    def test_resolves_model_from_kwargs(self):
        """get_context_window reads model from config.kwargs (line 148, 153)."""

        class FakeBranch:
            class chat_model:
                endpoint = _types.SimpleNamespace(
                    config=_types.SimpleNamespace(
                        context_window=None,
                        kwargs={"model": "gpt-4"},
                        provider="openai",
                    )
                )

        result = get_context_window(FakeBranch())
        assert result > 0

    def test_resolves_model_from_params_fallback(self):
        """get_context_window falls back to config.params when kwargs model is empty (line 150)."""

        class FakeBranch:
            class chat_model:
                endpoint = _types.SimpleNamespace(
                    config=_types.SimpleNamespace(
                        context_window=None,
                        kwargs={"model": ""},
                        params={"model": "gpt-4"},
                        provider="openai",
                    )
                )

        result = get_context_window(FakeBranch())
        assert result > 0

    def test_unknown_model_in_kwargs_returns_default(self):
        """get_context_window returns default when model name is not in any provider dict."""

        class FakeBranch:
            class chat_model:
                endpoint = _types.SimpleNamespace(
                    config=_types.SimpleNamespace(
                        context_window=None,
                        kwargs={"model": "completely-made-up-model-xyz"},
                        provider=None,
                    )
                )

        result = get_context_window(FakeBranch())
        assert result == 128_000


class TestGetTokenBudget:
    def test_returns_token_budget_instance(self):
        """get_token_budget returns TokenBudget with non-negative used and positive limit."""
        from lionagi.session.branch import Branch

        branch = Branch()
        budget = get_token_budget(branch)
        assert isinstance(budget, TokenBudget)
        assert budget.used >= 0
        assert budget.limit > 0

    def test_used_increases_after_message_added(self):
        """get_token_budget counts tokens for messages in the progression."""
        from lionagi.session.branch import Branch

        branch = Branch()
        before = get_token_budget(branch)
        branch.msgs.add_message(system="You are a helpful assistant with many things to say.")
        after = get_token_budget(branch)
        assert after.used >= before.used

    def test_model_field_from_chat_model_endpoint(self):
        """get_token_budget extracts model name from branch.chat_model.endpoint.config.kwargs."""
        from lionagi.session.branch import Branch

        branch = Branch()
        budget = get_token_budget(branch)
        # model may be None or a string — just check the type
        assert budget.model is None or isinstance(budget.model, str)


class TestCanonicalContextWindowRegistry:
    """Pin representative model→context-length lookups through the canonical registry.

    These values must stay consistent as provider registries evolve.
    Changing a value here requires a deliberate update with justification.
    """

    @pytest.mark.parametrize(
        ("provider", "model", "expected"),
        [
            (None, "gemini-2.5-pro", 1_048_576),
            ("gemini", "gemini-2.5-pro", 1_048_576),
            ("GeMiNi", "gemini-2.5-pro", 1_048_576),
            ("gemini-api", "gemini-2.5-pro", 1_048_576),
            (" gemini ", "gemini-2.5-pro", 1_048_576),
            ("gemini", "routing/GEMINI-2.5-PRO-release", 1_048_576),
            # The name holds another provider's key as well as the Gemini key, so the Gemini hint
            # and the scan over every provider give different windows: these rows tell a hint that
            # was recognised from one that fell through to the scan.
            (None, "o3/gemini-2.5-pro", 200_000),
            ("gemini", "o3/gemini-2.5-pro", 1_048_576),
            ("gemini-api", "o3/gemini-2.5-pro", 200_000),
            (" gemini ", "o3/gemini-2.5-pro", 200_000),
        ],
        ids=[
            "global",
            "hint",
            "case",
            "alias",
            "whitespace",
            "substring",
            "global-scan",
            "hint-over-scan",
            "alias-scan",
            "whitespace-scan",
        ],
    )
    def test_gemini_lookup_preserves_hint_and_global_precedence(self, provider, model, expected):
        """Context lookup has its own hint matching and provider scan order."""
        import lionagi.service.token_budget as token_budget

        # Use the production catalog's window tables.
        assert token_budget.lookup_context_window(model, provider=provider) == expected

    @pytest.mark.parametrize(
        "provider",
        [None, "gemini_code", "gemini"],
        ids=["global", "gemini_code", "gemini"],
    )
    def test_gemini_2_5_pro_has_one_window_on_every_route(self, provider):
        """gemini-2.5-pro has a 1,048,576-token window with no hint and with either Gemini hint."""
        import lionagi.service.token_budget as token_budget

        assert token_budget.lookup_context_window("gemini-2.5-pro", provider=provider) == 1_048_576

    def test_gpt_4_1_canonical_value_is_openai(self):
        """gpt-4.1 from the openai provider must match openai/_config.py (primary path)."""
        result = lookup_context_window("gpt-4.1", provider="openai")
        assert result == 1_000_000

    def test_gpt_5_5_canonical_value_is_openai(self):
        """gpt-5.5 from the openai provider must match openai/_config.py (primary path)."""
        result = lookup_context_window("gpt-5.5", provider="openai")
        assert result == 1_000_000

    def test_codex_mini_resolves_from_codex_provider(self):
        """codex-mini is codex-provider-only; not in openai/_config.py."""
        result = lookup_context_window("codex-mini", provider="codex")
        assert result == 200_000

    def test_gpt_4_1_cross_provider_matches_openai(self):
        """Without a provider hint, gpt-4.1 resolves to the openai value (openai wins iteration)."""
        openai_val = lookup_context_window("gpt-4.1", provider="openai")
        cross_val = lookup_context_window("gpt-4.1")
        assert openai_val == cross_val

    def test_gpt_5_5_no_conflict_between_providers(self):
        """gpt-5.5 removed from codex registry; only openai/_config.py returns a value."""
        openai_val = lookup_context_window("gpt-5.5", provider="openai")
        codex_val = lookup_context_window("gpt-5.5", provider="codex")
        # codex no longer has gpt-5.5; falls back to cross-provider search → finds openai
        assert openai_val == codex_val

    def test_claude_sonnet_4_6_anthropic(self):
        result = lookup_context_window("claude-sonnet-4-6", provider="anthropic")
        assert result == 1_000_000

    def test_gemini_2_5_flash_google(self):
        result = lookup_context_window("gemini-2.5-flash", provider="gemini")
        assert result == 1_048_576

    def test_deepseek_r1_resolves(self):
        result = lookup_context_window("deepseek-r1", provider="deepseek")
        assert result == 64_000
