# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Core gate and settings definitions retain their agent-facing contracts."""

from __future__ import annotations

import copy
import inspect
import logging
import pickle

import pytest
import yaml

from lionagi import _settings
from lionagi.agent import gate as agent_gate
from lionagi.agent import settings as agent_settings
from lionagi.protocols.action import gate as core_gate

_GATE_EXPORTS = (
    "ControlUnavailableError",
    "GateDeniedError",
    "GateEvaluator",
    "GateResult",
    "adapt_legacy_hook",
    "adapt_session_gate",
    "run_gate_pass",
)


@pytest.mark.parametrize("name", _GATE_EXPORTS)
def test_agent_gate_exports_are_the_core_objects(name):
    assert agent_gate.__all__ == core_gate.__all__ == _GATE_EXPORTS
    assert getattr(agent_gate, name) is getattr(core_gate, name)


@pytest.mark.parametrize("errored", [False, True], ids=["denial", "fault"])
def test_new_gate_pickles_name_core_and_old_names_still_load(errored):
    for cls in (
        core_gate.GateResult,
        core_gate.GateDeniedError,
        core_gate.ControlUnavailableError,
    ):
        assert cls.__module__ == "lionagi.protocols.action.gate"
    result = core_gate.GateResult(
        False, "policy", "bash", "run", "refused", mutated_args={"cmd": "ls"}, errored=errored
    )
    error = core_gate.GateDeniedError(result)
    error.seen_by = ["post_hook"]
    payload = pickle.dumps(error, protocol=2)
    core_global = b"clionagi.protocols.action.gate\n"
    assert core_global in payload
    old_payload = payload.replace(core_global, b"clionagi.agent.gate\n")
    restored = pickle.loads(old_payload)
    copied = copy.deepcopy(error)
    for clone in (restored, copied):
        assert type(clone) is core_gate.GateDeniedError
        assert type(clone.result) is core_gate.GateResult
        assert clone.result == result
        assert clone.seen_by == ["post_hook"]
        assert str(clone) == str(error)
    assert isinstance(restored, PermissionError)


async def test_core_gate_fault_keeps_logger_and_stops_the_pass(caplog):
    calls = []

    async def unavailable(tool_name, action, args):
        raise core_gate.ControlUnavailableError("backend unavailable")

    async def later(tool_name, action, args):
        calls.append(tool_name)
        return core_gate.GateResult(True, "later", tool_name, action, "allow")

    args = {"action": "read"}
    with caplog.at_level(logging.ERROR, logger="lionagi.agent.gate"):
        actual_args, denied = await core_gate.run_gate_pass(
            [core_gate.adapt_legacy_hook("guard", unavailable), later], "file", "read", args
        )
    assert actual_args is args
    assert denied.allowed is False
    assert denied.errored is True
    assert denied.reason == "backend unavailable"
    assert calls == []
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].name == "lionagi.agent.gate"
    assert "without reaching a verdict" in errors[0].getMessage()


async def test_core_gate_rewrite_reaches_the_next_control():
    rewritten = {"action": "read", "path": "safe.txt"}
    seen = []

    def rewrite(tool_name, action, args):
        return rewritten

    async def deny(tool_name, action, args):
        seen.append((tool_name, action, args))
        return core_gate.GateResult(False, "policy", tool_name, action, "denied")

    actual_args, denied = await core_gate.run_gate_pass(
        [core_gate.adapt_legacy_hook("rewrite", rewrite), deny],
        "file",
        "write",
        {"action": "write", "path": "unsafe.txt"},
    )
    assert actual_args is rewritten
    assert seen == [("file", "read", rewritten)]
    assert denied.allowed is False
    assert denied.errored is False


def test_agent_settings_reexports_the_core_reader():
    assert agent_settings.load_settings is _settings.load_settings
    parameters = inspect.signature(_settings.load_settings).parameters
    assert tuple(parameters) == ("project_dir", "include_project")
    assert parameters["project_dir"].default is None
    assert parameters["include_project"].default is True
    assert parameters["include_project"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("explicit_project", [False, True], ids=["cwd", "explicit"])
def test_core_reader_merges_only_the_nearest_project(tmp_path, monkeypatch, explicit_project):
    home = tmp_path / "home"
    project = tmp_path / "project"
    nested = project / "child"
    work = nested / "work"
    for directory in (home, project, nested):
        (directory / ".lionagi").mkdir(parents=True)
    work.mkdir()
    (home / ".lionagi" / "settings.yaml").write_text("items: [global]\nmode: global\n")
    (project / ".lionagi" / "settings.yaml").write_text("items: [farther]\nfarther: true\n")
    (nested / ".lionagi" / "settings.yaml").write_text("items: [nearest]\nmode: project\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)

    actual = _settings.load_settings(work if explicit_project else None)

    assert actual == {"items": ["global", "nearest"], "mode": "project"}


def test_core_reader_global_only_never_reads_malformed_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".lionagi").mkdir(parents=True)
    (project / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text("user: true\n")
    (project / ".lionagi" / "settings.yaml").write_text("value: [\n")
    monkeypatch.setenv("HOME", str(home))

    assert _settings.load_settings(project, include_project=False) == {"user": True}


@pytest.mark.parametrize("malformed_layer", ["global", "project"])
def test_core_reader_keeps_yaml_errors_visible(tmp_path, monkeypatch, malformed_layer):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".lionagi").mkdir(parents=True)
    (project / ".lionagi").mkdir(parents=True)
    layer = home if malformed_layer == "global" else project
    (layer / ".lionagi" / "settings.yaml").write_text("value: [\n")
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(yaml.YAMLError):
        _settings.load_settings(project)
