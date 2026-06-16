# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent settings loading and hook resolution security."""

import pytest

from lionagi.agent.settings import apply_hooks_from_settings, load_settings
from lionagi.agent.spec import AgentSpec


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
        apply_hooks_from_settings(AgentSpec.compose("implementer"), settings)


def test_apply_hooks_rejects_shell_string_commands():
    settings = {"hooks": {"pre": {"bash": [{"command": "echo unsafe"}]}}}

    with pytest.raises(ValueError, match="argv list"):
        apply_hooks_from_settings(AgentSpec.compose("implementer"), settings)


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
            AgentSpec.compose("implementer"),
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


# ---------------------------------------------------------------------------
# LIONAGI-AUDIT-004 (agent-standards 2026-06-06): timed-out hook subprocess
# termination.  The fix kills the process group and awaits cleanup before
# raising; these tests assert kill/wait are called on timeout.
# ---------------------------------------------------------------------------


async def test_pre_hook_timeout_kills_process_group(monkeypatch):
    """On TimeoutError the pre-hook kills the process group (LIONAGI-AUDIT-004)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from lionagi.agent.settings import _make_shell_hook

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    # communicate raises TimeoutError via wait_for
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.wait = AsyncMock(return_value=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    killed: list[int] = []
    waited: list[int] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    def fake_getpgid(pid: int) -> int:
        return pid  # pgid == pid for simplicity

    with (
        patch("lionagi.agent.settings.os.killpg", side_effect=fake_killpg),
        patch("lionagi.agent.settings.os.getpgid", side_effect=fake_getpgid),
    ):
        hook = _make_shell_hook(["slow_guard"], "pre", "bash")
        with pytest.raises(PermissionError, match="timed out"):
            await hook("bash", "run", {})

    assert killed, "Expected killpg to be called after pre-hook timeout"


async def test_post_hook_timeout_kills_process_group(monkeypatch):
    """On TimeoutError the post-hook kills the process group (LIONAGI-AUDIT-004)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from lionagi.agent.settings import _make_shell_hook

    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.wait = AsyncMock(return_value=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    killed: list[int] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    def fake_getpgid(pid: int) -> int:
        return pid

    with (
        patch("lionagi.agent.settings.os.killpg", side_effect=fake_killpg),
        patch("lionagi.agent.settings.os.getpgid", side_effect=fake_getpgid),
    ):
        hook = _make_shell_hook(["slow_notifier"], "post", "bash")
        # Post-hook swallows timeout; must not raise
        result = await hook("bash", "run", {}, {})

    assert result is None
    assert killed, "Expected killpg to be called after post-hook timeout"


# ---------------------------------------------------------------------------
# _kill_proc_group must never signal the init/session process group. A test
# double (or a not-yet-spawned proc) whose ``pid`` coerces to 1 via __index__
# would make os.killpg(1, ...) signal process group 1 — which on a CI runner
# contains the test process itself, SIGKILLing the whole run. Only a real
# child pid (> 1) may be signalled.
# ---------------------------------------------------------------------------


def test_kill_proc_group_ignores_unreal_pid(monkeypatch):
    from unittest.mock import MagicMock

    from lionagi.agent.settings import _kill_proc_group

    called: list = []
    monkeypatch.setattr("lionagi.agent.settings.os.killpg", lambda *a: called.append(a))

    _kill_proc_group(MagicMock())  # MagicMock().pid -> 1 via __index__
    for bad in (0, 1):
        proc = MagicMock()
        proc.pid = bad
        _kill_proc_group(proc)

    assert called == [], "killpg must not fire for mock/0/1 pids (would kill the runner)"


def test_kill_proc_group_signals_real_pid(monkeypatch):
    import signal
    from unittest.mock import MagicMock

    from lionagi.agent.settings import _kill_proc_group

    called: list = []
    monkeypatch.setattr(
        "lionagi.agent.settings.os.killpg", lambda pgid, sig: called.append((pgid, sig))
    )

    proc = MagicMock()
    proc.pid = 4242
    _kill_proc_group(proc)

    assert called == [(4242, signal.SIGKILL)]
