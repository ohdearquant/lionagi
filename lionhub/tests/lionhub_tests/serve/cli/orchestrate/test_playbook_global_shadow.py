# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A project-local playbook that hides the user's global one: the warning, the run record,
and the ``global/NAME`` spelling that runs the global file."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml

import lionhub.serve.cli.orchestrate as orch
from lionhub.serve.cli.orchestrate import (
    _playbook_resolution,
    _resolve_playbook_path,
    flow,
    run_orchestrate,
)

from .test_flow_phases import _make_env
from .test_flow_spec_file import _parse_flow_args


@pytest.fixture
def warnings(monkeypatch):
    """Every console warning the resolver prints, in order."""
    seen: list[str] = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", seen.append)
    return seen


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A home with a global ``.lionagi`` and a project directory, the project as cwd."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".lionagi" / "playbooks").mkdir(parents=True)
    (project / ".lionagi" / "playbooks").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    return home / ".lionagi" / "playbooks", project / ".lionagi" / "playbooks"


def _playbook(directory, name, prompt):
    path = directory / f"{name}.playbook.yaml"
    path.write_text(yaml.dump({"model": "codex/gpt-4o", "prompt": prompt}))
    return path


# ---------------------------------------------------------------- the warning


def test_a_project_playbook_that_hides_the_global_one_warns(layout, warnings):
    global_dir, project_dir = layout
    global_file = _playbook(global_dir, "shared", "global")
    local_file = _playbook(project_dir, "shared", "project")

    path, err = _resolve_playbook_path("shared")

    assert err is None
    assert path == local_file
    assert len(warnings) == 1
    assert str(local_file) in warnings[0]
    assert str(global_file) in warnings[0]
    assert "using the project-local file" in warnings[0]
    assert "'global/shared'" in warnings[0]


def test_a_project_playbook_with_no_global_twin_does_not_warn(layout, warnings):
    _global_dir, project_dir = layout
    _playbook(project_dir, "solo", "project")

    path, err = _resolve_playbook_path("solo")

    assert err is None
    assert path == project_dir / "solo.playbook.yaml"
    assert warnings == []


def test_a_global_playbook_with_no_project_twin_does_not_warn(layout, warnings):
    global_dir, _project_dir = layout
    global_file = _playbook(global_dir, "mine", "global")

    path, err = _resolve_playbook_path("mine")

    assert err is None
    assert path == global_file
    assert warnings == []


# ------------------------------------------------------------ global/NAME


def test_the_global_token_runs_the_global_file_past_a_project_one(layout, warnings):
    global_dir, project_dir = layout
    global_file = _playbook(global_dir, "shared", "global")
    _playbook(project_dir, "shared", "project")

    path, err = _resolve_playbook_path("global/shared")

    assert err is None
    assert path == global_file
    assert warnings == []


def test_the_global_token_without_a_global_file_names_where_it_looked(layout):
    global_dir, project_dir = layout
    _playbook(project_dir, "shared", "project")

    path, err = _resolve_playbook_path("global/shared")

    assert path is None
    assert "playbook not found" in err
    assert str(global_dir) in err


def test_the_global_token_refuses_a_link_out_of_the_playbooks_directory(layout, tmp_path):
    global_dir, _project_dir = layout
    outside = tmp_path / "outside.yaml"
    outside.write_text("prompt: elsewhere\n")
    (global_dir / "linked.playbook.yaml").symlink_to(outside)

    path, err = _resolve_playbook_path("global/linked")

    assert path is None
    assert "symlink escape blocked" in err


@pytest.mark.parametrize("name", ["global/../x", "global/.hidden", "global/a/b"])
def test_the_global_token_keeps_the_name_checks(layout, name):
    path, err = _resolve_playbook_path(name)

    assert path is None
    assert err


def test_a_plugin_named_global_keeps_its_token(layout, warnings, monkeypatch, tmp_path):
    global_dir, _project_dir = layout
    global_file = _playbook(global_dir, "shared", "global")
    _playbook(global_dir, "other", "global")
    plugin_file = tmp_path / "plugin" / "shared.playbook.yaml"
    plugin_file.parent.mkdir()
    plugin_file.write_text("prompt: plugin\n")
    monkeypatch.setattr(
        orch, "_plugin_playbook_files", lambda: {"global/shared": ("global", plugin_file)}
    )

    path, err = _resolve_playbook_path("global/shared")

    assert err is None
    assert path == plugin_file
    assert len(warnings) == 1
    assert str(global_file) in warnings[0]
    assert "is not used" in warnings[0]

    # A name the plugin does not declare still reaches the global file.
    warnings.clear()
    path, err = _resolve_playbook_path("global/other")

    assert err is None
    assert path == global_dir / "other.playbook.yaml"
    assert warnings == []


# ------------------------------------------------------------ the run record


def test_the_resolution_names_the_global_file_a_project_file_hid(layout):
    global_dir, project_dir = layout
    global_file = _playbook(global_dir, "shared", "global")
    local_file = _playbook(project_dir, "shared", "project")

    assert _playbook_resolution("shared", local_file) == {
        "playbook_path": str(local_file),
        "playbook_shadowed": str(global_file),
    }


def test_the_resolution_of_the_global_file_names_no_shadow(layout):
    global_dir, _project_dir = layout
    global_file = _playbook(global_dir, "mine", "global")

    assert _playbook_resolution("mine", global_file) == {"playbook_path": str(global_file)}
    assert _playbook_resolution("global/mine", global_file) == {"playbook_path": str(global_file)}


def test_the_resolution_of_a_plugin_token_names_no_shadow(layout, tmp_path):
    global_dir, _project_dir = layout
    _playbook(global_dir, "research", "global")
    plugin_file = tmp_path / "research.playbook.yaml"
    plugin_file.write_text("prompt: plugin\n")

    assert _playbook_resolution("web/research", plugin_file) == {"playbook_path": str(plugin_file)}


def test_a_play_hands_the_resolution_to_the_flow(layout, warnings):
    global_dir, project_dir = layout
    global_file = _playbook(global_dir, "shared", "global")
    local_file = _playbook(project_dir, "shared", "project")
    args = _parse_flow_args(["-p", "shared", "do it"])

    with patch(
        "lionhub.serve.cli.orchestrate._run_flow",
        AsyncMock(return_value=("done", "completed")),
    ) as run_flow:
        code = run_orchestrate(args)

    assert code == 0
    assert run_flow.call_args.kwargs["playbook_resolution"] == {
        "playbook_path": str(local_file),
        "playbook_shadowed": str(global_file),
    }


def test_a_global_token_play_runs_and_records_the_global_file(layout, warnings):
    global_dir, project_dir = layout
    global_file = _playbook(global_dir, "shared", "from the global file")
    _playbook(project_dir, "shared", "from the project file")
    args = _parse_flow_args(["-p", "global/shared", "do it"])

    with patch(
        "lionhub.serve.cli.orchestrate._run_flow",
        AsyncMock(return_value=("done", "completed")),
    ) as run_flow:
        code = run_orchestrate(args)

    assert code == 0
    kwargs = run_flow.call_args.kwargs
    assert kwargs["playbook_resolution"] == {"playbook_path": str(global_file)}
    assert kwargs["prompt"].startswith("from the global file")


class _PersistReached(Exception):
    pass


async def test_the_flow_writes_the_resolution_into_the_session_record(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    env.run.run_id = "test-run"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None
    seen: dict = {}

    async def start(env, **kwargs):
        seen.update(kwargs)
        raise _PersistReached

    monkeypatch.setattr(flow, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(flow, "start_live_persist", start)
    resolution = {"playbook_path": "/p/shared.playbook.yaml", "playbook_shadowed": "/g/x"}

    with pytest.raises(_PersistReached):
        await flow._run_flow(
            "fake/model", "work", playbook_name="shared", playbook_resolution=resolution
        )

    assert seen["invocation_kind"] == "play"
    assert seen["extra_node_metadata"]["playbook_path"] == "/p/shared.playbook.yaml"
    assert seen["extra_node_metadata"]["playbook_shadowed"] == "/g/x"
    assert seen["extra_node_metadata"]["run_id"] == "test-run"


async def test_a_flow_without_a_playbook_records_no_playbook_keys(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    env.run.run_id = "test-run"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None
    seen: dict = {}

    async def start(env, **kwargs):
        seen.update(kwargs)
        raise _PersistReached

    monkeypatch.setattr(flow, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(flow, "start_live_persist", start)

    with pytest.raises(_PersistReached):
        await flow._run_flow("fake/model", "work")

    assert "playbook_path" not in seen["extra_node_metadata"]
    assert "playbook_shadowed" not in seen["extra_node_metadata"]
