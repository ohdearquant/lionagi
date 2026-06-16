# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for BashTool: request model, response model, execution, security."""

import asyncio

import pytest
from pydantic import ValidationError

from lionagi.protocols.action.tool import Tool
from lionagi.tools.code.bash import BashRequest, BashResponse, BashTool

# ---------------------------------------------------------------------------
# BashRequest model
# ---------------------------------------------------------------------------


def test_bash_request_required_command():
    req = BashRequest(command="echo hi")
    assert req.command == "echo hi"


def test_bash_request_defaults():
    req = BashRequest(command="ls")
    assert req.timeout is None
    assert req.cwd is None


def test_bash_request_custom_fields():
    req = BashRequest(command="pwd", timeout=5000, cwd="/tmp")
    assert req.timeout == 5000
    assert req.cwd == "/tmp"


def test_bash_request_allow_shell_kwarg_raises_validation_error():
    # CWE-284 fix: allow_shell removed from BashRequest entirely.
    # Callers that previously passed allow_shell=True (or False) receive a
    # hard ValidationError — there is no silent bypass path on the model.
    with pytest.raises(ValidationError):
        BashRequest(command="ls", allow_shell=True)
    with pytest.raises(ValidationError):
        BashRequest(command="ls", allow_shell=False)


# ---------------------------------------------------------------------------
# BashResponse model
# ---------------------------------------------------------------------------


def test_bash_response_defaults():
    resp = BashResponse(return_code=0)
    assert resp.stdout == ""
    assert resp.stderr == ""
    assert resp.timed_out is False


def test_bash_response_fields():
    resp = BashResponse(stdout="out", stderr="err", return_code=1, timed_out=True)
    assert resp.stdout == "out"
    assert resp.stderr == "err"
    assert resp.return_code == 1
    assert resp.timed_out is True


# ---------------------------------------------------------------------------
# BashTool.handle_request: basic execution
# ---------------------------------------------------------------------------


async def test_handle_request_echo_returns_stdout():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="/bin/echo hello"))
    assert resp.return_code == 0
    assert "hello" in resp.stdout
    assert resp.timed_out is False


async def test_handle_request_returns_bash_response():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="/bin/echo ok"))
    assert isinstance(resp, BashResponse)


async def test_handle_request_non_zero_exit():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="false"))
    assert resp.return_code != 0
    assert resp.timed_out is False


async def test_handle_request_dict_input():
    tool = BashTool()
    resp = await tool.handle_request({"command": "/bin/echo dict"})
    assert resp.return_code == 0
    assert "dict" in resp.stdout


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


async def test_handle_request_timeout():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 10", timeout=100))
    assert resp.timed_out is True
    assert resp.return_code == -1


async def test_handle_request_timeout_stderr_message():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 10", timeout=100))
    assert "100" in resp.stderr or "timed out" in resp.stderr.lower()


# ---------------------------------------------------------------------------
# Shell control operators rejected
# ---------------------------------------------------------------------------


async def test_handle_request_semicolon_rejected():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="echo hi; echo there"))
    assert resp.return_code == -1
    assert "Shell control" in resp.stderr or "rejected" in resp.stderr.lower()


async def test_handle_request_pipe_rejected():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="echo hi | cat"))
    assert resp.return_code == -1


async def test_handle_request_and_and_rejected():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="true && echo yes"))
    assert resp.return_code == -1


@pytest.mark.parametrize(
    "cmd,operator",
    [
        ("false || echo pwned", "||"),
        ("echo `whoami`", "`"),
        ("echo $(whoami)", "$("),
        ("cat < /etc/hosts", "<"),
        ("echo x > /tmp/out", ">"),
        ("echo a\necho b", "newline"),
    ],
)
async def test_handle_request_shell_control_operators_rejected(cmd, operator):
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command=cmd))
    assert resp.return_code == -1, f"Operator {operator!r} was not rejected"
    assert "Shell control" in resp.stderr, (
        f"Operator {operator!r} rejection message missing: {resp.stderr}"
    )


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


async def test_handle_request_output_truncation(tmp_path):
    # Generate a Python script file that emits well over 100 KB of output.
    # Running it via `python3 <path>` has no shell operators, so shell=False
    # is used and the output-truncation path is exercised without any bypass.
    script_path = tmp_path / "big_output.py"
    script_path.write_text("import sys\nsys.stdout.write('A' * 200_000)\nsys.stdout.flush()\n")
    tool = BashTool()
    req = BashRequest(command=f"python3 {script_path}")
    resp = await tool.handle_request(req)
    assert "truncated" in resp.stdout.lower()
    assert resp.return_code == 0


# ---------------------------------------------------------------------------
# cwd parameter
# ---------------------------------------------------------------------------


async def test_handle_request_cwd(tmp_path):
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="pwd", cwd=str(tmp_path)))
    assert resp.return_code == 0
    assert str(tmp_path) in resp.stdout


# ---------------------------------------------------------------------------
# to_tool
# ---------------------------------------------------------------------------


def test_to_tool_returns_tool_instance():
    tool = BashTool()
    t = tool.to_tool()
    assert isinstance(t, Tool)


def test_to_tool_cached():
    tool = BashTool()
    t1 = tool.to_tool()
    t2 = tool.to_tool()
    assert t1 is t2


def test_to_tool_func_callable_is_async():
    tool = BashTool()
    t = tool.to_tool()
    assert asyncio.iscoroutinefunction(t.func_callable)


async def test_to_tool_callable_executes():
    tool = BashTool()
    t = tool.to_tool()
    result = await t.func_callable(command="/bin/echo from_tool")
    assert result["return_code"] == 0
    assert "from_tool" in result["stdout"]


# ---------------------------------------------------------------------------
# Security: CWE-284 — shell=False is unconditional; no bypass via kwargs
# ---------------------------------------------------------------------------


async def test_subprocess_always_invoked_with_shell_false(monkeypatch):
    """Popen must always receive shell=False regardless of command content."""
    import lionagi.tools._subprocess as subprocess_mod

    captured_kwargs = []
    original_popen = subprocess_mod.subprocess.Popen

    def recording_popen(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", recording_popen)

    tool = BashTool()
    await tool.handle_request(BashRequest(command="/bin/echo sec_test"))

    assert captured_kwargs, "Popen was never called"
    for kw in captured_kwargs:
        assert kw.get("shell") is False, (
            f"subprocess.Popen called with shell={kw.get('shell')!r} — must be False"
        )


async def test_shell_operators_do_not_execute_via_handle_request():
    """Shell operators in command string must be rejected, not executed.

    Probe: `echo sentinel; echo injected` — if shell=True were used, both
    lines appear in stdout.  With shell=False the command is rejected before
    reaching Popen, so stdout is empty and return_code is -1.
    """
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="echo sentinel; echo injected"))
    assert resp.return_code == -1
    assert "injected" not in resp.stdout


async def test_pipe_operator_does_not_execute():
    """Pipe operator must be rejected before reaching subprocess."""
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="echo hi | cat"))
    assert resp.return_code == -1
    assert resp.stdout == ""


# ---------------------------------------------------------------------------
# Malformed command returns permission error response
# ---------------------------------------------------------------------------


async def test_bash_tool_malformed_command_returns_permission_error_response():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="python -c 'unterminated"))
    assert resp.return_code == -1
    assert resp.stderr.startswith("Malformed command")


# ---------------------------------------------------------------------------
# Popen failure returns execution error response
# ---------------------------------------------------------------------------


async def test_bash_tool_popen_failure_returns_execution_error(monkeypatch):

    import lionagi.tools._subprocess as subprocess_mod

    def fake_popen(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", fake_popen)

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="/bin/echo hi"))
    assert resp.return_code == -1
    assert "Execution error" in resp.stderr
    assert "no exec" in resp.stderr


# ---------------------------------------------------------------------------
# MagicMock pid guard — os.killpg must not be called with non-int pid
# ---------------------------------------------------------------------------


async def test_bash_tool_timeout_mock_pid_calls_kill_not_killpg(monkeypatch):
    """MagicMock proc.pid must not reach os.killpg (would target PID 1 on CI)."""
    import subprocess
    from unittest.mock import MagicMock

    import lionagi.tools._subprocess as subprocess_mod

    mock_proc = MagicMock()
    # Set pid to a MagicMock object — isinstance(pid, int) returns False,
    # so the guard routes to proc.kill() instead of os.killpg().
    mock_proc.pid = MagicMock()
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 0.01), None]
    mock_proc.kill = MagicMock()

    killpg_calls = []

    def fake_popen(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess_mod.os, "killpg", lambda *a: killpg_calls.append(a))

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 60", timeout=10))

    assert killpg_calls == [], "os.killpg must not be called when proc.pid is not int > 1"
    mock_proc.kill.assert_called_once()
    assert resp.timed_out is True


@pytest.mark.parametrize("invalid_pid", [None, 0, 1, -1, True, False])
async def test_bash_tool_timeout_invalid_pid_calls_kill_not_killpg(monkeypatch, invalid_pid):
    """Lock in the `> 1` half of the guard against accidental removal.

    Mirrors the coding.py parametrization. killpg(0) → current pgroup;
    killpg(1) → init/CI runner; both catastrophic if the guard regresses.
    """
    import subprocess
    from unittest.mock import MagicMock

    import lionagi.tools._subprocess as subprocess_mod

    mock_proc = MagicMock()
    mock_proc.pid = invalid_pid
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 0.01), None]
    mock_proc.kill = MagicMock()

    killpg_calls = []

    def fake_popen(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess_mod.os, "killpg", lambda *a: killpg_calls.append(a))

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 60", timeout=10))

    assert killpg_calls == [], f"os.killpg must not be called for pid={invalid_pid!r}"
    mock_proc.kill.assert_called_once()
    assert resp.timed_out is True
