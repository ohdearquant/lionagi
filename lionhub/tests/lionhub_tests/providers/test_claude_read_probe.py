# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Claude Code read probe, driven without a claude binary.

A fake claude reads the settings off the command line, asks the probe's Messages stand-in what to
run, and answers each tool call as a sandbox applying those read rules would: the most specific
``allowRead`` or ``denyRead`` entry above a path's resolved form decides. The stand-in's reply logic
is the real one; only its transport is replaced by a function call, because some hosts refuse to
bind even a loopback port. One test drives the socket-backed stand-in and skips where binding is
refused. Named leaks break one behaviour at a time, so each arm is shown to fail for the reason it
exists.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import shlex
import socket
import stat
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionhub.providers.anthropic import claude_read_probe as probe_module
from lionhub.providers.anthropic.claude_read_probe import (
    PLACEHOLDER_API_KEY,
    REQUIRED_ARMS,
    MessagesStandIn,
    ProxyRecorder,
)

_REAL_SERVE = probe_module._serve
_PORTS = itertools.count(41000)
_LIVE: dict[str, object] = {}
_PREFIX = os.path.realpath(sys.prefix)


@contextlib.contextmanager
def _in_memory_serve(app):
    """The probe's listener without the socket: the fake claude calls the app directly."""
    url = f"http://127.0.0.1:{next(_PORTS)}"
    _LIVE[url] = app
    try:
        yield url
    finally:
        _LIVE.pop(url, None)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Records under the test's own LIONAGI_HOME, planted files under the test's directory, a
    fixed system read set, and the in-memory listener. A test that needs one of the real ones
    puts it back."""
    import lionagi._paths as paths

    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path / "lionagi-home")
    monkeypatch.setattr(probe_module, "_serve", _in_memory_serve)
    monkeypatch.setattr(probe_module, "_temp_roots", lambda: [])
    monkeypatch.setattr(probe_module, "_system_read_roots", lambda: (_PREFIX, "/usr", "/bin"))
    planted = []
    for name, _ in probe_module._PLANTED:
        directory = tmp_path / f"planted-{name}"
        directory.mkdir()
        planted.append((name, str(directory)))
    monkeypatch.setattr(probe_module, "_PLANTED", tuple(planted))


# ------------------------------------------------------------------------------- the fake claude
def _decode_stream(payload: bytes) -> list[dict]:
    events = []
    for chunk in payload.decode().split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def _tool_call(payload: bytes) -> tuple[str, str, dict] | None:
    """The tool call a streamed reply carries, as (id, name, input), or None for text."""
    call = None
    parts: list[str] = []
    for event in _decode_stream(payload):
        if event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            call = (event["content_block"]["id"], event["content_block"]["name"])
        if event["type"] == "content_block_delta" and event["delta"]["type"] == "input_json_delta":
            parts.append(event["delta"]["partial_json"])
    if call is None:
        return None
    return call[0], call[1], json.loads("".join(parts))


class FakeClaude:
    """A claude that applies the settings' read rules to its tools, with named leaks.

    Leaks: ``ignore_settings`` (the run with read rules reads like one without them),
    ``symlink`` (a path is judged as written, not resolved), ``stat`` (listing one path is never
    refused), ``list_parent`` (listing a directory is never refused), ``planted`` (the planted
    directories are readable), ``no_python`` (the interpreter cannot start), ``no_refusal_text``
    (a refused read prints another message), ``cwd_read`` (the working directory's file is
    refused), ``read_tool_unconfined`` (the Read tool ignores the rules), ``control_read_fails``
    (the control run's Read fails), ``proxy`` (the child sends a request through the proxy) and
    ``timeout`` (the run with read rules ends before any request).
    """

    def __init__(self, *leaks: str) -> None:
        self.leaks = set(leaks)
        self.calls: list[tuple[list[str], dict[str, str], Path]] = []

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> int:
        self.calls.append((list(argv), dict(env), cwd))
        settings = json.loads(argv[argv.index("--settings") + 1])
        rules = settings["sandbox"].get("filesystem")
        if rules is not None and "timeout" in self.leaks:
            return -1
        if rules is not None and "ignore_settings" in self.leaks:
            rules = None
        if "proxy" in self.leaks:
            _LIVE[env["HTTPS_PROXY"]].answer("CONNECT", "example.com:443", b"")
        stand_in = _LIVE[env["ANTHROPIC_BASE_URL"]]
        results: list[dict] = []
        for _ in range(64):
            body = {
                "model": argv[argv.index("--model") + 1],
                "stream": True,
                "system": "not kept by the stand-in",
                "tools": [{"name": "Bash"}, {"name": "Read"}],
                "messages": [
                    {"role": "user", "content": argv[-1]},
                    {"role": "user", "content": results},
                ],
            }
            status, _, payload = stand_in.answer(
                "POST", "/v1/messages?beta=true", json.dumps(body).encode()
            )
            assert status == 200
            call = _tool_call(payload)
            if call is None:
                return 0
            call_id, name, tool_input = call
            text, is_error = self._tool(name, tool_input, rules, cwd)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": text,
                    "is_error": is_error,
                }
            )
        raise AssertionError("the stand-in never ended the run")

    # The sandbox's judgement of one path: the most specific rule above it decides.
    def _readable(self, path: str, rules: dict | None) -> bool:
        if rules is None:
            return True
        judged = path if "symlink" in self.leaks else os.path.realpath(path)
        if "planted" in self.leaks and Path(judged).parent.name.startswith("planted-"):
            return True
        best, allowed = -1, True
        for entry, verdict in [(e, False) for e in rules["denyRead"]] + [
            (e, True) for e in rules["allowRead"]
        ]:
            if judged == entry or judged.startswith(entry.rstrip("/") + "/"):
                if len(entry) > best:
                    best, allowed = len(entry), verdict
        return allowed

    def _refusal(self, tool: str, path: str) -> str:
        message = (
            "Permission denied" if "no_refusal_text" in self.leaks else "Operation not permitted"
        )
        return f"{tool}: {path}: {message}\nrc=1"

    def _tool(self, name: str, tool_input: dict, rules: dict | None, cwd: Path) -> tuple[str, bool]:
        if name == "Read":
            path = tool_input["file_path"]
            if rules is None and "control_read_fails" in self.leaks:
                return "File does not exist.", True
            if "read_tool_unconfined" in self.leaks or self._readable(path, rules):
                return f"     1\t{Path(path).read_text()}", False
            return f"EPERM: operation not permitted, open '{path}'", True
        assert name == "Bash" and set(tool_input) == {"command", "description"}
        command = tool_input["command"]
        assert command.endswith("; echo rc=$?")
        words = shlex.split(command[: -len("; echo rc=$?")])
        if words[0] == "/bin/cat":
            path = words[1]
            if "cwd_read" in self.leaks and Path(path).parent == cwd:
                return self._refusal("cat", path), False
            if not self._readable(path, rules):
                return self._refusal("cat", path), False
            return Path(path).read_text() + "rc=0", False
        if words[:2] == ["/bin/ls", "-d"]:
            if "stat" not in self.leaks and not self._readable(words[2], rules):
                return self._refusal("ls", words[2]), False
            return f"{words[2]}\nrc=0", False
        if words[0] == "/bin/ls":
            target = words[1] if len(words) > 1 else str(cwd)
            if "list_parent" not in self.leaks and not self._readable(target, rules):
                return self._refusal("ls", target), False
            return "\n".join(sorted(os.listdir(target))) + "\nrc=0", False
        if words[0] == "echo":
            assert words[1:3] == ["w", ">"]
            Path(words[3]).write_text("w\n")
            return "rc=0", False
        assert words[1:3] == ["-I", "-c"], words
        if "no_python" in self.leaks or not self._readable(_PREFIX, rules):
            return "dyld: Library not loaded\nrc=134", False
        return "python=ok\nrc=0", False


def _binary(directory: Path, version: str = "2.1.330") -> Path:
    """An executable that only answers ``--version``, like a claude binary does."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude"
    path.write_text(f'#!/bin/sh\necho "{version} (Claude Code)"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _failing(arms: dict) -> set[str]:
    return {name for name, arm in arms.items() if not arm["passed"]}


# ---------------------------------------------------------------------------------------- arms
def test_every_arm_passes_against_a_claude_applying_the_read_rules(tmp_path):
    arms, read_roots, runs = probe_module.probe(
        "claude", runner=FakeClaude(), scratch=tmp_path / "scratch"
    )
    assert list(arms) == list(REQUIRED_ARMS)
    assert _failing(arms) == set(), {n: a["evidence"] for n, a in arms.items() if not a["passed"]}
    assert read_roots[2:] == (_PREFIX, "/usr", "/bin")
    assert runs["roots"]["exit_status"] == 0


_ROOTS_RUN_ARMS = set(REQUIRED_ARMS) - {
    "control_bash_reads_outside",
    "control_read_tool_reads_outside",
    "no_other_outbound",
}
_REFUSALS = {
    "read_outside_refused",
    "read_through_symlink_refused",
    "stat_outside_refused",
    "list_parent_refused",
    "read_via_tmp_link_refused",
    "read_via_var_link_refused",
    "read_users_shared_refused",
}


@pytest.mark.parametrize(
    ("leak", "expected"),
    [
        ("ignore_settings", _REFUSALS | {"read_tool_outside_refused"}),
        ("symlink", {"read_through_symlink_refused"}),
        ("stat", {"stat_outside_refused"}),
        ("list_parent", {"list_parent_refused"}),
        (
            "planted",
            {"read_via_tmp_link_refused", "read_via_var_link_refused", "read_users_shared_refused"},
        ),
        ("no_python", {"python_starts_with_site"}),
        ("no_refusal_text", _REFUSALS),
        ("cwd_read", {"read_cwd_works"}),
        ("read_tool_unconfined", {"read_tool_outside_refused"}),
        (
            "control_read_fails",
            {"control_read_tool_reads_outside", "read_tool_outside_refused"},
        ),
        ("proxy", {"no_other_outbound"}),
        ("timeout", _ROOTS_RUN_ARMS),
    ],
)
def test_each_leak_fails_exactly_its_arms(tmp_path, leak, expected):
    arms, _, _ = probe_module.probe("claude", runner=FakeClaude(leak), scratch=tmp_path / "s")
    assert _failing(arms) == expected


def test_a_refused_read_tool_counts_only_beside_a_control_that_read_the_file(tmp_path):
    arms, _, _ = probe_module.probe(
        "claude", runner=FakeClaude("control_read_fails"), scratch=tmp_path / "s"
    )
    evidence = arms["read_tool_outside_refused"]["evidence"]
    assert evidence.startswith("error ")
    assert "the control run did not read the file" in evidence


def test_the_outbound_arm_says_what_it_covers(tmp_path):
    arms, _, runs = probe_module.probe("claude", runner=FakeClaude("proxy"), scratch=tmp_path / "s")
    assert (
        "covers only traffic that honours the proxy variables"
        in arms["no_other_outbound"]["evidence"]
    )
    assert "CONNECT example.com:443" in arms["no_other_outbound"]["evidence"]
    assert runs["control"]["proxied"] == ["CONNECT example.com:443"]


# ------------------------------------------------------------------------- what the child gets
def test_the_two_runs_differ_only_in_the_read_rules(tmp_path):
    fake = FakeClaude()
    _, read_roots, runs = probe_module.probe("claude", runner=fake, scratch=tmp_path / "s")
    control, roots = runs["control"]["settings"], runs["roots"]["settings"]
    assert control == {
        "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
    }
    assert roots == {
        "sandbox": {
            **control["sandbox"],
            "filesystem": {"denyRead": ["/"], "allowRead": list(read_roots)},
        }
    }
    (control_argv, control_env, control_cwd), (roots_argv, roots_env, roots_cwd) = fake.calls
    assert control_cwd == roots_cwd
    assert control_env["HOME"] != roots_env["HOME"]
    assert control_env["CLAUDE_CONFIG_DIR"] != roots_env["CLAUDE_CONFIG_DIR"]
    settings_at = control_argv.index("--settings") + 1
    assert control_argv[:settings_at] == roots_argv[:settings_at]
    assert control_argv[settings_at + 1 :] == roots_argv[settings_at + 1 :]


def test_argv_has_only_the_listed_flags_and_the_prompt_last(tmp_path):
    fake = FakeClaude()
    probe_module.probe("/opt/claude", runner=fake, scratch=tmp_path / "s")
    argv = fake.calls[0][0]
    assert argv[0] == "/opt/claude"
    flags = [word for word in argv if word.startswith("-")]
    assert flags == [
        "-p",
        "--bare",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--model",
        "--tools",
        "--allowed-tools",
        "--permission-prompts",
        "--settings",
    ]
    assert "--mcp-config" not in argv
    assert argv[argv.index("--tools") + 1] == "Bash,Read"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[-2].startswith("{") and not argv[-1].startswith("-")


def test_the_child_environment_is_built_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-real-looking-key")
    monkeypatch.setenv("PROBE_UNRELATED_VARIABLE", "x")
    fake = FakeClaude()
    _, _, runs = probe_module.probe("claude", runner=fake, scratch=tmp_path / "s")
    env = fake.calls[1][1]
    assert env["ANTHROPIC_API_KEY"] == PLACEHOLDER_API_KEY
    assert "PROBE_UNRELATED_VARIABLE" not in env
    assert env["NO_PROXY"] == "127.0.0.1,localhost"
    assert {env[name] for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")} == {
        env["https_proxy"]
    }
    assert env["HTTPS_PROXY"] != env["ANTHROPIC_BASE_URL"]
    assert runs["roots"]["env_names"] == sorted(env)
    scratch = Path(os.path.realpath(tmp_path / "s"))
    for path in (env["HOME"], env["CLAUDE_CONFIG_DIR"]):
        assert scratch in Path(path).parents


def test_the_probe_root_is_removed_and_planted_files_with_it(tmp_path):
    scratch = tmp_path / "s"
    probe_module.probe("claude", runner=FakeClaude(), scratch=scratch)
    assert list(scratch.iterdir()) == []
    for _, directory in probe_module._PLANTED:
        assert list(Path(directory).iterdir()) == []


def test_a_scratch_root_inside_a_temp_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_module, "_temp_roots", lambda: [Path(os.path.realpath(tmp_path))])
    with pytest.raises(ConfigurationError, match="inside the temp directory"):
        probe_module.probe("claude", runner=FakeClaude(), scratch=tmp_path / "s")


def test_a_scratch_root_inside_a_read_root_is_refused(tmp_path, monkeypatch):
    real = os.path.realpath(tmp_path)
    monkeypatch.setattr(probe_module, "_system_read_roots", lambda: (real,))
    fake = FakeClaude()
    with pytest.raises(ConfigurationError, match="lies inside the read root"):
        probe_module.probe("claude", runner=fake, scratch=tmp_path / "s")
    assert fake.calls == []


# ------------------------------------------------------------------------------- the stand-ins
def _request(tools: bool, results=(), stream: bool = True) -> bytes:
    content = [
        {"type": "tool_result", "tool_use_id": f"toolu_probe_{i}", "content": text, "is_error": err}
        for i, (text, err) in enumerate(results)
    ]
    body = {"stream": stream, "messages": [{"role": "user", "content": content or "go"}]}
    if tools:
        body["tools"] = [{"name": "Bash"}]
    return json.dumps(body).encode()


def test_the_stand_in_scripts_one_tool_call_per_request_then_text():
    steps = [{"tool": "Bash", "input": {"command": "x", "description": "probe"}}]
    stand_in = MessagesStandIn(steps)
    status, kind, payload = stand_in.answer("POST", "/v1/messages", _request(True))
    assert (status, kind) == (200, "text/event-stream")
    assert _tool_call(payload) == (
        "toolu_probe_0",
        "Bash",
        {"command": "x", "description": "probe"},
    )
    events = [e["type"] for e in _decode_stream(payload)]
    assert events[0] == "message_start" and events[-1] == "message_stop"
    _, _, payload = stand_in.answer("POST", "/v1/messages", _request(True, [("out", False)]))
    assert _tool_call(payload) is None
    assert stand_in.results == {"toolu_probe_0": ("out", False)}


def test_the_stand_in_answers_without_tools_count_tokens_and_unknown_routes():
    stand_in = MessagesStandIn([{"tool": "Read", "input": {"file_path": "/x"}}])
    status, kind, payload = stand_in.answer("POST", "/v1/messages", _request(False, stream=False))
    assert (status, kind) == (200, "application/json")
    assert json.loads(payload)["content"] == [{"type": "text", "text": "done"}]
    assert stand_in.answer("POST", "/v1/messages/count_tokens", b"{}")[2] == b'{"input_tokens": 1}'
    assert stand_in.answer("GET", "/", b"")[0] == 404
    assert stand_in.answer("POST", "/v1/messages", b"not json")[0] == 200
    assert stand_in.seen == [
        "POST /v1/messages",
        "POST /v1/messages/count_tokens",
        "GET /",
        "POST /v1/messages",
    ]


def test_the_stand_in_keeps_no_more_of_a_request_than_its_route_and_tool_results():
    stand_in = MessagesStandIn([])
    body = json.loads(_request(True, [("kept", True)]))
    body["system"] = "system-text-not-kept"
    stand_in.answer("POST", "/v1/messages?beta=true&k=query-not-kept", json.dumps(body).encode())
    kept = repr(vars(stand_in))
    assert "system-text-not-kept" not in kept and "query-not-kept" not in kept
    assert stand_in.seen == ["POST /v1/messages"]
    assert stand_in.results == {"toolu_probe_0": ("kept", True)}


def test_the_proxy_recorder_refuses_and_keeps_the_target():
    proxy = ProxyRecorder()
    assert proxy.answer("CONNECT", "api.example.com:443", b"body-not-kept")[0] == 403
    assert proxy.seen == ["CONNECT api.example.com:443"]


def test_the_socket_backed_stand_in_answers_over_loopback():
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        pytest.skip(f"this host refuses a loopback bind: {exc}")
    finally:
        probe.close()
    stand_in = MessagesStandIn([{"tool": "Read", "input": {"file_path": "/x"}}])
    with _REAL_SERVE(stand_in) as url:
        request = urllib.request.Request(f"{url}/v1/messages", data=_request(True), method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- loopback
            payload = response.read()
    assert _tool_call(payload) == ("toolu_probe_0", "Read", {"file_path": "/x"})
    assert stand_in.seen == ["POST /v1/messages"]


# ------------------------------------------------------------------------------ running claude
def _script(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "child.sh"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_run_claude_returns_the_exit_status(tmp_path):
    script = _script(tmp_path / "bin", "exit 3\n")
    assert probe_module.run_claude([str(script)], {"PATH": "/usr/bin:/bin"}, tmp_path) == 3


def test_run_claude_ends_a_child_that_outlives_the_timeout_with_its_group(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_module, "_RUN_TIMEOUT_SECONDS", 1)
    pidfile = tmp_path / "pid"
    script = _script(
        tmp_path / "bin", f"/bin/sleep 60 &\necho $! > {shlex.quote(str(pidfile))}\nwait\n"
    )
    started = time.monotonic()
    status = probe_module.run_claude([str(script)], {"PATH": "/usr/bin:/bin"}, tmp_path)
    assert status == -1
    assert time.monotonic() - started < 30
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the child's background process {pid} outlived the run")


def _exited_and_uncollected(pid: int) -> bool:
    """Whether *pid*, a child of this process, has exited and has not been collected yet."""
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    if hasattr(os, "waitid"):
        try:
            info = os.waitid(os.P_PID, pid, flags)
        except ChildProcessError:
            return False
        return info is not None and info.si_pid == pid
    if sys.platform != "darwin":
        pytest.skip("no non-collecting wait on this platform")
    import ctypes
    import struct

    waitid = ctypes.CDLL(None, use_errno=True).waitid
    waitid.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    waitid.restype = ctypes.c_int
    info = ctypes.create_string_buffer(128)
    if waitid(os.P_PID, pid, ctypes.byref(info), flags) != 0:
        return False
    # Darwin siginfo_t: si_signo, si_errno, si_code, then si_pid at byte 12.
    return struct.unpack_from("i", info.raw, 12)[0] == pid


def test_run_claude_kills_the_group_before_it_collects_the_child(tmp_path, monkeypatch):
    # Once the child is collected its pid, and so its group id, can be reused, and a kill sent
    # to that id could reach an unrelated group.
    seen: list[tuple[int, bool]] = []
    real_killpg = os.killpg

    def recording_killpg(pgid, sig):
        seen.append((pgid, _exited_and_uncollected(pgid)))
        real_killpg(pgid, sig)

    monkeypatch.setattr(probe_module.os, "killpg", recording_killpg)
    script = _script(tmp_path / "bin", "exit 3\n")
    assert probe_module.run_claude([str(script)], {"PATH": "/usr/bin:/bin"}, tmp_path) == 3
    assert len(seen) == 1
    assert seen[0][1], "the group was killed after the child had been collected"


# ---------------------------------------------------------------------------------- the record
def test_the_record_holds_what_the_child_was_given_and_never_a_value(tmp_path):
    binary = _binary(tmp_path / "bin")
    arms, read_roots, runs = probe_module.probe(
        str(binary), runner=FakeClaude(), scratch=tmp_path / "s"
    )
    path = probe_module.write_record(str(binary), arms, read_roots, runs)
    assert path.parent == probe_module.read_record_dir()
    assert path.parent == tmp_path / "lionagi-home" / "claude-read-probes"
    assert path.name.startswith("claude-2.1.330-")
    text = path.read_text()
    record = json.loads(text)
    assert record["format"] == 1 and record["version"] == "2.1.330"
    assert record["binary"] == os.path.realpath(binary)
    assert record["read_roots"] == list(read_roots)
    assert record["arms"] == arms
    assert "POST /v1/messages" in record["runs"]["roots"]["requests"]
    assert "ANTHROPIC_API_KEY" in record["runs"]["roots"]["env_names"]
    assert PLACEHOLDER_API_KEY not in text


def test_a_binary_rewritten_in_place_has_another_record_name(tmp_path):
    binary = _binary(tmp_path / "bin")
    first = probe_module.write_record(str(binary), {}, (), {})
    binary.write_text(binary.read_text() + "# changed\n")
    second = probe_module.write_record(str(binary), {}, (), {})
    assert first != second and first.exists() and second.exists()


def test_write_record_refuses_a_binary_whose_version_cannot_be_read(tmp_path):
    binary = _binary(tmp_path / "bin", version="no version here")
    with pytest.raises(RuntimeError, match="version"):
        probe_module.write_record(str(binary), {}, (), {})


def test_main_writes_the_record_and_exits_zero_only_when_every_arm_passed(
    tmp_path, monkeypatch, capsys
):
    binary = _binary(tmp_path / "bin")
    monkeypatch.setattr(probe_module, "run_claude", FakeClaude())
    assert probe_module.main(["--binary", str(binary), "--scratch", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out
    assert out.count("pass  ") == len(REQUIRED_ARMS)
    record = Path(out.strip().splitlines()[-1].removeprefix("record: "))
    assert record.is_file()
    monkeypatch.setattr(probe_module, "run_claude", FakeClaude("stat"))
    assert probe_module.main(["--binary", str(binary), "--scratch", str(tmp_path / "s")]) == 1
    assert "FAIL  stat_outside_refused" in capsys.readouterr().out


def test_main_reports_a_refused_scratch_root_and_exits_two(tmp_path, monkeypatch, capsys):
    binary = _binary(tmp_path / "bin")
    monkeypatch.setattr(probe_module, "_temp_roots", lambda: [Path(os.path.realpath(tmp_path))])
    assert probe_module.main(["--binary", str(binary), "--scratch", str(tmp_path / "s")]) == 2
    assert "inside the temp directory" in capsys.readouterr().err
