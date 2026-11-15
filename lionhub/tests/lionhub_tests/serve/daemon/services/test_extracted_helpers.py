"""Run-tag storage, the control-consumer rule and the launch-time engine-definition lookups live in
lionhub; Studio keeps the same objects."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[6]

_PAIRS = (
    [
        ("lionagi.studio.services.run_tags", "lionhub.serve.daemon.services.run_tags_store", name)
        for name in (
            "add_tag",
            "remove_tag",
            "session_ids_with_tags",
            "tags_for_sessions",
            "_ensure_table",
        )
    ]
    + [
        (
            "lionagi.studio.operator.run_control",
            "lionhub.serve.daemon.services.control_consumers",
            name,
        )
        for name in (
            "session_has_control_consumer",
            "_flow_control_poller_state",
            "_runner_drains_controls",
        )
    ]
    + [
        ("lionagi.studio.services.engine_defs", "lionhub.serve.daemon.services.engine_lookup", name)
        for name in ("get_engine_def", "get_engine_def_by_name", "is_registered_engine_kind")
    ]
)


@pytest.mark.parametrize(("old", "new", "name"), _PAIRS)
def test_the_studio_name_is_the_lionhub_object(old, new, name):
    import importlib

    assert getattr(importlib.import_module(old), name) is getattr(
        importlib.import_module(new), name
    )


@pytest.mark.parametrize(
    "path", ["lionagi/studio/services/runs.py", "lionagi/studio/services/sessions.py"]
)
def test_the_daemon_routes_import_neither_studio_module(path):
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            source = ("." * node.level) + (node.module or "")
            assert "run_control" not in source, f"{path} imports {source}"
            assert not (source in {".", "lionagi.studio.services"} and "run_tags" in names), path
            assert not source.endswith("run_tags"), f"{path} imports {source}"


def test_the_launch_route_does_not_import_the_engine_definition_application_module():
    path = "lionagi/studio/services/launches.py"
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            source = ("." * node.level) + (node.module or "")
            assert not source.endswith("engine_defs"), f"{path} imports {source}"
            assert not (source in {".", "lionagi.studio.services"} and "engine_defs" in names), path
