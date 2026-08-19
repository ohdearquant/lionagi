# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.ln's lazy __getattr__/__dir__ import surface.

Covers targets 5+7 of the import-time optimization: `.concurrency` (anyio),
`._utils`/`._proc` (anyio), and `._async_call`/`._to_list` are deferred off the
cold `import lionagi` path but every public symbol must still resolve.
"""

import subprocess
import sys

import pytest

import lionagi.ln as ln


@pytest.mark.parametrize("name", ln.__all__)
def test_all_exports_resolve(name):
    """Every name in __all__ is resolvable via getattr and non-None."""
    assert getattr(ln, name) is not None


@pytest.mark.parametrize("name", ln.__all__)
def test_all_exports_importable(name):
    """Every name in __all__ is importable via `from lionagi.ln import <name>`."""
    mod = __import__("lionagi.ln", fromlist=[name])
    assert getattr(mod, name) is not None


def test_invalid_attribute_raises_attribute_error():
    with pytest.raises(AttributeError, match="has no attribute"):
        _ = ln.NonExistentAttribute


def test_lazy_attribute_cached_after_first_access():
    first = ln.alcall
    second = ln.alcall
    assert first is second
    assert "alcall" in vars(ln)


def test_dir_is_superset_of_all():
    d = set(dir(ln))
    assert set(ln.__all__) <= d


def test_dir_includes_submodule_names():
    """`concurrency`, `fuzzy`, `types` are submodule attributes, not in __all__,
    but were part of the pre-change public dir() surface and must stay visible."""
    d = set(dir(ln))
    assert {"concurrency", "fuzzy", "types"} <= d


def test_concurrency_module_accessible_via_attribute():
    import lionagi.ln.concurrency as direct

    assert ln.concurrency is direct


def test_import_lionagi_ln_raises_no_warnings():
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import lionagi.ln; print('RUN_OK')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "RUN_OK"


def _run_and_report(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_bare_import_defers_anyio_and_heavy_ln_submodules():
    code = (
        "import sys, lionagi\n"
        "names = ['anyio', 'lionagi.ln.concurrency', 'lionagi.ln._async_call', "
        "'lionagi.ln._to_list', 'lionagi.ln._utils', 'lionagi.ln._proc']\n"
        "print(','.join(str(n in sys.modules) for n in names))\n"
    )
    out = _run_and_report(code)
    assert out == "False,False,False,False,False,False"


def test_accessing_alcall_loads_concurrency_and_anyio():
    code = (
        "import sys, lionagi.ln as ln\n"
        "_ = ln.alcall\n"
        "print('anyio' in sys.modules, 'lionagi.ln.concurrency' in sys.modules)\n"
    )
    out = _run_and_report(code)
    assert out == "True True"


def test_accessing_to_list_loads_to_list_module_only():
    code = (
        "import sys, lionagi.ln as ln\n"
        "_ = ln.to_list\n"
        "print('lionagi.ln._to_list' in sys.modules, 'anyio' in sys.modules)\n"
    )
    out = _run_and_report(code)
    assert out == "True False"


def test_accessing_now_utc_loads_utils_and_anyio():
    code = (
        "import sys, lionagi.ln as ln\n"
        "_ = ln.now_utc()\n"
        "print('lionagi.ln._utils' in sys.modules, 'anyio' in sys.modules)\n"
    )
    out = _run_and_report(code)
    assert out == "True True"


def test_lcall_does_not_eagerly_load_to_list_module():
    """lcall's own module import must not force _to_list unless a transform is used."""
    code = (
        "import sys\n"
        "from lionagi.ln._list_call import lcall\n"
        "print('lionagi.ln._to_list' in sys.modules)\n"
    )
    out = _run_and_report(code)
    assert out == "False"


def test_spec_module_does_not_eagerly_load_concurrency():
    """lionagi.ln.types.spec must not pull in .concurrency (anyio) at import time."""
    code = (
        "import sys\n"
        "import lionagi.ln.types.spec\n"
        "print('lionagi.ln.concurrency' in sys.modules, 'anyio' in sys.modules)\n"
    )
    out = _run_and_report(code)
    assert out == "False False"


def test_lightweight_types_do_not_pull_in_target_or_composition_layers():
    code = (
        "import sys\n"
        "import lionagi.ln.types\n"
        "prefixes = (\n"
        "    'pydantic', 'sqlalchemy', 'lionagi.state', 'lionagi.studio',\n"
        "    'lionagi.cli', 'lionagi.providers', 'openai', 'anthropic',\n"
        ")\n"
        "loaded = sorted(\n"
        "    name for name in sys.modules\n"
        "    if any(name == prefix or name.startswith(prefix + '.') for prefix in prefixes)\n"
        ")\n"
        "print(loaded)\n"
    )
    out = _run_and_report(code)
    assert out == "[]"


def test_to_list_functional_through_lazy_attribute():
    assert ln.to_list([1, [2, 3], None], flatten=True, dropna=True) == [1, 2, 3]


def test_alcall_params_class_definition_intact():
    params = ln.AlcallParams(retry_attempts=2)
    assert params.retry_attempts == 2


def test_terminate_process_group_resolves_and_is_callable():
    assert callable(ln.terminate_process_group)


def test_spec_default_factory_async_warning_still_fires():
    """Spec.__init__'s async-default-factory warning must still fire correctly
    now that is_coro_func is imported lazily inside the module."""

    async def _factory():
        return 1

    with pytest.warns(UserWarning, match="Async default factories"):
        ln.Spec(int, default_factory=_factory)
