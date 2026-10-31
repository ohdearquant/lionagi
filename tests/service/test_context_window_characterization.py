# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Context-window lookup as it behaves today, before the provider discovery contract
(docs/adr/ADR-0140-provider-discovery-contract.md) replaces it.

Every check that loads provider window tables runs in a fresh interpreter, so no table cached
by another test can change the result; the child prints one JSON line and the test asserts on
it. Expected values are literals, never derived from the tables under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lionagi.service.token_budget import get_context_window
from tests._core_imports import ROOT

# This checkout's lionhub package, put first on each child's path so the window tables read are
# the ones in this tree and not an installed copy.
_LIONHUB_SRC = str(ROOT / "lionhub" / "src")

# Unhinted lookups scan the window tables in this order and the first match wins. D1 removes
# _PROVIDER_MODULES; D5 keeps scanning the declared mappings in their declared order for a
# healthy catalog.
_PROVIDER_ORDER = [
    "openai",
    "anthropic",
    "claude_code",
    "codex",
    "deepseek",
    "nvidia_nim",
    "perplexity",
    "gemini_code",
    "pi",
    "groq",
    "gemini",
    "openrouter",
]

# Number of entries in each provider's CONTEXT_WINDOWS table.
_TABLE_SIZES = {
    "openai": 22,
    "anthropic": 12,
    "claude_code": 15,
    "codex": 8,
    "deepseek": 8,
    "nvidia_nim": 5,
    "perplexity": 2,
    "gemini_code": 11,
    "pi": 1,
    "groq": 4,
    "gemini": 4,
    "openrouter": 16,
}

# (provider, model, window): two model names per provider, looked up with that provider as the
# hint, so the hinted table answers.
_HINTED = [
    ("openai", "gpt-4.1-nano", 1_000_000),
    ("openai", "o4-mini", 200_000),
    ("anthropic", "claude-opus-4-7", 1_000_000),
    ("anthropic", "claude-3-opus", 200_000),
    ("claude_code", "fable", 1_000_000),
    ("claude_code", "haiku-4-5", 200_000),
    ("codex", "o4-mini", 200_000),
    ("codex", "o3", 200_000),
    ("deepseek", "deepseek-v4-pro", 1_000_000),
    ("deepseek", "deepseek-coder-v2", 128_000),
    ("nvidia_nim", "llama-4-scout", 10_485_760),
    ("nvidia_nim", "llama-4-maverick", 1_048_576),
    ("perplexity", "sonar", 128_000),
    ("perplexity", "sonar-pro", 200_000),
    ("gemini_code", "gemini-3.1-pro", 1_048_576),
    ("gemini_code", "gemini-2.5-pro", 2_097_152),
    # The pi table has one key, so the second name is one that contains it. That key's window
    # equals the unknown-model estimate, so these two rows cannot tell a table hit from a miss;
    # the entry count in _TABLE_SIZES is what pins this table.
    ("pi", "pi", 128_000),
    ("pi", "pi-large", 128_000),
    ("groq", "mixtral-8x7b-32768", 32_768),
    ("groq", "gemma2-9b-it", 8_192),
    ("gemini", "gemini-2.0-flash", 1_048_576),
    ("gemini", "gemini-2.5-flash", 1_048_576),
    ("openrouter", "anthropic/claude-opus-4-5", 200_000),
    ("openrouter", "openai/gpt-4.1", 1_000_000),
]

# (provider, model, window): a name that two keys of the hinted table both match resolves to the
# longer key's window. D5 keeps the rule: longest substring, despite the prefix-oriented name.
_LONGEST_KEY = [
    # "gemini-2.5-pro" (2,097,152) is listed after the shorter "argon" (1,048,576), so the
    # length of the key, not its position in the table, decides.
    ("gemini_code", "gemini-2.5-pro-argon", 2_097_152),
    # "sonnet-4-5" (200,000) beats the shorter "sonnet" (1,000,000).
    ("claude_code", "claude-sonnet-4-5", 200_000),
    # "sonar-pro" (200,000) beats the shorter "sonar" (128,000).
    ("perplexity", "sonar-pro-search", 200_000),
    # "gpt-5.4-mini" (1,000,000) beats "gpt-5.4" (1,048,576).
    ("openai", "gpt-5.4-mini", 1_000_000),
]

_HEALTHY_CHILD = """
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
from lionagi.service import token_budget

cases = json.loads(sys.argv[2])
lookup = token_budget.lookup_context_window
providers = list(token_budget._PROVIDER_MODULES)
out = {
    "order": providers,
    "sizes": {p: len(token_budget._get_provider_windows(p)) for p in providers},
    "lookups": {f"{p}|{m}": lookup(m, p) for p, m in cases},
    "unknown_hinted": lookup("zzz-unknown-model-9", "openai"),
    "unknown_unhinted": lookup("zzz-unknown-model-9"),
}


def branch(**config):
    endpoint = SimpleNamespace(config=SimpleNamespace(**config))
    return SimpleNamespace(chat_model=SimpleNamespace(endpoint=endpoint))


model = {"model": "gpt-5.5"}
out["explicit"] = token_budget.get_context_window(
    branch(context_window=32000, provider="openai", kwargs=model)
)
out["table"] = token_budget.get_context_window(
    branch(context_window=None, provider="openai", kwargs=model)
)
print(json.dumps(out))
"""

_BROKEN_CHILD = """
import json
import sys

sys.path.insert(0, sys.argv[1])
from lionagi.service import token_budget

sys.path.insert(0, sys.argv[2])
lookup = token_budget.lookup_context_window
real = token_budget._PROVIDER_MODULES["openai"]
out = {"cached_before": sorted(token_budget._provider_cache)}
for label, module in (("missing", "window_module_absent"), ("raises", "window_import_error")):
    token_budget._PROVIDER_MODULES["openai"] = module
    out[label] = {
        "hinted": lookup("gpt-5.5", "openai"),
        "unhinted": lookup("gpt-5.5"),
        "unknown": lookup("zzz-unknown-model-9", "openai"),
    }
token_budget._PROVIDER_MODULES["openai"] = real
out["restored"] = lookup("gpt-5.5", "openai")
print(json.dumps(out))
"""


def _run_child(code: str, *args: str) -> dict:
    """Run ``code`` in a fresh interpreter with this process's environment; parse its JSON line."""
    proc = subprocess.run(  # noqa: S603 -- this interpreter, fixed scripts
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def healthy() -> dict:
    cases = [[provider, model] for provider, model, _ in _HINTED + _LONGEST_KEY]
    return _run_child(_HEALTHY_CHILD, _LIONHUB_SRC, json.dumps(cases))


@pytest.fixture(scope="module")
def broken(tmp_path_factory) -> dict:
    directory = tmp_path_factory.mktemp("window_modules")
    (directory / "window_import_error.py").write_text(
        "raise ImportError('provider unavailable')\n", encoding="utf-8"
    )
    return _run_child(_BROKEN_CHILD, _LIONHUB_SRC, str(directory))


class _MissingKey:
    """A mapping whose ``get`` raises ``KeyError``, the smallest object that reaches that path."""

    def get(self, key, default=None):
        raise KeyError(key)


def _branch(config: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(chat_model=SimpleNamespace(endpoint=SimpleNamespace(config=config)))


def test_provider_scan_order(healthy):
    assert healthy["order"] == _PROVIDER_ORDER


@pytest.mark.parametrize(("provider", "size"), list(_TABLE_SIZES.items()))
def test_window_table_size(healthy, provider, size):
    assert healthy["sizes"][provider] == size


@pytest.mark.parametrize(("provider", "model", "window"), _HINTED)
def test_hinted_lookup_reads_the_hinted_table(healthy, provider, model, window):
    assert healthy["lookups"][f"{provider}|{model}"] == window


@pytest.mark.parametrize(("provider", "model", "window"), _LONGEST_KEY)
def test_longest_matching_key_wins(healthy, provider, model, window):
    assert healthy["lookups"][f"{provider}|{model}"] == window


def test_failed_window_module_is_indistinguishable_from_unknown_model(healthy, broken):
    # An unknown model gets the 128,000 estimate with or without a provider hint; D5 keeps it
    # for a healthy catalog.
    assert healthy["unknown_hinted"] == 128_000
    assert healthy["unknown_unhinted"] == 128_000
    # Nothing has filled the table cache when a fresh process starts, so the repointed
    # module is the one the lookup uses.
    assert broken["cached_before"] == []
    for scenario in ("missing", "raises"):
        # A hinted provider whose module is missing, or raises ImportError, returns the same
        # 128,000, hinted or not; D5 replaces this fallback with a typed error.
        assert broken[scenario]["hinted"] == 128_000
        assert broken[scenario]["unhinted"] == 128_000
        # The same broken provider with an unknown model gives the identical value.
        assert broken[scenario]["unknown"] == 128_000
    # Control: with the real module restored, the same lookup reads the table row, so the
    # 128,000 above came from the failed load and not from gpt-5.5 being unknown.
    assert broken["restored"] == 1_000_000


def test_explicit_window_beats_the_table(healthy):
    # An explicit context_window on the endpoint config wins over the table row for its model.
    assert healthy["explicit"] == 32_000
    # Control: the same endpoint without an explicit window reads the gpt-5.5 table row.
    assert healthy["table"] == 1_000_000


@pytest.mark.parametrize(
    "config",
    [
        SimpleNamespace(context_window=None, provider="openai", kwargs=_MissingKey()),
        SimpleNamespace(context_window=None, provider="openai", params=_MissingKey()),
    ],
    ids=["kwargs", "params"],
)
def test_key_error_while_reading_the_model_returns_default(config):
    # A KeyError raised while reading the model name is swallowed and the 128,000 estimate
    # is returned; no window table is loaded on this path.
    assert get_context_window(_branch(config)) == 128_000
