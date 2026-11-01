# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Provider-window imports remain lazy, cached only on success, and ordered."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from lionagi.service import token_budget


@pytest.fixture
def window_sources(tmp_path, monkeypatch):
    package = f"window_sources_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("CONTEXT_WINDOWS = {'parent': 1}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(token_budget, "_provider_cache", {})

    def write(name, source):
        (root / f"{name}.py").write_text(source, encoding="utf-8")
        importlib.invalidate_caches()
        return f"{package}.{name}"

    yield package, write
    for name in tuple(sys.modules):
        if name == package or name.startswith(f"{package}."):
            sys.modules.pop(name, None)


def test_cold_leaf_dictionary_and_case_insensitive_cache(window_sources, monkeypatch):
    _package, write = window_sources
    leaf = write("windows", "CONTEXT_WINDOWS = {'fixture': 8192}\n")
    monkeypatch.setattr(token_budget, "_PROVIDER_MODULES", {"fixture": leaf})
    assert leaf not in sys.modules

    windows = token_budget._get_provider_windows("FIXTURE")

    assert windows == {"fixture": 8192}
    assert windows is sys.modules[leaf].CONTEXT_WINDOWS
    sys.modules[leaf].CONTEXT_WINDOWS = {"replacement": 1}
    assert token_budget._get_provider_windows("fixture") is windows


@pytest.mark.parametrize("source", ["OTHER = 1\n", "CONTEXT_WINDOWS = ()\n"])
def test_missing_or_invalid_dictionary_is_not_cached(window_sources, monkeypatch, source):
    _package, write = window_sources
    leaf = write("windows", source)
    monkeypatch.setattr(token_budget, "_PROVIDER_MODULES", {"fixture": leaf})

    assert token_budget._get_provider_windows("fixture") is None
    assert "fixture" not in token_budget._provider_cache

    windows = {"fixture": 4096}
    sys.modules[leaf].CONTEXT_WINDOWS = windows
    assert token_budget._get_provider_windows("fixture") is windows


def test_failed_dependency_import_is_retryable(window_sources, monkeypatch):
    package, write = window_sources
    leaf = write("windows", "from .dependency import CONTEXT_WINDOWS\n")
    monkeypatch.setattr(token_budget, "_PROVIDER_MODULES", {"fixture": leaf})

    assert token_budget._get_provider_windows("fixture") is None
    assert leaf not in sys.modules
    assert "fixture" not in token_budget._provider_cache

    dependency = write("dependency", "CONTEXT_WINDOWS = {'fixture': 16384}\n")
    windows = token_budget._get_provider_windows("fixture")
    assert windows == {"fixture": 16384}
    assert windows is sys.modules[dependency].CONTEXT_WINDOWS
    assert f"{package}.windows" in sys.modules


@pytest.mark.parametrize(
    "source",
    ["raise ImportError('provider unavailable')\n", "from .missing import CONTEXT_WINDOWS\n"],
)
def test_import_failures_remain_best_effort(window_sources, monkeypatch, source):
    _package, write = window_sources
    leaf = write("windows", source)
    monkeypatch.setattr(token_budget, "_PROVIDER_MODULES", {"fixture": leaf})

    assert token_budget._get_provider_windows("fixture") is None
    assert token_budget._provider_cache == {}
    assert token_budget._get_provider_windows("unknown") is None


def test_provider_iteration_and_hint_precedence_stay_distinct(window_sources, monkeypatch):
    _package, write = window_sources
    first = write("first", "CONTEXT_WINDOWS = {'shared': 111}\n")
    second = write("second", "CONTEXT_WINDOWS = {'shared-longer': 222}\n")
    monkeypatch.setattr(token_budget, "_PROVIDER_MODULES", {"first": first, "second": second})

    assert token_budget.lookup_context_window("shared-longer") == 111
    assert token_budget.lookup_context_window("shared-longer", provider="second") == 222
    assert list(token_budget._all_provider_windows()) == [
        sys.modules[first].CONTEXT_WINDOWS,
        sys.modules[second].CONTEXT_WINDOWS,
    ]
    assert token_budget.lookup_context_window("unknown-model") == 128_000


ROOT = Path(__file__).resolve().parents[2]
_LIONHUB_SRC = str(ROOT / "lionhub" / "src")
_PROVIDERS = "lionhub.providers"

# Provider package -> context-window tables its ``_config`` module must expose.
_CONFIG_TABLES = {
    "ag2": (),
    "anthropic": ("ANTHROPIC_CONTEXT_WINDOWS", "CLAUDE_CODE_CONTEXT_WINDOWS"),
    "deepseek": ("CONTEXT_WINDOWS",),
    "exa": (),
    "firecrawl": (),
    "google": ("GEMINI_CONTEXT_WINDOWS", "GEMINI_CODE_CONTEXT_WINDOWS"),
    "groq": ("CONTEXT_WINDOWS",),
    "lattice": (),
    "nvidia_nim": ("CONTEXT_WINDOWS",),
    "ollama": (),
    "openai": ("CONTEXT_WINDOWS", "CODEX_CONTEXT_WINDOWS"),
    "openrouter": ("CONTEXT_WINDOWS",),
    "perplexity": ("CONTEXT_WINDOWS",),
    "pi": ("CONTEXT_WINDOWS",),
    "tavily": (),
}

# token_budget provider key, endpoint module, package holding its table, table name.
_ENDPOINT_TABLES = [
    ("anthropic", "anthropic.messages", "anthropic", "ANTHROPIC_CONTEXT_WINDOWS"),
    ("claude_code", "anthropic.claude_code", "anthropic", "CLAUDE_CODE_CONTEXT_WINDOWS"),
    ("codex", "openai.codex", "openai", "CODEX_CONTEXT_WINDOWS"),
    ("deepseek", "deepseek.chat", "deepseek", "CONTEXT_WINDOWS"),
    ("nvidia_nim", "nvidia_nim.chat", "nvidia_nim", "CONTEXT_WINDOWS"),
    ("perplexity", "perplexity.chat", "perplexity", "CONTEXT_WINDOWS"),
    ("gemini_code", "google.gemini_code", "google", "GEMINI_CODE_CONTEXT_WINDOWS"),
    ("pi", "pi.cli", "pi", "CONTEXT_WINDOWS"),
    ("groq", "groq.chat", "groq", "CONTEXT_WINDOWS"),
    ("gemini", "google.chat", "google", "GEMINI_CONTEXT_WINDOWS"),
    ("openrouter", "openrouter.chat", "openrouter", "CONTEXT_WINDOWS"),
]

# Every key of ``token_budget._PROVIDER_MODULES`` with its ``_config`` table.
_PROVIDER_TABLES = [("openai", "openai", "CONTEXT_WINDOWS")] + [
    (key, package, name) for key, _endpoint, package, name in _ENDPOINT_TABLES
]

_CONFIG_IMPORT_CHILD = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
tables = json.loads(sys.argv[2])
implementations = json.loads(sys.argv[3])

missing = {}
for package, names in tables.items():
    module = importlib.import_module(f"lionhub.providers.{package}._config")
    absent = [name for name in names if not isinstance(getattr(module, name, None), dict)]
    if absent:
        missing[package] = absent
loaded = sorted(name for name in implementations if name in sys.modules)
for name in implementations:
    importlib.import_module(name)
loaded_after = sorted(name for name in implementations if name in sys.modules)
print(json.dumps({"missing": missing, "loaded": loaded, "loaded_after": loaded_after}))
"""


def _run_child(code: str, *args: str) -> dict:
    """Run ``code`` in a fresh interpreter with this process's environment; parse its JSON line."""
    proc = subprocess.run(  # noqa: S603 -- this interpreter, fixed script
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_config_modules_expose_tables_without_loading_endpoints():
    implementations = [f"{_PROVIDERS}.{endpoint}" for _k, endpoint, _p, _n in _ENDPOINT_TABLES]
    implementations.append(f"{_PROVIDERS}.openai.chat")

    result = _run_child(
        _CONFIG_IMPORT_CHILD,
        _LIONHUB_SRC,
        json.dumps({package: list(names) for package, names in _CONFIG_TABLES.items()}),
        json.dumps(implementations),
    )

    assert result["missing"] == {}
    assert result["loaded"] == []
    # The same check sees every listed endpoint module once it is imported, so the
    # empty list above is a measurement and not a misspelled module name.
    assert result["loaded_after"] == sorted(implementations)


def test_provider_table_rows_cover_every_token_budget_provider():
    assert sorted(key for key, _package, _name in _PROVIDER_TABLES) == sorted(
        token_budget._PROVIDER_MODULES
    )


@pytest.mark.parametrize(
    ("key", "endpoint", "package", "name"),
    _ENDPOINT_TABLES,
    ids=[row[0] for row in _ENDPOINT_TABLES],
)
def test_endpoint_module_shares_config_table(key, endpoint, package, name):
    config = importlib.import_module(f"{_PROVIDERS}.{package}._config")
    table = getattr(config, name)
    module = importlib.import_module(f"{_PROVIDERS}.{endpoint}")

    assert module.CONTEXT_WINDOWS is table


@pytest.mark.parametrize(
    ("key", "package", "name"),
    _PROVIDER_TABLES,
    ids=[row[0] for row in _PROVIDER_TABLES],
)
def test_provider_window_lookup_returns_config_table(key, package, name, monkeypatch):
    monkeypatch.setattr(token_budget, "_provider_cache", {})
    config = importlib.import_module(f"{_PROVIDERS}.{package}._config")
    table = getattr(config, name)

    assert token_budget._get_provider_windows(key) is table
