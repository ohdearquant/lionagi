# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Engine preset registry and execution-admission contracts."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from lionagi.cli import _engine_definitions as registry  # noqa: E402
from lionagi.cli import engine as engine_cli  # noqa: E402
from lionagi.studio.scheduler.subprocess import build_argv  # noqa: E402
from lionagi.studio.services import engine_defs, launches, workflow_defs, workflow_run  # noqa: E402
from lionagi.studio.services.workflow_compile import (  # noqa: E402
    compile_workflow_def,
    make_engine_operation,
)


def test_registry_options_and_dynamic_kind_membership(monkeypatch):
    class UnhashableString(str):
        __hash__ = None

    assert {k: v["preset_options"] for k, v in registry.KIND_META.items()} == {
        "research": {},
        "review": {},
        "coding": {
            "test_cmd": {"type": "string", "required": True},
            "export_dir": {"type": "string", "required": False},
        },
        "hypothesis": {"export_dir": {"type": "string", "required": False}},
        "planning": {},
    }
    assert registry._is_registered_engine_kind("research")
    for invalid in (None, True, 42, [], {}, "missing", UnhashableString("research")):
        assert not registry._is_registered_engine_kind(invalid)
    monkeypatch.setitem(registry.KIND_META, "new_kind", {"preset_options": {}})
    assert registry._is_registered_engine_kind("new_kind")
    monkeypatch.delitem(registry.KIND_META, "research")
    assert not registry._is_registered_engine_kind("research")


async def test_unregistered_preset_launch_is_refused_before_any_side_effect(monkeypatch):
    from lionagi.studio.app import create_app

    resolve_def = AsyncMock(return_value={"kind": "retired", "options": {}})
    executable = Mock(side_effect=AssertionError("unexpected executable resolution"))
    build = Mock(side_effect=AssertionError("unexpected argument construction"))
    detached = AsyncMock(side_effect=AssertionError("unexpected invocation or process"))
    db = Mock(side_effect=AssertionError("unexpected invocation database"))
    monkeypatch.setattr(launches, "_resolve_engine_def", resolve_def)
    monkeypatch.setattr(launches, "resolve_li_executable", executable)
    monkeypatch.setattr(launches, "build_argv", build)
    monkeypatch.setattr(launches, "launch_detached_argv", detached)
    monkeypatch.setattr(launches, "StateDB", db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.post(
            "/api/launches/",
            json={
                "action_kind": "engine",
                "action_engine_def": "saved",
                "action_prompt": "a task",
            },
        )
    assert response.status_code == 422
    resolve_def.assert_awaited_once_with("saved")
    executable.assert_not_called()
    build.assert_not_called()
    detached.assert_not_awaited()
    db.assert_not_called()


async def test_unregistered_workflow_preset_is_refused_before_session_or_persistence(monkeypatch):
    from lionagi.session import session as session_module
    from lionagi.studio.app import create_app
    from lionagi.studio.services import workflow_compile

    spec = {
        "nodes": [{"id": "node", "kind": "engine", "config": {"engine_def_id": "saved"}}],
        "edges": [],
    }
    get_workflow = AsyncMock(return_value={"name": "workflow", "spec_json": spec})
    get_preset = AsyncMock(return_value={"kind": "retired", "options": {}})
    create_session = Mock(side_effect=AssertionError("unexpected session"))
    persist = AsyncMock(side_effect=AssertionError("unexpected persistence"))
    make_operation = Mock(side_effect=AssertionError("unexpected engine operation"))
    monkeypatch.setattr(workflow_defs, "get_workflow_def", get_workflow)
    monkeypatch.setattr(engine_defs, "get_engine_def", get_preset)
    monkeypatch.setattr(session_module, "Session", create_session)
    monkeypatch.setattr(workflow_run, "_setup_run_persist", persist)
    monkeypatch.setattr(workflow_compile, "make_engine_operation", make_operation)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.post("/api/workflow-defs/workflow/run", json={})
    assert response.status_code == 422
    assert response.json()["detail"]["node_id"] == "node"
    get_workflow.assert_awaited_once_with("workflow")
    get_preset.assert_awaited_once_with("saved")
    create_session.assert_not_called()
    persist.assert_not_awaited()
    make_operation.assert_not_called()


async def test_registered_preset_with_legacy_option_remains_launchable_and_compilable(monkeypatch):
    preset = {"kind": "research", "options": {"test_cmd": "pytest"}}
    resolve = AsyncMock(return_value=preset)
    detached = AsyncMock(return_value="invocation")
    monkeypatch.setattr(launches, "_resolve_engine_def", resolve)
    monkeypatch.setattr(launches, "resolve_li_executable", lambda: (("li",), None))
    monkeypatch.setattr(launches, "launch_detached_argv", detached)

    result = await launches.launch(
        {"action_kind": "engine", "action_engine_def": "saved", "action_prompt": "a task"}
    )
    assert result["invocation_id"] == "invocation"
    assert "--test-cmd" in detached.await_args.args[0]

    graph, ids = await compile_workflow_def(
        {
            "nodes": [{"id": "node", "kind": "engine", "config": {"engine_def_id": "saved"}}],
            "edges": [],
        },
        resolve_engine_def=resolve,
    )
    assert graph.internal_nodes[ids["node"]].parameters["engine_options"] == {"test_cmd": "pytest"}


@pytest.mark.parametrize("kind", ["research", "review", "coding", "hypothesis", "planning"])
async def test_preset_option_metadata_matches_real_consumers(kind, monkeypatch):
    calls = []

    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run(self, _spec, **kwargs):
            calls.append(kwargs)
            return "done"

    monkeypatch.setattr(registry, "import_engine_class", lambda *_args: FakeEngine)
    monkeypatch.setattr(engine_cli, "import_engine_class", lambda *_args: FakeEngine)
    options = {"test_cmd": "pytest", "export_dir": "output"}
    expected = set(registry.KIND_META[kind]["preset_options"])

    # Exercise scheduler argv and the actual CLI consumer without a child process.
    argv, tmp_path = build_argv(
        {
            "action_kind": "engine",
            "action_agent": kind,
            "action_prompt": "a task",
            "action_engine_options": options,
        },
        {},
        executable_prefix=("li",),
    )
    assert tmp_path is None
    parser = argparse.ArgumentParser()
    engine_cli.add_engine_subparser(parser.add_subparsers())
    args = parser.parse_args([*argv[1:3], "--no-persist", *argv[3:]])
    assert await engine_cli._do_engine_run_bound(args) == 0
    cli_options = set(calls[-1]) & set(options)

    # The workflow operation has its own actual argument selection path.
    operation = make_engine_operation(session=object())
    await operation(context={"prompt": "a task"}, engine_kind=kind, engine_options=options)
    workflow_options = set(calls[-1]) & set(options)
    assert cli_options == workflow_options == expected


def test_registry_mutation_updates_blueprint_availability_and_cli_choices(monkeypatch):
    """All three read the runtime registry rather than a fixed known-kind copy."""
    fake = {
        "cls_path": ("unused_fake_engine", "FakeEngine"),
        "pos_arg": "question",
        "pos_help": "A question for the synthetic kind.",
        "preset_options": {},
    }
    monkeypatch.setitem(registry.KIND_META, "synthetic", fake)
    monkeypatch.delitem(registry.KIND_META, "research")

    descriptors = {row["kind"]: row for row in engine_defs.engine_catalog_blueprints()["items"]}
    assert descriptors["synthetic"]["input"] == {
        "name": "question",
        "help": "A question for the synthetic kind.",
    }
    assert "research" not in descriptors

    new_row = engine_defs._catalog_annotate_row({"id": "new", "kind": "synthetic", "options": "{}"})
    old_row = engine_defs._catalog_annotate_row({"id": "old", "kind": "research", "options": "{}"})
    assert new_row["availability"] == "available"
    assert new_row["reasons"] == []
    assert old_row["availability"] == "unavailable"
    assert old_row["reasons"] == ["kind_not_registered"]

    parser = argparse.ArgumentParser()
    engine_cli.add_engine_subparser(parser.add_subparsers())
    assert parser.parse_args(["engine", "run", "synthetic", "input"]).kind == "synthetic"
    with pytest.raises(SystemExit):
        parser.parse_args(["engine", "run", "research", "input"])


@pytest.mark.parametrize(
    "module",
    [
        "lionagi.studio.services.engine_defs",
        "lionagi.studio.services.launches",
        "lionagi.studio.services.workflow_compile",
    ],
)
def test_catalog_related_imports_are_cycle_free_in_fresh_interpreters(module, tmp_path):
    """Import each entry point first, without reusing the test runner's module cache."""
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join((str(repo), str(repo / "lionhub/src")))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys; "
            "module = importlib.import_module(sys.argv[1]); "
            "definitions = importlib.import_module('lionagi.studio.services.engine_defs'); "
            "assert module.is_registered_engine_kind is definitions.is_registered_engine_kind",
            module,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
