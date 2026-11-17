# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for BashTool: request model, response model, execution, security."""

import asyncio
import re
import sys

import pytest
from pydantic import ValidationError

from lionagi.protocols.action.tool import Tool
from lionhub.tools.code.bash import BashRequest, BashResponse, BashTool


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
    import lionhub.tools._subprocess as subprocess_mod

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

    import lionhub.tools._subprocess as subprocess_mod

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

    import lionhub.tools._subprocess as subprocess_mod

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


def test_subprocess_timeout_passes_spawn_bound_witnesses_to_cleanup(monkeypatch):
    """A timeout cleanup may signal only the group observed at spawn."""
    import subprocess
    from unittest.mock import MagicMock

    import lionhub.tools._subprocess as subprocess_mod

    proc = MagicMock()
    proc.pid = 4242
    proc.stdout.read.return_value = b""
    proc.stderr.read.return_value = b""
    proc.wait.side_effect = [
        subprocess.TimeoutExpired("cmd", 0.01),
        subprocess.TimeoutExpired("cmd", 1.0),
    ]
    witnesses = ((4242, 12.5),)
    seen: dict[str, object] = {}

    def _popen(*args, **kwargs):
        seen["env"] = kwargs["env"]
        return proc

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(
        subprocess_mod,
        "prepare_process_group_env",
        lambda env=None: ({"ONLY": "marked"}, "spawn-token"),
        raising=False,
    )

    def _observe(pid, *, expected_marker):
        seen["observe"] = (pid, expected_marker)
        return 12.5

    monkeypatch.setattr(
        subprocess_mod,
        "observe_process_group_leader",
        _observe,
        raising=False,
    )

    def _capture(child, *, expected_leader_created, expected_marker):
        seen["capture"] = (child, expected_leader_created, expected_marker)
        return witnesses

    def _terminate(child, **kwargs):
        seen["terminate"] = (child, kwargs)

    monkeypatch.setattr(
        subprocess_mod,
        "capture_group_witnesses",
        _capture,
        raising=False,
    )
    monkeypatch.setattr(subprocess_mod, "terminate_process_group", _terminate)

    result = subprocess_mod._subprocess_sync(["slow"], False, 0.01, None)

    assert result["timed_out"] is True
    assert seen["env"] == {"ONLY": "marked"}
    assert seen["observe"] == (4242, "spawn-token")
    assert seen["capture"] == (proc, 12.5, "spawn-token")
    assert seen["terminate"] == (
        proc,
        {"grace": None, "group_witnesses": witnesses},
    )
    _, reap = proc.wait.call_args_list
    assert 0 <= reap.kwargs["timeout"] <= 1.0


@pytest.mark.parametrize("pipe_stays_open", [False, True], ids=["complete", "drain-timeout"])
def test_subprocess_pipe_drains_share_the_leader_deadline(monkeypatch, pipe_stays_open):
    from io import BytesIO
    from types import SimpleNamespace

    import lionhub.tools._subprocess as subprocess_mod

    elapsed = 0.0
    waits = []
    drains = []
    joins = []
    signals = []
    witnesses = ((4243, 13.0),)

    class Process:
        pid = 4242
        returncode = None

        def __init__(self):
            self.stdout = BytesIO(b"partial output")
            self.stderr = BytesIO(b"diagnostic")

        def wait(self, timeout=None):
            nonlocal elapsed
            assert timeout is not None, "process reap must be bounded"
            waits.append(timeout)
            if self.returncode is None:
                elapsed += 2.0
                self.returncode = 17
            return self.returncode

    class Drain:
        def __init__(self, *, target, daemon):
            assert daemon, "a pipe drain must not keep the process alive"
            self.target = target
            self.index = len(drains)
            self.duration = 3.0 if self.index == 0 else (float("inf") if pipe_stays_open else 2.0)
            self.alive = False
            drains.append(self)

        def start(self):
            self.alive = True

        def join(self, timeout=None):
            nonlocal elapsed
            assert timeout is not None, "pipe drain join must be bounded"
            joins.append((self.index, timeout))
            if self.alive:
                elapsed += min(self.duration, timeout)
                self.duration -= timeout
                if self.duration <= 0:
                    self.target()
                    self.alive = False

        def is_alive(self):
            return self.alive

    child = Process()

    def capture(proc, *, expected_leader_created, expected_marker):
        assert proc is child and proc.returncode == 17
        assert expected_leader_created == 12.5 and expected_marker == "spawn-token"
        return witnesses

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(subprocess_mod, "threading", SimpleNamespace(Thread=Drain))
    monkeypatch.setattr(
        subprocess_mod, "time", SimpleNamespace(monotonic=lambda: elapsed), raising=False
    )
    monkeypatch.setattr(
        subprocess_mod, "prepare_process_group_env", lambda env: (env, "spawn-token")
    )
    monkeypatch.setattr(subprocess_mod, "observe_process_group_leader", lambda *a, **kw: 12.5)
    monkeypatch.setattr(subprocess_mod, "capture_group_witnesses", capture)
    monkeypatch.setattr(
        subprocess_mod, "terminate_process_group", lambda *a, **kw: signals.append((a, kw))
    )

    result = subprocess_mod._subprocess_sync(["synthetic"], False, 8.0, None, 8000, env={})

    assert waits[0] == 8.0
    assert joins[:2] == [(0, 6.0), (1, 3.0)]
    if pipe_stays_open:
        assert result == {
            "stdout": "partial output",
            "stderr": "Command timed out after 8000 ms",
            "returncode": -1,
            "timed_out": True,
        }
        assert signals == [((child,), {"grace": None, "group_witnesses": witnesses})]
        assert waits == [8.0, 1.0] and joins[2:] == [(0, 1.0), (1, 1.0)]
        assert elapsed == 9.0 and drains[1].is_alive()
    else:
        assert result == {"stdout": "partial output", "stderr": "diagnostic", "returncode": 17}
        assert waits == [8.0] and len(joins) == 2 and not signals


def test_subprocess_marker_mismatch_is_direct_child_only(monkeypatch):
    """A child that cannot prove the injected marker gets no group authority."""
    import subprocess
    from unittest.mock import MagicMock

    import lionhub.tools._subprocess as subprocess_mod

    proc = MagicMock()
    proc.pid = 4343
    proc.stdout.read.return_value = b""
    proc.stderr.read.return_value = b""
    proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 0.01), None]
    seen: dict[str, object] = {}

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        subprocess_mod,
        "prepare_process_group_env",
        lambda env=None: ({"MARKED": "yes"}, "expected-token"),
    )
    monkeypatch.setattr(
        subprocess_mod,
        "observe_process_group_leader",
        lambda pid, *, expected_marker: None,
    )
    monkeypatch.setattr(
        subprocess_mod,
        "capture_group_witnesses",
        lambda *a, **k: pytest.fail("marker mismatch must not capture teardown witnesses"),
    )

    def _terminate(child, **kwargs):
        seen["terminate"] = (child, kwargs)

    monkeypatch.setattr(subprocess_mod, "terminate_process_group", _terminate)

    result = subprocess_mod._subprocess_sync(["slow"], False, 0.01, None)

    assert result["timed_out"] is True
    assert seen["terminate"] == (
        proc,
        {"grace": None, "group_witnesses": ()},
    )


@pytest.mark.parametrize("invalid_pid", [None, 0, 1, -1, True, False])
async def test_bash_tool_timeout_invalid_pid_calls_kill_not_killpg(monkeypatch, invalid_pid):
    """Lock in the `> 1` half of the guard against accidental removal.

    Mirrors the coding.py parametrization. killpg(0) → current pgroup;
    killpg(1) → init/CI runner; both catastrophic if the guard regresses.
    """
    import subprocess
    from unittest.mock import MagicMock

    import lionhub.tools._subprocess as subprocess_mod

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


def _toolkit_bash(tmp_path):
    from lionagi.session.branch import Branch
    from lionhub.tools.coding import CodingToolkit

    toolkit = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=["bash"])
    return next(t for t in toolkit.bind(Branch()) if t.func_callable.__name__ == "bash")


@pytest.mark.parametrize(
    ("timeout", "expected_ms"),
    [(None, 30_000), (0, 1), (-5, 1), (100, 100), (900_000, 300_000)],
)
async def test_toolkit_and_standalone_bash_clamp_the_timeout_the_same_way(
    tmp_path, monkeypatch, timeout, expected_ms
):
    import lionhub.tools.code.bash as bash_mod

    seen = []
    envs = []

    def run(cmd, shell, timeout_sec, timeout_ms, cwd, *, _command_env=None):
        seen.append((cmd, shell, timeout_sec, timeout_ms, cwd))
        envs.append(_command_env)
        return BashResponse(stdout="ok", return_code=0)

    monkeypatch.setattr(bash_mod, "_subprocess_sync", run)
    toolkit_bash = _toolkit_bash(tmp_path).func_callable

    standalone = await BashTool().handle_request(BashRequest(command="echo hi", timeout=timeout))
    from_toolkit = await toolkit_bash(command="echo hi", timeout=timeout)

    assert from_toolkit == standalone.model_dump()
    # The toolkit runs in its workspace root; the standalone tool keeps the process directory.
    assert seen == [
        (["echo", "hi"], False, expected_ms / 1000.0, expected_ms, None),
        (["echo", "hi"], False, expected_ms / 1000.0, expected_ms, str(tmp_path.resolve())),
    ]
    # Both callers hand the runner a prepared command environment.
    assert len(envs) == 2 and all(isinstance(env, dict) for env in envs)


@pytest.mark.parametrize("command", ["echo hi && echo there", "echo 'unterminated"])
async def test_toolkit_bash_refuses_as_the_standalone_tool_does(tmp_path, command):
    standalone = await BashTool().handle_request(BashRequest(command=command))
    from_toolkit = await _toolkit_bash(tmp_path).func_callable(command=command)

    assert standalone.return_code == -1
    assert from_toolkit == standalone.model_dump()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and /bin/sh")
def test_a_descendant_holding_the_pipe_is_bounded_and_keeps_printed_output(tmp_path):
    """A real child that backgrounds a long sleep keeps stdout open after the leader exits.

    The call must return within the deadline plus the cleanup grace, keep what the
    leader printed, report a timeout, and leave the backgrounded process stopped.
    """
    import os
    import time

    import lionhub.tools._subprocess as subprocess_mod

    pid_file = tmp_path / "bg.pid"
    # Python on both sides: the group-kill authority reads the spawn marker from
    # the process environment, which macOS does not expose for system binaries.
    script = (
        "import subprocess, sys\n"
        "bg = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(bg.pid))\n"
        "print('started', flush=True)\n"
    )
    start = time.monotonic()
    # A generous deadline so a loaded machine still spawns the child before it fires.
    result = subprocess_mod._subprocess_sync([sys.executable, "-c", script], False, 2.0, None)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0 + subprocess_mod._CLEANUP_GRACE_SECONDS + 3.0
    assert result["timed_out"] is True
    assert "started" in result["stdout"]

    bg_pid = int(pid_file.read_text().strip())
    stop_by = time.monotonic() + 2.0
    alive = True
    while time.monotonic() < stop_by:
        try:
            os.kill(bg_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    if alive:
        os.kill(bg_pid, 9)
    assert not alive, "the backgrounded process outlived the call"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell background job")
def test_output_printed_before_the_deadline_is_kept_while_a_descendant_holds_the_pipe(tmp_path):
    """Output already in the pipe is returned even when the pipe stays open past the deadline."""
    import os
    import time

    import lionhub.tools._subprocess as subprocess_mod

    pid_file = tmp_path / "bg.pid"
    script = f"echo started; sleep 30 & echo $! > {pid_file}; exit 0"
    start = time.monotonic()
    try:
        result = subprocess_mod._subprocess_sync(["/bin/sh", "-c", script], False, 1.0, None)
        elapsed = time.monotonic() - start
    finally:
        try:
            os.kill(int(pid_file.read_text().strip()), 9)
        except (OSError, ValueError):
            pass

    assert elapsed < 1.0 + subprocess_mod._CLEANUP_GRACE_SECONDS + 3.0
    assert result["timed_out"] is True
    assert "started" in result["stdout"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipes")
def test_a_clean_exit_is_not_reported_as_a_timeout_when_the_readers_lag(monkeypatch):
    """The command exits well inside its deadline, but its pipe readers only finish after it.

    The readers' own latency is not the command's: it reports its return code and output.
    """
    import time

    import lionhub.tools._subprocess as subprocess_mod

    real_drain = subprocess_mod._drain

    def slow_drain(stream, buf):
        # A reader thread that is scheduled late, as under load.
        time.sleep(2.5)
        return real_drain(stream, buf)

    monkeypatch.setattr(subprocess_mod, "_drain", slow_drain)
    monkeypatch.setattr(subprocess_mod, "_DRAIN_GRACE_SECONDS", 5.0, raising=False)
    code = "import sys; sys.stdout.write('done'); sys.exit(3)"

    result = subprocess_mod._subprocess_sync([sys.executable, "-c", code], False, 2.0, None)

    assert result.get("timed_out") is None
    assert result["returncode"] == 3
    assert result["stdout"] == "done"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell background job")
def test_a_timed_out_command_marks_truncation_only_when_the_cap_was_reached(monkeypatch):
    import lionhub.tools._subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "_MAX_OUTPUT_BYTES", 16)
    code = "import sys, time; sys.stdout.write(sys.argv[1]); sys.stdout.flush(); time.sleep(30)"

    short = subprocess_mod._subprocess_sync([sys.executable, "-c", code, "tiny"], False, 1.0, None)
    full = subprocess_mod._subprocess_sync([sys.executable, "-c", code, "x" * 40], False, 1.0, None)

    assert short["timed_out"] is True and short["stdout"] == "tiny"
    assert full["timed_out"] is True
    assert full["stdout"] == "x" * 16 + "\n\n[... output truncated at 16 bytes ...]\n"
