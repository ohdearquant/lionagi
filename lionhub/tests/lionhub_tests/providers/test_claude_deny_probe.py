# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Claude Code deny-rule probe, driven without a claude binary.

A fake claude reads the deny rules off the command line, asks the probe's Messages stand-in what to
run, and answers each tool call as Claude Code applying those rules would: a ``Read`` rule written
from ``//`` or ``~/`` refuses a path its gitignore-style pattern matches, and a ``Bash(prefix:*)``
rule refuses a command that starts with that word. The stand-in's reply logic is the real one; only
its transport is replaced by a function call. Named leaks break one behaviour at a time, so each
arm is shown to fail for the reason it exists.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import os
import re
import shlex
import stat
from pathlib import Path

import pytest

from lionhub.providers.anthropic import claude_deny_probe as probe_module
from lionhub.providers.anthropic import claude_read_probe as read_probe
from lionhub.providers.anthropic.claude_deny_probe import REQUIRED_ARMS, SOURCE_RULES

_PORTS = itertools.count(42000)
_LIVE: dict[str, object] = {}


@contextlib.contextmanager
def _in_memory_serve(app):
    url = f"http://127.0.0.1:{next(_PORTS)}"
    _LIVE[url] = app
    try:
        yield url
    finally:
        _LIVE.pop(url, None)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Records under the test's own LIONAGI_HOME and the in-memory listener."""
    import lionagi._paths as paths

    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path / "lionagi-home")
    monkeypatch.setattr(read_probe, "_serve", _in_memory_serve)


def _decode_stream(payload: bytes) -> list[dict]:
    events = []
    for chunk in payload.decode().split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def _tool_call(payload: bytes) -> tuple[str, str, dict] | None:
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


def _glob(pattern: str, *, at_least_one_directory: bool) -> re.Pattern:
    """A gitignore-style pattern as a regular expression over a whole path."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.+/)" if at_least_one_directory else "(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


class FakeClaude:
    """A claude that applies the settings' deny rules to its tools, with named leaks.

    Leaks: ``ignore_settings`` (the rules are not applied), ``read_unconfined`` (the Read tool
    ignores them), ``refuse_all_reads`` (the run with rules refuses every read), ``no_top_level``
    (``**/`` needs at least one directory), ``home_unexpanded`` (a ``~/`` rule never matches),
    ``bash_unconfined`` (Bash ignores the rules), ``bash_ran_anyway`` (a refused command still
    runs), ``control_read_fails`` (the control run's Read fails), ``proxy`` (the child sends a
    request through the proxy) and ``timeout`` (the run with rules ends before any request).
    """

    def __init__(self, *leaks: str) -> None:
        self.leaks = set(leaks)
        self.calls: list[tuple[list[str], dict[str, str], Path]] = []

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> int:
        self.calls.append((list(argv), dict(env), cwd))
        rules = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
        if rules and "timeout" in self.leaks:
            return -1
        if "ignore_settings" in self.leaks:
            rules = []
        if "proxy" in self.leaks:
            _LIVE[env["HTTPS_PROXY"]].answer("CONNECT", "example.com:443", b"")
        stand_in = _LIVE[env["ANTHROPIC_BASE_URL"]]
        results: list[dict] = []
        for _ in range(64):
            body = {
                "model": argv[argv.index("--model") + 1],
                "stream": True,
                "tools": [{"name": "Bash"}, {"name": "Read"}],
                "messages": [
                    {"role": "user", "content": argv[-1]},
                    {"role": "user", "content": results},
                ],
            }
            status, _, payload = stand_in.answer("POST", "/v1/messages", json.dumps(body).encode())
            assert status == 200
            call = _tool_call(payload)
            if call is None:
                return 0
            call_id, name, tool_input = call
            text, is_error = self._tool(name, tool_input, rules, env["HOME"])
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": text,
                    "is_error": is_error,
                }
            )
        raise AssertionError("the stand-in never ended the run")

    def _read_denied(self, path: str, rules: list[str], home: str) -> bool:
        if rules and "refuse_all_reads" in self.leaks:
            return True
        for rule in rules:
            if not (rule.startswith("Read(") and rule.endswith(")")):
                continue
            spec = rule[len("Read(") : -1]
            if spec.startswith("//"):
                pattern = spec[1:]
            elif spec.startswith("~/"):
                if "home_unexpanded" in self.leaks:
                    continue
                pattern = home.rstrip("/") + spec[1:]
            else:
                continue
            matcher = _glob(pattern, at_least_one_directory="no_top_level" in self.leaks)
            if matcher.fullmatch(os.path.realpath(path)):
                return True
        return False

    def _tool(self, name: str, tool_input: dict, rules: list[str], home: str) -> tuple[str, bool]:
        if name == "Read":
            path = tool_input["file_path"]
            if not rules and "control_read_fails" in self.leaks:
                return "File does not exist.", True
            if "read_unconfined" not in self.leaks and self._read_denied(path, rules, home):
                return f"Permission to read {path} has been denied.", True
            return f"     1\t{Path(path).read_text()}", False
        assert name == "Bash" and set(tool_input) == {"command", "description"}
        command = tool_input["command"]
        assert command.endswith("; echo rc=$?")
        words = shlex.split(command[: -len("; echo rc=$?")])
        assert words[0] == "touch" and len(words) == 2
        denied = "bash_unconfined" not in self.leaks and any(
            rule == f"Bash({words[0]}:*)" for rule in rules
        )
        if denied and "bash_ran_anyway" not in self.leaks:
            return f"Permission to use Bash with command {command} has been denied.", True
        Path(words[1]).touch()
        if denied:
            return "denied after running", True
        return "rc=0", False


def _failing(arms: dict) -> set[str]:
    return {name for name, arm in arms.items() if not arm["passed"]}


def _binary(directory: Path, version: str = "2.1.330") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude"
    path.write_text(f'#!/bin/sh\necho "{version} (Claude Code)"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_every_arm_passes_against_a_claude_applying_the_rules(tmp_path):
    arms, rendering, runs = probe_module.probe(
        "claude", runner=FakeClaude(), scratch=tmp_path / "s"
    )
    assert list(arms) == list(REQUIRED_ARMS)
    assert _failing(arms) == set(), {n: a["evidence"] for n, a in arms.items() if not a["passed"]}
    assert rendering["source"] == {kind: list(rules) for kind, rules in SOURCE_RULES.items()}
    assert rendering["ask_rules_as_deny"] == 1
    assert runs["rules"]["exit_status"] == 0


def test_the_rules_are_rendered_by_the_submit_renderer_for_both_directories(tmp_path):
    _, rendering, _ = probe_module.probe("claude", runner=FakeClaude(), scratch=tmp_path / "s")
    project = rendering["project_dir"].strip("/")
    leg = rendering["leg_cwd"].strip("/")
    assert project != leg
    assert rendering["rendered"] == [
        f"Read(//{project}/config/key.txt)",
        f"Read(//{leg}/config/key.txt)",
        f"Read(//{project}/**/.env)",
        f"Read(//{leg}/**/.env)",
        "Read(~/.probe/secret.txt)",
        "Bash(touch:*)",
        f"Read(//{project}/**/private/**)",
        f"Read(//{leg}/**/private/**)",
    ]


_READ_REFUSALS = {
    "anchored_project_file_refused",
    "anchored_leg_file_refused",
    "any_depth_name_top_refused",
    "any_depth_name_nested_refused",
    "home_rule_refused",
    "ask_rule_as_deny_refused",
}


@pytest.mark.parametrize(
    ("leak", "expected"),
    [
        ("ignore_settings", _READ_REFUSALS | {"bash_rule_refused"}),
        ("read_unconfined", _READ_REFUSALS),
        ("refuse_all_reads", {"uncovered_file_read_works"}),
        ("no_top_level", {"any_depth_name_top_refused", "ask_rule_as_deny_refused"}),
        ("home_unexpanded", {"home_rule_refused"}),
        ("bash_unconfined", {"bash_rule_refused"}),
        ("bash_ran_anyway", {"bash_rule_refused"}),
        ("control_read_fails", _READ_REFUSALS | {"uncovered_file_read_works"}),
        ("proxy", {"no_other_outbound"}),
        (
            "timeout",
            set(REQUIRED_ARMS) - {"rules_rendered", "no_other_outbound"},
        ),
    ],
)
def test_each_leak_fails_exactly_its_arms(tmp_path, leak, expected):
    arms, _, _ = probe_module.probe("claude", runner=FakeClaude(leak), scratch=tmp_path / "s")
    assert _failing(arms) == expected


def test_a_renderer_that_loses_the_ask_count_fails_only_the_rendering_arm(tmp_path, monkeypatch):
    real = probe_module.plan_carry

    def uncounted(*args, **kwargs):
        return dataclasses.replace(real(*args, **kwargs), ask_rules_as_deny=0)

    monkeypatch.setattr(probe_module, "plan_carry", uncounted)
    arms, _, _ = probe_module.probe("claude", runner=FakeClaude(), scratch=tmp_path / "s")
    assert _failing(arms) == {"rules_rendered"}


def test_a_renderer_that_drops_the_leg_directory_fails_the_leg_shapes(tmp_path, monkeypatch):
    real = probe_module.plan_carry

    def project_only(*args, **kwargs):
        plan = real(*args, **kwargs)
        leg = "//" + kwargs["leg_cwd"].strip("/") + "/"
        kept = tuple(rule for rule in plan.deny_rules if leg not in rule)
        return dataclasses.replace(plan, deny_rules=kept)

    monkeypatch.setattr(probe_module, "plan_carry", project_only)
    arms, _, _ = probe_module.probe("claude", runner=FakeClaude(), scratch=tmp_path / "s")
    assert _failing(arms) == {
        "rules_rendered",
        "anchored_leg_file_refused",
        "any_depth_name_top_refused",
        "any_depth_name_nested_refused",
        "ask_rule_as_deny_refused",
    }


def test_a_refusal_counts_only_beside_a_control_that_did_the_step(tmp_path):
    arms, _, _ = probe_module.probe(
        "claude", runner=FakeClaude("control_read_fails"), scratch=tmp_path / "s"
    )
    evidence = arms["home_rule_refused"]["evidence"]
    assert "the control run did not do this step" in evidence


def test_a_refused_command_that_ran_says_so(tmp_path):
    arms, _, _ = probe_module.probe(
        "claude", runner=FakeClaude("bash_ran_anyway"), scratch=tmp_path / "s"
    )
    assert "the command ran: its file exists" in arms["bash_rule_refused"]["evidence"]


def test_the_two_runs_differ_only_in_the_rules(tmp_path):
    fake = FakeClaude()
    _, rendering, runs = probe_module.probe("claude", runner=fake, scratch=tmp_path / "s")
    assert runs["control"]["settings"] == {"permissions": {"deny": []}}
    assert runs["rules"]["settings"] == {"permissions": {"deny": rendering["rendered"]}}
    (control_argv, control_env, control_cwd), (rules_argv, rules_env, rules_cwd) = fake.calls
    assert control_cwd == rules_cwd == Path(rendering["leg_cwd"])
    assert control_env["HOME"] != rules_env["HOME"]
    # Each run has its own Claude Code temp directory, recorded in the run.
    assert control_env["CLAUDE_CODE_TMPDIR"] == runs["control"]["claude_tmp"]
    assert rules_env["CLAUDE_CODE_TMPDIR"] == runs["rules"]["claude_tmp"]
    assert control_env["CLAUDE_CODE_TMPDIR"] != rules_env["CLAUDE_CODE_TMPDIR"]
    settings_at = control_argv.index("--settings") + 1
    assert control_argv[:settings_at] == rules_argv[:settings_at]
    assert control_argv[settings_at + 1 :] == rules_argv[settings_at + 1 :]
    assert "--mcp-config" not in control_argv


def test_the_probe_root_is_removed(tmp_path):
    scratch = tmp_path / "s"
    probe_module.probe("claude", runner=FakeClaude(), scratch=scratch)
    assert list(scratch.iterdir()) == []


def test_the_record_holds_the_rules_and_every_arm(tmp_path):
    binary = str(_binary(tmp_path / "bin"))
    arms, rendering, runs = probe_module.probe(binary, runner=FakeClaude(), scratch=tmp_path / "s")
    path = probe_module.write_record(binary, arms, rendering, runs)
    assert path.parent == probe_module.deny_record_dir()
    assert path.parent == tmp_path / "lionagi-home" / "claude-deny-probes"
    record = json.loads(path.read_text())
    assert record["version"] == "2.1.330"
    assert record["rules"]["rendered"] == rendering["rendered"]
    assert list(record["arms"]) == sorted(REQUIRED_ARMS)
    assert all(arm["passed"] for arm in record["arms"].values())
    assert set(record["runs"]) == {"control", "rules"}


def test_main_without_a_claude_on_path_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(probe_module.shutil, "which", lambda name: None)
    assert probe_module.main([]) == 2
    assert "claude is not on PATH" in capsys.readouterr().err
