# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Public-API smoke tests for top-level `lionagi` imports.

Issue #1134 — regression guard for lionagi/__init__.py lazy-export table.

Purpose
-------
Every symbol listed here must be importable directly from ``lionagi``. A
failure means the symbol is missing from ``_LAZY_MAP`` / ``__all__`` in
``lionagi/__init__.py``, which breaks downstream code that relies on the
documented public surface.

Dependency on PR #1122
-----------------------
Tests in ``TestLndlPublicAPI`` and ``TestAdaptersPublicAPI`` will **fail**
on a branch that has not merged PR #1122 (lndl + adapters export fix).
This is intentional: the TDD red signal.  Once #1122 is merged the full
suite goes green.

The "sanity" group (``TestExistingPublicAPI``) and sub-module tests
(``TestSubmoduleImportPaths``) must always pass regardless of #1122 status.
"""

import pytest

# ---------------------------------------------------------------------------
# Module-level import — intentionally NOT importorskip.
#
# A broken lionagi/__init__.py must FAIL the entire test module, not skip it.
# Using importorskip here would mask exactly the regressions this file guards
# against (codex P1 on PR #1143).
# ---------------------------------------------------------------------------
import lionagi as _lionagi  # noqa: E402 — must come after pytest import

# ---------------------------------------------------------------------------
# Sanity: pre-existing symbols — must always pass
# ---------------------------------------------------------------------------


class TestExistingPublicAPI:
    """Sanity check — symbols exported before PR #1122.

    These must pass on ANY branch.  A failure here indicates a regression
    in the core export table.
    """

    @pytest.mark.parametrize(
        "symbol",
        [
            "Branch",
            "Session",
            "iModel",
        ],
    )
    def test_core_session_symbols_importable(self, symbol: str) -> None:
        """Branch, Session, iModel must always be importable from lionagi."""
        import lionagi

        obj = getattr(lionagi, symbol)
        assert obj is not None, f"lionagi.{symbol} resolved to None"

    def test_branch_direct_import(self) -> None:
        from lionagi import Branch

        assert Branch is not None

    def test_session_direct_import(self) -> None:
        from lionagi import Session

        assert Session is not None

    def test_imodel_direct_import(self) -> None:
        from lionagi import iModel

        assert iModel is not None


# ---------------------------------------------------------------------------
# New: LNDL public API (requires PR #1122)
# ---------------------------------------------------------------------------


class TestLndlPublicAPI:
    """Verify lndl symbols are importable from the top-level lionagi package.

    All tests here will FAIL until PR #1122 (export fix) is merged into main.
    That failure is the TDD red signal — do not skip or xfail these tests;
    they must go green when #1122 lands.
    """

    def test_lndloutput_importable(self) -> None:
        """LNDLOutput must be importable from lionagi."""
        from lionagi import LNDLOutput  # noqa: F401

        assert LNDLOutput is not None

    def test_lndlerror_importable(self) -> None:
        """LNDLError must be importable from lionagi."""
        from lionagi import LNDLError  # noqa: F401

        assert LNDLError is not None

    def test_get_lndl_system_prompt_importable(self) -> None:
        """get_lndl_system_prompt must be importable from lionagi."""
        from lionagi import get_lndl_system_prompt  # noqa: F401

        assert get_lndl_system_prompt is not None
        assert callable(get_lndl_system_prompt)

    def test_get_lndl_system_prompt_returns_string(self) -> None:
        """get_lndl_system_prompt() must return a non-empty string."""
        from lionagi import get_lndl_system_prompt

        result = get_lndl_system_prompt()
        assert isinstance(result, str), "expected str, got " + type(result).__name__
        assert len(result) > 0, "get_lndl_system_prompt() returned empty string"

    def test_lndloutput_is_correct_type(self) -> None:
        """LNDLOutput from lionagi must be the canonical dataclass from lionagi.lndl."""
        from lionagi import LNDLOutput
        from lionagi.lndl import LNDLOutput as LNDLOutputDirect

        assert LNDLOutput is LNDLOutputDirect, (
            "lionagi.LNDLOutput is not the same object as lionagi.lndl.LNDLOutput — "
            "possible shadowing or wrong import path in _LAZY_MAP"
        )

    def test_lndlerror_is_correct_type(self) -> None:
        """LNDLError from lionagi must be the canonical exception from lionagi.lndl."""
        from lionagi import LNDLError
        from lionagi.lndl import LNDLError as LNDLErrorDirect

        assert LNDLError is LNDLErrorDirect

    @pytest.mark.parametrize(
        "symbol",
        [
            "LNDLOutput",
            "LNDLError",
            "get_lndl_system_prompt",
        ],
    )
    def test_lndl_symbols_in_all(self, symbol: str) -> None:
        """Every lndl symbol must appear in lionagi.__all__."""
        import lionagi

        assert symbol in lionagi.__all__, (
            f"'{symbol}' is missing from lionagi.__all__. "
            "Add it to the __all__ tuple in lionagi/__init__.py."
        )


# ---------------------------------------------------------------------------
# New: Adapters public API (requires PR #1122)
# ---------------------------------------------------------------------------


class TestAdaptersPublicAPI:
    """Verify adapter symbols are importable from the top-level lionagi package.

    All tests here will FAIL until PR #1122 (export fix) is merged into main.
    """

    def test_adapterregistry_importable(self) -> None:
        from lionagi import AdapterRegistry  # noqa: F401

        assert AdapterRegistry is not None

    def test_adaptable_importable(self) -> None:
        from lionagi import Adaptable  # noqa: F401

        assert Adaptable is not None

    def test_jsonadapter_importable(self) -> None:
        from lionagi import JsonAdapter  # noqa: F401

        assert JsonAdapter is not None

    def test_csvadapter_importable(self) -> None:
        from lionagi import CsvAdapter  # noqa: F401

        assert CsvAdapter is not None

    def test_tomladapter_importable(self) -> None:
        from lionagi import TomlAdapter  # noqa: F401

        assert TomlAdapter is not None

    def test_adapterregistry_is_correct_type(self) -> None:
        """AdapterRegistry from lionagi must be the canonical class from lionagi.adapters."""
        from lionagi import AdapterRegistry
        from lionagi.adapters import AdapterRegistry as AdapterRegistryDirect

        assert AdapterRegistry is AdapterRegistryDirect, (
            "lionagi.AdapterRegistry is not the same object as "
            "lionagi.adapters.AdapterRegistry — possible shadowing"
        )

    def test_adaptable_is_correct_type(self) -> None:
        from lionagi import Adaptable
        from lionagi.adapters import Adaptable as AdaptableDirect

        assert Adaptable is AdaptableDirect

    def test_jsonadapter_is_correct_type(self) -> None:
        from lionagi import JsonAdapter
        from lionagi.adapters import JsonAdapter as JsonAdapterDirect

        assert JsonAdapter is JsonAdapterDirect

    def test_csvadapter_is_correct_type(self) -> None:
        from lionagi import CsvAdapter
        from lionagi.adapters import CsvAdapter as CsvAdapterDirect

        assert CsvAdapter is CsvAdapterDirect

    def test_tomladapter_is_correct_type(self) -> None:
        from lionagi import TomlAdapter
        from lionagi.adapters import TomlAdapter as TomlAdapterDirect

        assert TomlAdapter is TomlAdapterDirect

    @pytest.mark.parametrize(
        "symbol",
        [
            "AdapterRegistry",
            "Adaptable",
            "JsonAdapter",
            "CsvAdapter",
            "TomlAdapter",
        ],
    )
    def test_adapter_symbols_in_all(self, symbol: str) -> None:
        """Every adapter symbol must appear in lionagi.__all__."""
        import lionagi

        assert symbol in lionagi.__all__, (
            f"'{symbol}' is missing from lionagi.__all__. "
            "Add it to the __all__ tuple in lionagi/__init__.py."
        )


# ---------------------------------------------------------------------------
# __all__ consistency: every symbol in __all__ must be importable
# ---------------------------------------------------------------------------


class TestAllConsistency:
    """Every symbol declared in lionagi.__all__ must resolve without error.

    This catches stale __all__ entries that point to removed or renamed symbols.
    """

    def test_all_is_defined(self) -> None:
        import lionagi

        assert hasattr(lionagi, "__all__"), "lionagi must define __all__"
        assert isinstance(lionagi.__all__, tuple), "__all__ must be a tuple"
        assert len(lionagi.__all__) > 0, "__all__ must not be empty"

    @pytest.mark.parametrize(
        "symbol",
        # Use the module-level _lionagi import (plain `import lionagi`), NOT
        # importorskip.  importorskip inside parametrize args is evaluated at
        # collection time and silently skips the whole module on any import
        # error — exactly the failure mode this file is meant to catch.
        list(_lionagi.__all__),
    )
    def test_all_symbol_importable_via_getattr(self, symbol: str) -> None:
        """getattr(lionagi, symbol) must not raise for any symbol in __all__."""
        try:
            getattr(_lionagi, symbol)
        except (AttributeError, ImportError) as exc:
            pytest.fail(
                f"lionagi.__all__ declares '{symbol}' but getattr raised "
                f"{type(exc).__name__}: {exc}"
            )

    def test_all_sorted_dunders_first(self) -> None:
        """__all__ must be globally sorted: dunder names first, then regular names alphabetically."""
        import lionagi

        dunder_names = [n for n in lionagi.__all__ if n.startswith("__")]
        regular_names = [n for n in lionagi.__all__ if not n.startswith("__")]

        assert list(dunder_names) == sorted(dunder_names), "__all__ dunder entries are not sorted"
        assert list(regular_names) == sorted(regular_names), (
            "__all__ regular entries are not globally sorted. "
            "The full regular-name list must be alphabetically ordered."
        )

        dunder_indices = [i for i, n in enumerate(lionagi.__all__) if n.startswith("__")]
        regular_indices = [i for i, n in enumerate(lionagi.__all__) if not n.startswith("__")]
        if dunder_indices and regular_indices:
            assert max(dunder_indices) < min(regular_indices), (
                "All dunder entries in __all__ must appear before regular entries"
            )

    def test_all_no_duplicates(self) -> None:
        """__all__ must not contain duplicate entries."""
        import lionagi

        seen: set[str] = set()
        duplicates = []
        for name in lionagi.__all__:
            if name in seen:
                duplicates.append(name)
            seen.add(name)
        assert not duplicates, f"Duplicate entries in __all__: {duplicates}"


# ---------------------------------------------------------------------------
# Sub-module import paths (always pass — does NOT require PR #1122)
# ---------------------------------------------------------------------------


class TestSubmoduleImportPaths:
    """Verify lndl and adapters sub-packages are reachable via their own paths.

    These do NOT go through the top-level lionagi.__init__ lazy table and
    must always pass regardless of whether #1122 has merged.
    """

    def test_lndl_subpackage_importable(self) -> None:
        import lionagi.lndl as lndl_mod

        assert lndl_mod is not None

    def test_lndloutput_via_subpackage(self) -> None:
        from lionagi.lndl import LNDLOutput

        assert LNDLOutput is not None

    def test_lndlerror_via_subpackage(self) -> None:
        from lionagi.lndl import LNDLError

        assert issubclass(LNDLError, Exception)

    def test_get_lndl_system_prompt_via_subpackage(self) -> None:
        from lionagi.lndl import get_lndl_system_prompt

        assert callable(get_lndl_system_prompt)
        prompt = get_lndl_system_prompt()
        assert isinstance(prompt, str) and len(prompt) > 0

    def test_adapters_subpackage_importable(self) -> None:
        import lionagi.adapters as adapters_mod

        assert adapters_mod is not None

    def test_adapterregistry_via_subpackage(self) -> None:
        from lionagi.adapters import AdapterRegistry

        assert AdapterRegistry is not None

    def test_adaptable_via_subpackage(self) -> None:
        from lionagi.adapters import Adaptable

        assert Adaptable is not None

    def test_jsonadapter_via_subpackage(self) -> None:
        from lionagi.adapters import JsonAdapter

        assert JsonAdapter is not None

    def test_csvadapter_via_subpackage(self) -> None:
        from lionagi.adapters import CsvAdapter

        assert CsvAdapter is not None

    def test_tomladapter_via_subpackage(self) -> None:
        from lionagi.adapters import TomlAdapter

        assert TomlAdapter is not None
