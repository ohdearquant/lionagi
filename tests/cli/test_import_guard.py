# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The import-layering helpers resolve names the way the interpreter does."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.cli._import_guard import (
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
    imported = resolved_imports(path, "lionagi.cli")
    assert {
        "lionagi.cli._providers",
        "lionagi.cli._providers.a",
        "lionagi.b",
        "lionagi.state.db",
        "lionagi.state.db.c",
        "lionagi.cli",
        "lionagi.cli.d",
        "e.f",
        "g.h",
        "g.h.i",
    } <= imported


@pytest.mark.parametrize("probe", COMMAND_IMPORT_PROBES)
def test_every_probe_is_flagged_as_a_command_import(tmp_path: Path, probe: str) -> None:
    imported = resolved_imports(_write(tmp_path, probe + "\n"), "lionagi.cli")
    flagged = command_module_imports(imported)
    assert flagged, probe
    assert all(name.split(".")[2] in {"status", "orchestrate"} for name in flagged)


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
    imported = resolved_imports(_write(tmp_path, line + "\n"), "lionagi.cli")
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
