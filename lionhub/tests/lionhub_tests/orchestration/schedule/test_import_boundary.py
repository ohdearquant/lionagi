# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"The schedule parser imports no Studio execution module."

from pathlib import Path

from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


def test_schedule_parser_has_no_studio_imports():
    import ast

    from lionhub.orchestration.schedule import parsing as _schedule_parsing
    from tests.cli._import_guard import resolved_imports

    imported = resolved_imports(Path(_schedule_parsing.__file__), "lionhub.orchestration.schedule")
    assert not {
        name for name in imported if name == "lionagi.studio" or name.startswith("lionagi.studio.")
    }
    tree = ast.parse(Path(_schedule_parsing.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not (isinstance(node.func, ast.Name) and node.func.id == "__import__")
            assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
