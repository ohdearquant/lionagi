# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Core flow requests do not import their former casts implementation."""

from __future__ import annotations

from tests._core_imports import ROOT, core_files, package_imports


def test_core_has_no_casts_imports():
    files = core_files(root=ROOT)
    assert ROOT / "lionagi" / "__init__.py" in files
    assert ROOT / "lionagi" / "_auto.py" in files
    violations = [hit for path in files for hit in package_imports(path, "casts", root=ROOT)]
    assert not violations, "Core imports casts:\n" + "\n".join(violations)


def test_casts_check_detects_a_reintroduced_import_in_a_temp_copy(tmp_path):
    relative = "lionagi/operations/flow.py"
    copied = tmp_path / relative
    copied.parent.mkdir(parents=True)
    source = (ROOT / relative).read_text(encoding="utf-8")
    copied.write_text(source, encoding="utf-8")
    assert package_imports(copied, "casts", root=tmp_path) == []

    mutated = source + "\nfrom lionagi.casts.emission import SpawnRequest\n"
    copied.write_text(mutated, encoding="utf-8")
    line = source.count("\n") + 2
    assert package_imports(copied, "casts", root=tmp_path) == [
        f"{relative}:{line}: lionagi.casts.emission"
    ]
