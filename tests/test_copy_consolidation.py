# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Keep value copies on ln while retaining Pile's required deepcopy memo."""

from __future__ import annotations

import ast
from pathlib import Path


def test_stdlib_copy_imports_stay_at_the_shared_or_memo_aware_boundary():
    root = Path(__file__).resolve().parents[1]
    violations = []
    for source in sorted((root / "lionagi").rglob("*.py")):
        relative = source.relative_to(root).as_posix()
        if relative.startswith("lionagi/ln/"):
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        exempt = set()
        if relative == "lionagi/protocols/generic/pile.py":
            # Only this method needs a caller-supplied memo, which ln.copy
            # deliberately does not accept. Other Pile methods are not exempt.
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "__deepcopy__":
                    exempt.update(id(child) for child in ast.walk(node))
        for node in ast.walk(tree):
            direct = (
                isinstance(node, ast.Import) and any(alias.name == "copy" for alias in node.names)
            ) or (isinstance(node, ast.ImportFrom) and node.module == "copy")
            if direct and id(node) not in exempt:
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, "Use lionagi.ln.copy for value copies: " + ", ".join(violations)
