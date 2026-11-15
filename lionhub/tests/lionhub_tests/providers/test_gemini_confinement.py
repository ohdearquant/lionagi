# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The Antigravity CLI runs under the macOS sandbox: writes held to its working directory.

agy's own ``--sandbox`` restricts its terminal, not where a command writes, so the spawn path
wraps the whole process in the Seatbelt policy the API-route tools use. HOME points at a
temporary directory in every test, so the state directory created here is never the real one.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

import pytest

import lionhub.providers.google.gemini_code as gemini_code
import lionhub.tools._seatbelt as seatbelt
from lionagi._errors import ConfigurationError
from lionhub.providers.google.gemini_code import GeminiCodeRequest, _confined_agy_command
from lionhub.tools._seatbelt import SANDBOX_EXEC, SeatbeltPolicy, seatbelt_available

AGY = "/opt/agy"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _params(argv):
    """The ``-D NAME=VALUE`` parameters of a wrapped argv, by name."""
    return dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "-D")


def _profile(argv):
    return argv[argv.index("-p") + 1]


def test_the_command_is_wrapped_in_the_sandbox(monkeypatch, tmp_path, _home):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    work = tmp_path / "work"
    work.mkdir()
    request = GeminiCodeRequest(prompt="hi", repo=work, yolo=True)
    argv = _confined_agy_command(AGY, request)
    assert argv[0] == SANDBOX_EXEC
    assert argv[argv.index(AGY) :] == [AGY, *request.as_cmd_args()]
    roots = {v for k, v in _params(argv).items() if k.startswith("WRITABLE_ROOT_")}
    assert str(work.resolve()) in roots
    assert str((_home / ".gemini" / "antigravity-cli").resolve()) in roots
    assert str((_home / ".gemini").resolve()) not in roots
    profile = _profile(argv)
    assert "(allow network-outbound)" in profile
    assert "/dev/ttys" in profile


def test_the_state_directory_is_created_before_it_is_resolved(monkeypatch, tmp_path, _home):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=tmp_path))
    assert (_home / ".gemini" / "antigravity-cli").is_dir()


def test_yolo_without_the_sandbox_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: False)
    with pytest.raises(ConfigurationError, match="needs the macOS sandbox"):
        _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=tmp_path, yolo=True))


def test_a_request_without_yolo_runs_as_before_where_there_is_no_sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: False)
    request = GeminiCodeRequest(prompt="hi", repo=tmp_path, workload="light")
    assert _confined_agy_command(AGY, request) == [AGY, *request.as_cmd_args()]
    assert "--sandbox" in request.as_cmd_args()


def test_a_light_request_under_the_sandbox_leaves_out_agys_own(monkeypatch, tmp_path):
    # macOS refuses a sandbox inside a sandbox, so agy's --sandbox would fail every command.
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    request = GeminiCodeRequest(prompt="hi", repo=tmp_path, workload="light", yolo=True)
    argv = _confined_agy_command(AGY, request)
    assert argv[0] == SANDBOX_EXEC
    tail = argv[argv.index(AGY) + 1 :]
    assert "--sandbox" not in tail
    assert tail == [arg for arg in request.as_cmd_args() if arg != "--sandbox"]


def test_the_spawn_path_hands_the_wrapped_command_to_the_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    monkeypatch.setattr(gemini_code, "AGY_CLI", AGY)
    seen = {}

    async def fake_ndjson(cmd, **kwargs):
        seen["cmd"], seen["cwd"] = cmd, kwargs["cwd"]
        return
        yield  # pragma: no cover - an empty async generator

    monkeypatch.setattr(gemini_code, "ndjson_from_cli", fake_ndjson)
    request = GeminiCodeRequest(prompt="hi", repo=tmp_path, yolo=True)

    async def drain():
        async with contextlib.aclosing(gemini_code._ndjson_from_cli(request)) as stream:
            async for _ in stream:
                pass

    import anyio

    anyio.run(drain)
    assert seen["cmd"][0] == SANDBOX_EXEC
    assert seen["cmd"][seen["cmd"].index(AGY) :] == [AGY, *request.as_cmd_args()]
    assert seen["cwd"] == request.cwd()


def test_a_policy_without_pty_renders_no_pty_rule(tmp_path):
    assert "/dev/ttys" not in SeatbeltPolicy.for_workspace(tmp_path).profile()


@pytest.mark.skipif(not seatbelt_available(), reason="needs macOS sandbox-exec")
def test_the_confined_command_cannot_delete_outside_the_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    work, outside = tmp_path / "work", tmp_path / "outside"
    for directory in (work / "inside", outside):
        directory.mkdir(parents=True)
        (directory / "MARKER").write_text("decoy\n")

    def rm(target: Path) -> int:
        # The policy _confined_agy_command builds, applied to rm instead of agy.
        argv = _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=work, yolo=True))
        argv = [*argv[: argv.index(AGY)], "/bin/rm", "-r", str(target)]
        return subprocess.run(argv, capture_output=True, timeout=60).returncode

    assert rm(outside) != 0
    assert (outside / "MARKER").read_text() == "decoy\n"
    assert rm(work / "inside") == 0
    assert not (work / "inside").exists()


def test_the_pty_rule_reaches_only_terminals_the_child_allocates():
    rule = seatbelt._PTY_POLICY
    assert '#"^/dev/ttys[0-9]+$"' in rule
    assert '(extension "com.apple.sandbox.pty")' in rule
    # Every terminal device grant carries the extension; /dev/ptmx is the allocator itself.
    for line in rule.split("(allow")[1:]:
        assert "ttys" not in line or "com.apple.sandbox.pty" in line


_PTY_CHILD = r"""
import json, os, pty, sys
out = {}
try:
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY)
    os.write(fd, b"FOREIGN")
    out["foreign"] = "written"
except OSError as exc:
    out["foreign"] = type(exc).__name__
pid, fd = pty.fork()
if pid == 0:
    os.execv("/bin/echo", ["/bin/echo", "own-terminal"])
data = b""
try:
    while chunk := os.read(fd, 100):
        data += chunk
except OSError:
    pass
os.waitpid(pid, 0)
out["own"] = data.decode()
print(json.dumps(out))
"""


@pytest.mark.skipif(not seatbelt_available(), reason="needs macOS sandbox-exec")
def test_the_confined_child_uses_its_own_terminal_and_not_another(tmp_path):
    import json
    import os
    import pty
    import select

    master, slave = pty.openpty()
    try:
        policy = SeatbeltPolicy.for_workspace(tmp_path, pty=True)
        argv = policy.wrap([sys.executable, "-I", "-S", "-c", _PTY_CHILD, os.ttyname(slave)])
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
        )
        shown = b""
        while select.select([master], [], [], 0.2)[0]:
            shown += os.read(master, 4096)
    finally:
        os.close(master)
        os.close(slave)
    out = json.loads(result.stdout)
    assert out["foreign"] == "PermissionError"
    assert b"FOREIGN" not in shown
    assert "own-terminal" in out["own"]
