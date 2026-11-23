# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The codex read-roots probe, driven without a codex binary.

A fake codex reads the permission table off the command line, asks the probe's stand-in provider
what to run, and answers each command as a sandbox that applies the table's most specific entry
to the resolved path would. The stand-in's replies are the real ones; only the transport is
replaced. Named leaks break one behaviour at a time, so each arm is shown to fail for the reason
it exists.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import stat
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionhub.providers.openai import codex_read_probe as read_probe
from lionhub.providers.openai import codex_sandbox_probe as sandbox_probe
from lionhub.providers.openai._codex_permissions import (
    PERMISSION_PROFILE,
    permission_profile_table,
    probe_record_dir,
    probe_record_path,
)
from lionhub.providers.openai.codex import _permission_profile_overrides


def _toml_loads(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: the declared `tomli` reads the same TOML
        import tomli as tomllib

    return tomllib.loads(text)


# ---------------------------------------------------------------------------- the stand-in
_PORTS = itertools.count(41000)
_LIVE: dict[str, InMemoryStub] = {}


class InMemoryStub(sandbox_probe.StubProvider):
    """The probe's stand-in provider without the socket: the same replies, no bind."""

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.requests = []
        self._port = next(_PORTS)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        _LIVE[self.base_url] = self

    def stop(self) -> None:
        _LIVE.pop(self.base_url, None)


def _ask(base: str, items: list[dict]) -> dict:
    stub = _LIVE[base.removesuffix("/v1")]
    request = {"input": items}
    stub.requests.append(request)
    body = stub._reply(request).decode()
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    return next(e["item"] for e in events if e["type"] == "response.output_item.done")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """The in-memory stand-in, a lionagi home and planted directories under the test's own
    directory, a TMPDIR outside the probe's root, and the default child-sandbox settings."""
    import lionagi._paths as paths
    import lionhub.agent.child_sandbox as child_sandbox

    _LIVE.clear()
    monkeypatch.setattr(sandbox_probe, "StubProvider", InMemoryStub)
    # pytest's temp root is where the tests' scratch lives; the refusal has its own tests in the
    # sandbox probe's file.
    monkeypatch.setattr(sandbox_probe, "_temp_roots", lambda: [])
    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path / "lionagi-home")
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    (tmp_path / "tmpdir").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    planted = []
    for name, label in (
        ("read_via_tmp_link_refused", "plant-tmp"),
        ("read_via_var_link_refused", "plant-var"),
        ("read_users_shared_refused", "plant-shared"),
    ):
        (tmp_path / label).mkdir()
        planted.append((name, str(tmp_path / label)))
    monkeypatch.setattr(read_probe, "_PLANTED", tuple(planted))


def _planted_dirs() -> list[Path]:
    return [Path(directory) for _, directory in read_probe._PLANTED]


# ---------------------------------------------------------------------------- the fake codex
def _parts(path: str) -> tuple[str, ...]:
    return tuple(p for p in path.split("/") if p)


class FakeCodex:
    """Stands in for the codex binary: applies the table's most specific entry to the path a
    command names, resolved, except for the behaviours named in ``leaks``."""

    def __init__(self, leaks: tuple[str, ...] = ()) -> None:
        self.leaks = set(leaks)
        self.runs: list[tuple[list[str], dict]] = []
        self.commands: list[str] = []

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> int:
        provider = next(a for a in argv if a.startswith("model_providers.stub="))
        base = re.search(r'base_url="([^"]+)"', provider).group(1)
        row = next(a for a in argv if a.startswith(f"permissions.{PERMISSION_PROFILE}="))
        table = _toml_loads("table = " + row.split("=", 1)[1])["table"]
        self.runs.append((argv, table))
        if "/" in table["filesystem"] and "table_runs" in self.leaks:
            return 1  # codex refuses the table and runs nothing
        items: list[dict] = []
        for _ in range(40):
            item = _ask(base, items)
            if item["type"] != "function_call":
                break
            cmd = json.loads(item["arguments"])["cmd"]
            self.commands.append(cmd)
            output = self._answer(cmd, table, Path(cwd), env)
            items.append(
                {"type": "function_call_output", "call_id": item["call_id"], "output": output}
            )
        return 0

    def _access(self, path: str, table: dict, cwd: Path, env: dict[str, str]) -> str:
        entries: list[tuple[str, str]] = [(str(cwd), "write")]  # what `:workspace` grants
        for key, mode in table["filesystem"].items():
            spots = {":tmpdir": [env.get("TMPDIR", "")], ":slash_tmp": ["/tmp", "/private/tmp"]}
            for spot in spots.get(key, [key]):
                if spot:
                    entries += [(spot, mode), (os.path.realpath(spot), mode)]
        target = _parts(path)
        best, mode = -1, "read"  # `:workspace` reads everywhere
        for spot, access in entries:
            parts = _parts(spot)
            if target[: len(parts)] == parts and len(parts) > best:
                best, mode = len(parts), access
        return mode

    def _answer(self, cmd: str, table: dict, cwd: Path, env: dict[str, str]) -> str:
        leaks = self.leaks
        words = shlex.split(cmd.rsplit("; echo rc=", 1)[0])
        roots_run = "/" in table["filesystem"]

        def refusal(target) -> str:
            reason = (
                "No such file or directory"
                if "refusal_is_enoent" in leaks
                else "Operation not permitted"
            )
            return f"{words[0]}: {target}: {reason}\nrc=1"

        def readable(target: str, arm: str) -> bool:
            judged = target if "read_through_symlink_refused" in leaks else os.path.realpath(target)
            allowed = self._access(judged, table, cwd, env) != "deny"
            return allowed or (roots_run and arm in leaks)

        if words[0] == "/bin/cat":
            target = words[1]
            name = Path(target).name
            arm = {
                "inside.txt": "read_inside_root_works",
                "cwd.txt": "read_cwd_works",
                "target.txt": "read_outside_refused",
                "outside-alias": "read_through_symlink_refused",
            }.get(name, "")
            for planted_arm, directory in read_probe._PLANTED:
                if Path(target).parent == Path(directory):
                    arm = planted_arm
            if arm in ("read_inside_root_works", "read_cwd_works") and arm in leaks:
                return refusal(target)
            if not roots_run and "control_reads_outside_without_read_roots" in leaks:
                return refusal(target)
            if not readable(target, arm):
                return refusal(target)
            return Path(target).read_text() + "rc=0"
        if words[0] == "/bin/ls" and words[1:2] == ["-d"]:
            target = words[2]
            if not readable(target, "stat_outside_refused"):
                return refusal(target)
            return f"{target}\nrc=0"
        if words[0] == "/bin/ls":
            target = words[1] if len(words) > 1 else str(cwd)
            arm = "list_parent_refused" if len(words) > 1 else "shell_runs_in_root"
            if arm == "shell_runs_in_root" and arm in leaks:
                return "sh: /bin/ls: Operation not permitted\nrc=126"
            if not readable(target, arm):
                return refusal(target)
            return "\n".join(sorted(os.listdir(target))) + "\nrc=0"
        if words[0] == "echo":
            target = words[-1]
            if self._access(target, table, cwd, env) != "write" or "write_cwd_works" in leaks:
                return f"sh: {target}: Operation not permitted\nrc=1"
            Path(target).write_text("w\n")
            return "rc=0"
        if words[1:3] == ["-I", "-c"]:
            python = os.path.realpath(words[0])
            if (
                "python_starts_with_site" in leaks
                or self._access(python, table, cwd, env) == "deny"
            ):
                return f"dyld: {python}: Operation not permitted\nrc=134"
            return "python=ok\nrc=0"
        raise AssertionError(f"unexpected probe command {cmd!r}")


def _failed(arms: dict) -> set[str]:
    return {name for name, arm in arms.items() if not arm["passed"]}


# ----------------------------------------------------------------------------------- the arms
def test_a_codex_that_denies_reads_by_default_passes_every_arm(tmp_path):
    arms, read_roots, table = read_probe.probe("codex", runner=FakeCodex(), scratch=tmp_path / "s")

    assert list(arms) == list(read_probe.REQUIRED_ARMS)
    assert _failed(arms) == set(), {n: arms[n]["evidence"] for n in _failed(arms)}
    assert table["filesystem"]["/"] == "deny"
    for directory in _planted_dirs():
        assert list(directory.iterdir()) == [], "a planted file was left behind"


@pytest.mark.parametrize("leak", [arm for arm in read_probe.REQUIRED_ARMS if arm != "table_runs"])
def test_each_arm_fails_when_its_behaviour_is_not_enforced(tmp_path, leak):
    arms, _, _ = read_probe.probe("codex", runner=FakeCodex((leak,)), scratch=tmp_path / "s")

    assert _failed(arms) == {leak}


def test_a_codex_that_will_not_run_under_the_table_passes_only_the_control(tmp_path):
    arms, _, _ = read_probe.probe(
        "codex", runner=FakeCodex(("table_runs",)), scratch=tmp_path / "s"
    )

    assert _failed(arms) == set(read_probe.REQUIRED_ARMS) - {
        "control_reads_outside_without_read_roots"
    }
    assert arms["table_runs"]["evidence"].startswith("0 of ")


def test_a_refusal_counts_only_when_it_is_the_sandbox_s(tmp_path):
    """A read that fails because the path is missing is not a refusal."""
    arms, _, _ = read_probe.probe(
        "codex", runner=FakeCodex(("refusal_is_enoent",)), scratch=tmp_path / "s"
    )

    assert _failed(arms) == {
        "read_outside_refused",
        "read_through_symlink_refused",
        "stat_outside_refused",
        "list_parent_refused",
        "read_via_tmp_link_refused",
        "read_via_var_link_refused",
        "read_users_shared_refused",
    }


def test_a_codex_that_ignores_the_denial_of_slash_fails_every_refusal(tmp_path):
    """The control: a table whose ``/`` entry has no effect leaves every outside read open."""
    fake = FakeCodex()
    original = fake._access

    def ignore_slash(path, table, cwd, env):
        files = {k: v for k, v in table["filesystem"].items() if k != "/"}
        return original(path, {**table, "filesystem": files}, cwd, env)

    fake._access = ignore_slash
    arms, _, _ = read_probe.probe("codex", runner=fake, scratch=tmp_path / "s")

    assert {"read_outside_refused", "stat_outside_refused", "list_parent_refused"} <= _failed(arms)
    assert "read_inside_root_works" not in _failed(arms)


# ----------------------------------------------------------------------------- the two runs
def test_only_the_second_run_denies_slash_and_it_reads_the_root_and_the_system_set(tmp_path):
    fake = FakeCodex()
    _, read_roots, table = read_probe.probe("codex", runner=fake, scratch=tmp_path / "s")

    (_, control), (_, roots) = fake.runs
    assert "/" not in control["filesystem"]
    assert control["extends"] == roots["extends"] == ":workspace"
    assert control["network"] == roots["network"] == {"enabled": False}
    assert roots == table
    files = roots["filesystem"]
    assert files["/"] == "deny"
    assert files[":tmpdir"] == files[":slash_tmp"] == "deny"
    assert Path(read_roots[0]).name == "readroot"
    assert set(read_roots[1:]) == set(read_probe._system_read_roots())
    assert all(files[root] == "read" for root in read_roots)


def test_the_probe_carries_the_profile_overrides_a_launch_renders(tmp_path):
    fake = FakeCodex()
    _, _, table = read_probe.probe("codex", runner=fake, scratch=tmp_path / "s")

    argv = fake.runs[1][0]
    rendered = _permission_profile_overrides(table)
    starts = [i for i in range(len(argv)) if argv[i : i + len(rendered)] == rendered]
    assert starts, "the run did not carry the overrides a launch renders for its table"


def test_each_planted_file_is_read_by_the_spelling_of_its_directory(tmp_path):
    fake = FakeCodex()
    read_probe.probe("codex", runner=fake, scratch=tmp_path / "s")

    for directory in _planted_dirs():
        prefix = f"/bin/cat {directory}/lionagi-read-probe-"
        assert any(c.startswith(prefix) for c in fake.commands), prefix


def test_a_directory_that_takes_no_planted_file_fails_its_arm_and_no_other(tmp_path, monkeypatch):
    planted = list(read_probe._PLANTED)
    planted[2] = ("read_users_shared_refused", str(tmp_path / "absent" / "deeper"))
    monkeypatch.setattr(read_probe, "_PLANTED", tuple(planted))

    arms, _, _ = read_probe.probe("codex", runner=FakeCodex(), scratch=tmp_path / "s")

    assert _failed(arms) == {"read_users_shared_refused"}
    assert "no file could be planted" in arms["read_users_shared_refused"]["evidence"]


def test_a_scratch_root_inside_a_read_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(read_probe, "_system_read_roots", lambda: (str(tmp_path),))

    with pytest.raises(ConfigurationError, match="inside the read root"):
        read_probe.probe("codex", runner=FakeCodex(), scratch=tmp_path / "s")


# ------------------------------------------------------------------------------- the table
def test_without_read_roots_the_table_is_unchanged():
    table = permission_profile_table(
        deny=["/d"], write=["/w"], light=True, read_only=False, network=True
    )

    assert table["filesystem"] == {
        "/w": "write",
        "/d": "deny",
        ":tmpdir": "read",
        ":slash_tmp": "read",
    }


@pytest.mark.parametrize("light", [True, False])
def test_read_roots_deny_slash_and_both_temp_directories_and_read_each_root(light):
    table = permission_profile_table(
        deny=["/r/secret"],
        write=["/w"],
        light=light,
        read_only=False,
        network=False,
        read_roots=["/r", "/r/secret", "/w"],
    )

    assert table["filesystem"] == {
        "/": "deny",
        "/r": "read",
        "/r/secret": "deny",
        "/w": "write",
        ":tmpdir": "deny",
        ":slash_tmp": "deny",
    }


def test_an_empty_list_of_read_roots_still_denies_reads_by_default():
    table = permission_profile_table(
        deny=[], write=[], light=False, read_only=False, network=False, read_roots=[]
    )

    assert table["filesystem"] == {"/": "deny", ":tmpdir": "deny", ":slash_tmp": "deny"}


# ------------------------------------------------------------------------------ the record
def _fake_binary(directory: Path, version: str = "0.162.1") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "codex"
    path.write_text(f'#!/bin/sh\necho "codex-cli {version}"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_record_is_named_as_the_sandbox_record_in_a_directory_of_its_own(tmp_path):
    binary = _fake_binary(tmp_path / "bin")
    arms = {name: {"passed": True, "evidence": "ok"} for name in read_probe.REQUIRED_ARMS}
    table = {"extends": ":workspace", "filesystem": {"/": "deny"}, "network": {"enabled": False}}

    path = read_probe.write_record(str(binary), arms, ["/r"], table)

    from lionhub.providers.openai._codex_permissions import binary_sha256

    resolved = os.path.realpath(binary)
    expected = probe_record_path(resolved, "0.162.1", binary_sha256(resolved))
    assert path.parent == read_probe.read_record_dir() != probe_record_dir()
    assert path.name == expected.name
    record = json.loads(path.read_text())
    assert record["format"] == 1
    assert record["read_roots"] == ["/r"]
    assert record["table"] == table
    assert record["arms"] == arms
    assert not expected.exists(), "the read probe wrote into the sandbox probe's records"


def _stub_probe(monkeypatch, failed: str | None = None) -> None:
    def probe(binary, *, runner=None, scratch=None):
        arms = {
            name: {"passed": name != failed, "evidence": "e"} for name in read_probe.REQUIRED_ARMS
        }
        return arms, ("/r",), {"extends": ":workspace", "filesystem": {}, "network": {}}

    monkeypatch.setattr(read_probe, "probe", probe)


def test_main_writes_the_record_and_exits_zero_when_every_arm_passes(tmp_path, monkeypatch, capsys):
    _stub_probe(monkeypatch)
    binary = _fake_binary(tmp_path / "bin")

    assert read_probe.main(["--binary", str(binary)]) == 0
    out = capsys.readouterr().out
    assert "record: " in out
    assert len(list(read_probe.read_record_dir().glob("*.json"))) == 1


def test_main_exits_one_when_an_arm_fails(tmp_path, monkeypatch, capsys):
    _stub_probe(monkeypatch, failed="read_outside_refused")
    binary = _fake_binary(tmp_path / "bin")

    assert read_probe.main(["--binary", str(binary)]) == 1
    assert "FAIL  read_outside_refused" in capsys.readouterr().out


def test_main_reports_a_missing_codex_binary(monkeypatch, capsys):
    monkeypatch.setattr(read_probe.shutil, "which", lambda name: None)

    assert read_probe.main([]) == 2
    assert "codex is not on PATH" in capsys.readouterr().err
