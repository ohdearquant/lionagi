# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Context-window lookup through the provider catalog
(docs/adr/ADR-0140-provider-discovery-contract.md).

Every check that loads the catalog runs in a fresh interpreter, so no catalog or loader state
kept by another test can change the result; the child prints one JSON line and the test
asserts on it. Expected values are literals, except in the parity check, whose expected values
come from a frozen copy of the earlier lookup algorithm reading each provider's own tables.
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

# Unhinted lookups scan the window tables in this order and the first match wins; the catalog
# carries it as its context_window_order.
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

# (provider key, provider package, table name): where each window table is declared, in the
# order of _PROVIDER_ORDER. The parity check reads these tables from the packages' own config
# modules, not through the catalog.
_LEGACY_TABLES = [
    ("openai", "openai", "CONTEXT_WINDOWS"),
    ("anthropic", "anthropic", "ANTHROPIC_CONTEXT_WINDOWS"),
    ("claude_code", "anthropic", "CLAUDE_CODE_CONTEXT_WINDOWS"),
    ("codex", "openai", "CODEX_CONTEXT_WINDOWS"),
    ("deepseek", "deepseek", "CONTEXT_WINDOWS"),
    ("nvidia_nim", "nvidia_nim", "CONTEXT_WINDOWS"),
    ("perplexity", "perplexity", "CONTEXT_WINDOWS"),
    ("gemini_code", "google", "GEMINI_CODE_CONTEXT_WINDOWS"),
    ("pi", "pi", "CONTEXT_WINDOWS"),
    ("groq", "groq", "CONTEXT_WINDOWS"),
    ("gemini", "google", "GEMINI_CONTEXT_WINDOWS"),
    ("openrouter", "openrouter", "CONTEXT_WINDOWS"),
]

# Lookups the parity check compares: every distinct model string built from the window keys,
# times every distinct hint (none, the empty string, each catalog key, every alias a provider
# declares, each key in upper case and padded with spaces, and an unknown name).
_PARITY_COMPARISONS = 27440

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
    # test_pi_table_items is what pins the table's key and window.
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
    # "o1-mini" (128,000) beats the shorter "o1" (200,000).
    ("openai", "o1-mini-2024", 128_000),
]

_HEALTHY_CHILD = """
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
from lionagi.service import token_budget
from lionagi.service.connections._provider_compat import default_provider_catalog

cases = json.loads(sys.argv[2])
lookup = token_budget.lookup_context_window
catalog = default_provider_catalog()
out = {
    "order": list(catalog.context_window_order),
    "sizes": {f.key: len(f.context_windows) for f in catalog.providers if f.context_windows},
    "pi_items": dict(catalog.fragment("pi").context_windows),
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

# One scenario per interpreter, because the loader keeps its first failure for the life of the
# process. "missing": the hub's top-level package cannot be imported. "raises": a hub comes first
# on the path whose manifest module fails when it is imported.
_BROKEN_CHILD = """
import json
import sys
from types import SimpleNamespace

from lionagi.service import token_budget

mode = sys.argv[1]
for name in [n for n in sys.modules if n == "lionhub" or n.startswith("lionhub.")]:
    del sys.modules[name]
if mode == "missing":

    class _Absent:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "lionhub":
                raise ModuleNotFoundError("No module named 'lionhub'", name="lionhub")
            return None

    sys.meta_path.insert(0, _Absent())
else:
    sys.path.insert(0, sys.argv[2])

lookup = token_budget.lookup_context_window


def attempt(call):
    try:
        return {"value": call()}
    except Exception as exc:
        cause = exc.__cause__
        return {
            "error": type(exc).__name__,
            "cause": None if cause is None else type(cause).__name__,
        }


def branch(**config):
    endpoint = SimpleNamespace(config=SimpleNamespace(**config))
    return SimpleNamespace(chat_model=SimpleNamespace(endpoint=endpoint))


model = {"model": "gpt-5.5"}
out = {
    "hinted": attempt(lambda: lookup("gpt-5.5", "openai")),
    "unhinted": attempt(lambda: lookup("gpt-5.5")),
    "unknown": attempt(lambda: lookup("zzz-unknown-model-9", "openai")),
    "explicit": attempt(
        lambda: token_budget.get_context_window(
            branch(context_window=32000, provider="openai", kwargs=model)
        )
    ),
    "modelled": attempt(
        lambda: token_budget.get_context_window(
            branch(context_window=None, provider="openai", kwargs=model)
        )
    ),
}
print(json.dumps(out))
"""

# Every window key of the catalog, in four spellings, against every hint: the catalog lookup
# must give what a frozen copy of the earlier algorithm gives (hinted table first, then the
# tables in legacy order, longest key contained in the lower-cased model name, first declared
# wins a tie, 128,000 when nothing matches). The earlier algorithm honoured a hint only when
# its lower-cased form was one of the twelve provider keys, so an alias, a padded name and the
# empty string all fall through to the unhinted scan.
_PARITY_CHILD = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
tables = json.loads(sys.argv[2])
order = json.loads(sys.argv[3])

from lionagi.service import token_budget
from lionagi.service.connections._provider_compat import default_provider_catalog

legacy = {}
for key, package, name in tables:
    module = importlib.import_module(f"lionhub.providers.{package}._config")
    legacy[key] = dict(getattr(module, name))


def longest(windows, lowered):
    best, best_len = None, 0
    for prefix, window in windows.items():
        if prefix in lowered and len(prefix) > best_len:
            best, best_len = window, len(prefix)
    return best


def expected(model, hint):
    lowered = model.lower()
    windows = legacy.get(hint.lower()) if hint else None
    if windows:
        found = longest(windows, lowered)
        if found is not None:
            return found
    for key in order:
        found = longest(legacy[key], lowered)
        if found is not None:
            return found
    return 128000


catalog = default_provider_catalog()
keys = sorted({k for f in catalog.providers for k in (f.context_windows or {})})
models = sorted({v for k in keys for v in (k, k + "-x", "x/" + k, k.upper())})
provider_keys = [f.key for f in catalog.providers]
aliases = [alias for f in catalog.providers for alias in f.aliases]
hints = list(
    dict.fromkeys(
        [None, ""]
        + provider_keys
        + aliases
        + [key.upper() for key in provider_keys]
        + [f" {key} " for key in provider_keys]
        + ["unknown"]
    )
)
differences = []
compared = 0
for model in models:
    for hint in hints:
        got = token_budget.lookup_context_window(model, hint, provider_catalog=catalog)
        want = expected(model, hint)
        compared += 1
        if got != want:
            differences.append([model, hint, got, want])
print(json.dumps({
    "compared": compared,
    "models": len(models),
    "hints": len(hints),
    "alias_hints": aliases,
    "difference_count": len(differences),
    "differences": differences[:20],
}))
"""

# An explicit catalog is used as given: an empty one answers every model with the estimate and
# never loads the hub.
_EMPTY_CATALOG_CHILD = """
import json
import sys

sys.path.insert(0, sys.argv[1])
from lionagi.service import token_budget
from lionagi.service.connections.provider_catalog import compose_provider_catalog

empty = compose_provider_catalog([], source="empty-catalog-test")
lookup = token_budget.lookup_context_window
print(json.dumps({
    "hinted": lookup("gpt-5.5", "openai", provider_catalog=empty),
    "unhinted": lookup("gpt-5.5", provider_catalog=empty),
    "hub_modules": sorted(
        n for n in sys.modules if n == "lionhub" or n.startswith("lionhub.")
    ),
}))
"""

# The lookup reads window metadata only: nothing is loaded from the hub before the lookup, and
# afterwards only provider packages, their config modules and the manifest.
_PURITY_CHILD = """
import json
import sys

sys.path.insert(0, sys.argv[1])
from lionagi.service import token_budget


def loaded():
    return sorted(n for n in sys.modules if n == "lionhub" or n.startswith("lionhub."))


before = loaded()
if sys.argv[2] == "hinted":
    token_budget.lookup_context_window("gemini-2.5-pro", "gemini_code")
else:
    token_budget.lookup_context_window("zzz-unknown-model-9")
print(json.dumps({"before": before, "loaded": loaded()}))
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
    hub = tmp_path_factory.mktemp("broken_hub")
    package = hub / "lionhub" / "providers"
    package.mkdir(parents=True)
    (hub / "lionhub" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "manifest.py").write_text(
        "raise ImportError('provider unavailable')\n", encoding="utf-8"
    )
    return {
        scenario: _run_child(_BROKEN_CHILD, scenario, str(hub))
        for scenario in ("missing", "raises")
    }


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


def test_pi_table_items(healthy):
    # The pi table's own contents. The pi rows in _HINTED get the same 128,000 from a table hit
    # and from a miss, so only the items show that the key is still there.
    assert healthy["pi_items"] == {"pi": 128_000}


@pytest.mark.parametrize(("provider", "model", "window"), _HINTED)
def test_hinted_lookup_reads_the_hinted_table(healthy, provider, model, window):
    assert healthy["lookups"][f"{provider}|{model}"] == window


@pytest.mark.parametrize(("provider", "model", "window"), _LONGEST_KEY)
def test_longest_matching_key_wins(healthy, provider, model, window):
    assert healthy["lookups"][f"{provider}|{model}"] == window


def test_failed_provider_load_raises_a_typed_error_not_the_unknown_model_estimate(healthy, broken):
    # An unknown model gets the 128,000 estimate with or without a provider hint when the
    # catalog loads.
    assert healthy["unknown_hinted"] == 128_000
    assert healthy["unknown_unhinted"] == 128_000
    # Control: with a healthy catalog the same gpt-5.5 lookup reads its table row, so the
    # errors below replace a real answer and not an estimate.
    assert healthy["table"] == 1_000_000
    expected = {
        # The hub's top-level package cannot be imported.
        "missing": {"error": "MissingFeatureError", "cause": "ModuleNotFoundError"},
        # The hub is present and its manifest module fails when it is imported.
        "raises": {"error": "ProviderActivationError", "cause": "ImportError"},
    }
    for scenario, failure in expected.items():
        result = broken[scenario]
        # Hinted, unhinted, unknown-model and branch lookups all raise the loader's typed
        # error; none of them returns 128,000.
        for call in ("hinted", "unhinted", "unknown", "modelled"):
            assert result[call] == failure, (scenario, call, result)
        # An explicit context window on the endpoint config never consults the catalog.
        assert result["explicit"] == {"value": 32_000}, (scenario, result)


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


def test_catalog_lookup_matches_the_earlier_algorithm_on_every_window_key():
    result = _run_child(
        _PARITY_CHILD,
        _LIONHUB_SRC,
        json.dumps(_LEGACY_TABLES),
        json.dumps(_PROVIDER_ORDER),
    )

    print(
        f"parity comparisons: {result['compared']} = {result['models']} models x "
        f"{result['hints']} hints, {result['difference_count']} differences"
    )
    assert result["difference_count"] == 0, result
    assert result["differences"] == []
    # The hint set really contains aliases; without them the check would not see an alias hint.
    assert "gemini-api" in result["alias_hints"], result
    assert result["compared"] == result["models"] * result["hints"]
    assert result["compared"] == _PARITY_COMPARISONS


def test_explicit_empty_catalog_gives_the_estimate_and_never_loads_the_hub():
    result = _run_child(_EMPTY_CATALOG_CHILD, _LIONHUB_SRC)

    # gpt-5.5 has a 1,000,000 row in the hub's table (see test_explicit_window_beats_the_table),
    # so 128,000 here comes from the empty catalog and not from the table.
    assert result == {"hinted": 128_000, "unhinted": 128_000, "hub_modules": []}


def _unexpected_hub_modules(loaded: list[str]) -> list[str]:
    """Loaded hub modules other than the root, provider packages, config modules and manifest."""
    fixed = {"lionhub", "lionhub.providers", "lionhub.providers.manifest"}
    unexpected = []
    for name in loaded:
        parts = name.split(".")
        if name in fixed:
            continue
        if len(parts) == 3 and parts[:2] == ["lionhub", "providers"]:
            continue
        if len(parts) == 4 and parts[:2] == ["lionhub", "providers"] and parts[3] == "_config":
            continue
        unexpected.append(name)
    return unexpected


@pytest.mark.parametrize("mode", ["unhinted", "hinted"])
def test_window_lookup_loads_metadata_without_endpoint_implementations(mode):
    result = _run_child(_PURITY_CHILD, _LIONHUB_SRC, mode)
    loaded = result["loaded"]

    # Importing the budget module loads nothing from the hub.
    assert result["before"] == []
    # The lookup did read the manifest; an empty list would not show purity.
    assert "lionhub.providers.manifest" in loaded, f"loaded lionhub modules: {loaded}"
    assert _unexpected_hub_modules(loaded) == [], f"loaded lionhub modules: {loaded}"
