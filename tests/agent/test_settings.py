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


def test_load_settings_discovers_parent_of_explicit_project_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    project = tmp_path / "project"
    overridden_cwd = project / "nested" / "worktree"
    overridden_cwd.mkdir(parents=True)
    (project / ".lionagi").mkdir(parents=True)
    (project / ".lionagi" / "settings.yaml").write_text("notifications:\n  enabled: true\n")

    settings = load_settings(project_dir=overridden_cwd, include_project=True)

    assert settings["notifications"]["enabled"] is True


def test_apply_hooks_from_settings_rejects_untrusted_python_hook():
    settings = {"hooks": {"pre": {"bash": [{"python": "os:path"}]}}}

    with pytest.raises(PermissionError, match="Untrusted hook module"):
        apply_hooks_from_settings(
            AgentSpec.compose("implementer"),
            settings,
            trusted_hook_modules={"lionagi.agent.hooks"},
        )


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


async def test_pre_hook_timeout_passes_spawn_bound_group_witnesses(monkeypatch):
    """Timeout cleanup carries identity captured from this exact spawn."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import lionagi.ln._proc as proc_mod
    from lionagi.agent.settings import _make_shell_hook

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    # communicate raises TimeoutError via wait_for
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.wait = AsyncMock(return_value=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))
    witnesses = ((9999, 10.0),)
    cleanup = AsyncMock()
    monkeypatch.setattr(
        proc_mod,
        "prepare_process_group_env",
        lambda env=None: ({"MARKED": "pre"}, "pre-token"),
    )
    monkeypatch.setattr(
        proc_mod,
        "observe_process_group_leader",
        lambda pid, *, expected_marker: (
            10.0 if (pid, expected_marker) == (9999, "pre-token") else None
        ),
    )
    monkeypatch.setattr(proc_mod, "capture_group_witnesses", lambda *a, **k: witnesses)
    monkeypatch.setattr(proc_mod, "aterminate_process_group", cleanup)

    hook = _make_shell_hook(["slow_guard"], "pre", "bash")
    with pytest.raises(PermissionError, match="timed out"):
        await hook("bash", "run", {})

    cleanup.assert_awaited_once_with(
        mock_proc,
        grace=None,
        group_witnesses=witnesses,
    )
    assert asyncio.create_subprocess_exec.await_args.kwargs["env"] == {"MARKED": "pre"}


async def test_post_hook_timeout_passes_spawn_bound_group_witnesses(monkeypatch):
    """Post-hook cleanup uses the same spawn-bound authority as pre-hooks."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import lionagi.ln._proc as proc_mod
    from lionagi.agent.settings import _make_shell_hook

    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.wait = AsyncMock(return_value=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))
    witnesses = ((8888, 11.0),)
    cleanup = AsyncMock()
    monkeypatch.setattr(
        proc_mod,
        "prepare_process_group_env",
        lambda env=None: ({"MARKED": "post"}, "post-token"),
    )
    monkeypatch.setattr(
        proc_mod,
        "observe_process_group_leader",
        lambda pid, *, expected_marker: (
            11.0 if (pid, expected_marker) == (8888, "post-token") else None
        ),
    )
    monkeypatch.setattr(proc_mod, "capture_group_witnesses", lambda *a, **k: witnesses)
    monkeypatch.setattr(proc_mod, "aterminate_process_group", cleanup)

    hook = _make_shell_hook(["slow_notifier"], "post", "bash")
    # Post-hook swallows timeout; must not raise
    result = await hook("bash", "run", {}, {})

    assert result is None
    cleanup.assert_awaited_once_with(
        mock_proc,
        grace=None,
        group_witnesses=witnesses,
    )
    assert asyncio.create_subprocess_exec.await_args.kwargs["env"] == {"MARKED": "post"}


def _patch_hook_spawn(monkeypatch, mock_proc, created, witnesses):
    import asyncio
    from unittest.mock import AsyncMock

    import lionagi.ln._proc as proc_mod

    spawn = AsyncMock(return_value=mock_proc)
    cleanup = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(proc_mod, "prepare_process_group_env", lambda env=None: ({}, "token"))
    monkeypatch.setattr(
        proc_mod, "observe_process_group_leader", lambda pid, *, expected_marker: created
    )
    monkeypatch.setattr(proc_mod, "capture_group_witnesses", lambda *a, **k: witnesses)
    monkeypatch.setattr(proc_mod, "aterminate_process_group", cleanup)
    return spawn, cleanup


@pytest.mark.parametrize("phase", ["pre", "post"])
async def test_a_cancelled_hook_ends_its_process_group(monkeypatch, phase):
    """Cancelling the tool call kills the hook's group instead of leaving it running."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from lionagi.agent.settings import _make_shell_hook

    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
    mock_proc.wait = AsyncMock(return_value=None)
    witnesses = ((7777, 12.0),)
    _, cleanup = _patch_hook_spawn(monkeypatch, mock_proc, 12.0, witnesses)

    hook = _make_shell_hook(["guard"], phase, "bash")
    with pytest.raises(asyncio.CancelledError):
        if phase == "pre":
            await hook("bash", "run", {})
        else:
            await hook("bash", "run", {}, {})

    cleanup.assert_awaited_once_with(mock_proc, grace=None, group_witnesses=witnesses)
    mock_proc.wait.assert_awaited()


async def test_a_pre_hook_payload_that_cannot_be_serialized_refuses_without_spawning(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from lionagi.agent.settings import _make_shell_hook

    spawn, cleanup = _patch_hook_spawn(monkeypatch, MagicMock(), 1.0, ())

    hook = _make_shell_hook(["guard"], "pre", "bash")
    with pytest.raises(PermissionError, match="Hook execution error"):
        await hook("bash", "run", {"value": object()})

    spawn.assert_not_awaited()
    cleanup.assert_not_awaited()


async def test_a_post_hook_result_that_cannot_be_serialized_is_dropped_without_spawning(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from lionagi.agent.settings import _make_shell_hook

    spawn, cleanup = _patch_hook_spawn(monkeypatch, MagicMock(), 1.0, ())

    hook = _make_shell_hook(["notifier"], "post", "bash")
    assert await hook("bash", "run", {}, {"value": object()}) is None

    spawn.assert_not_awaited()
    cleanup.assert_not_awaited()


@pytest.mark.parametrize("phase", ["pre", "post"])
async def test_a_hook_whose_exchange_fails_ends_its_process_group(monkeypatch, phase):
    from unittest.mock import AsyncMock, MagicMock

    from lionagi.agent.settings import _make_shell_hook

    mock_proc = MagicMock()
    mock_proc.pid = 6666
    mock_proc.communicate = AsyncMock(side_effect=OSError("pipe closed"))
    mock_proc.wait = AsyncMock(return_value=None)
    witnesses = ((6666, 13.0),)
    _, cleanup = _patch_hook_spawn(monkeypatch, mock_proc, 13.0, witnesses)

    hook = _make_shell_hook(["guard"], phase, "bash")
    if phase == "pre":
        with pytest.raises(PermissionError, match="pipe closed"):
            await hook("bash", "run", {})
    else:
        assert await hook("bash", "run", {}, {}) is None

    cleanup.assert_awaited_once_with(mock_proc, grace=None, group_witnesses=witnesses)
    mock_proc.wait.assert_awaited()


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancelling_a_pre_hook_kills_the_hook_and_the_process_it_started(tmp_path):
    """A real hook and its own child are both gone once the cancelled call returns."""
    import asyncio
    import contextlib
    import os
    import signal
    import sys

    from lionagi.agent.settings import _make_shell_hook

    pids_file = tmp_path / "pids"
    script = tmp_path / "hook.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "with open(sys.argv[1] + '.tmp', 'w') as f:\n"
        "    f.write(f'{os.getpid()} {child.pid}')\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "time.sleep(30)\n"
    )
    hook = _make_shell_hook([sys.executable, str(script), str(pids_file)], "pre", "bash")
    task = asyncio.create_task(hook("bash", "run", {}))
    pids: list[int] = []
    try:
        for _ in range(200):
            if pids_file.exists():
                break
            await asyncio.sleep(0.05)
        pids = [int(pid) for pid in pids_file.read_text().split()]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if not any(_pid_alive(pid) for pid in pids):
                break
            await asyncio.sleep(0.05)
        assert [pid for pid in pids if _pid_alive(pid)] == []
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for pid in pids:
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)


async def test_cancelling_a_hook_that_exited_kills_the_process_it_left_running(tmp_path):
    """A hook's child holding the pipes open is killed after the hook itself was reaped."""
    import asyncio
    import contextlib
    import os
    import signal
    import sys

    from lionagi.agent.settings import _make_shell_hook

    pids_file = tmp_path / "pids"
    script = tmp_path / "hook.py"
    script.write_text(
        "import os, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "with open(sys.argv[1] + '.tmp', 'w') as f:\n"
        "    f.write(f'{os.getpid()} {child.pid}')\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    )
    hook = _make_shell_hook([sys.executable, str(script), str(pids_file)], "pre", "bash")
    task = asyncio.create_task(hook("bash", "run", {}))
    pids: list[int] = []
    try:
        for _ in range(200):
            if pids_file.exists():
                break
            await asyncio.sleep(0.05)
        pids = [int(pid) for pid in pids_file.read_text().split()]
        hook_pid, child_pid = pids
        for _ in range(200):
            if not _pid_alive(hook_pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(hook_pid)
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if not _pid_alive(child_pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(child_pid)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for pid in pids:
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)


async def test_a_hook_cancelled_while_it_is_being_spawned_is_killed_once_it_exists(
    tmp_path, monkeypatch
):
    """Cancellation inside the spawn call does not lose the hook's process group."""
    import asyncio
    import contextlib
    import os
    import signal
    import sys

    from lionagi.agent.settings import _make_shell_hook

    real_create = asyncio.create_subprocess_exec
    release = asyncio.Event()

    async def create_then_hold(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        await release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_then_hold)

    pids_file = tmp_path / "pids"
    script = tmp_path / "hook.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "with open(sys.argv[1] + '.tmp', 'w') as f:\n"
        "    f.write(f'{os.getpid()} {child.pid}')\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "time.sleep(30)\n"
    )
    hook = _make_shell_hook([sys.executable, str(script), str(pids_file)], "pre", "bash")
    task = asyncio.create_task(hook("bash", "run", {}))
    pids: list[int] = []
    try:
        for _ in range(200):
            if pids_file.exists():
                break
            await asyncio.sleep(0.05)
        pids = [int(pid) for pid in pids_file.read_text().split()]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

        for _ in range(100):
            if not any(_pid_alive(pid) for pid in pids):
                break
            await asyncio.sleep(0.05)
        assert [pid for pid in pids if _pid_alive(pid)] == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for pid in pids:
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("phase", ["pre", "post"])
async def test_a_scope_cancelled_while_a_failed_hook_is_ended_stays_cancelled(monkeypatch, phase):
    """The cancellation is delivered, not replaced by the hook's own error handling."""
    from unittest.mock import AsyncMock, MagicMock

    import anyio

    from lionagi.agent.settings import _make_shell_hook

    scopes: list[anyio.CancelScope] = []

    async def wait_while_the_scope_is_cancelled():
        scopes[0].cancel()

    mock_proc = MagicMock()
    mock_proc.pid = 5555
    mock_proc.communicate = AsyncMock(side_effect=OSError("pipe closed"))
    mock_proc.wait = wait_while_the_scope_is_cancelled
    witnesses = ((5555, 14.0),)
    _, cleanup = _patch_hook_spawn(monkeypatch, mock_proc, 14.0, witnesses)

    hook = _make_shell_hook(["guard"], phase, "bash")
    with anyio.CancelScope() as scope:
        scopes.append(scope)
        if phase == "pre":
            await hook("bash", "run", {})
        else:
            await hook("bash", "run", {}, {})

    assert scope.cancelled_caught
    cleanup.assert_awaited_once_with(mock_proc, grace=None, group_witnesses=witnesses)


@pytest.mark.parametrize("phase", ["pre", "post"])
async def test_any_payload_serialization_failure_is_handled_without_spawning(monkeypatch, phase):
    from unittest.mock import MagicMock

    import lionagi.agent.settings as settings_mod
    from lionagi.agent.settings import _make_shell_hook

    def too_deep(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded while encoding")

    spawn, cleanup = _patch_hook_spawn(monkeypatch, MagicMock(), 1.0, ())
    monkeypatch.setattr(settings_mod.json, "dumps", too_deep)

    hook = _make_shell_hook(["guard"], phase, "bash")
    if phase == "pre":
        with pytest.raises(PermissionError, match="maximum recursion depth"):
            await hook("bash", "run", {})
    else:
        assert await hook("bash", "run", {}, {}) is None

    spawn.assert_not_awaited()
    cleanup.assert_not_awaited()


# aterminate_process_group must never signal the init/session process group.
# A test double (or a not-yet-spawned proc) whose ``pid`` coerces to 1 via
# __index__ would make os.killpg(1, ...) signal process group 1 — which on a
# CI runner contains the test process itself, SIGKILLing the whole run. Only a
# real child pid (> 1) may be signalled.


def test_kill_proc_group_ignores_unreal_pid(monkeypatch):
    from unittest.mock import MagicMock

    from lionagi.ln._proc import terminate_process_group

    called: list = []
    monkeypatch.setattr("lionagi.ln._proc.os.killpg", lambda *a: called.append(a))

    terminate_process_group(MagicMock(), grace=None)  # MagicMock().pid -> 1 via __index__
    for bad in (0, 1):
        proc = MagicMock()
        proc.pid = bad
        terminate_process_group(proc, grace=None)

    assert called == [], "killpg must not fire for mock/0/1 pids (would kill the runner)"


def test_kill_proc_group_signals_real_pid(monkeypatch):
    import signal
    from unittest.mock import MagicMock

    import lionagi.ln._proc as proc_mod
    from lionagi.ln._proc import terminate_process_group

    called: list = []
    monkeypatch.setattr("lionagi.ln._proc.os.killpg", lambda pgid, sig: called.append((pgid, sig)))
    monkeypatch.setattr(proc_mod, "_group_has_witness", lambda pgid, witnesses: True)

    proc = MagicMock()
    proc.pid = 4242
    terminate_process_group(proc, grace=None, group_witnesses=((4242, 10.0),))

    assert called == [(4242, signal.SIGKILL)]
