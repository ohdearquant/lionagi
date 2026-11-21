# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The Antigravity CLI runs under the macOS sandbox: writes held to its working directory.

agy's own ``--sandbox`` restricts its terminal, not where a command writes, so the spawn path
wraps the whole process in the Seatbelt policy the API-route tools use. HOME points at a
temporary directory in every test, so the state directory created here is never the real one.
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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
    assert "com.apple.SecurityServer" not in profile
    assert "/dev/ttys" in profile


def test_the_state_directory_is_created_before_it_is_resolved(monkeypatch, tmp_path, _home):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    work = tmp_path / "work"
    work.mkdir()
    _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=work))
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
    work = tmp_path / "work"
    work.mkdir()
    request = GeminiCodeRequest(prompt="hi", repo=work, workload="light", yolo=True)
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
    work = tmp_path / "work"
    work.mkdir()
    request = GeminiCodeRequest(prompt="hi", repo=work, yolo=True)

    async def drain():
        async with contextlib.aclosing(gemini_code._ndjson_from_cli(request)) as stream:
            async for _ in stream:
                pass

    import anyio

    anyio.run(drain)
    assert seen["cmd"][0] == SANDBOX_EXEC
    assert seen["cmd"][seen["cmd"].index(AGY) :] == [AGY, *request.as_cmd_args()]
    assert seen["cwd"] == request.cwd()


def test_the_cli_cannot_read_the_credential_locations(monkeypatch, tmp_path, _home):
    from lionhub.agent.child_sandbox import DEFAULT_DENY_READ

    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    work = tmp_path / "work"
    work.mkdir()
    argv = _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=work, yolo=True))
    denied = {v for k, v in _params(argv).items() if k.startswith("DENY_READ_")}

    assert gemini_code._AGY_SIGN_IN_READS == frozenset()
    for entry in DEFAULT_DENY_READ:
        assert str(Path(entry).expanduser().resolve()) in denied
    assert str((_home / ".ssh").resolve()) in denied
    assert str((_home / ".gemini" / "oauth_creds.json").resolve()) in denied
    assert '(deny file-read* file-write* (subpath (param "DENY_READ_0")))' in _profile(argv)


def test_the_cli_cannot_read_the_subscription_account_sign_ins(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    logins = (tmp_path / "logins").resolve()
    logins.mkdir()
    token = logins / "claude-alpha"
    token.write_text("x\n")
    codex_home = (tmp_path / "codex-beta").resolve()
    codex_home.mkdir()
    (logins / "accounts.toml").write_text(f'[codex.beta]\nhome = "{codex_home}"\n')
    monkeypatch.setenv("LIONAGI_LOGINS_DIR", str(logins))
    work = tmp_path / "work"
    work.mkdir()

    argv = _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=work, yolo=True))
    denied = {v for k, v in _params(argv).items() if k.startswith("DENY_READ_")}

    assert {str(logins), str(token), str(codex_home)} <= denied


def test_the_cli_is_refused_when_the_accounts_file_cannot_be_read(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    logins = tmp_path / "logins"
    logins.mkdir()
    (logins / "accounts.toml").write_text("not toml [")
    monkeypatch.setenv("LIONAGI_LOGINS_DIR", str(logins))
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(ConfigurationError, match="sign-ins") as refused:
        _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=work, yolo=True))
    assert "child_sandbox: {enabled: false}" not in str(refused.value)


@pytest.fixture
def leg_roots(monkeypatch, tmp_path):
    """A workspace, the policy's temporary directory and a shared uv cache, all resolved."""
    base = tmp_path.resolve()
    for name in ("work", "tmp", "uv-cache"):
        (base / name).mkdir()
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    monkeypatch.setattr(tempfile, "tempdir", str(base / "tmp"))
    monkeypatch.setattr(
        seatbelt, "_uv_cache_dir", lambda **kwargs: pytest.fail("asked uv for the shared cache")
    )
    return base


def test_the_cli_gets_a_uv_cache_of_its_own_and_not_the_shared_one(leg_roots, _home):
    work = leg_roots / "work"
    request = GeminiCodeRequest(prompt="hi", repo=work, yolo=True)
    argv = _confined_agy_command(AGY, request)

    assert argv[argv.index("--") + 1 : argv.index(AGY)][0] == "/usr/bin/env"
    name, leg_cache = argv[argv.index(AGY) - 1].split("=", 1)
    assert name == "UV_CACHE_DIR"
    assert Path(leg_cache).parent == leg_roots / "tmp"
    assert Path(leg_cache).is_dir() and list(Path(leg_cache).iterdir()) == []
    roots = {v for k, v in _params(argv).items() if k.startswith("WRITABLE_ROOT_")}
    state = str((_home / ".gemini" / "antigravity-cli").resolve())
    assert roots == {str(work), str(leg_roots / "tmp"), state}
    again = _confined_agy_command(AGY, request)
    assert again[again.index(AGY) - 1] != argv[argv.index(AGY) - 1]


def test_an_agy_path_holding_an_equals_sign_is_refused(leg_roots):
    request = GeminiCodeRequest(prompt="hi", repo=leg_roots / "work", yolo=True)
    with pytest.raises(ConfigurationError, match="contains '='"):
        _confined_agy_command("/opt/a=b/agy", request)


@pytest.mark.skipif(not seatbelt_available(), reason="needs macOS sandbox-exec")
def test_the_cli_writes_its_own_uv_cache_and_cannot_write_the_shared_one(leg_roots):
    argv = _confined_agy_command(
        AGY, GeminiCodeRequest(prompt="hi", repo=leg_roots / "work", yolo=True)
    )
    prefix = argv[: argv.index(AGY)]
    shared = leg_roots / "uv-cache" / "planted"

    def sh(script: str) -> subprocess.CompletedProcess:
        # The command _confined_agy_command builds, with a shell in the CLI's place.
        return subprocess.run(
            [*prefix, "/bin/sh", "-c", script], capture_output=True, text=True, timeout=60
        )

    shown = sh("printenv UV_CACHE_DIR")
    own = sh('touch "$UV_CACHE_DIR/entry"')
    planted = sh(f"touch {shared}")

    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.strip() == argv[argv.index(AGY) - 1].split("=", 1)[1]
    assert own.returncode == 0, own.stderr
    assert planted.returncode != 0
    assert "Operation not permitted" in planted.stderr
    assert not shared.exists()


def test_a_policy_without_pty_renders_no_pty_rule(tmp_path):
    assert "/dev/ttys" not in SeatbeltPolicy.for_workspace(tmp_path).profile()


@pytest.mark.skipif(not seatbelt_available(), reason="needs macOS sandbox-exec")
def test_the_confined_command_cannot_delete_outside_the_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    # The policy grants the temporary directory, and pytest's tmp_path lies inside it on
    # macOS. Point the policy's temporary directory at a sibling of "outside", so "outside"
    # sits under no granted root.
    granted_temp = tmp_path / "temp"
    granted_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(granted_temp))
    work, outside = tmp_path / "work", tmp_path / "outside"
    for directory in (work / "inside", outside):
        directory.mkdir(parents=True)
        (directory / "MARKER").write_text("decoy\n")

    def confined(argv: list[str]) -> list[str]:
        # The policy _confined_agy_command builds, applied to another command instead of agy.
        wrapped = _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=work, yolo=True))
        return [*wrapped[: wrapped.index(AGY)], *argv]

    def rm(target: Path) -> int:
        return subprocess.run(
            confined(["/bin/rm", "-r", str(target)]), capture_output=True, timeout=60
        ).returncode

    roots = [
        Path(value)
        for name, value in _params(confined(["/bin/true"])).items()
        if name.startswith("WRITABLE_ROOT_")
    ]
    assert str(granted_temp.resolve()) in {str(root) for root in roots}
    assert not [root for root in roots if outside.resolve().is_relative_to(root)], roots

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


@pytest.mark.skipif(not seatbelt_available(), reason="needs macOS sandbox-exec")
def test_the_cli_cannot_connect_to_the_daemon_reconnect_socket(monkeypatch, tmp_path):
    """The reconnect socket answers a same-user connect with a pairing code and the daemon's key;
    the gemini-cli policy always has the network on."""
    import lionhub.serve.daemon.reconnect as reconnect

    granted_temp = tmp_path / "temp"
    granted_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(granted_temp))
    work = tmp_path / "work"
    work.mkdir()
    # A unix socket path is limited to about a hundred bytes, so the sockets sit in a short one.
    base = Path(tempfile.mkdtemp(prefix="sbx", dir="/private/tmp")).resolve()
    servers = []
    try:
        lionagi_home = base / "home" / ".lionagi"
        monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", lionagi_home)
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", lionagi_home / "state.db")
        sock = reconnect.default_socket_path()
        assert sock == lionagi_home / "studio" / "studio-reconnect.sock"
        sock.parent.mkdir(parents=True, mode=0o700)
        (base / "open").mkdir()
        for path in (sock, base / "open" / "s"):
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen()
            servers.append(server)
            threading.Thread(target=_answer_pong, args=(server,), daemon=True).start()
        client = work / "client.py"
        client.write_text(
            "import socket, sys\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "s.connect(sys.argv[1])\n"
            "print(s.recv(4).decode())\n"
        )
        wrapped = _confined_agy_command(AGY, GeminiCodeRequest(prompt="hi", repo=work, yolo=True))

        def connect(target: Path) -> subprocess.CompletedProcess:
            argv = [*wrapped[: wrapped.index(AGY)], sys.executable, "-I", str(client), str(target)]
            return subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=work)

        outside = connect(base / "open" / "s")
        confined = connect(sock)

        assert outside.returncode == 0 and outside.stdout.strip() == "pong", outside
        assert confined.returncode != 0, confined
        assert "pong" not in confined.stdout
        assert "Operation not permitted" in confined.stderr, confined
    finally:
        for server in servers:
            server.close()
        shutil.rmtree(base, ignore_errors=True)


def _answer_pong(server: socket.socket) -> None:
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.sendall(b"pong")
