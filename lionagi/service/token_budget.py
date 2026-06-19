# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Token budget tracking — context window lookup and usage calculation per branch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

_DEFAULT_CONTEXT_WINDOW = 128_000

_provider_cache: dict[str, dict[str, int]] = {}

_PROVIDER_MODULES: dict[str, str] = {
    "openai": "lionagi.providers.openai._config",
    "anthropic": "lionagi.providers.anthropic.messages",
    "claude_code": "lionagi.providers.anthropic.claude_code",
    "codex": "lionagi.providers.openai.codex",
    "deepseek": "lionagi.providers.deepseek.chat",
    "nvidia_nim": "lionagi.providers.nvidia_nim.chat",
    "perplexity": "lionagi.providers.perplexity.chat",
    "gemini_code": "lionagi.providers.google.gemini_code",
    "pi": "lionagi.providers.pi.cli",
    "groq": "lionagi.providers.groq.chat",
    "gemini": "lionagi.providers.google.chat",
    "openrouter": "lionagi.providers.openrouter.chat",
}


def _get_provider_windows(provider: str) -> dict[str, int] | None:
    """Return CONTEXT_WINDOWS for the named provider, importing lazily."""
    provider_lower = provider.lower()
    if provider_lower in _provider_cache:
        return _provider_cache[provider_lower]

    module_path = _PROVIDER_MODULES.get(provider_lower)
    if module_path is None:
        return None

    try:
        import importlib

        mod = importlib.import_module(module_path)
        windows = getattr(mod, "CONTEXT_WINDOWS", None)
        if isinstance(windows, dict):
            _provider_cache[provider_lower] = windows
            return windows
    except ImportError:
        pass

    return None


def _all_provider_windows():
    """Yield CONTEXT_WINDOWS dicts for all known providers."""
    import importlib

    for provider_lower, module_path in _PROVIDER_MODULES.items():
        if provider_lower in _provider_cache:
            yield _provider_cache[provider_lower]
            continue
        try:
            mod = importlib.import_module(module_path)
            windows = getattr(mod, "CONTEXT_WINDOWS", None)
            if isinstance(windows, dict):
                _provider_cache[provider_lower] = windows
                yield windows
        except ImportError:
            continue


def _longest_prefix_match(model_name: str, windows: dict[str, int]) -> int | None:
    """Return the context window for the longest matching prefix in windows."""
    model_lower = model_name.lower()
    best_match: int | None = None
    best_len = 0
    for prefix, window in windows.items():
        if prefix in model_lower and len(prefix) > best_len:
            best_match = window
            best_len = len(prefix)
    return best_match


def lookup_context_window(model_name: str, provider: str | None = None) -> int:
    """Return context window for model_name via longest-prefix match; tries provider-specific dict first."""
    if provider:
        windows = _get_provider_windows(provider)
        if windows:
            result = _longest_prefix_match(model_name, windows)
            if result is not None:
                return result

    for prov_windows in _all_provider_windows():
        result = _longest_prefix_match(model_name, prov_windows)
        if result is not None:
            return result

    return _DEFAULT_CONTEXT_WINDOW


@dataclass(frozen=True)
class TokenBudget:
    used: int
    limit: int
    model: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def usage_pct(self) -> float:
        return self.used / self.limit if self.limit > 0 else 0.0

    @property
    def is_warning(self) -> bool:
        return self.usage_pct >= 0.7

    @property
    def is_critical(self) -> bool:
        return self.usage_pct >= 0.9


def get_context_window(branch: Branch) -> int:
    """Resolve context window: endpoint config > provider lookup > default (128k)."""
    try:
        endpoint = branch.chat_model.endpoint
        if getattr(endpoint.config, "context_window", None):
            return endpoint.config.context_window

        model_name = ""
        provider = getattr(endpoint.config, "provider", None)
        if hasattr(endpoint.config, "kwargs"):
            model_name = endpoint.config.kwargs.get("model", "")
        if not model_name and hasattr(endpoint.config, "params"):
            model_name = endpoint.config.params.get("model", "")

        if model_name:
            return lookup_context_window(model_name, provider)
    except (AttributeError, KeyError):
        pass

    return _DEFAULT_CONTEXT_WINDOW


def get_token_budget(branch: Branch) -> TokenBudget:
    """Calculate current token budget for a branch."""
    from lionagi.service.token_calculator import TokenCalculator

    limit = get_context_window(branch)
    progression = branch.progression
    pile = branch.msgs.messages

    used = 0
    for uid in progression:
        if uid in pile:
            msg = pile[uid]
            c = msg.content if hasattr(msg, "content") else ""
            if c:
                used += TokenCalculator.tokenize(str(c) if not isinstance(c, str) else c)

    model_name = None
    try:
        if hasattr(branch.chat_model.endpoint.config, "kwargs"):
            model_name = branch.chat_model.endpoint.config.kwargs.get("model")
    except AttributeError:
        pass

    return TokenBudget(used=used, limit=limit, model=model_name)
