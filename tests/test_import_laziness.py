# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Import-time laziness regression gate.

Guards against a specific regression class: lazy-import work that gets
silently undone by a new top-level import. ``lionagi/__init__.py`` uses a
``_LAZY_MAP`` + ``__getattr__`` scheme (see ``lionagi.ln._lazy_init``) so a
bare ``import lionagi`` stays cheap -- provider SDKs, the CLI command tree,
and the flow engine are only pulled in once a caller actually touches them.

It is easy for a future change (a stray top-level import added to satisfy a
type checker, a convenience re-export, a "just import it eagerly" shortcut
somewhere deep in the import graph) to defeat that laziness without anyone
noticing, because the symptom only shows up as slower startup and heavier
memory for every consumer of the package -- it does not raise or fail a
normal unit test. This module runs ``import lionagi`` in a fresh subprocess
(so no other test can have already warmed ``sys.modules``) and pins today's
known-good set of modules and timing, so a regression trips CI immediately
instead of shipping silently.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# Modules (or module prefixes) that must NOT be present in sys.modules after
# a bare `import lionagi` in a fresh interpreter. Verified empirically against
# current main: none of these are pulled in by the lazy `_LAZY_MAP` +
# `__getattr__` scheme in `lionagi/__init__.py`.
_FORBIDDEN_MODULE_PREFIXES: tuple[str, ...] = (
    "lionagi.providers.",
    "lionagi.cli",
    "lionagi.operations.flow",
)

# Third-party packages that a bare `import lionagi` does not need today.
# Verified empirically against the current lazy-import surface -- none of
# these show up in sys.modules after `import lionagi` on a fresh interpreter.
_FORBIDDEN_THIRD_PARTY: tuple[str, ...] = (
    "openai",
    "anthropic",
    "httpx",
)

# Generous wall-clock ceiling for a bare `import lionagi` in a fresh
# interpreter. Measured baseline in this environment: ~0.9s with a cold
# bytecode cache, ~0.15s warm. 5s leaves well over 3x headroom on the cold
# figure so the assertion tolerates a slower/loaded CI runner without going
# flaky, while still catching a regression that makes import materially
# heavier (e.g. an eagerly-imported provider SDK or the CLI command tree).
_MAX_IMPORT_SECONDS = 5.0

_PROBE_SCRIPT = """
import json
import sys
import time

t0 = time.perf_counter()
import lionagi
elapsed = time.perf_counter() - t0

print(json.dumps({"elapsed": elapsed, "modules": sorted(sys.modules.keys())}))
"""


@pytest.fixture(scope="module")
def import_probe() -> dict:
    """Run `import lionagi` once in a fresh subprocess; share the result."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"import lionagi failed in a fresh subprocess (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


def test_import_lionagi_does_not_pull_heavy_modules(import_probe: dict):
    """A bare `import lionagi` must not eagerly load provider/CLI/flow modules."""
    modules = import_probe["modules"]

    offenders = [
        m for m in modules if any(m == p or m.startswith(p) for p in _FORBIDDEN_MODULE_PREFIXES)
    ]
    assert not offenders, (
        "import lionagi pulled in modules that should stay lazy: "
        f"{offenders}. This means lazy-import work in lionagi/__init__.py "
        "(the _LAZY_MAP + __getattr__ scheme) has been silently undone by a "
        "new top-level import somewhere on the import path."
    )

    third_party_offenders = [
        pkg
        for pkg in _FORBIDDEN_THIRD_PARTY
        if any(m == pkg or m.startswith(pkg + ".") for m in modules)
    ]
    assert not third_party_offenders, (
        "import lionagi pulled in third-party packages that should stay "
        f"lazy: {third_party_offenders}. These are only needed once a "
        "caller actually uses the corresponding provider/feature; check "
        "for a new eager top-level import."
    )


def test_import_lionagi_is_fast(import_probe: dict):
    """A bare `import lionagi` must stay well under a generous time ceiling."""
    elapsed = import_probe["elapsed"]
    assert elapsed < _MAX_IMPORT_SECONDS, (
        f"import lionagi took {elapsed:.3f}s in a fresh subprocess, exceeding "
        f"the generous {_MAX_IMPORT_SECONDS}s ceiling. This usually means "
        "something newly eager (a provider SDK, the CLI command tree, the "
        "flow engine, etc.) is being loaded at import time instead of lazily."
    )
