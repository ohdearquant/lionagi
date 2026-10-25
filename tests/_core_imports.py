# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Static imports across the core boundary; never import the inspected modules."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ADR-0136 D6: these are feature adapters or composition packages that move later.
# Retained support and root facades are inspected alongside the five SDK roots.
NON_CORE_PACKAGES = frozenset(
    {
        "agent",
        "casts",
        "cli",
        "hooks",
        "integrations",
        "lndl",
        "mcp",
        "plugins",
        "providers",
        "studio",
        "tools",
    }
)


def core_files(root: Path = ROOT) -> list[Path]:
    package = root / "lionagi"
    return [
        path
        for path in sorted(package.rglob("*.py"))
        if path.relative_to(package).parts[0] not in NON_CORE_PACKAGES
    ]


def _argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((item.value for item in node.keywords if item.arg == keyword), None)


def _resolve(name: str, anchor: str) -> str | None:
    try:
        return resolve_name(name, anchor)
    except (ImportError, ValueError):
        return None


def _from_module(node: ast.ImportFrom, package: str) -> str | None:
    if node.level:
        return _resolve("." * node.level + (node.module or ""), package)
    return node.module


def _literal_call(node: ast.Call, call: str, package: str) -> str | None:
    name = _argument(node, 0, "name")
    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
        return None
    if call == "__import__":
        level = _argument(node, 4, "level")
        if name.value.startswith(".") or (
            level is not None and not (isinstance(level, ast.Constant) and level.value == 0)
        ):
            return None
        return name.value
    if not name.value.startswith("."):
        return name.value
    anchor = _argument(node, 1, "package")
    if isinstance(anchor, ast.Constant) and isinstance(anchor.value, str):
        return _resolve(name.value, anchor.value)
    if isinstance(anchor, ast.Name) and anchor.id == "__package__":
        return _resolve(name.value, package)
    return None


def package_imports(
    path: Path, target: str, source: str | None = None, *, root: Path = ROOT
) -> list[str]:
    """Find statements and literal dynamic calls importing one ``lionagi`` package.

    TYPE_CHECKING and function bodies count. Computed targets are not resolved;
    concrete catalogs and configurable loaders need their own census. Logger and
    persisted class-name strings are data, not imports. Aliases are conservative
    syntactic candidates: this is not an interprocedural binding analysis.
    """
    text = path.read_text(encoding="utf-8") if source is None else source
    package = ".".join(path.relative_to(root).with_suffix("").parts[:-1])
    tree = ast.parse(text, filename=str(path))
    calls = {"import_module": "import_module", "__import__": "__import__"}
    calls.update(
        {
            alias.asname: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and _from_module(node, package) in {"importlib", "lionagi.ln"}
            for alias in node.names
            if alias.name == "import_module" and alias.asname
        }
    )
    prefix = f"lionagi.{target}"
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = _from_module(node, package)
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)] if module else []
        elif isinstance(node, ast.Call):
            callee = getattr(node.func, "attr", getattr(node.func, "id", None))
            name = _literal_call(node, calls[callee], package) if callee in calls else None
            names = [name] if name else []
        else:
            continue
        matches = [name for name in names if name == prefix or name.startswith(prefix + ".")]
        if matches:
            hits.append(f"{path.relative_to(root)}:{node.lineno}: {matches[0]}")
    return hits
