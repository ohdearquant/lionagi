# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Which parts of ``lionagi.service.accounts`` the rest of the core may import.

The routing engine (router, ledger, credentials, pricing) lives in
``lionhub.providers.anthropic.accounts`` and is reached only from the hub's Anthropic adapter.
The core keeps four modules the rest of it reads (the shared types, the routing scope, the
request guard and the usage-shape helpers) and secret resolution.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = "lionagi.service.accounts"
SEAM = frozenset({"_types", "_scope", "_request_guard", "_usage_shape"})
# Every file the core package holds: the seam, secret resolution and the package initializer.
CORE_FILES = frozenset({"__init__.py", "_secret_resolution.py", *(f"{name}.py" for name in SEAM)})


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path) -> set[str]:
    """Absolute names of every module imported anywhere in *path*, relative forms resolved."""
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base)
                target = f"{prefix}.{node.module}" if node.module else prefix
            else:
                target = node.module or ""
            names.add(target)
            names.update(f"{target}.{alias.name}" for alias in node.names)
    return names


def _accounts_modules_used(path: Path) -> set[str]:
    used = set()
    for name in _imported_modules(path):
        if name.startswith(PACKAGE + "."):
            used.add(name[len(PACKAGE) + 1 :].split(".")[0])
    return used


def _core_files_outside_package() -> list[Path]:
    accounts = ROOT / "lionagi" / "service" / "accounts"
    return sorted(
        path
        for path in (ROOT / "lionagi").rglob("*.py")
        if accounts not in path.parents and path.name != "_moved.py"
    )


def test_core_imports_only_the_seam_modules_from_accounts():
    users = {}
    for path in _core_files_outside_package():
        used = _accounts_modules_used(path)
        if used:
            users[str(path.relative_to(ROOT))] = used
    # The scan must see the known core users, or an empty result proves nothing.
    for expected in (
        "lionagi/service/connections/endpoint.py",
        "lionagi/service/imodel.py",
        "lionagi/operations/run/run.py",
        "lionagi/session/signal.py",
    ):
        assert expected in users, (expected, sorted(users))
    outside_seam = {path: sorted(used - SEAM) for path, used in users.items() if used - SEAM}
    assert outside_seam == {}


def test_seam_modules_import_nothing_from_the_engine():
    accounts = ROOT / "lionagi" / "service" / "accounts"
    for name in sorted(SEAM):
        used = _accounts_modules_used(accounts / f"{name}.py")
        assert used <= {"_types"}, (name, sorted(used))


def test_scan_reports_an_engine_import(tmp_path, monkeypatch):
    """The scan flags a relative import of the router from a core module."""
    fake_root = tmp_path
    target = fake_root / "lionagi" / "service" / "connections" / "probe.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f():\n    from ..accounts._router import get_router\n    return get_router\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(__import__(__name__, fromlist=["ROOT"]), "ROOT", fake_root)
    assert _accounts_modules_used(target) == {"_router"}


def test_the_core_package_holds_only_the_seam():
    """No engine module is left in, or comes back to, the core package."""
    accounts = ROOT / "lionagi" / "service" / "accounts"
    files = {path.name for path in accounts.iterdir() if path.suffix == ".py"}
    assert "_types.py" in files, sorted(files)
    assert files == CORE_FILES
