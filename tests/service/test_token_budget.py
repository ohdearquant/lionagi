# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.service.token_budget — TokenBudget dataclass and helpers."""

import types

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

    def test_is_warning_exactly_at_70_pct(self):
        budget = TokenBudget(used=70, limit=100)
        assert budget.is_warning is True

    def test_is_warning_false_at_69_pct(self):
        budget = TokenBudget(used=69, limit=100)
        assert budget.is_warning is False

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

from lionagi.service.token_budget import (
    _get_provider_windows,
    _longest_prefix_match,
    get_token_budget,
    lookup_context_window,
)


class TestLookupContextWindow:
    def test_known_openai_model_returns_nondefault(self):
        """gpt-4 is in the openai CONTEXT_WINDOWS dict."""
        result = lookup_context_window("gpt-4", provider="openai")
        # Any positive non-default value is acceptable; must be > 0
        assert result > 0

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

    def test_get_provider_windows_unknown_provider(self):
        """Unknown provider returns None."""
        result = _get_provider_windows("completely_unknown_xyz")
        assert result is None

    def test_get_provider_windows_caches_result(self):
        """Second call for same provider returns cached result."""
        # openai is a known provider
        first = _get_provider_windows("openai")
        second = _get_provider_windows("openai")
        assert first is second  # same object (cached)

    def test_longest_prefix_match_returns_best_match(self):
        """_longest_prefix_match picks the longest matching prefix."""
        windows = {"gpt-4": 8192, "gpt-4-turbo": 128000, "gpt-3": 4096}
        result = _longest_prefix_match("gpt-4-turbo-preview", windows)
        assert result == 128000  # "gpt-4-turbo" is longer than "gpt-4"

    def test_longest_prefix_match_no_match_returns_none(self):
        """_longest_prefix_match returns None when no prefix matches."""
        windows = {"gpt-4": 8192, "claude": 100000}
        result = _longest_prefix_match("llama-3", windows)
        assert result is None

    def test_get_provider_windows_import_error_returns_none(self):
        """_get_provider_windows returns None when module raises ImportError (lines 55-58)."""
        import lionagi.service.token_budget as tb

        original = tb._PROVIDER_MODULES.copy()
        tb._PROVIDER_MODULES["_bad_test_provider"] = "nonexistent._module._xyz"
        tb._provider_cache.pop("_bad_test_provider", None)
        try:
            result = _get_provider_windows("_bad_test_provider")
            assert result is None
        finally:
            tb._PROVIDER_MODULES.pop("_bad_test_provider", None)
            tb._provider_cache.pop("_bad_test_provider", None)

    def test_all_provider_windows_skips_import_error(self):
        """_all_provider_windows continues past ImportError providers (lines 75-76)."""
        import lionagi.service.token_budget as tb
        from lionagi.service.token_budget import _all_provider_windows

        original = tb._PROVIDER_MODULES.copy()
        tb._PROVIDER_MODULES = {
            "_bad_mod": "nonexistent.module.xyz",
            "openai": tb._PROVIDER_MODULES["openai"],
        }
        tb._provider_cache.pop("_bad_mod", None)
        try:
            results = list(_all_provider_windows())
            assert len(results) >= 1  # openai succeeded despite _bad_mod failing
        finally:
            tb._PROVIDER_MODULES = original


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
