# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for BashTool: request model, response model, execution, security."""

import asyncio
import re
import sys

import pytest
from pydantic import ValidationError

from lionagi.protocols.action.tool import Tool
from lionagi.tools.code.bash import BashRequest, BashResponse, BashTool


def test_bash_request_defaults():
    req = BashRequest(command="ls")
    assert req.timeout is None
    assert req.cwd is None


def test_bash_request_allow_shell_kwarg_raises_validation_error():
    # CWE-284 fix: allow_shell removed from BashRequest entirely.
    # Callers that previously passed allow_shell=True (or False) receive a
    # hard ValidationError — there is no silent bypass path on the model.
    with pytest.raises(ValidationError):
        BashRequest(command="ls", allow_shell=True)
    with pytest.raises(ValidationError):
        BashRequest(command="ls", allow_shell=False)


def test_bash_response_defaults():
    resp = BashResponse(return_code=0)
    assert resp.stdout == ""
    assert resp.stderr == ""
    assert resp.timed_out is False


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


async def test_handle_request_timeout():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 10", timeout=100))
    assert resp.timed_out is True
    assert resp.return_code == -1


async def test_handle_request_timeout_stderr_message():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 10", timeout=100))
    assert "100" in resp.stderr or "timed out" in resp.stderr.lower()


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


async def test_handle_request_cwd(tmp_path):
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="pwd", cwd=str(tmp_path)))
    assert resp.return_code == 0
    assert str(tmp_path) in resp.stdout


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


# Security: CWE-284 — shell=False is unconditional; no bypass via kwargs


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


async def test_bash_tool_malformed_command_returns_permission_error_response():
    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="python -c 'unterminated"))
    assert resp.return_code == -1
    assert resp.stderr.startswith("Malformed command")


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


# MagicMock pid guard — os.killpg must not be called with non-int pid


async def test_bash_tool_timeout_mock_pid_calls_kill_not_killpg(monkeypatch):
    """MagicMock proc.pid must not reach os.killpg (would target PID 1 on CI)."""
    import subprocess
    from unittest.mock import MagicMock

    import lionagi.tools._subprocess as subprocess_mod

    mock_proc = MagicMock()
    # Set pid to a MagicMock object — isinstance(pid, int) returns False,
    # so the guard routes to proc.kill() instead of os.killpg().
    mock_proc.pid = MagicMock()
    # EOF on read() so _subprocess_sync's drain threads exit instead of
    # busy-spinning forever on a bare-mock stream (saturates CPU on CI).
    mock_proc.stdout.read.return_value = b""
    mock_proc.stderr.read.return_value = b""
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 0.01), None]
    mock_proc.kill = MagicMock()

    killpg_calls = []

    def fake_popen(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", fake_popen)
    import lionagi.ln._proc as proc_mod

    monkeypatch.setattr(proc_mod.os, "killpg", lambda *a: killpg_calls.append(a))

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
    # EOF on read() so _subprocess_sync's drain threads exit instead of
    # busy-spinning forever on a bare-mock stream (saturates CPU on CI).
    mock_proc.stdout.read.return_value = b""
    mock_proc.stderr.read.return_value = b""
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 0.01), None]
    mock_proc.kill = MagicMock()

    killpg_calls = []

    def fake_popen(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", fake_popen)
    import lionagi.ln._proc as proc_mod

    monkeypatch.setattr(proc_mod.os, "killpg", lambda *a: killpg_calls.append(a))

    tool = BashTool()
    resp = await tool.handle_request(BashRequest(command="sleep 60", timeout=10))

    assert killpg_calls == [], f"os.killpg must not be called for pid={invalid_pid!r}"
    mock_proc.kill.assert_called_once()
    assert resp.timed_out is True


_ADVICE_LABELS = ("Supported remedies", "Not available here")
_LABEL_PATTERN = re.compile(r"(?<![`\w])([A-Z][A-Za-z ]{2,40}):")


def _truncation_advice(doc: str) -> tuple[list[str], list[str]]:
    """Split the oversized-output advice into the commands it offers and the ones it rules out.

    Returns (offered, ruled_out) as literal command templates, read off the
    docstring's own structure rather than matched against expected wording.

    Only two labels carry meaning: commands before the second one are offered,
    commands after it are ruled out. A label this split has no rule for would be
    silently folded into one of those two lists, so it fails here instead.
    """
    offered_at = doc.index("Supported remedies:")
    ruled_out_at = doc.index("Not available here:")
    ruled_out_text = doc[ruled_out_at:].split("\n\n")[0]
    labels = set(_LABEL_PATTERN.findall(doc[offered_at:ruled_out_at] + ruled_out_text))
    assert labels == set(_ADVICE_LABELS), (
        f"the advice uses a label with no defined meaning: {sorted(labels)}"
    )
    offered = re.findall(r"`([^`]+)`", doc[offered_at:ruled_out_at])
    ruled_out = re.findall(r"`([^`]+)`", ruled_out_text)
    return offered, ruled_out


async def test_docstring_recovery_advice_is_executable(tmp_path):
    """The oversized-output advice must name remedies that run and reduce output.

    Every command listed as a supported remedy is run here and must succeed;
    its stdout must be at most a tenth of the unremedied command's output.
    Every command listed as unavailable is run here and must be refused by the
    guard. Neither list is compared against expected prose — both are read out
    of the docstring — so rewording the advice keeps this green, while moving a
    command between the lists, or changing the guard so a listed remedy stops
    running, turns it red.
    """
    tool = BashTool()
    doc = tool.to_tool().func_callable.__doc__
    offered, ruled_out = _truncation_advice(doc)
    assert offered, "advice must name at least one supported remedy"
    assert ruled_out, "advice must name at least one unavailable alternative"

    # Placeholders the listed commands may use, bound to things this test can
    # run inside tmp_path. A remedy using an unknown placeholder is a failure,
    # not a silent skip.
    payload = tmp_path / "payload.txt"
    large_payload = "line\n" * 40_000
    payload.write_text(large_payload)
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import sys\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        "open(out, 'w').write('generated\\n')\n"
    )
    fixtures = {"FILE": str(payload), "PROG": f"{sys.executable} {writer}"}
    baseline = await tool.handle_request(BashRequest(command=f"cat {payload}"))
    assert baseline.return_code == 0
    assert "truncated" in baseline.stdout.lower()
    baseline_bytes = len(baseline.stdout.encode())

    def runnable(template: str) -> str:
        used = set(re.findall(r"\b[A-Z]{2,}\b", template))
        assert used <= set(fixtures), f"advice names remedies this test cannot run: {template!r}"
        for name, value in fixtures.items():
            template = template.replace(name, value)
        return template

    for template in offered:
        payload.write_text(large_payload)
        resp = await tool.handle_request(BashRequest(command=runnable(template)))
        assert resp.return_code == 0, f"advised remedy {template!r} does not run: {resp.stderr}"
        remedy_bytes = len(resp.stdout.encode())
        max_remedy_bytes = baseline_bytes // 10
        assert remedy_bytes <= max_remedy_bytes, (
            f"advised remedy {template!r} did not materially reduce stdout: "
            f"{remedy_bytes} bytes exceeds {max_remedy_bytes}"
        )

    for template in ruled_out:
        payload.write_text(large_payload)
        command = runnable(template)
        resp = await tool.handle_request(BashRequest(command=command))
        assert resp.return_code == -1, (
            f"{template!r} is advertised as unavailable but the guard let it through"
        )
        # -1 is also what a signalled or unspawnable child reports, so pin the
        # refusal itself: the guard's own diagnostic, naming this command, and a
        # response that never entered the timeout path because nothing ran.
        assert "shell control operators are not supported" in resp.stderr.lower(), (
            f"{template!r} returned -1 without the guard refusing it: {resp.stderr!r}"
        )
        assert command in resp.stderr, (
            f"the refusal does not name the command it rejected: {resp.stderr!r}"
        )
        assert not resp.timed_out, (
            f"{template!r} was refused before execution, so it cannot have timed out"
        )
