# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A codex launch with a scratch directory carries it as one write entry in its permission
profile, in every workload and mode, and sets TMPDIR, TMP and TEMP to it for the commands the
child runs. The launch asks the probe record for the arm that measured that delivery. A scratch
directory no profile would hold, or one that overlaps a location the profile denies or makes
read-only, is refused before the child starts (ADR-0041 Amendment 6)."""

from __future__ import annotations

import logging
import os

import pytest

from lionagi._errors import CLIInputRefusedError, ConfigurationError
from lionhub.providers.openai._codex_permissions import (
    READ_ONLY_WRITE_ROOT_ARM,
    SCRATCH_ENV_ARM,
    launch_arms,
)
from lionhub.providers.openai.codex import CodexCodeRequest

_ENV_NAMES = ("TMPDIR", "TMP", "TEMP")


@pytest.fixture(autouse=True)
def gate_calls(tmp_path, monkeypatch):
    """The child sandbox on with its default policy, and the probe record lookup recorded.

    ``/tmp`` as the scratch refusal reads it is a directory of the test's own, so a test whose
    temporary directory lies under the real ``/tmp`` (as on Linux) is not refused for that.
    """
    import lionhub.agent.child_sandbox as child_sandbox
    import lionhub.providers.openai.codex as codex_module

    calls: list[dict] = []

    def record(binary, env=None, cwd=None, table=None, scratch=False):
        calls.append({"table": table, "scratch": scratch})
        return "probe-record"

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    monkeypatch.setattr(codex_module, "require_passing_probe_record", record)
    slash_tmp = tmp_path / "slash-tmp"
    slash_tmp.mkdir()
    monkeypatch.setattr(codex_module, "_SLASH_TMP", str(slash_tmp))
    return calls


def _project(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


def _scratch(tmp_path):
    scratch = tmp_path / "run" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _config_home(tmp_path, *lines: str) -> dict[str, str]:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text("".join(f"{line}\n" for line in lines))
    return {"CODEX_HOME": str(home)}


def _codex(tmp_path, **kw) -> list[str]:
    if "env" not in kw:
        kw["env"] = _config_home(tmp_path)
    return CodexCodeRequest(prompt="hi", repo=_project(tmp_path), **kw).as_cmd_args()


def _overrides(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "-c"]


def _toml(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


def _table(args: list[str]) -> dict:
    rows = [o for o in _overrides(args) if o.startswith("permissions.lionagi_child=")]
    assert len(rows) == 1
    return _toml("table = " + rows[0].split("=", 1)[1])["table"]


def _env_rows(args: list[str]) -> list[tuple[int, str, str]]:
    """Each ``shell_environment_policy.set.<NAME>`` override: its index, name and value."""
    rows = []
    for index, override in enumerate(_overrides(args)):
        key, _, value = override.partition("=")
        if key.startswith("shell_environment_policy.set."):
            name = key.rsplit(".", 1)[1]
            rows.append((index, name, _toml(f"v = {value}")["v"]))
    return rows


_WORKLOADS = pytest.mark.parametrize(
    "kw",
    [
        {"workload": "light"},
        {"sandbox": "workspace-write"},
        {"sandbox": "read-only"},
        {},  # heavy, no mode named and none configured: read-only
    ],
    ids=["light", "heavy-workspace-write", "heavy-read-only", "heavy-unset"],
)


@_WORKLOADS
def test_the_scratch_directory_is_one_write_entry_in_every_mode(tmp_path, gate_calls, kw):
    scratch = os.path.realpath(_scratch(tmp_path))
    with_scratch = _table(_codex(tmp_path, scratch_dir=scratch, **kw))
    without = _table(_codex(tmp_path, **kw))
    assert with_scratch["filesystem"].get(scratch) == "write"
    assert scratch not in without["filesystem"]
    # Nothing else in the table moves.
    files = dict(with_scratch["filesystem"])
    del files[scratch]
    assert files == without["filesystem"]
    assert with_scratch["extends"] == without["extends"]
    assert with_scratch["network"] == without["network"]


@_WORKLOADS
def test_the_gate_asks_for_the_scratch_arm_only_when_the_launch_has_a_scratch_directory(
    tmp_path, gate_calls, kw
):
    _codex(tmp_path, scratch_dir=str(_scratch(tmp_path)), **kw)
    _codex(tmp_path, **kw)
    assert [call["scratch"] for call in gate_calls] == [True, False]
    with_scratch, without = (call["table"] for call in gate_calls)
    assert SCRATCH_ENV_ARM in launch_arms(with_scratch, scratch=True)
    assert SCRATCH_ENV_ARM not in launch_arms(without, scratch=False)
    # A read-only table with a write entry needs the arm that measured such an entry; a
    # read-only launch without a scratch directory grants none and does not.
    read_only = with_scratch["extends"] == ":read-only"
    assert (READ_ONLY_WRITE_ROOT_ARM in launch_arms(with_scratch, scratch=True)) is read_only
    assert READ_ONLY_WRITE_ROOT_ARM not in launch_arms(without, scratch=False)


@_WORKLOADS
def test_the_three_variables_name_the_resolved_scratch_directory_after_every_other_override(
    tmp_path, kw
):
    scratch = _scratch(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "run")
    overrides = {"shell_environment_policy.set.TMPDIR": "/elsewhere"}
    args = _codex(tmp_path, scratch_dir=str(alias / "scratch"), config_overrides=overrides, **kw)
    rows = _env_rows(args)
    ours = rows[-3:]
    assert [name for _, name, _ in ours] == list(_ENV_NAMES)
    assert {value for _, _, value in ours} == {os.path.realpath(scratch)}
    # Codex keeps the last `-c` of a key: the run's value follows the caller's and the profile.
    profile_index = next(
        i for i, o in enumerate(_overrides(args)) if o.startswith("permissions.lionagi_child=")
    )
    assert all(index > profile_index for index, _, _ in ours)
    assert all(index < ours[0][0] for index, _, _ in rows[:-3])


@_WORKLOADS
def test_a_launch_without_a_scratch_directory_sets_none_of_the_variables(tmp_path, kw):
    assert _env_rows(_codex(tmp_path, **kw)) == []


def test_a_gone_scratch_directory_adds_nothing_and_says_so(tmp_path, gate_calls, caplog):
    gone = tmp_path / "run" / "scratch"
    with caplog.at_level(logging.WARNING):
        args = _codex(tmp_path, scratch_dir=str(gone), sandbox="workspace-write")
    assert _env_rows(args) == []
    assert str(gone) not in _table(args)["filesystem"]
    assert gate_calls[-1]["scratch"] is False
    assert any("is gone" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("shape", ["link", "file"])
def test_a_scratch_path_that_is_a_link_or_not_a_directory_is_refused(tmp_path, shape):
    target = _scratch(tmp_path)
    path = tmp_path / "scratch-path"
    if shape == "link":
        path.symlink_to(target)
    else:
        path.write_text("x\n")
    with pytest.raises(ConfigurationError, match="symbolic link or not a directory"):
        _codex(tmp_path, scratch_dir=str(path), sandbox="workspace-write")


def _sandbox_off(monkeypatch):
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"enabled": False}}
    )


def test_a_scratch_directory_with_the_child_sandbox_off_is_refused(tmp_path, monkeypatch):
    _sandbox_off(monkeypatch)
    scratch = str(_scratch(tmp_path))
    with pytest.raises(ConfigurationError, match="the child sandbox is switched off"):
        _codex(tmp_path, scratch_dir=scratch, sandbox="workspace-write")
    # Control: the same launch without the scratch directory starts.
    _codex(tmp_path, sandbox="workspace-write")


def test_a_scratch_directory_under_bypass_approvals_is_refused(tmp_path, monkeypatch):
    _sandbox_off(monkeypatch)
    scratch = str(_scratch(tmp_path))
    with pytest.raises(ConfigurationError, match="bypass_approvals turns codex's sandbox off"):
        _codex(tmp_path, scratch_dir=scratch, bypass_approvals=True)
    _codex(tmp_path, bypass_approvals=True)


@pytest.fixture
def lionagi_home(tmp_path, monkeypatch):
    import lionagi._paths as paths

    home = tmp_path / "lionagi-home"
    (home / "codex-sandbox-probes").mkdir(parents=True)
    monkeypatch.setattr(paths, "LIONAGI_HOME", home)
    return home


@pytest.mark.parametrize("where", ["above", "equal", "inside"])
@pytest.mark.parametrize("kw", [{"sandbox": "workspace-write"}, {"sandbox": "read-only"}])
def test_a_scratch_directory_over_the_probe_record_directory_is_refused(
    tmp_path, lionagi_home, where, kw
):
    record = lionagi_home / "codex-sandbox-probes"
    scratch = {"above": lionagi_home, "equal": record, "inside": record / "sub"}[where]
    scratch.mkdir(exist_ok=True)
    with pytest.raises(CLIInputRefusedError, match="the scratch directory .* probe record"):
        _codex(tmp_path, scratch_dir=str(scratch), **kw)


@pytest.mark.parametrize("where", ["above", "equal", "inside"])
@pytest.mark.parametrize("kw", [{"sandbox": "workspace-write"}, {"sandbox": "read-only"}])
def test_a_scratch_directory_over_a_denied_location_is_refused(tmp_path, monkeypatch, where, kw):
    import lionhub.agent.child_sandbox as child_sandbox

    denied = tmp_path / "config" / "gh"
    denied.mkdir(parents=True)
    monkeypatch.setattr(
        child_sandbox,
        "_global_settings",
        lambda: {"child_sandbox": {"deny_read": [str(denied)], "allow_write": []}},
    )
    scratch = {"above": denied.parent, "equal": denied, "inside": denied / "sub"}[where]
    scratch.mkdir(exist_ok=True)
    with pytest.raises(CLIInputRefusedError, match="the scratch directory .* denied reading"):
        _codex(tmp_path, scratch_dir=str(scratch), **kw)
    # Control: a scratch directory beside the denied one is granted.
    beside = denied.parent / "gh-other"
    beside.mkdir()
    table = _table(_codex(tmp_path, scratch_dir=str(beside), **kw))
    assert table["filesystem"][os.path.realpath(beside)] == "write"


def test_a_light_scratch_directory_inside_slash_tmp_is_refused(tmp_path, monkeypatch):
    import lionhub.providers.openai.codex as codex_module

    scratch = _scratch(tmp_path)
    monkeypatch.setattr(codex_module, "_SLASH_TMP", str(tmp_path / "run"))
    with pytest.raises(CLIInputRefusedError, match=r"lies inside /tmp"):
        _codex(tmp_path, scratch_dir=str(scratch), workload="light")
    # A heavy workspace-write table leaves /tmp writable, so the same directory is granted there.
    table = _table(_codex(tmp_path, scratch_dir=str(scratch), sandbox="workspace-write"))
    assert table["filesystem"][os.path.realpath(scratch)] == "write"


def test_the_slash_tmp_refusal_compares_the_written_and_the_resolved_slash_tmp(monkeypatch):
    # No file is made: both paths are only compared. On macOS /tmp resolves to /private/tmp.
    import lionhub.providers.openai.codex as codex_module

    monkeypatch.setattr(codex_module, "_SLASH_TMP", "/tmp")  # noqa: S108
    table = {"filesystem": {":slash_tmp": "read"}}
    for scratch in ("/tmp/run/x", os.path.realpath("/tmp") + "/run/x"):  # noqa: S108
        with pytest.raises(CLIInputRefusedError, match=r"lies inside /tmp"):
            codex_module._refuse_scratch_under_read_only_temp(table, scratch, {})
    # Control: a table that leaves /tmp writable refuses nothing.
    codex_module._refuse_scratch_under_read_only_temp({"filesystem": {}}, "/tmp/run/x", {})  # noqa: S108


@pytest.mark.parametrize("excluded", [True, False])
def test_a_scratch_directory_inside_tmpdir_is_refused_where_the_profile_makes_it_read_only(
    tmp_path, excluded
):
    tmpdir = tmp_path / "tmpdir"
    scratch = tmpdir / "run" / "scratch"
    scratch.mkdir(parents=True)
    env = _config_home(
        tmp_path,
        'sandbox_mode = "workspace-write"',
        "",
        "[sandbox_workspace_write]",
        f"exclude_tmpdir_env_var = {str(excluded).lower()}",
    )
    env["TMPDIR"] = str(tmpdir)
    if excluded:
        with pytest.raises(CLIInputRefusedError, match=r"lies inside \$TMPDIR"):
            _codex(tmp_path, env=env, scratch_dir=str(scratch))
    else:
        table = _table(_codex(tmp_path, env=env, scratch_dir=str(scratch)))
        # The configured workspace-write mode is the one in force, and $TMPDIR stays writable.
        assert table["extends"] == ":workspace"
        assert ":tmpdir" not in table["filesystem"]
        assert table["filesystem"][os.path.realpath(scratch)] == "write"


def test_the_scratch_directory_is_not_part_of_the_request_s_dump_or_repr(tmp_path):
    scratch = str(_scratch(tmp_path))
    request = CodexCodeRequest(prompt="hi", repo=_project(tmp_path), scratch_dir=scratch)
    assert "scratch_dir" not in request.model_dump()
    assert scratch not in repr(request)
    assert "scratch_dir" not in CodexCodeRequest.model_json_schema().get("properties", {})
