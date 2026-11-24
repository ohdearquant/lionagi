# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Trusted project ``.lionagi`` directories (ADR-0088 Amendment 2): the setting, the record,
the check, and the project settings layer that the check guards."""

from __future__ import annotations

import os
import pickle
from pathlib import Path

import pytest
import yaml

from lionagi import _lionagi_dir_trust as trust
from lionagi._settings import load_settings


@pytest.fixture
def world(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(trust.GATE_ENV_VAR, raising=False)
    monkeypatch.chdir(project)
    return home, project


def _user_settings(home: Path, text: str) -> None:
    (home / ".lionagi" / "settings.yaml").write_text(text)


def _trust(home: Path, *dirs: Path, gate: str | None = "mcp") -> None:
    data: dict = {trust.RECORD_KEY: [os.path.realpath(d) for d in dirs]}
    if gate is not None:
        data[trust.GATE_SETTING] = {"gate": gate}
    _user_settings(home, yaml.safe_dump(data))


# The setting


def test_the_gate_is_off_when_the_file_or_the_block_is_absent(world):
    home, _ = world
    assert trust.gate_setting() == "off"
    _user_settings(home, "other: 1\n")
    assert trust.gate_setting() == "off"


def test_a_bare_yaml_off_reads_as_off_not_as_an_invalid_value(world):
    home, _ = world
    # YAML 1.1 parses a bare `off` as false; the setting must still mean off.
    _user_settings(home, "lionagi_dir_trust:\n  gate: off\n")
    assert yaml.safe_load((home / ".lionagi" / "settings.yaml").read_text()) == {
        "lionagi_dir_trust": {"gate": False}
    }
    assert trust.gate_setting() == "off"


def test_the_gate_reads_mcp(world):
    home, _ = world
    _user_settings(home, "lionagi_dir_trust:\n  gate: mcp\n")
    assert trust.gate_setting() == "mcp"


@pytest.mark.parametrize(
    "text",
    (
        "lionagi_dir_trust:\n  gate: on\n",
        "lionagi_dir_trust:\n  gate: everything\n",
        "lionagi_dir_trust: mcp\n",
        "lionagi_dir_trust: [\n",
    ),
)
def test_an_unreadable_or_invalid_setting_raises_instead_of_guessing_off(world, text):
    home, _ = world
    _user_settings(home, text)
    with pytest.raises(trust.LionagiDirTrustSettingError):
        trust.gate_setting()


# Which processes check


def test_a_process_checks_only_with_the_variable_or_inside_enforced(world, monkeypatch):
    assert not trust.gate_active()
    with trust.enforced(False):
        assert not trust.gate_active()
    with trust.enforced():
        assert trust.gate_active()
    assert not trust.gate_active()
    monkeypatch.setenv(trust.GATE_ENV_VAR, "1")
    assert trust.gate_active()


def test_the_variable_alone_gates_whatever_the_setting_says(world, monkeypatch):
    home, project = world
    _user_settings(home, "lionagi_dir_trust:\n  gate: off\n")
    monkeypatch.setenv(trust.GATE_ENV_VAR, "1")
    with pytest.raises(trust.UntrustedLionagiDirError):
        trust.check_lionagi_dir(
            "agent profile", project / ".lionagi" / "a.md", project / ".lionagi"
        )


# The check


def test_an_ungated_process_checks_nothing(world):
    _, project = world
    trust.check_lionagi_dir("playbook", project / ".lionagi" / "p", project / ".lionagi")


def test_the_refusal_names_the_file_and_the_directory_by_resolved_path(world):
    _, project = world
    lionagi_dir = project / ".lionagi"
    path = lionagi_dir / "agents" / "x.md"
    with trust.enforced(), pytest.raises(trust.UntrustedLionagiDirError) as caught:
        trust.check_lionagi_dir("agent profile", path, lionagi_dir)
    real_dir = os.path.realpath(lionagi_dir)
    assert str(caught.value) == (
        f"agent profile {os.path.realpath(path)} comes from {real_dir}, which is not a trusted "
        ".lionagi directory; this run was started through the MCP server with "
        "lionagi_dir_trust.gate set to mcp. Run `li lionagi-dir trust "
        f"{os.path.dirname(real_dir)}` to trust it, or start the run from a directory without "
        "project files."
    )


def test_the_refusal_is_no_error_a_resolver_falls_back_on():
    assert not issubclass(trust.UntrustedLionagiDirError, (ValueError, OSError))


def test_the_refusal_survives_a_process_boundary(world):
    _, project = world
    err = trust.UntrustedLionagiDirError(
        "playbook", project / ".lionagi" / "p", project / ".lionagi"
    )
    copy = pickle.loads(pickle.dumps(err))
    assert str(copy) == str(err)
    assert copy.lionagi_dir == err.lionagi_dir


def test_a_trusted_directory_passes_and_the_user_directory_is_never_gated(world):
    home, project = world
    _trust(home, project / ".lionagi")
    with trust.enforced():
        trust.check_lionagi_dir("playbook", project / ".lionagi" / "p", project / ".lionagi")
        trust.check_lionagi_dir("agent profile", home / ".lionagi" / "a.md", home / ".lionagi")
    assert trust.is_trusted(home / ".lionagi")


def test_a_link_to_a_trusted_directory_is_checked_as_its_target(world, tmp_path):
    home, _ = world
    shared = tmp_path / "shared" / ".lionagi"
    shared.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (other / ".lionagi").symlink_to(shared, target_is_directory=True)
    _trust(home, shared)
    assert trust.is_trusted(other / ".lionagi")


def test_a_recorded_path_later_replaced_by_a_link_does_not_carry_its_trust(world, tmp_path):
    home, project = world
    recorded = tmp_path / "was-a-directory" / ".lionagi"
    recorded.mkdir(parents=True)
    _trust(home, recorded)
    recorded.rmdir()
    recorded.symlink_to(project / ".lionagi", target_is_directory=True)
    assert not trust.is_trusted(recorded)


def test_trusting_a_parent_admits_no_child_and_the_reverse(world):
    home, project = world
    child = project / "sub" / ".lionagi"
    child.mkdir(parents=True)
    _trust(home, project / ".lionagi")
    assert not trust.is_trusted(child)
    _trust(home, child)
    assert not trust.is_trusted(project / ".lionagi")


def test_an_unreadable_record_trusts_nothing(world):
    home, project = world
    _user_settings(home, f"{trust.RECORD_KEY}: [\n")
    assert trust.trusted_lionagi_dirs() == ()
    assert not trust.is_trusted(project / ".lionagi")


def test_a_trust_step_refuses_only_with_the_variable(world, monkeypatch):
    assert trust.trust_step_refusal("hooks trust") is None
    monkeypatch.setenv(trust.GATE_ENV_VAR, "1")
    assert trust.trust_step_refusal("hooks trust") == (
        "li hooks trust refuses to run inside a run the MCP server started "
        "(LIONAGI_DIR_TRUST_GATE is set); run it from your own shell."
    )


# The project settings layer

_SETTINGS_FILES = {
    "notify": "notify:\n  on_terminal:\n    adapter:\n      kind: exec\n      argv: [echo, done]\n",
    "dispatch": "dispatch:\n  notify_template: [echo, '{run_id}']\n",
    "hooks_external": (
        "hooks_external:\n  PreToolUse:\n    - hooks:\n        - type: command\n"
        "          command: [echo, hi]\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_SETTINGS_FILES))
def test_a_gated_process_refuses_an_untrusted_settings_layer_whatever_it_holds(
    world, monkeypatch, name
):
    home, project = world
    settings = project / ".lionagi" / "settings.yaml"
    settings.write_text(_SETTINGS_FILES[name])
    assert load_settings(project_dir=project) == yaml.safe_load(_SETTINGS_FILES[name])
    monkeypatch.setenv(trust.GATE_ENV_VAR, "1")
    with pytest.raises(trust.UntrustedLionagiDirError) as caught:
        load_settings(project_dir=project)
    assert str(caught.value).startswith(f"project settings {os.path.realpath(settings)} comes ")
    # The global layer alone is still readable.
    assert load_settings(project_dir=project, include_project=False) == {}
    _trust(home, project / ".lionagi", gate=None)
    loaded = load_settings(project_dir=project)
    assert set(yaml.safe_load(_SETTINGS_FILES[name])) <= set(loaded)
