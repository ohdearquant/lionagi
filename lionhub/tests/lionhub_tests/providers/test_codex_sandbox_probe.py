# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The codex permission-profile probe, driven without a codex binary.

A fake codex reads the profile off the command line, asks the probe's stand-in provider what to
run, and answers each command as a sandbox enforcing that profile would. The stand-in's reply
logic is the real one; only its transport is replaced by a function call, because some hosts
refuse to bind even a loopback port. One test drives the socket-backed stand-in and skips where
binding is refused. Named leaks break one behaviour at a time, so each arm is shown to fail for
the reason it exists. The record the probe writes is read back by the same lookup a launch uses.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from lionagi._errors import CLIInputRefusedError
from lionhub.providers.openai import codex_sandbox_probe as probe_module
from lionhub.providers.openai._codex_permissions import (
    PERMISSION_PROFILE,
    PROBE_COMMAND,
    REQUIRED_ARMS,
    SOCKET_ARMS,
    binary_sha256,
    launch_arms,
    permission_profile_table,
    probe_record,
    probe_record_path,
    require_passing_probe_record,
)


def _toml_loads(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


@pytest.fixture
def sandbox_on(monkeypatch, tmp_path):
    """The child sandbox on with the default policy (the suite switches it off by default),
    plus one denied file outside the probe's scratch directory, so the real-path deny arm has a
    real file to open on any host."""
    import lionhub.agent.child_sandbox as child_sandbox

    planted = tmp_path / "planted-deny" / "credentials"
    planted.parent.mkdir()
    planted.write_text("not a real credential\n")
    settings = {"child_sandbox": {"deny_read": [str(planted)]}}
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: settings)


@pytest.fixture
def record_dir(tmp_path, monkeypatch):
    """Probe records written under the test's own directory, never the real one."""
    import lionagi._paths as paths

    home = tmp_path / "lionagi-home"
    monkeypatch.setattr(paths, "LIONAGI_HOME", home)
    return home / "codex-sandbox-probes"


def _fake_binary(directory: Path, version: str = "0.162.1", name: str = "codex") -> Path:
    """An executable that only answers ``--version``, like a codex binary does for the lookup."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f'#!/bin/sh\necho "codex-cli {version}"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _passing_arms() -> dict[str, dict]:
    return {name: {"passed": True, "evidence": "rc=0 ok"} for name in REQUIRED_ARMS}


# ------------------------------------------------------------------------------- the fake codex
def _under(target: Path, roots: list[Path]) -> bool:
    return any(root == target or root in target.parents for root in roots)


_REAL_STUB = probe_module.StubProvider
_REAL_TEMP_ROOTS = probe_module._temp_roots
_PORTS = itertools.count(40000)
_LIVE_STUBS: dict[str, InMemoryStub] = {}


class InMemoryStub(_REAL_STUB):
    """The probe's stand-in provider without the socket: the same replies, no bind.

    Requests reach ``_reply`` through ``_ask`` below instead of HTTP, and are recorded the way
    the HTTP handler records them, so the probe reads the same ``requests`` either way.
    """

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.requests = []
        self._port = next(_PORTS)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        _LIVE_STUBS[self.base_url] = self

    def stop(self) -> None:
        _LIVE_STUBS.pop(self.base_url, None)


@pytest.fixture(autouse=True)
def _in_memory_stub(monkeypatch):
    """The probe builds its provider by module-level name, so the tests swap it for the
    in-memory one; the socket-backed class is exercised by its own test."""
    _LIVE_STUBS.clear()
    monkeypatch.setattr(probe_module, "StubProvider", InMemoryStub)


def _parse_sse(body: str) -> dict:
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    return next(e["item"] for e in events if e["type"] == "response.output_item.done")


def _ask(base: str, items: list[dict]) -> dict:
    """What codex does each turn: send the conversation so far, read the next item back."""
    # The probe hands codex the provider's ``/v1`` root; the registry is keyed by the bare origin.
    stub = _LIVE_STUBS[base.removesuffix("/v1")]
    request = {"input": items}
    stub.requests.append(request)
    return _parse_sse(stub._reply(request).decode())


@pytest.fixture(autouse=True)
def _scratch_may_sit_in_a_temp_root(monkeypatch):
    """The tests' scratch directories live under pytest's temp root, which the probe refuses for
    a real scratch root; the tests that exercise that refusal set the roots themselves."""
    monkeypatch.setattr(probe_module, "_temp_roots", lambda: [])


class FakeListener:
    """A unix listener in memory. A connect is queued, and the queue is counted in order only
    when the probe waits for a mark or stops the listener, the way a real listener's accept
    thread can lag behind the kernel: a count read before the drain would miss the child.

    Pytest's temp paths can exceed the platform's unix-socket path limit, so the arm tests swap
    the real listener for this one; the real class has its own tests on short relative paths."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.accepted = 0
        self.queued: list[str | None] = []
        self.marks: dict[str, int] = {}

    def connect(self, mark: str | None = None) -> None:
        self.queued.append(mark)

    def _count_queued(self) -> None:
        for mark in self.queued:
            self.accepted += 1
            if mark is not None:
                self.marks.setdefault(mark, self.accepted)
        self.queued.clear()

    def wait_for_mark(self, mark: str, timeout: float) -> int | None:
        self._count_queued()
        return self.marks.get(mark)

    def stop(self) -> None:
        self._count_queued()


_LISTENERS: dict[Path, FakeListener] = {}
_REAL_START_LISTENER = probe_module._start_listener
_REAL_OUTSIDE_CONNECT = probe_module._outside_connect


def _outside_in_memory(path, mark):
    """The probe's own connect from outside the sandbox, against the in-memory listeners."""
    listener = _LISTENERS.get(Path(path))
    if listener is None:
        return "ENOENT"
    listener.connect(mark)
    return "ok"


@pytest.fixture(autouse=True)
def _unix_listeners_in_memory(monkeypatch):
    _LISTENERS.clear()

    def start(path):
        listener = FakeListener(path)
        _LISTENERS[Path(path)] = listener
        return listener, None

    monkeypatch.setattr(probe_module, "_start_listener", start)
    monkeypatch.setattr(probe_module, "_outside_connect", _outside_in_memory)


@pytest.fixture(autouse=True)
def _tmpdir_is_set(tmp_path, monkeypatch):
    """TMPDIR names a directory, so the second light-tmp arm runs rather than being skipped."""
    (tmp_path / "tmpdir").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))


class FakeCodex:
    """Stands in for the codex binary: enforces the profile on its command line, except for
    the behaviours named in ``leaks`` (an arm name each), which it gets wrong."""

    def __init__(self, leaks: tuple[str, ...] = ()) -> None:
        self.leaks = set(leaks)
        self.commands: list[str] = []
        self.runs: list[tuple[list[str], dict[str, str], Path]] = []

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> int:
        self.runs.append((argv, env, cwd))
        provider = next(a for a in argv if a.startswith("model_providers.stub="))
        base = re.search(r'base_url="([^"]+)"', provider).group(1)
        row = next(a for a in argv if a.startswith(f"permissions.{PERMISSION_PROFILE}="))
        table = _toml_loads("table = " + row.split("=", 1)[1])["table"]
        add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        # What the commands see: the run's own TMPDIR, unless shell_environment_policy.set names
        # the variable, as codex sets it for the commands it runs.
        child_env = {"TMPDIR": env.get("TMPDIR", ""), "TMP": "", "TEMP": ""}
        for arg in argv:
            found = re.match(r"shell_environment_policy\.set\.(\w+)=(.*)\Z", arg, re.DOTALL)
            if found and "scratch_env" not in self.leaks:
                if found.group(1) == "TEMP" and "scratch_env_partial" in self.leaks:
                    continue
                child_env[found.group(1)] = _toml_loads("v = " + found.group(2))["v"]
        if "scratch_env_other" in self.leaks and child_env["TMP"]:
            child_env["TMP"] += "-other"
        items: list[dict] = []
        for _ in range(40):
            item = _ask(base, items)
            if item["type"] != "function_call":
                break
            cmd = json.loads(item["arguments"])["cmd"]
            self.commands.append(cmd)
            output = self._answer(
                cmd, table, Path(cwd), add_dirs, Path(env["CODEX_HOME"]), child_env
            )
            items.append(
                {"type": "function_call_output", "call_id": item["call_id"], "output": output}
            )
        return 0

    def _answer(self, cmd, table, cwd: Path, add_dirs, home: Path, child_env) -> str:
        if cmd.startswith('echo "TMPDIR=$TMPDIR"'):
            return "".join(f"{n}={child_env[n]}\n" for n in ("TMPDIR", "TMP", "TEMP")) + "rc=0"
        words = [
            w.replace("$TMPDIR", child_env["TMPDIR"])
            for w in shlex.split(cmd.rsplit("; echo rc=", 1)[0])
        ]
        files = table["filesystem"]
        deny = [Path(p) for p, mode in files.items() if mode == "deny"]
        table_writes = (
            []
            if "table_write_ignored" in self.leaks
            else [Path(p) for p, m in files.items() if m == "write"]
        )
        grants = [cwd, *map(Path, add_dirs), *table_writes]
        read_only_leak = "read_only_cwd_write_refused" in self.leaks
        read_only = table.get("extends") == ":read-only"
        if read_only and not read_only_leak:
            # codex's `:read-only` built-in grants no write, the working directory included; a
            # write entry of the table is granted all the same, unless that is the leak.
            grants = [] if "read_only_write_root" in self.leaks else table_writes
        scratch = cwd.parent
        leaks = self.leaks
        # A user-layer sandbox_mode that wins over the profile is a profile that is not applied.
        ignored = "user_layer_sandbox_mode" in leaks and (home / "config.toml").exists()
        if words[0] == "/bin/sh" and words[1:4] == ["-c", ': < "$1"', "_"]:
            # The shell opens its argument for reading and reads nothing.
            target = Path(words[-1])
            refused = (_under(target, deny) and not ignored) or "opens_always_fail" in leaks
            if refused:
                return f"/bin/sh: {target}: Permission denied\nrc=1"
            return "rc=0"
        if words[0] != "/bin/sh" and words[1:2] == ["-c"]:
            # The child's connect to a unix socket: refused by the sandbox (EPERM) inside the
            # denied set, else queued on the listener there; it prints its token as the real
            # connect code does.
            target = Path(words[-1])
            network_on = table["network"]["enabled"]
            denied_path = _under(target, deny)
            listener = _LISTENERS.get(target)
            if denied_path and "unix_denied_enoent" in leaks:
                return "connect=ENOENT\nrc=1"
            if not denied_path and not network_on and "unix_control_eperm_but_reached" in leaks:
                if listener is not None:
                    listener.connect()
                return "connect=EPERM\nrc=1"
            refused = denied_path and not ignored
            refused &= SOCKET_ARMS[network_on] not in leaks
            refused &= not (read_only and "unix_read_only_run_connects" in leaks)
            refused &= not (not read_only and not network_on and "unix_light_run_connects" in leaks)
            refused |= "unix_always_fails" in leaks
            refused |= not network_on and "unix_network_off_refuses_all" in leaks
            if refused:
                return "connect=EPERM\nrc=1"
            if listener is None:
                return "connect=ENOENT\nrc=1"
            listener.connect()
            return "connect=ok\nrc=0"
        if words[0] == "/bin/cat":
            target = Path(words[-1])
            ours = scratch in target.parents
            refused = _under(target, deny) and not ignored
            refused &= not (ours and target.name == "secret.txt" and "deny_canary" in leaks)
            refused &= not (ours and target.name == "auth.json" and "own_codex_home" in leaks)
            refused |= ours and target.name == "f.txt" and "read_control" in leaks
            refused |= ours and target.name == "ro.txt" and "read_only_read_fails" in leaks
            refused |= ours and target.name == "rw.txt" and "read_only_rw_read_fails" in leaks
            if refused:
                return f"{words[0]}: {target}: Permission denied\nrc=1"
            text = target.read_text() if words[0] == "/bin/cat" and target.exists() else ""
            return f"{text}rc=0"
        if words[0] == "echo":
            target = Path(words[-1])
            if target.name.startswith("scratch-env-") and "scratch_env_write_elsewhere" in leaks:
                # The variables read right, but the write lands in the run's own TMPDIR.
                elsewhere = Path(os.environ["TMPDIR"]) / target.name
                elsewhere.write_text("w\n")
                return "rc=0"
            where = target.parent.name
            if "lionagi-probe-" in target.name:
                which = (
                    "light_tmpdir_refused" if target.name.endswith("-env") else "light_tmp_refused"
                )
                if which in leaks:
                    return "rc=0"
                return f"sh: {target}: Permission denied\nrc=1"
            allowed = ignored or (_under(target, grants) and not _under(target, deny))
            allowed |= "write_outside_refused" in leaks and where == "outside"
            broken = ("write_cwd" in leaks and where == "cwd") or (
                "write_entry" in leaks and where == "entry"
            )
            if not allowed or broken:
                return f"sh: {target}: Permission denied\nrc=1"
            target.write_text("w\n")
            return "rc=0"
        if words[0] == "curl":
            light = not table["network"]["enabled"]
            reachable = not light
            if light and "light_network_off" in leaks:
                reachable = True
            if not light and "heavy_network_on" in leaks:
                reachable = False
            # curl -s prints the body with no newline, so the step's marker follows it directly.
            return "pongrc=0" if reachable else "rc=7"
        raise AssertionError(f"unexpected probe command {cmd!r}")


# ----------------------------------------------------------------------------------- the arms
def test_a_sandbox_that_enforces_the_profile_passes_every_arm(tmp_path, sandbox_on):
    arms = probe_module.probe("codex", runner=FakeCodex(), scratch=tmp_path / "scratch")
    assert list(arms) == list(REQUIRED_ARMS)
    failed = {name: arm["evidence"] for name, arm in arms.items() if not arm["passed"]}
    assert not failed
    assert all(isinstance(arm["evidence"], str) and arm["evidence"] for arm in arms.values())


# A leak in the arm's own behaviour fails that arm; the user-layer arm repeats four of those
# checks under a config that sets a sandbox mode, so it fails with them.
_SHARED_WITH_THE_USER_LAYER_ARM = {
    "deny_canary",
    "own_codex_home",
    "read_control",
    "write_outside_refused",
}


@pytest.mark.parametrize("leak", REQUIRED_ARMS)
def test_each_arm_fails_when_its_behaviour_is_not_enforced(tmp_path, sandbox_on, leak):
    arms = probe_module.probe("codex", runner=FakeCodex((leak,)), scratch=tmp_path / "scratch")
    failed = {name for name, arm in arms.items() if not arm["passed"]}
    assert leak in failed
    allowed_company = (
        {"user_layer_sandbox_mode"} if leak in _SHARED_WITH_THE_USER_LAYER_ARM else set()
    )
    if leak == "read_only_cwd_write_refused":
        # The write-root run is read-only too, and its working-directory write lands with it.
        allowed_company = {"read_only_write_root"}
    assert failed - {leak} <= allowed_company


def test_a_run_that_never_reaches_the_provider_passes_no_arm(tmp_path, sandbox_on):
    # Codex's own startup line is not evidence: only tool outputs the stand-in received count.
    arms = probe_module.probe("codex", runner=lambda argv, env, cwd: 0, scratch=tmp_path / "s")
    assert list(arms) == list(REQUIRED_ARMS)
    assert not [name for name, arm in arms.items() if arm["passed"]]


def test_a_run_that_times_out_passes_no_arm(tmp_path, sandbox_on):
    arms = probe_module.probe("codex", runner=lambda argv, env, cwd: -1, scratch=tmp_path / "s")
    assert not [name for name, arm in arms.items() if arm["passed"]]


def test_the_read_only_arm_fails_when_its_same_run_read_fails(tmp_path, sandbox_on):
    # A child that can open nothing also refuses the write; the read tells the two apart.
    fake = FakeCodex(("read_only_read_fails",))
    arms = probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    assert {name for name, arm in arms.items() if not arm["passed"]} == {
        "read_only_cwd_write_refused"
    }


def test_the_read_only_run_starts_from_the_read_only_built_in(tmp_path, sandbox_on):
    fake = FakeCodex()
    probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    tables = []
    for argv, _env, _cwd in fake.runs:
        row = next(a for a in argv if a.startswith(f"permissions.{PERMISSION_PROFILE}="))
        tables.append(_toml_loads("table = " + row.split("=", 1)[1])["table"])
    # Two runs extend the read-only built-in: the read-only run grants no write at all, and the
    # write-root run grants exactly one write entry.
    read_only = [t for t in tables if t["extends"] == ":read-only"]
    assert len(read_only) == 2
    assert all(t["network"]["enabled"] is False for t in read_only)
    writes = sorted(
        len([p for p, mode in t["filesystem"].items() if mode == "write"]) for t in read_only
    )
    assert writes == [0, 1]


def test_the_probe_runs_codex_under_the_profile_a_launch_builds(tmp_path, sandbox_on):
    fake = FakeCodex()
    probe_module.probe("/opt/codex", runner=fake, scratch=tmp_path / "scratch")
    assert len(fake.runs) == 6
    networks = []
    for argv, env, cwd in fake.runs:
        assert argv[0] == "/opt/codex"
        assert "--sandbox" not in argv
        assert "-s" not in argv
        assert f'default_permissions="{PERMISSION_PROFILE}"' in argv
        row = next(a for a in argv if a.startswith(f"permissions.{PERMISSION_PROFILE}="))
        table = _toml_loads("table = " + row.split("=", 1)[1])["table"]
        # Each run denies its own scratch CODEX_HOME, as a launch denies the child's.
        assert table["filesystem"][env["CODEX_HOME"]] == "deny"
        assert Path(env["CODEX_HOME"]).parent == Path(cwd).parent
        networks.append(table["network"]["enabled"])
    # The scratch run is light, so its network is off.
    assert sorted(networks) == [False, False, False, False, True, True]


def test_the_probe_carries_the_profile_overrides_a_launch_renders(tmp_path, sandbox_on):
    from lionhub.providers.openai.codex import _permission_profile_overrides

    fake = FakeCodex()
    probe_module.probe("/opt/codex", runner=fake, scratch=tmp_path / "scratch")
    for argv, _env, _cwd in fake.runs:
        row = next(a for a in argv if a.startswith(f"permissions.{PERMISSION_PROFILE}="))
        table = _toml_loads("table = " + row.split("=", 1)[1])["table"]
        pair = _permission_profile_overrides(table)
        start = argv.index(pair[1]) - 1
        assert argv[start : start + len(pair)] == pair


def test_the_probe_command_line_turns_apps_and_plugins_off_as_a_contained_launch_does(tmp_path):
    table = {"extends": ":read-only", "network": {"enabled": False}}
    argv = probe_module._argv("/opt/codex", "http://127.0.0.1:9", table, tmp_path, ())
    assert argv[:2] == ["/opt/codex", "exec"]
    disabled = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"]
    assert disabled == ["apps", "plugins"]
    # Flags of `codex exec`, so they sit after the subcommand and before the prompt.
    assert argv.index("--disable") < argv.index("probe")


def test_every_probe_run_turns_codex_apps_and_plugins_off(tmp_path, sandbox_on):
    fake = FakeCodex()
    probe_module.probe("/opt/codex", runner=fake, scratch=tmp_path / "scratch")
    assert len(fake.runs) == 6
    for argv, _env, _cwd in fake.runs:
        disabled = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"]
        assert disabled == ["apps", "plugins"]
        assert argv.index("exec") < argv.index("--disable") < argv.index("probe")


def test_a_tilde_deny_entry_expands_against_the_home_the_probe_child_gets(tmp_path, monkeypatch):
    import lionhub.agent.child_sandbox as child_sandbox

    child_home = tmp_path / "child-home"
    child_home.mkdir()
    monkeypatch.setenv("HOME", str(child_home))
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"deny_read": ["~/vault"]}}
    )
    fake = FakeCodex()
    probe_module.probe("/opt/codex", runner=fake, scratch=tmp_path / "scratch")
    for argv, env, _cwd in fake.runs:
        assert env["HOME"] == str(child_home)
        row = next(a for a in argv if a.startswith(f"permissions.{PERMISSION_PROFILE}="))
        denied = _toml_loads("table = " + row.split("=", 1)[1])["table"]["filesystem"]
        assert denied.get(str(child_home / "vault")) == "deny"


def test_a_real_denied_path_is_opened_for_zero_bytes_and_never_read(
    tmp_path, sandbox_on, monkeypatch
):
    # No cap, so the planted file is opened whatever the host's own denied files fill first.
    monkeypatch.setattr(probe_module, "_REAL_PATHS_OPENED", 1000)
    fake = FakeCodex()
    scratch = tmp_path / "scratch"
    probe_module.probe("codex", runner=fake, scratch=scratch)
    opens = []
    for command in fake.commands:
        words = shlex.split(command.rsplit("; echo rc=", 1)[0])
        assert words[0] != "head"
        if words[0] == "/bin/cat":
            assert words[1].startswith(str(scratch))
        if words[0] == "/bin/sh":
            assert words[1:4] == ["-c", ': < "$1"', "_"]
            opens.append(words[4])
    # The first open is the control of a readable file the profile allows.
    assert opens and opens[0].startswith(str(scratch))
    # The denied file the fixture planted outside the scratch directory is among the real opens.
    assert str(tmp_path / "planted-deny" / "credentials") in opens[1:]


def test_the_stand_in_answers_each_step_in_order_then_a_final_message():
    stub = probe_module.StubProvider([{"cmd": "echo a"}, {"cmd": "echo b"}])
    first = _parse_sse(stub._reply({"input": [{"type": "message"}]}).decode())
    assert (first["type"], first["call_id"], first["name"]) == (
        "function_call",
        "call_0",
        "exec_command",
    )
    assert json.loads(first["arguments"]) == {"cmd": "echo a"}
    done0 = {"type": "function_call_output", "call_id": "call_0", "output": "x"}
    second = _parse_sse(stub._reply({"input": [done0]}).decode())
    assert (second["type"], second["call_id"]) == ("function_call", "call_1")
    assert json.loads(second["arguments"]) == {"cmd": "echo b"}
    done1 = {**done0, "call_id": "call_1"}
    last = _parse_sse(stub._reply({"input": [done0, done1]}).decode())
    assert last["type"] == "message"


def _can_bind_loopback() -> bool:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(
    not _can_bind_loopback(), reason="this host refuses to bind a loopback port to the test"
)
def test_the_socket_backed_stand_in_serves_steps_and_ping_over_loopback():
    stub = _REAL_STUB([{"cmd": "echo a"}])
    stub.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{stub.base_url}/ping", timeout=10) as response:
            assert response.read() == b"pong"
        post = urllib.request.Request(
            f"{stub.base_url}/responses", data=json.dumps({"input": []}).encode(), method="POST"
        )
        with opener.open(post, timeout=10) as response:
            item = _parse_sse(response.read().decode())
    finally:
        stub.stop()
    assert item["call_id"] == "call_0"
    assert stub.requests == [{"input": []}]


def test_tool_outputs_are_collected_by_call_id():
    requests = [
        {"input": [{"type": "message"}]},
        {
            "input": [
                {"type": "function_call_output", "call_id": "call_0", "output": "a"},
                {"type": "function_call_output", "call_id": "call_1", "output": {"k": 1}},
            ]
        },
    ]
    assert probe_module.tool_outputs(requests) == {"call_0": "a", "call_1": '{"k": 1}'}


# --------------------------------------------------------------------------------- the record
def test_a_written_record_is_found_by_the_launch_for_the_same_binary_and_version(
    tmp_path, record_dir
):
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    assert path.parent == record_dir
    record = json.loads(path.read_text())
    assert record["format"] == 6
    assert record["sha256"] == binary_sha256(os.path.realpath(binary))
    assert record["binary"] == os.path.realpath(binary)
    assert record["version"] == "0.162.1"
    assert set(record["arms"]) == set(REQUIRED_ARMS)
    assert require_passing_probe_record(str(binary)) == path
    assert probe_record(str(binary))[:1] == (path,)


def test_the_record_is_keyed_by_the_resolved_path(tmp_path, record_dir):
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    link = tmp_path / "link"
    link.symlink_to(binary)
    # A link to the probed binary runs the same code, so it finds the same record.
    assert require_passing_probe_record(str(link)) == path
    # A copy at another path is a different binary until it is probed itself.
    other = _fake_binary(tmp_path / "other")
    with pytest.raises(CLIInputRefusedError, match="no probe record") as refused:
        require_passing_probe_record(str(other))
    assert PROBE_COMMAND in str(refused.value)


def test_the_record_is_keyed_by_the_version(tmp_path, record_dir):
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    _fake_binary(tmp_path / "bin", version="0.163.0")  # the same path, upgraded in place
    with pytest.raises(CLIInputRefusedError, match="no probe record for codex 0.163.0"):
        require_passing_probe_record(str(binary))
    # A record file moved to the new version's name still names the old version.
    real = os.path.realpath(binary)
    moved = probe_record_path(real, "0.163.0", binary_sha256(real))
    shutil.copy(path, moved)
    with pytest.raises(CLIInputRefusedError, match="is not for codex 0.163.0"):
        require_passing_probe_record(str(binary))


@pytest.mark.parametrize("earlier", [1, 2, 3])
def test_a_record_of_another_format_is_no_record(tmp_path, record_dir, earlier):
    # Format 2 records predate the read-only arm and format 3 records the per-network-state
    # socket arms, so neither admits a launch.
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    record = json.loads(path.read_text())
    record["format"] = earlier
    path.write_text(json.dumps(record))
    with pytest.raises(CLIInputRefusedError, match=f"is of format {earlier}"):
        require_passing_probe_record(str(binary))


def test_the_record_is_keyed_by_the_bytes_of_the_binary(tmp_path, record_dir):
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    assert require_passing_probe_record(str(binary)) == path
    assert json.loads(path.read_text())["sha256"] == binary_sha256(os.path.realpath(binary))
    # The same path and the same version, but other bytes: a replaced binary has no record.
    binary.write_text(binary.read_text() + "# replaced\n")
    with pytest.raises(CLIInputRefusedError, match="no probe record for codex 0.162.1") as refused:
        require_passing_probe_record(str(binary))
    assert PROBE_COMMAND in str(refused.value)
    # A record file copied to the replaced binary's name still names the old bytes.
    real = os.path.realpath(binary)
    shutil.copy(path, probe_record_path(real, "0.162.1", binary_sha256(real)))
    with pytest.raises(CLIInputRefusedError, match="is not for codex 0.162.1"):
        require_passing_probe_record(str(binary))


def _count_reads(monkeypatch, real: str) -> list[str]:
    """Every open of the file at ``real`` by the module that hashes codex binaries."""
    import builtins

    from lionhub.providers.openai import _codex_permissions as permissions

    reads: list[str] = []

    def counting_open(file, *args, **kwargs):
        if os.fspath(file) == real:
            reads.append(real)
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(permissions, "open", counting_open, raising=False)
    return reads


def test_a_launch_reads_the_binary_once_and_not_again_while_it_is_unchanged(
    tmp_path, record_dir, monkeypatch
):
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    reads = _count_reads(monkeypatch, os.path.realpath(binary))
    # Writing the record read the binary; launches on the same bytes read it no more.
    assert require_passing_probe_record(str(binary)) == path
    assert require_passing_probe_record(str(binary)) == path
    assert reads == []
    # Other bytes and another size: the next launch reads the binary once, and is refused.
    binary.write_text(binary.read_text() + "# replaced\n")
    with pytest.raises(CLIInputRefusedError, match="no probe record for codex 0.162.1"):
        require_passing_probe_record(str(binary))
    assert len(reads) == 1


def test_a_record_found_for_the_binary_is_checked_against_the_digest_it_was_found_by(
    tmp_path, record_dir, monkeypatch
):
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    binary.write_text(binary.read_text() + "# replaced\n")
    real = os.path.realpath(binary)
    shutil.copy(path, probe_record_path(real, "0.162.1", binary_sha256(real)))
    # A new modification time, so the launch reads the bytes afresh rather than from memory.
    seen = os.stat(real)
    os.utime(real, ns=(seen.st_atime_ns, seen.st_mtime_ns + 1_000_000_000))
    reads = _count_reads(monkeypatch, real)
    with pytest.raises(CLIInputRefusedError, match="is not for codex 0.162.1"):
        require_passing_probe_record(str(binary))
    # The record is found by one digest and checked against that digest, so one read.
    assert len(reads) == 1


def test_the_digest_is_read_again_when_the_size_or_the_modification_time_changes(tmp_path):
    import hashlib

    target = tmp_path / "binary"
    target.write_bytes(b"one")
    assert binary_sha256(str(target)) == hashlib.sha256(b"one").hexdigest()
    # The same size: only the modification time tells the bytes apart.
    target.write_bytes(b"two")
    seen = target.stat()
    os.utime(target, ns=(seen.st_atime_ns, seen.st_mtime_ns + 1_000_000_000))
    assert binary_sha256(str(target)) == hashlib.sha256(b"two").hexdigest()
    # Another size, the modification time put back to what it was.
    target.write_bytes(b"three")
    os.utime(target, ns=(seen.st_atime_ns, seen.st_mtime_ns + 1_000_000_000))
    assert binary_sha256(str(target)) == hashlib.sha256(b"three").hexdigest()
    assert binary_sha256(str(tmp_path / "missing")) is None


def test_the_binary_is_looked_up_on_the_path_of_the_given_environment(
    tmp_path, record_dir, monkeypatch
):
    probed = _fake_binary(tmp_path / "probed")
    other = _fake_binary(tmp_path / "other")
    path = probe_module.write_record(str(probed), _passing_arms())
    # The parent's PATH finds the unprobed binary first; the child's PATH finds the probed one.
    monkeypatch.setenv("PATH", f"{other.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    child = {"PATH": str(probed.parent)}
    assert require_passing_probe_record("codex", env=child, cwd=tmp_path) == path
    with pytest.raises(CLIInputRefusedError, match="no probe record"):
        require_passing_probe_record("codex", env={"PATH": str(other.parent)}, cwd=tmp_path)
    with pytest.raises(CLIInputRefusedError, match="no probe record"):
        require_passing_probe_record("codex", cwd=tmp_path)


def test_a_record_with_a_failing_arm_is_refused(tmp_path, record_dir):
    binary = _fake_binary(tmp_path / "bin")
    arms = _passing_arms()
    arms["write_entry"] = {"passed": False, "evidence": "rc=1 denied"}
    probe_module.write_record(str(binary), arms)
    with pytest.raises(CLIInputRefusedError, match="did not pass: write_entry") as refused:
        require_passing_probe_record(str(binary))
    assert PROBE_COMMAND in str(refused.value)


def test_a_record_missing_an_arm_is_refused(tmp_path, record_dir):
    binary = _fake_binary(tmp_path / "bin")
    arms = _passing_arms()
    del arms["deny_canary"]
    probe_module.write_record(str(binary), arms)
    with pytest.raises(CLIInputRefusedError, match="did not pass: deny_canary"):
        require_passing_probe_record(str(binary))


def test_a_binary_whose_version_cannot_be_read_has_no_record(tmp_path, record_dir):
    broken = tmp_path / "bin" / "codex"
    broken.parent.mkdir()
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(broken.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(CLIInputRefusedError, match="cannot be read"):
        require_passing_probe_record(str(broken))
    with pytest.raises(RuntimeError, match="cannot be read"):
        probe_module.write_record(str(broken), _passing_arms())


# ------------------------------------------------------------------------------------- main
def test_main_writes_the_record_and_exits_zero_when_every_arm_passes(
    tmp_path, record_dir, monkeypatch, capsys
):
    binary = _fake_binary(tmp_path / "bin")
    monkeypatch.setattr(probe_module, "probe", lambda path: _passing_arms())
    assert probe_module.main(["--binary", str(binary)]) == 0
    assert "record:" in capsys.readouterr().out
    assert require_passing_probe_record(str(binary)).parent == record_dir


def test_main_exits_nonzero_and_leaves_a_record_a_launch_refuses_when_an_arm_fails(
    tmp_path, record_dir, monkeypatch, capsys
):
    binary = _fake_binary(tmp_path / "bin")
    arms = _passing_arms()
    arms["deny_canary"] = {"passed": False, "evidence": "rc=0 DENY-CANARY"}
    monkeypatch.setattr(probe_module, "probe", lambda path: arms)
    assert probe_module.main(["--binary", str(binary)]) == 1
    assert "FAIL  deny_canary" in capsys.readouterr().out
    with pytest.raises(CLIInputRefusedError, match="did not pass: deny_canary"):
        require_passing_probe_record(str(binary))


def test_main_reports_a_missing_codex_binary(monkeypatch, capsys):
    monkeypatch.setattr(probe_module.shutil, "which", lambda name: None)
    assert probe_module.main([]) == 2
    assert "not on PATH" in capsys.readouterr().err


# ------------------------------------------------------------- arms that test what they say
def _failed_arm_names(arms: dict[str, dict]) -> set[str]:
    return {name for name, arm in arms.items() if not arm["passed"]}


def test_a_deny_arm_fails_when_its_same_run_control_open_fails(tmp_path, sandbox_on):
    # A shell that cannot open anything also "refuses" every denied path; the control open of a
    # readable file in the same run tells the two apart.
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("opens_always_fail",)), scratch=tmp_path / "scratch"
    )
    assert not arms["deny_canary"]["passed"]
    assert "control open of a readable file succeeded: False" in arms["deny_canary"]["evidence"]


def test_the_deny_arm_fails_when_no_real_denied_path_was_opened(tmp_path, sandbox_on, monkeypatch):
    # A host with none of the denied files has nothing to open; an arm that tested nothing does
    # not pass.
    monkeypatch.setattr(probe_module, "_REAL_PATHS_OPENED", 0)
    arms = probe_module.probe("codex", runner=FakeCodex(), scratch=tmp_path / "scratch")
    assert not arms["deny_canary"]["passed"]
    assert "0 real denied path(s) opened for 0 bytes" in arms["deny_canary"]["evidence"]


def test_the_control_open_is_the_same_command_on_a_readable_file(tmp_path, sandbox_on):
    fake = FakeCodex()
    probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    opens = [
        shlex.split(c.rsplit("; echo rc=", 1)[0])
        for c in fake.commands
        if c.startswith("/bin/sh -c")
    ]
    assert opens
    assert all(words[:4] == ["/bin/sh", "-c", ': < "$1"', "_"] for words in opens)
    assert opens[0][4].endswith(os.path.join("open", "f.txt"))


def test_the_write_entry_is_granted_by_the_profile_table_alone(tmp_path, sandbox_on):
    fake = FakeCodex()
    arms = probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    assert arms["write_entry"]["passed"]
    assert all("--add-dir" not in argv for argv, _, _ in fake.runs)


def test_the_write_entry_arm_fails_when_only_add_dir_would_grant_it(tmp_path, sandbox_on):
    # A sandbox that ignores the table's write entry passes the arm only if --add-dir covers it.
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("table_write_ignored",)), scratch=tmp_path / "scratch"
    )
    # The write-root run's entry and the scratch run's are granted by their tables alone too.
    assert _failed_arm_names(arms) == {"write_entry", "read_only_write_root", "scratch_env"}


def test_the_scratch_env_arm_fails_when_one_variable_is_not_set(tmp_path, sandbox_on):
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("scratch_env_partial",)), scratch=tmp_path / "scratch"
    )
    assert _failed_arm_names(arms) == {"scratch_env"}
    assert "TEMP=''" in arms["scratch_env"]["evidence"]


def test_the_scratch_env_arm_fails_when_one_variable_names_another_directory(tmp_path, sandbox_on):
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("scratch_env_other",)), scratch=tmp_path / "scratch"
    )
    assert _failed_arm_names(arms) == {"scratch_env"}
    assert "-other'" in arms["scratch_env"]["evidence"]


def test_the_scratch_env_arm_fails_when_the_file_lands_elsewhere(tmp_path, sandbox_on):
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("scratch_env_write_elsewhere",)), scratch=tmp_path / "scratch"
    )
    assert _failed_arm_names(arms) == {"scratch_env"}
    assert "did not land" in arms["scratch_env"]["evidence"]


def test_only_the_scratch_run_sets_the_three_variables_after_its_profile(tmp_path, sandbox_on):
    fake = FakeCodex()
    arms = probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    assert arms["scratch_env"]["passed"]
    keyed = [argv for argv, _, _ in fake.runs if any("shell_environment_policy" in a for a in argv)]
    assert len(keyed) == 1 and len(fake.runs) == 6
    argv = keyed[0]
    sets = [a for a in argv if a.startswith("shell_environment_policy.set.")]
    assert [a.split("=", 1)[0].rsplit(".", 1)[1] for a in sets] == ["TMPDIR", "TMP", "TEMP"]
    values = {_toml_loads("v = " + a.split("=", 1)[1])["v"] for a in sets}
    assert len(values) == 1 and Path(values.pop()).name == "envscratch"
    row = next(i for i, a in enumerate(argv) if a.startswith(f"permissions.{PERMISSION_PROFILE}="))
    assert row < argv.index(sets[0]) < argv.index("-C")


def test_the_scratch_root_defaults_under_the_lionagi_state_directory(tmp_path, monkeypatch):
    import lionagi._paths as paths

    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path / "home")
    parent = probe_module._scratch_parent(None)
    assert parent == Path(os.path.realpath(tmp_path / "home" / "state" / "codex-probe"))
    assert parent.is_dir()


def test_a_scratch_root_inside_a_temp_root_is_refused(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    monkeypatch.setattr(probe_module, "_temp_roots", lambda: [temp, Path(os.path.realpath(temp))])
    with pytest.raises(probe_module.ScratchRootError):
        probe_module._scratch_parent(temp / "scratch")
    assert probe_module._scratch_parent(tmp_path / "elsewhere").is_dir()


def test_the_temp_roots_name_slash_tmp_and_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "scratch-tmp"))
    roots = _REAL_TEMP_ROOTS()
    assert Path("/tmp") in roots
    assert Path(os.path.realpath("/tmp")) in roots
    assert tmp_path / "scratch-tmp" in roots


def test_with_tmpdir_unset_the_second_light_arm_is_skipped_not_run_on_tmp_again(
    tmp_path, sandbox_on, monkeypatch
):
    monkeypatch.delenv("TMPDIR", raising=False)
    fake = FakeCodex()
    arms = probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    assert list(arms) == list(REQUIRED_ARMS)
    skipped = arms["light_tmpdir_refused"]
    assert skipped["skipped"] is True
    assert skipped["passed"] is False
    assert "TMPDIR" in skipped["evidence"]
    assert len([c for c in fake.commands if "lionagi-probe-" in c]) == 1
    assert _failed_arm_names(arms) == {"light_tmpdir_refused"}


def test_a_skipped_conditional_arm_satisfies_the_record_only_while_tmpdir_is_unset(
    tmp_path, record_dir
):
    binary = _fake_binary(tmp_path / "bin")
    arms = _passing_arms()
    arms["light_tmpdir_refused"] = {
        "passed": False,
        "skipped": True,
        "evidence": "skipped: TMPDIR is unset",
    }
    probe_module.write_record(str(binary), arms)
    without = {"PATH": os.environ.get("PATH", "")}
    require_passing_probe_record(str(binary), env=without)
    with pytest.raises(CLIInputRefusedError, match="light_tmpdir_refused"):
        require_passing_probe_record(str(binary), env={**without, "TMPDIR": str(tmp_path)})


def test_an_arm_that_is_not_conditional_cannot_be_skipped_into_a_pass(tmp_path, record_dir):
    binary = _fake_binary(tmp_path / "bin")
    arms = _passing_arms()
    arms["deny_canary"] = {"passed": False, "skipped": True, "evidence": "skipped"}
    probe_module.write_record(str(binary), arms)
    with pytest.raises(CLIInputRefusedError, match="did not pass: deny_canary"):
        require_passing_probe_record(str(binary), env={"PATH": os.environ.get("PATH", "")})


_ON, _OFF = SOCKET_ARMS[True], SOCKET_ARMS[False]


def test_the_network_on_arm_fails_when_the_denied_socket_accepts_a_connection(tmp_path, sandbox_on):
    arms = probe_module.probe("codex", runner=FakeCodex((_ON,)), scratch=tmp_path / "scratch")
    assert _failed_arm_names(arms) == {_ON}
    assert "accepted 1 connection" in arms[_ON]["evidence"]
    assert "accepted 0 connection" in arms[_OFF]["evidence"]


def test_the_network_off_arm_fails_when_both_network_off_runs_connect(tmp_path, sandbox_on):
    arms = probe_module.probe("codex", runner=FakeCodex((_OFF,)), scratch=tmp_path / "scratch")
    assert _failed_arm_names(arms) == {_OFF}
    evidence = arms[_OFF]["evidence"]
    assert "light run: the denied-path socket accepted 1" in evidence
    assert "read-only run: the denied-path socket accepted 1" in evidence


@pytest.mark.parametrize(
    ("leak", "run"),
    [("unix_light_run_connects", "light"), ("unix_read_only_run_connects", "read-only")],
)
def test_the_network_off_arm_needs_both_of_its_runs_to_refuse(tmp_path, sandbox_on, leak, run):
    arms = probe_module.probe("codex", runner=FakeCodex((leak,)), scratch=tmp_path / "scratch")
    assert _failed_arm_names(arms) == {_OFF}
    assert f"{run} run: the denied-path socket accepted 1" in arms[_OFF]["evidence"]


def test_a_passing_record_names_each_network_off_case_and_keeps_every_token(tmp_path, sandbox_on):
    arms = probe_module.probe("codex", runner=FakeCodex(), scratch=tmp_path / "scratch")
    assert _failed_arm_names(arms) == set()
    assert arms[_OFF]["cases"] == {"light": "control reached", "read_only": "control reached"}
    for arm, runs in ((_ON, ("heavy",)), (_OFF, ("light", "read_only"))):
        for run in runs:
            connects = arms[arm]["connects"][run]
            assert connects["child"] == {"denied": "EPERM", "control": "ok"}
            assert connects["in_run"] == {"denied": 0, "control": 1}
            assert connects["outside"] == {
                "before": {"denied": "ok", "control": "ok"},
                "after": {"denied": "ok", "control": "ok"},
            }


def test_the_network_on_arm_needs_its_control_reached(tmp_path, sandbox_on):
    # Every connect refused: the network-on arm fails, the network-off arm passes as "control
    # refused", since a sandbox that refuses every local socket with the network off refuses
    # the denied one too.
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("unix_always_fails",)), scratch=tmp_path / "scratch"
    )
    assert _failed_arm_names(arms) == {_ON}
    assert arms[_OFF]["cases"] == {"light": "control refused", "read_only": "control refused"}


def test_a_network_off_run_that_refuses_every_socket_passes_as_control_refused(
    tmp_path, sandbox_on
):
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("unix_network_off_refuses_all",)), scratch=tmp_path / "scratch"
    )
    assert _failed_arm_names(arms) == set()
    assert arms[_OFF]["cases"] == {"light": "control refused", "read_only": "control refused"}
    assert "control refused: no local socket connects at all" in arms[_OFF]["evidence"]
    assert arms[_OFF]["connects"]["light"]["in_run"] == {"denied": 0, "control": 0}


def test_a_denied_connect_that_finds_no_socket_is_not_a_refusal(tmp_path, sandbox_on):
    # ENOENT is not the sandbox refusing, with the network on or off.
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("unix_denied_enoent",)), scratch=tmp_path / "scratch"
    )
    assert _failed_arm_names(arms) == {_ON, _OFF}
    assert "connect=ENOENT for the denied socket" in arms[_ON]["evidence"]
    assert "connect=ENOENT for the denied socket" in arms[_OFF]["evidence"]
    assert arms[_OFF]["cases"] == {"light": None, "read_only": None}


def test_a_control_that_printed_eperm_but_reached_its_listener_fails(tmp_path, sandbox_on):
    arms = probe_module.probe(
        "codex", runner=FakeCodex(("unix_control_eperm_but_reached",)), scratch=tmp_path / "scratch"
    )
    assert _failed_arm_names(arms) == {_OFF}
    assert "neither reached its listener nor was refused" in arms[_OFF]["evidence"]


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("how", ["token", "mark"])
def test_the_socket_arms_fail_closed_when_the_outside_control_does_not_reach(
    tmp_path, sandbox_on, monkeypatch, when, how
):
    # The probe's own connect from outside the sandbox either prints no connect=ok, or prints it
    # and its mark is never recorded: no count is read for that run, and every socket arm fails.
    def outside(path, mark):
        if not mark.endswith(f"-{when}"):
            return _outside_in_memory(path, mark)
        if how == "token":
            return "ECONNREFUSED"
        return "ok"

    monkeypatch.setattr(probe_module, "_outside_connect", outside)
    arms = probe_module.probe("codex", runner=FakeCodex(), scratch=tmp_path / "scratch")
    assert _failed_arm_names(arms) == {_ON, _OFF}
    for arm in (_ON, _OFF):
        assert "did not reach both listeners" in arms[arm]["evidence"]
        assert "fails closed" in arms[arm]["evidence"]


def test_the_socket_arms_fail_closed_when_a_listener_cannot_be_bound(
    tmp_path, sandbox_on, monkeypatch
):
    monkeypatch.setattr(probe_module, "_start_listener", lambda path: (None, "OSError: too long"))
    arms = probe_module.probe("codex", runner=FakeCodex(), scratch=tmp_path / "scratch")
    assert _failed_arm_names(arms) == {_ON, _OFF}
    assert "fails closed" in arms[_ON]["evidence"]
    assert "fails closed" in arms[_OFF]["evidence"]


def test_the_light_and_read_only_runs_carry_the_two_connects(tmp_path, sandbox_on):
    fake = FakeCodex()
    probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    connects = [c for c in fake.commands if "socket.AF_UNIX" in c]
    # Two in each of the heavy, light and read-only runs.
    assert len(connects) == 6
    assert sum("d.sock" in c for c in connects) == 3
    assert sum("c.sock" in c for c in connects) == 3


def test_the_write_root_run_is_read_only_with_one_write_entry(tmp_path, sandbox_on):
    fake = FakeCodex()
    arms = probe_module.probe("codex", runner=fake, scratch=tmp_path / "scratch")
    assert arms["read_only_write_root"]["passed"]
    tables = [
        _toml_loads("t = " + a.split("=", 1)[1])["t"]
        for argv, _, _ in fake.runs
        for a in argv
        if a.startswith(f"permissions.{PERMISSION_PROFILE}=")
    ]
    read_only_writes = [
        [p for p, m in t["filesystem"].items() if m == "write"]
        for t in tables
        if t["extends"] == ":read-only"
    ]
    assert sorted(len(w) for w in read_only_writes) == [0, 1]
    assert read_only_writes[1 if read_only_writes[0] == [] else 0][0].endswith("/scratch")


@pytest.mark.parametrize(
    "leak", ["read_only_write_root", "read_only_cwd_write_refused", "read_only_rw_read_fails"]
)
def test_the_write_root_arm_fails_on_each_of_its_three_checks(tmp_path, sandbox_on, leak):
    arms = probe_module.probe("codex", runner=FakeCodex((leak,)), scratch=tmp_path / "scratch")
    assert "read_only_write_root" in _failed_arm_names(arms)


# ------------------------------------------------------------------ the gate, per network state
def _table(*, light=False, read_only=False, network=False, read_only_write=()) -> dict:
    return permission_profile_table(
        deny=[],
        write=[],
        light=light,
        read_only=read_only,
        network=network,
        read_only_write=read_only_write,
    )


#: Each launch the amendment's table names, by the table it renders.
_LAUNCHES = {
    "light": _table(light=True),
    "read-only": _table(read_only=True),
    "heavy, network off": _table(),
    "heavy, network on": _table(network=True),
}


def _record_with(tmp_path, **failed: bool) -> Path:
    binary = _fake_binary(tmp_path / "bin")
    arms = _passing_arms()
    for name, fails in failed.items():
        if fails:
            arms[name] = {"passed": False, "evidence": "rc=0 accepted 1 connection(s)"}
    probe_module.write_record(str(binary), arms)
    return binary


def _admitted(binary: Path, table) -> bool:
    try:
        require_passing_probe_record(str(binary), env={"PATH": ""}, table=table)
    except CLIInputRefusedError:
        return False
    return True


@pytest.mark.parametrize(
    ("on_fails", "off_fails", "expected"),
    [
        (
            True,
            False,
            {"light": 1, "read-only": 1, "heavy, network off": 1, "heavy, network on": 0},
        ),
        (
            False,
            True,
            {"light": 0, "read-only": 0, "heavy, network off": 0, "heavy, network on": 1},
        ),
        (True, True, {"light": 0, "read-only": 0, "heavy, network off": 0, "heavy, network on": 0}),
        (
            False,
            False,
            {"light": 1, "read-only": 1, "heavy, network off": 1, "heavy, network on": 1},
        ),
    ],
)
def test_each_launch_needs_the_socket_arm_of_its_own_network_state(
    tmp_path, record_dir, on_fails, off_fails, expected
):
    # The amendment's outcome table, column by column.
    binary = _record_with(tmp_path, **{_ON: on_fails, _OFF: off_fails})
    got = {name: int(_admitted(binary, table)) for name, table in _LAUNCHES.items()}
    assert got == expected


def test_a_network_on_refusal_names_the_binary_the_arm_and_both_remedies(tmp_path, record_dir):
    binary = _record_with(tmp_path, **{_ON: True})
    with pytest.raises(CLIInputRefusedError) as refused:
        require_passing_probe_record(
            str(binary), env={"PATH": ""}, table=_LAUNCHES["heavy, network on"]
        )
    text = str(refused.value)
    assert "codex 0.162.1" in text
    assert repr(os.path.realpath(binary)) in text
    assert _ON in text
    assert "sandbox_workspace_write.network_access = false" in text
    assert "workload: light" in text
    # Its own remedies only: switching the sandbox off is not offered for this arm.
    assert "child_sandbox" not in text


def test_a_network_on_refusal_offers_no_setting_when_the_network_off_arm_failed_too(
    tmp_path, record_dir
):
    # The both-fail column read as text: turning the network off would meet a second refusal.
    binary = _record_with(tmp_path, **{_ON: True, _OFF: True})
    with pytest.raises(CLIInputRefusedError) as refused:
        require_passing_probe_record(
            str(binary), env={"PATH": ""}, table=_LAUNCHES["heavy, network on"]
        )
    text = str(refused.value)
    assert "codex 0.162.1" in text
    assert repr(os.path.realpath(binary)) in text
    assert _ON in text
    assert _OFF in text
    assert "no setting admits a profiled launch on this binary" in text
    assert f"{PROBE_COMMAND} --binary" in text
    assert "network_access" not in text
    assert "workload" not in text
    assert "child_sandbox" not in text


def test_a_network_off_refusal_says_no_setting_admits_it_and_names_the_probe(tmp_path, record_dir):
    binary = _record_with(tmp_path, **{_OFF: True})
    for name in ("light", "read-only", "heavy, network off"):
        with pytest.raises(CLIInputRefusedError) as refused:
            require_passing_probe_record(str(binary), env={"PATH": ""}, table=_LAUNCHES[name])
        text = str(refused.value)
        assert "codex 0.162.1" in text
        assert _OFF in text
        assert "No setting admits a launch with the network off" in text
        assert f"{PROBE_COMMAND} --binary" in text


@pytest.mark.parametrize("earlier", [3, 4, 5])
def test_an_earlier_format_record_admits_no_launch(tmp_path, record_dir, earlier):
    binary = _record_with(tmp_path)
    path, record, _resolved, _version = probe_record(str(binary))
    record["format"] = earlier
    path.write_text(json.dumps(record), encoding="utf-8")
    for table in (*_LAUNCHES.values(), None):
        with pytest.raises(CLIInputRefusedError, match=f"is of format {earlier}"):
            require_passing_probe_record(str(binary), env={"PATH": ""}, table=table)


def test_without_a_table_every_arm_is_needed(tmp_path, record_dir):
    assert launch_arms(None) == REQUIRED_ARMS
    assert launch_arms({"network": {"enabled": "yes"}}) == REQUIRED_ARMS
    for arm in (_ON, _OFF, "read_only_write_root", "scratch_env"):
        binary = _record_with(tmp_path / arm, **{arm: True})
        assert not _admitted(binary, None)


def test_a_scratch_launch_needs_the_scratch_env_arm_and_one_without_scratch_does_not(
    tmp_path, record_dir
):
    binary = _record_with(tmp_path, scratch_env=True)
    for name, table in _LAUNCHES.items():
        # The control: the same launch without a scratch directory starts on this record.
        require_passing_probe_record(str(binary), env={"PATH": ""}, table=table)
        with pytest.raises(CLIInputRefusedError, match="scratch_env"):
            require_passing_probe_record(str(binary), env={"PATH": ""}, table=table, scratch=True)
    assert "scratch_env" not in launch_arms(_LAUNCHES["light"])
    assert "scratch_env" in launch_arms(_LAUNCHES["light"], scratch=True)


def test_a_record_whose_scratch_env_arm_passed_admits_a_scratch_launch(tmp_path, record_dir):
    binary = _record_with(tmp_path)
    for table in _LAUNCHES.values():
        require_passing_probe_record(str(binary), env={"PATH": ""}, table=table, scratch=True)


def test_the_write_root_arm_is_needed_only_by_a_read_only_table_with_a_write_entry(
    tmp_path, record_dir
):
    binary = _record_with(tmp_path, read_only_write_root=True)
    for table in _LAUNCHES.values():
        assert _admitted(binary, table)
    scratch = _table(read_only=True, read_only_write=[str(tmp_path / "scratch")])
    assert not _admitted(binary, scratch)
    assert "read_only_write_root" in launch_arms(scratch)


@pytest.mark.parametrize(
    "env",
    [
        {"PATH": ""},
        {"PATH": "", "TMPDIR": "/nonexistent"},
        {"PATH": "", "CODEX_HOME": "/nonexistent", "LIONAGI_CHILD_SANDBOX": "0"},
    ],
)
def test_the_environment_does_not_change_the_socket_answer(tmp_path, record_dir, env):
    binary = _record_with(tmp_path, **{_ON: True})
    with pytest.raises(CLIInputRefusedError, match=_ON):
        require_passing_probe_record(str(binary), env=env, table=_LAUNCHES["heavy, network on"])
    require_passing_probe_record(str(binary), env=env, table=_LAUNCHES["light"])


def test_the_unix_listener_counts_the_connections_that_reach_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a relative socket name stays under the platform's path limit
    listener = probe_module.UnixListener(Path("s.sock"))
    try:
        listener.start()
    except OSError as exc:
        pytest.skip(f"this host refuses to bind a unix socket: {exc}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("s.sock")
    listener.stop()
    assert listener.accepted == 1
    assert not Path("s.sock").exists()


def test_the_connect_code_prints_enoent_for_a_missing_socket(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _REAL_OUTSIDE_CONNECT(Path("missing.sock"), "lionagi-probe-mark-x") == "ENOENT"


def _real_listeners(monkeypatch) -> dict[str, probe_module.UnixListener]:
    """Real listeners for ``_listening``, by socket name; the test skips where binding fails."""
    started: dict[str, probe_module.UnixListener] = {}

    def start(path):
        listener, why = _REAL_START_LISTENER(path)
        if listener is None:
            pytest.skip(f"this host refuses to bind a unix socket: {why}")
        started[Path(path).name] = listener
        return listener, why

    monkeypatch.setattr(probe_module, "_start_listener", start)
    monkeypatch.setattr(probe_module, "_outside_connect", _REAL_OUTSIDE_CONNECT)
    return started


def _child_connect(path: str) -> str:
    """A connect step's output as a probe step prints it: the connect line, then ``rc=``."""
    done = subprocess.run(
        [sys.executable, "-c", probe_module._CONNECT_CODE, path],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return f"{done.stdout}rc={done.returncode}"


def test_the_outside_marks_bracket_a_real_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # relative socket names stay under the platform's path limit
    _real_listeners(monkeypatch)

    def run():
        return {"unix_denied": "connect=EPERM\nrc=1", "unix_control": _child_connect("c.sock")}

    outputs, seen = probe_module._listening(Path("d.sock"), Path("c.sock"), run)
    assert outputs["unix_control"].startswith("connect=ok")
    assert seen["outside_reached"]
    assert (seen["denied"], seen["control"]) == (0, 1)
    assert probe_module._network_off_verdict(outputs, seen)[:2] == (True, "control reached")


def test_a_child_connection_counted_only_after_the_child_exits_still_fails_the_denied_arm(
    tmp_path, monkeypatch
):
    # The denied listener's accept thread is held while the child connects and exits, so at
    # the end of the run the listener has not counted the child. The child's connection is still
    # in the in-run count, read after the after-run mark, and the denied arm fails on it even
    # though the run's output claims a refusal.
    monkeypatch.chdir(tmp_path)
    started = _real_listeners(monkeypatch)

    def run():
        denied = started["d.sock"]
        with denied._counted:  # the accept thread cannot count a connection while this is held
            _child_connect("d.sock")
            assert denied.accepted == 1  # the before-run mark only
        return {"unix_denied": "connect=EPERM\nrc=1", "unix_control": _child_connect("c.sock")}

    outputs, seen = probe_module._listening(Path("d.sock"), Path("c.sock"), run)
    assert seen["outside_reached"]
    assert seen["denied"] == 1
    passed, case, note = probe_module._network_off_verdict(outputs, seen)
    assert (passed, case) == (False, None)
    assert "the denied-path socket accepted 1 connection" in note
    assert probe_module._network_on_verdict(outputs, seen)[0] is False


# ------------------------------------------------------------------------- reading a step's rc
@pytest.mark.parametrize(
    ("command", "rc"),
    [
        # The network step's shape: curl -s prints its body with no newline.
        ("printf pong", 0),
        ("printf pong; false", 1),
        ("echo pong", 0),
        ("true", 0),
    ],
)
def test_a_step_run_by_the_shell_reads_back_its_own_exit_status(command, rc):
    step = probe_module._step(command)
    result = subprocess.run(
        [step["shell"], "-c", step["cmd"]], capture_output=True, text=True, timeout=30
    )
    assert probe_module._rc(result.stdout) == rc


def test_the_network_step_s_output_reads_as_reached():
    step = probe_module._step("printf pong")
    out = subprocess.run(
        [step["shell"], "-c", step["cmd"]], capture_output=True, text=True, timeout=30
    ).stdout
    assert out.startswith("pongrc=0")
    assert probe_module._ok(out, "pong")
    assert not probe_module._refused(out, "pong")


@pytest.mark.parametrize(
    ("text", "rc"),
    [
        ("pongrc=0", 0),
        ("pongrc=0\n", 0),
        ("pongrc=7", 7),
        ("sh: x: Permission denied\nrc=1", 1),
        ("rc=1 then more words", 1),
        ("no marker at all", None),
        ("", None),
        (None, None),
    ],
)
def test_the_marker_is_read_glued_at_the_end_or_starting_a_word(text, rc):
    assert probe_module._rc(text) == rc
