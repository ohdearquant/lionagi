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
import urllib.request
from pathlib import Path

import pytest

from lionagi._errors import CLIInputRefusedError
from lionhub.providers.openai import codex_sandbox_probe as probe_module
from lionhub.providers.openai._codex_permissions import (
    PERMISSION_PROFILE,
    PROBE_COMMAND,
    REQUIRED_ARMS,
    probe_record,
    probe_record_path,
    require_passing_probe_record,
)


def _toml_loads(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: the declared `tomli` reads the same TOML
        import tomli as tomllib

    return tomllib.loads(text)


@pytest.fixture
def sandbox_on(monkeypatch):
    """The child sandbox on with the default policy (the suite switches it off by default)."""
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})


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
        items: list[dict] = []
        for _ in range(40):
            item = _ask(base, items)
            if item["type"] != "function_call":
                break
            cmd = json.loads(item["arguments"])["cmd"]
            self.commands.append(cmd)
            output = self._answer(cmd, table, Path(cwd), add_dirs, Path(env["CODEX_HOME"]))
            items.append(
                {"type": "function_call_output", "call_id": item["call_id"], "output": output}
            )
        return 0

    def _answer(self, cmd, table, cwd: Path, add_dirs, home: Path) -> str:
        words = shlex.split(cmd.rsplit("; echo rc=", 1)[0])
        files = table["filesystem"]
        deny = [Path(p) for p, mode in files.items() if mode == "deny"]
        grants = [cwd, *map(Path, add_dirs), *(Path(p) for p, m in files.items() if m == "write")]
        scratch = cwd.parent
        leaks = self.leaks
        # A user-layer sandbox_mode that wins over the profile is a profile that is not applied.
        ignored = "user_layer_sandbox_mode" in leaks and (home / "config.toml").exists()
        if words[0] in ("/bin/cat", "head"):
            target = Path(words[-1])
            ours = scratch in target.parents
            refused = _under(target, deny) and not ignored
            refused &= not (ours and target.name == "secret.txt" and "deny_canary" in leaks)
            refused &= not (ours and target.name == "auth.json" and "own_codex_home" in leaks)
            refused |= ours and target.name == "f.txt" and "read_control" in leaks
            if refused:
                return f"{words[0]}: {target}: Permission denied\nrc=1"
            text = target.read_text() if words[0] == "/bin/cat" and target.exists() else ""
            return f"{text}rc=0"
        if words[0] == "echo":
            target = Path(words[-1])
            where = target.parent.name
            if "lionagi-probe-" in target.name:
                if "light_tmp_refused" in leaks:
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
            return "pong\nrc=0" if reachable else "rc=7"
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
    assert failed - {leak} <= allowed_company


def test_a_run_that_never_reaches_the_provider_passes_no_arm(tmp_path, sandbox_on):
    # Codex's own startup line is not evidence: only tool outputs the stand-in received count.
    arms = probe_module.probe("codex", runner=lambda argv, env, cwd: 0, scratch=tmp_path / "s")
    assert list(arms) == list(REQUIRED_ARMS)
    assert not [name for name, arm in arms.items() if arm["passed"]]


def test_a_run_that_times_out_passes_no_arm(tmp_path, sandbox_on):
    arms = probe_module.probe("codex", runner=lambda argv, env, cwd: -1, scratch=tmp_path / "s")
    assert not [name for name, arm in arms.items() if arm["passed"]]


def test_the_probe_runs_codex_under_the_profile_a_launch_builds(tmp_path, sandbox_on):
    fake = FakeCodex()
    probe_module.probe("/opt/codex", runner=fake, scratch=tmp_path / "scratch")
    assert len(fake.runs) == 3
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
    assert sorted(networks) == [False, True, True]


def test_a_real_denied_path_is_opened_for_zero_bytes_and_never_read(tmp_path, sandbox_on):
    fake = FakeCodex()
    scratch = tmp_path / "scratch"
    probe_module.probe("codex", runner=fake, scratch=scratch)
    for command in fake.commands:
        words = shlex.split(command.rsplit("; echo rc=", 1)[0])
        if words[0] == "/bin/cat":
            assert words[1].startswith(str(scratch))
        if words[0] == "head":
            assert words[:3] == ["head", "-c", "0"]


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
    assert record["format"] == 1
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
    moved = probe_record_path(os.path.realpath(binary), "0.163.0")
    shutil.copy(path, moved)
    with pytest.raises(CLIInputRefusedError, match="is not for codex 0.163.0"):
        require_passing_probe_record(str(binary))


def test_a_record_of_another_format_is_no_record(tmp_path, record_dir):
    binary = _fake_binary(tmp_path / "bin")
    path = probe_module.write_record(str(binary), _passing_arms())
    record = json.loads(path.read_text())
    record["format"] = 2
    path.write_text(json.dumps(record))
    with pytest.raises(CLIInputRefusedError, match="is not for codex"):
        require_passing_probe_record(str(binary))


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
