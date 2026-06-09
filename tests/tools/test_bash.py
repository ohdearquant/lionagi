# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for BashTool: request model, response model, execution, security."""

import pytest

from lionagi.tools.code.bash import BashRequest, BashResponse, BashTool

# ---------------------------------------------------------------------------
# BashRequest model
# ---------------------------------------------------------------------------


def test_bash_request_allow_shell_excluded_from_dump():
    # exclude=True means field is excluded from model_dump(), not from schema
    req = BashRequest(command="ls", allow_shell=True)
    dumped = req.model_dump()
    assert "allow_shell" not in dumped


# ---------------------------------------------------------------------------
# BashResponse model
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BashTool.handle_request: basic execution
# ---------------------------------------------------------------------------


async def test_handle_request_echo_returns_stdout():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="/bin/echo hello"))
    assert resp.return_code == 0
    assert "hello" in resp.stdout
    assert resp.timed_out is False


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
    assert "Shell control" in resp.stderr or "trusted shell mode" in resp.stderr, (
        f"Operator {operator!r} rejection message missing: {resp.stderr}"
    )


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


async def test_handle_request_output_truncation(tmp_path):
    # Generate a python one-liner that emits well over 100 KB of output
    script = "python3 -c \"import sys; sys.stdout.write('A' * 200000); sys.stdout.flush()\""
    tool = BashTool()
    req = BashRequest(command=script, allow_shell=True)
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


async def test_to_tool_callable_executes():
    tool = BashTool()
    t = tool.to_tool()
    result = await t.func_callable(command="/bin/echo from_tool")
    assert result["return_code"] == 0
    assert "from_tool" in result["stdout"]


# ---------------------------------------------------------------------------
# C1: malformed command returns permission error response
# ---------------------------------------------------------------------------


async def test_bash_tool_malformed_command_returns_permission_error_response():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="python -c 'unterminated"))
    assert resp.return_code == -1
    assert resp.stderr.startswith("Malformed command")


# ---------------------------------------------------------------------------
# C2: Popen failure returns execution error response
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
# C3: MagicMock pid guard — os.killpg must not be called with non-int pid
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


# ---------------------------------------------------------------------------
# Edge cases: empty command, null bytes, very long command, mixed I/O
# ---------------------------------------------------------------------------


async def test_bash_tool_empty_command_string():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command=""))
    # shlex.split("") → [] → Popen rejects or execution error
    assert isinstance(resp.return_code, int)


async def test_bash_tool_command_with_null_bytes_rejected():
    tool = BashTool()
    # shlex raises ValueError on null bytes → PermissionError → return_code -1
    resp = await tool.handle_request(BashRequest(command="echo\x00hello"))
    assert resp.return_code == -1


async def test_bash_tool_very_long_command_executes_or_fails_gracefully():
    tool = BashTool()
    long_arg = "A" * 65536
    resp = await tool.handle_request(BashRequest(command=f"/bin/echo {long_arg}"))
    assert isinstance(resp.return_code, int)


async def test_bash_tool_stdout_and_stderr_simultaneously(tmp_path):
    # Write a script to a file so we don't need shell quoting tricks
    script = tmp_path / "dual_output.py"
    script.write_text("import sys\nsys.stdout.write('OUT')\nsys.stderr.write('ERR')\n")
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command=f"python3 {script}"))
    assert resp.return_code == 0
    assert "OUT" in resp.stdout
    assert "ERR" in resp.stderr


async def test_bash_tool_allow_shell_does_not_reject_operators():
    tool = BashTool()
    resp = await tool.handle_request(
        BashRequest(command="/bin/echo hi && /bin/echo there", allow_shell=True)
    )
    assert resp.return_code == 0
    assert "hi" in resp.stdout


async def test_bash_tool_allow_shell_adversarial_injection_executes():
    tool = BashTool()
    # With allow_shell=True the command runs as a shell string.
    # This confirms allow_shell=True does NOT silently reject.
    resp = await tool.handle_request(
        BashRequest(command="/bin/echo injected; /bin/echo second", allow_shell=True)
    )
    assert resp.return_code == 0
    assert "injected" in resp.stdout
