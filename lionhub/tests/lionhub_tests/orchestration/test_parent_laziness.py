# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The orchestration parent activates patterns only when a caller requests them."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]

_CHILD_SETUP = """
import importlib
import importlib.abc
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root), str(root / "lionhub" / "src")]

exports = (
    "spawn_roles",
    "plan",
    "fanout",
    "build_fanout_graph",
    "build_dag_graph",
    "role_node_builder",
    "grant_spawn",
    "normalize_dep_indices",
)
blocked = ("lionhub.orchestration.patterns", "lionagi.agent")
legacy_names = set(exports) | {
    "__all__", "__builtins__", "__cached__", "__doc__", "__file__",
    "__loader__", "__name__", "__package__", "__path__", "__spec__",
    "annotations", "patterns", "prompts",
}

def matches(name, prefixes):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)

assert not [name for name in sys.modules if matches(name, ("lionagi", "lionhub"))]

def assert_cold():
    assert not [name for name in sys.modules if matches(name, blocked)]

def assert_source(module, relative):
    assert Path(module.__file__).resolve() == root / relative, module.__file__

def assert_checkout_modules():
    package_roots = {
        "lionagi": root / "lionagi",
        "lionhub": root / "lionhub" / "src" / "lionhub",
    }
    for name, module in tuple(sys.modules.items()):
        package_root = package_roots.get(name.partition(".")[0])
        source = getattr(module, "__file__", None)
        if package_root is not None and source is not None:
            assert Path(source).resolve().is_relative_to(package_root), (name, source)

class NoPatternOrAgent(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if matches(fullname, blocked):
            raise AssertionError(f"unexpected feature import: {fullname}")
        return None
"""


def _run_fresh(body: str) -> None:
    result = subprocess.run(  # noqa: S603 -- this interpreter and fixed test programs
        [sys.executable, "-I", "-c", _CHILD_SETUP + body, str(_ROOT)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


def test_parent_and_prompts_stay_cold_and_dir_lists_exports() -> None:
    _run_fresh(
        """
sys.meta_path.insert(0, NoPatternOrAgent())
parent = importlib.import_module("lionhub.orchestration")
assert_source(parent, "lionhub/src/lionhub/orchestration/__init__.py")
assert parent.__all__ == exports
assert set(dir(parent)) == legacy_names
assert_cold()

prompts = parent.prompts
assert prompts is importlib.import_module("lionhub.orchestration.prompts")
assert_source(prompts, "lionhub/src/lionhub/orchestration/prompts.py")
assert prompts.DECOMPOSE_INSTRUCTION
assert set(dir(parent)) == legacy_names
assert_cold()
assert_checkout_modules()
"""
    )


@pytest.mark.parametrize(
    ("module_name", "relative_source"),
    [
        (
            "lionhub.orchestration.arguments",
            "lionhub/src/lionhub/orchestration/arguments.py",
        ),
        ("lionhub.serve.diagnostics", "lionhub/src/lionhub/serve/diagnostics.py"),
    ],
    ids=["arguments", "diagnostics"],
)
def test_lightweight_modules_do_not_activate_patterns_or_agent(
    module_name: str, relative_source: str
) -> None:
    _run_fresh(
        f"""
sys.meta_path.insert(0, NoPatternOrAgent())
import {module_name}
module = sys.modules[{module_name!r}]

assert_source(module, {relative_source!r})
assert_cold()
assert_checkout_modules()
"""
    )


def test_from_parent_activates_patterns_and_preserves_all_export_identities() -> None:
    _run_fresh(
        """
parent = importlib.import_module("lionhub.orchestration")
assert_cold()

from lionhub.orchestration import fanout

assert "lionhub.orchestration.patterns" in sys.modules
assert "lionagi.agent" in sys.modules
patterns = sys.modules["lionhub.orchestration.patterns"]
assert_source(parent, "lionhub/src/lionhub/orchestration/__init__.py")
assert_source(patterns, "lionhub/src/lionhub/orchestration/patterns.py")
assert fanout is patterns.fanout is parent.fanout
assert parent.__all__ == exports
assert set(dir(parent)) == legacy_names
for name in exports:
    original = getattr(patterns, name)
    assert getattr(parent, name) is original
assert_checkout_modules()
"""
    )


def test_patterns_attribute_activates_the_canonical_child() -> None:
    _run_fresh(
        """
parent = importlib.import_module("lionhub.orchestration")
assert_cold()
patterns = parent.patterns
from lionhub.orchestration import patterns as imported_patterns

assert patterns is imported_patterns is importlib.import_module("lionhub.orchestration.patterns")
assert_source(patterns, "lionhub/src/lionhub/orchestration/patterns.py")
for name in exports:
    assert getattr(parent, name) is getattr(patterns, name)
assert set(dir(parent)) == legacy_names
assert_checkout_modules()
"""
    )


@pytest.mark.parametrize("parent_first", [True, False], ids=["parent-first", "patterns-first"])
def test_explicit_patterns_import_keeps_the_same_module(parent_first: bool) -> None:
    _run_fresh(
        f"""
if {parent_first!r}:
    parent = importlib.import_module("lionhub.orchestration")
    assert_cold()

patterns = importlib.import_module("lionhub.orchestration.patterns")
parent = importlib.import_module("lionhub.orchestration")
from lionhub.orchestration import patterns as imported_patterns

assert patterns is imported_patterns is parent.patterns
assert patterns is sys.modules["lionhub.orchestration.patterns"]
assert_source(parent, "lionhub/src/lionhub/orchestration/__init__.py")
assert_source(patterns, "lionhub/src/lionhub/orchestration/patterns.py")
for name in exports:
    assert getattr(parent, name) is getattr(patterns, name)
assert_checkout_modules()
"""
    )


def test_resolved_parent_export_keeps_normal_module_patch_behavior() -> None:
    _run_fresh(
        """
parent = importlib.import_module("lionhub.orchestration")
assert_cold()
replacement = object()
parent.plan = replacement
parent.fanout
patterns = sys.modules["lionhub.orchestration.patterns"]
assert parent.plan is replacement
parent.plan = patterns.plan
original = parent.plan
assert vars(parent)["plan"] is original is patterns.plan

parent.plan = replacement
assert parent.plan is replacement
parent.plan = original

patterns.plan = replacement
assert parent.plan is original
patterns.plan = original
del parent.plan
assert parent.plan is original
assert vars(parent)["plan"] is original
parent.plan = replacement
importlib.reload(parent)
assert parent.plan is original
assert_source(parent, "lionhub/src/lionhub/orchestration/__init__.py")
assert_source(patterns, "lionhub/src/lionhub/orchestration/patterns.py")
assert_checkout_modules()
"""
    )


def test_unknown_attribute_does_not_activate_patterns_agent_or_sdks() -> None:
    _run_fresh(
        """
sys.meta_path.insert(0, NoPatternOrAgent())
parent = importlib.import_module("lionhub.orchestration")
try:
    parent.not_an_orchestration_export
except AttributeError:
    pass
else:
    raise AssertionError("unknown parent attribute must raise AttributeError")

assert not hasattr(parent, "not_an_orchestration_export")
assert_cold()
assert not [name for name in sys.modules if matches(name, ("openai", "anthropic", "httpx"))]
assert_source(parent, "lionhub/src/lionhub/orchestration/__init__.py")
assert_checkout_modules()
"""
    )
