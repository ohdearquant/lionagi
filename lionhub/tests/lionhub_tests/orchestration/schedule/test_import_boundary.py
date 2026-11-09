# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"The schedule parser imports no Studio execution module."

from pathlib import Path


def test_schedule_parser_has_no_studio_imports():
    import ast

    from lionhub.orchestration.schedule import parsing as _schedule_parsing

    tree = ast.parse(Path(_schedule_parsing.__file__).read_text(encoding="utf-8"))
    package = "lionhub.orchestration.schedule".split(".")
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = ("lionagi.studio", "lionagi.cli", "fastapi", "pydantic")
    assert not {
        name
        for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not (isinstance(node.func, ast.Name) and node.func.id == "__import__")
            assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")


def test_quick_create_parser_builds_and_rejects_unknown_flags():
    import argparse

    import pytest

    from lionhub.orchestration.schedule.parsing import (
        QUICK_CREATE_KINDS,
        build_quick_create_parser,
    )

    required = {
        "agent": ["--profile", "reviewer"],
        "flow": ["--file", "flow.yaml"],
        "playbook": ["--playbook", "nightly"],
        "command": [],
    }
    for kind in QUICK_CREATE_KINDS:
        parser = build_quick_create_parser(kind)
        args = parser.parse_args(["release-check", *required[kind], "--at", "2026-07-15T09:00:00Z"])
        assert args.name == "release-check"
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["release-check", *required[kind], "--at", "2026-07-15T09:00:00Z", "--unknown"]
            )
