# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent settings loading and hook resolution security."""

import pytest

from lionagi.agent.config import AgentConfig
from lionagi.agent.settings import apply_hooks_from_settings, load_settings


def test_load_settings_skips_project_settings_when_untrusted(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".lionagi").mkdir(parents=True)
    (project / ".lionagi" / "settings.yaml").write_text(
        "hooks:\n  pre:\n    bash:\n      - python: lionagi.agent.hooks:guard_destructive\n"
    )
    monkeypatch.setenv("HOME", str(home))

    assert load_settings(project, include_project=False) == {}


def test_load_settings_includes_project_settings_when_trusted(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".lionagi").mkdir(parents=True)
    (project / ".lionagi" / "settings.yaml").write_text(
        "hooks:\n  pre:\n    bash:\n      - python: lionagi.agent.hooks:guard_destructive\n"
    )
    monkeypatch.setenv("HOME", str(home))

    settings = load_settings(project, include_project=True)

    assert settings["hooks"]["pre"]["bash"][0]["python"] == (
        "lionagi.agent.hooks:guard_destructive"
    )


def test_apply_hooks_rejects_untrusted_python_modules():
    settings = {"hooks": {"pre": {"bash": [{"python": "os:path"}]}}}

    with pytest.raises(PermissionError, match="Untrusted hook module"):
        apply_hooks_from_settings(AgentConfig(), settings)


def test_apply_hooks_rejects_shell_string_commands():
    settings = {"hooks": {"pre": {"bash": [{"command": "echo unsafe"}]}}}

    with pytest.raises(ValueError, match="argv list"):
        apply_hooks_from_settings(AgentConfig(), settings)


# ---------------------------------------------------------------------------
# A1: deep merge of global + project settings
# ---------------------------------------------------------------------------


def test_load_settings_deep_merges_global_and_project_settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text(
        'hooks:\n  pre:\n    bash:\n      - command: ["guard", "global"]\n'
    )
    (project / ".lionagi").mkdir(parents=True)
    (project / ".lionagi" / "settings.yaml").write_text(
        'hooks:\n  pre:\n    bash:\n      - command: ["guard", "local"]\npermissions:\n  mode: rules\n'
    )
    monkeypatch.setenv("HOME", str(home))

    settings = load_settings(project, include_project=True)

    bash_hooks = settings["hooks"]["pre"]["bash"]
    assert len(bash_hooks) == 2
    assert bash_hooks[0]["command"] == ["guard", "global"]
    assert bash_hooks[1]["command"] == ["guard", "local"]
    assert settings["permissions"]["mode"] == "rules"


# ---------------------------------------------------------------------------
# A2: cwd parent discovery
# ---------------------------------------------------------------------------


def test_load_settings_discovers_parent_project_settings_from_cwd(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    project = tmp_path / "project"
    sub_dir = project / "sub" / "dir"
    sub_dir.mkdir(parents=True)
    (project / ".lionagi").mkdir(parents=True)
    (project / ".lionagi" / "settings.yaml").write_text("x:\n  y: 1\n")

    monkeypatch.chdir(sub_dir)
    settings = load_settings(project_dir=None, include_project=True)

    assert settings.get("x", {}).get("y") == 1


# ---------------------------------------------------------------------------
# A3: untrusted python hook rejected via explicit trusted_hook_modules
# ---------------------------------------------------------------------------


def test_apply_hooks_from_settings_rejects_untrusted_python_hook():
    settings = {"hooks": {"pre": {"bash": [{"python": "os:path"}]}}}

    with pytest.raises(PermissionError, match="Untrusted hook module"):
        apply_hooks_from_settings(
            AgentConfig(),
            settings,
            trusted_hook_modules={"lionagi.agent.hooks"},
        )


# ---------------------------------------------------------------------------
# A4: shell pre hook raises PermissionError on nonzero subprocess exit
# ---------------------------------------------------------------------------


async def test_shell_pre_hook_raises_permission_error_on_nonzero_exit(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from lionagi.agent.settings import _make_shell_hook

    mock_proc = MagicMock()
    mock_proc.returncode = 7
    mock_proc.communicate = AsyncMock(return_value=(b"", b"blocked"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    hook = _make_shell_hook(["guard", "{file_path}"], "pre", "bash")

    with pytest.raises(PermissionError, match="blocked"):
        await hook("bash", "run", {"file_path": "test.py"})
