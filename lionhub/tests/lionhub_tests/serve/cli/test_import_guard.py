# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The import-layering helpers resolve names the way the interpreter does."""

from __future__ import annotations

from pathlib import Path

import pytest

from ._import_guard import (
    _DYNAMIC_COMMAND_IMPORT_PROBES,
    COMMAND_IMPORT_PROBES,
    command_module_imports,
    resolved_imports,
    source_with_line,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "module.py"
    path.write_text(text)
    return path


def test_relative_imports_resolve_against_the_package(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "from ._providers import a\n"
        "from .. import b\n"
        "from ..state.db import c\n"
        "from . import d\n"
        "import e.f\n"
        "from g.h import i\n",
    )
    imported = resolved_imports(path, "lionhub.serve.cli")
    assert {
        "lionhub.serve.cli._providers",
        "lionhub.serve.cli._providers.a",
        "lionhub.serve.b",
        "lionhub.serve.state.db",
        "lionhub.serve.state.db.c",
        "lionhub.serve.cli",
        "lionhub.serve.cli.d",
        "e.f",
        "g.h",
        "g.h.i",
    } <= imported


@pytest.mark.parametrize("probe", COMMAND_IMPORT_PROBES)
def test_every_probe_is_flagged_as_a_command_import(tmp_path: Path, probe: str) -> None:
    imported = resolved_imports(_write(tmp_path, probe + "\n"), "lionhub.serve.cli")
    flagged = command_module_imports(imported)
    assert flagged, probe
    assert all(name.split(".")[3] in {"status", "orchestrate"} for name in flagged)


@pytest.mark.parametrize("line", ["from .status import _x", "from . import status"])
def test_orchestration_relative_import_does_not_name_a_command(tmp_path: Path, line: str) -> None:
    imported = resolved_imports(_write(tmp_path, line + "\n"), "lionhub.orchestration")

    assert "lionhub.orchestration.status" in imported
    assert command_module_imports(imported) == set()


@pytest.mark.parametrize("package", ["lionhub.serve.cli", "lionhub.orchestration"])
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        *zip(
            _DYNAMIC_COMMAND_IMPORT_PROBES,
            (
                "lionhub.serve.cli.status",
                "lionhub.serve.cli.status",
                "lionhub.serve.cli.orchestrate",
                "lionhub.serve.cli.status",
                "{package}.status",
                "{package}.status",
            ),
            strict=True,
        ),
        (
            "from importlib import import_module as load; load('lionhub.serve.cli.status')",
            "lionhub.serve.cli.status",
        ),
        (
            "import importlib as il; il.import_module('.status', 'lionhub.serve.cli')",
            "lionhub.serve.cli.status",
        ),
        (
            "from importlib import import_module; import_module('.status', package=__package__)",
            "{package}.status",
        ),
        (
            "from importlib import import_module; import_module(name='.status')",
            "{package}.status",
        ),
        (
            "__import__(name='', globals=globals(), fromlist=['status'], level=1)",
            "{package}.status",
        ),
    ],
)
def test_literal_dynamic_imports_resolve(tmp_path: Path, package: str, line: str, expected: str):
    imported = resolved_imports(_write(tmp_path, line + "\n"), package)
    assert expected.format(package=package) in imported


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "__import__('lionhub.serve.cli.status', level=0)",
            {"lionhub.serve.cli.status"},
        ),
        (
            "__import__('lionhub.serve.cli.status', level=depth)",
            set(),
        ),
        (
            "__import__('lionhub.serve.cli', fromlist=['status', 'orchestrate'], level=0)",
            {"lionhub.serve.cli", "lionhub.serve.cli.status", "lionhub.serve.cli.orchestrate"},
        ),
        (
            "__import__('lionhub.serve.cli', fromlist=targets)",
            {"lionhub.serve.cli"},
        ),
    ],
)
def test_builtin_import_reports_statically_known_targets(
    tmp_path: Path, line: str, expected: set[str]
) -> None:
    assert resolved_imports(_write(tmp_path, line + "\n"), "lionhub.orchestration") == expected


@pytest.mark.parametrize(
    "line",
    [
        "import importlib; importlib.import_module(target)",
        "from importlib import import_module; import_module('.status', custom_package)",
        "import loader; loader.import_module('lionhub.serve.cli.status')",
        "def import_module(name): pass\nimport_module('lionhub.serve.cli.status')",
        "import importlib; importlib.import_module('....status', 'lionhub.serve.cli')",
    ],
)
def test_unresolved_or_unrelated_calls_do_not_report_command_imports(tmp_path: Path, line: str):
    imported = resolved_imports(_write(tmp_path, line + "\n"), "lionhub.serve.cli")
    assert command_module_imports(imported) == set()


@pytest.mark.parametrize(
    "line",
    [
        "from ._providers import add_common_cli_args",
        "from lionagi.state.db import StateDB",
        "from . import _providers",
        "import json",
    ],
)
def test_shared_layer_imports_are_not_flagged(tmp_path: Path, line: str) -> None:
    imported = resolved_imports(_write(tmp_path, line + "\n"), "lionhub.serve.cli")
    assert imported
    assert command_module_imports(imported) == set()


def test_source_with_line_appends_to_a_copy(tmp_path: Path) -> None:
    original = _write(tmp_path, "import json\n")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    copy = source_with_line(original, "from . import status", copy_dir)
    assert copy != original
    assert original.read_text() == "import json\n"
    assert copy.read_text().splitlines()[-1] == "from . import status"
