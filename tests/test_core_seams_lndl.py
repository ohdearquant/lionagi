# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The remaining LNDL imports and root compatibility catalog are bounded."""

from __future__ import annotations

import ast

from tests._core_imports import ROOT, core_files, package_imports


def test_remaining_core_lndl_imports_are_exactly_the_allowed_sites():
    files = core_files(root=ROOT)
    assert ROOT / "lionagi" / "__init__.py" in files
    assert ROOT / "lionagi" / "_auto.py" in files
    actual = [hit for path in files for hit in package_imports(path, "lndl", root=ROOT)]
    # Hits read "path:line: module"; the line is dropped so edits elsewhere in a file
    # do not move the boundary.
    sites = [(hit.split(":", 1)[0], hit.rsplit(": ", 1)[1]) for hit in actual]
    assert sites == [
        ("lionagi/__init__.py", "lionagi.lndl"),
        ("lionagi/operations/lndl_middle/lndl_middle.py", "lionagi.lndl"),
    ], actual


def test_nine_root_lazy_exports_are_the_explicit_compatibility_catalog():
    source = (ROOT / "lionagi" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    declaration = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_LAZY_MAP"
    )
    catalog = ast.literal_eval(declaration.value)
    actual = {
        name: target
        for name, target in catalog.items()
        if target[0] == "lndl" or target[0].startswith("lndl.")
    }
    names = {
        "LNDLOutput",
        "LNDLError",
        "MissingLvarError",
        "MissingFieldError",
        "TypeMismatchError",
        "InvalidConstructorError",
        "get_lndl_system_prompt",
        "extract_lndl_blocks",
        "normalize_lndl_text",
    }
    assert actual == {name: ("lndl", name) for name in names}


def test_lndl_check_detects_a_reintroduced_import_in_a_temp_copy(tmp_path):
    relative = "lionagi/operations/parse/parse.py"
    copied = tmp_path / relative
    copied.parent.mkdir(parents=True)
    source = (ROOT / relative).read_text(encoding="utf-8")
    copied.write_text(source, encoding="utf-8")
    assert package_imports(copied, "lndl", root=tmp_path) == []

    copied.write_text(
        source + "\nfrom lionagi.lndl.extract import extract_lndl_blocks\n", encoding="utf-8"
    )
    line = source.count("\n") + 2
    assert package_imports(copied, "lndl", root=tmp_path) == [
        f"{relative}:{line}: lionagi.lndl.extract"
    ]
