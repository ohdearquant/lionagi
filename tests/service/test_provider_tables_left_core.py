# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The CLI providers' tables and ``parse_model_spec`` stay out of the core.

They live in ``lionhub.providers.model_spec``. The core keeps the effort vocabulary and the
model-name suffix grammar in ``lionagi.service.providers``, which forwards the moved names to
the hub, and ``iModel`` reads a provider's effort field from its endpoint class. No core module
imports a moved name, by its old path or its new one, and the core module defines nothing else.
Every check reads source text, so none imports the hub.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests._core_imports import ROOT, package_imports

PROVIDERS = ROOT / "lionagi" / "service" / "providers.py"
MODEL_SPEC = ROOT / "lionhub" / "src" / "lionhub" / "providers" / "model_spec.py"
KEPT = frozenset({"EFFORT_LEVELS", "normalize_effort", "_EFFORT_SUFFIX_RE", "split_effort_suffix"})
FORWARD = frozenset({"__all__", "_MODEL_SPEC_OLD_MODULE", "_MODEL_SPEC_NAMES", "__getattr__"})


def _defined(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _literal(path: Path, name: str):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Call):
                value = value.args[0]
            return ast.literal_eval(value)
    raise AssertionError(f"{path.name} assigns no {name}")


def _moved_imports(path: Path, source: str | None = None) -> list[str]:
    hits = []
    for name in sorted(_literal(PROVIDERS, "_MODEL_SPEC_NAMES")):
        hits += package_imports(path, f"service.providers.{name}", source)
    hits += package_imports(path, "service._model_spec", source)
    hits += package_imports(path, "providers.model_spec", source, top="lionhub")
    return hits


def test_the_core_module_defines_only_the_vocabulary_and_the_forward():
    assert _defined(PROVIDERS) == KEPT | FORWARD


def test_the_forward_serves_exactly_what_the_hub_module_defines():
    forwarded = _literal(PROVIDERS, "_MODEL_SPEC_NAMES")
    exported = set(_literal(PROVIDERS, "__all__"))
    assert len(exported) == 17
    assert exported - forwarded == {"EFFORT_LEVELS", "normalize_effort", "split_effort_suffix"}
    assert not forwarded & KEPT
    # ``_overlap`` is a module-level check the hub module deletes once it has run.
    assert forwarded == _defined(MODEL_SPEC) - {"__all__", "_overlap"}


def test_no_core_module_imports_a_moved_name():
    files = [path for path in sorted((ROOT / "lionagi").rglob("*.py")) if path.name != "_moved.py"]
    imodel = ROOT / "lionagi" / "service" / "imodel.py"
    # The scan reaches the core's one importer of the kept grammar, so an empty result counts.
    assert imodel in files
    assert package_imports(imodel, "service.providers.split_effort_suffix")
    hits = [hit for path in files for hit in _moved_imports(path)]
    assert hits == [], hits


def test_the_scan_sees_each_form_of_a_moved_import_and_passes_the_kept_names():
    probe = ROOT / "lionagi" / "service" / "connections" / "_probe.py"
    source = (
        "from ..providers import PROVIDER_EFFORT_KWARG\n"
        "from lionagi.service.providers import _clamp_claude_effort\n"
        "from lionagi.service import _model_spec\n"
        "import lionhub.providers.model_spec\n"
        "from lionhub.providers.model_spec import BACKENDS\n"
        "importlib.import_module('lionagi.service._model_spec')\n"
        "def local():\n"
        "    from ..providers import parse_model_spec\n"
        "from ..providers import normalize_effort, split_effort_suffix\n"
        "from lionhub.providers import model_specs\n"
        "from ..providers import EFFORT_LEVELS\n"
    )
    hits = _moved_imports(probe, source)
    assert sorted(int(hit.split(":")[1]) for hit in hits) == [1, 2, 3, 4, 5, 6, 8], hits
