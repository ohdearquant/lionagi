# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Provider-window imports remain lazy, cached only on success, and ordered."""

from __future__ import annotations

import importlib
import sys
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
