# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A child whose working directory is the home directory or an ancestor of it is refused
while the child sandbox is enabled, on every route: Claude Code, codex, the Antigravity CLI
and the Seatbelt policy API-model legs run under. The working directory is writable to a
confined child by construction, so such a workspace would make the whole home writable."""

from __future__ import annotations

import os

import pytest

from lionagi._errors import ConfigurationError

_MESSAGE = "start the agent from a project directory"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home two levels under tmp_path, set as this process's HOME, with a project inside."""
    home = tmp_path / "users" / "u"
    (home / "project").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def sandbox_on(monkeypatch):
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})


def _respelled(path):
    """``path`` with its last component upper-cased, or None on a case-sensitive volume."""
    respelled = str(path.parent / path.name.upper())
    if os.path.exists(respelled) and os.path.samefile(respelled, path):
        return respelled
    return None


@pytest.mark.parametrize(
    "spell",
    [
        lambda home: str(home),
        lambda home: str(home) + "/",
        lambda home: str(home.parent),
        lambda home: "/",
        lambda home: "~",
    ],
    ids=["home", "trailing-slash", "parent", "root", "tilde"],
)
def test_home_and_its_ancestors_reach_home(fake_home, spell):
    from lionhub.agent.child_sandbox import _reaches_home

    assert _reaches_home(spell(fake_home))


def test_directories_inside_or_beside_home_do_not(fake_home, tmp_path):
    from lionhub.agent.child_sandbox import _reaches_home

    assert not _reaches_home(fake_home / "project")
    assert not _reaches_home(tmp_path / "users" / "other")
    assert not _reaches_home(tmp_path / "elsewhere")


@pytest.mark.parametrize("which", ["home", "parent"])
def test_a_case_changed_spelling_is_compared_by_identity(fake_home, which):
    from lionhub.agent.child_sandbox import _reaches_home

    respelled = _respelled(fake_home if which == "home" else fake_home.parent)
    if respelled is None:
        pytest.skip("the volume under tmp_path is case-sensitive")
    assert _reaches_home(respelled)


def test_a_link_to_home_reaches_home(fake_home, tmp_path):
    from lionhub.agent.child_sandbox import _reaches_home

    link = tmp_path / "link"
    link.symlink_to(fake_home)
    assert _reaches_home(link)


def test_the_childs_own_home_counts(fake_home, tmp_path):
    from lionhub.agent.child_sandbox import _reaches_home

    other = tmp_path / "other-home"
    other.mkdir()
    assert not _reaches_home(other)
    assert _reaches_home(other, {"HOME": str(other)})


def _account_home(monkeypatch, path):
    """Make the password database name ``path`` as this account's home."""
    import pwd

    fields = list(pwd.getpwuid(os.getuid()))
    fields[5] = str(path)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: pwd.struct_passwd(fields))


@pytest.mark.parametrize("which", ["home", "parent"])
def test_the_account_home_counts_when_home_points_elsewhere(
    fake_home, tmp_path, monkeypatch, which
):
    """A parent started with HOME at a scratch directory still refuses the account's
    home, and its parent, as a workspace; a project under it is not refused."""
    pytest.importorskip("pwd")
    account = tmp_path / "accounts" / "me"
    (account / "project").mkdir(parents=True)
    _account_home(monkeypatch, account)
    from lionhub.agent.child_sandbox import _reaches_home

    assert _reaches_home(account if which == "home" else account.parent)
    assert not _reaches_home(account / "project")


def test_the_real_account_home_is_refused_under_a_scratch_home(tmp_path, monkeypatch):
    """The same with nothing faked but HOME: the account's own home from the password
    database, and its parent, are refused; a directory under it is not. Read-only."""
    pwd = pytest.importorskip("pwd")
    account = pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", str(tmp_path / "scratch"))
    from lionhub.agent.child_sandbox import _reaches_home

    assert _reaches_home(account)
    assert _reaches_home(os.path.dirname(account))
    assert not _reaches_home(os.path.join(account, "a-project-that-need-not-exist"))


def test_a_password_database_without_this_account_falls_back_to_home(fake_home, monkeypatch):
    pwd = pytest.importorskip("pwd")

    def _missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", _missing)
    from lionhub.agent.child_sandbox import _home_directories, _reaches_home

    assert _home_directories() == {os.path.realpath(fake_home)}
    assert _reaches_home(fake_home)


@pytest.mark.parametrize("workload", ["heavy", "light"])
def test_a_codex_child_at_home_is_refused(fake_home, sandbox_on, workload):
    from lionhub.providers.openai.codex import CodexCodeRequest

    request = CodexCodeRequest(prompt="hi", repo=fake_home, workload=workload)
    with pytest.raises(ConfigurationError, match=_MESSAGE):
        request.as_cmd_args()


def test_a_codex_child_in_a_project_runs(fake_home, sandbox_on, monkeypatch):
    import lionhub.providers.openai.codex as codex_module
    from lionhub.providers.openai.codex import CodexCodeRequest

    # The probe record gate reads the host's codex binary; this test is about the working
    # directory only.
    monkeypatch.setattr(
        codex_module, "require_passing_probe_record", lambda binary, env=None, cwd=None: "record"
    )
    args = CodexCodeRequest(prompt="hi", repo=fake_home / "project").as_cmd_args()
    assert args[:2] == ["exec", "--json"]


def test_a_codex_child_at_home_runs_with_the_child_sandbox_off(fake_home):
    from lionhub.providers.openai.codex import CodexCodeRequest

    assert CodexCodeRequest(prompt="hi", repo=fake_home).as_cmd_args()[:2] == ["exec", "--json"]


@pytest.mark.parametrize("workload", ["heavy", "light"])
def test_a_claude_code_child_at_home_is_refused(fake_home, sandbox_on, workload):
    from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest

    request = ClaudeCodeRequest(prompt="hi", repo=fake_home, workload=workload)
    with pytest.raises(ConfigurationError, match=_MESSAGE):
        request.as_cmd_args()


def test_a_claude_code_child_at_home_runs_with_the_child_sandbox_off(fake_home):
    from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest

    args = ClaudeCodeRequest(prompt="hi", repo=fake_home).as_cmd_args()
    assert "--settings" not in args


def test_the_seatbelt_policy_refuses_a_home_workspace(fake_home, sandbox_on):
    from lionhub.tools._seatbelt import SeatbeltPolicy

    with pytest.raises(ConfigurationError, match=_MESSAGE):
        SeatbeltPolicy.for_workspace(fake_home)
    with pytest.raises(ConfigurationError, match=_MESSAGE):
        SeatbeltPolicy.for_workspace("/")
    policy = SeatbeltPolicy.for_workspace(fake_home / "project")
    assert str((fake_home / "project").resolve()) in policy.writable_roots


def test_the_seatbelt_policy_takes_a_home_workspace_with_the_child_sandbox_off(fake_home):
    from lionhub.tools._seatbelt import SeatbeltPolicy

    assert str(fake_home.resolve()) in SeatbeltPolicy.for_workspace(fake_home).writable_roots


@pytest.mark.parametrize("workload", ["heavy", "light"])
def test_an_antigravity_child_at_home_is_refused(fake_home, sandbox_on, monkeypatch, workload):
    import lionhub.tools._seatbelt as seatbelt
    from lionhub.providers.google import gemini_code

    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    request = gemini_code.GeminiCodeRequest(prompt="hi", repo=fake_home, workload=workload)
    with pytest.raises(ConfigurationError, match=_MESSAGE):
        gemini_code._confined_agy_command("agy", request)
