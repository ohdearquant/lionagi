"""The account home's own `.lionagi` is user-level configuration, never a project directory.

Discovery finds project `.lionagi` directories by walking up from the working
directory. From a checkout below the account's home that walk reached the
account's own `.lionagi`, so a run that redirected HOME still read the real
profiles, MCP configuration and settings. These tests build a synthetic account
home and point the password-database lookup at it.
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi import _paths
from lionagi import _settings as settings_mod
from lionagi.agent.factory import _resolve_mcp_path
from lionagi.agent.settings import load_settings


def _lionagi(root: Path, *, mcp: bool = False, settings: str | None = None) -> Path:
    directory = root / ".lionagi"
    (directory / "agents").mkdir(parents=True, exist_ok=True)
    if mcp:
        (directory / ".mcp.json").write_text('{"mcpServers": {}}')
    if settings is not None:
        (directory / "settings.yaml").write_text(settings)
    return directory


def _stub_git_root(monkeypatch, root: Path, cwd: Path, *, returncode: int = 0) -> None:
    def _run(args, **kwargs):
        assert args == ["git", "rev-parse", "--show-toplevel"]
        assert kwargs["cwd"] == str(cwd)
        stdout = f"{root}\n" if returncode == 0 else ""
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(_paths.subprocess, "run", _run)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """top/ holds acct/ (the account home), which holds work/repo/; HOME is safe/."""
    account_home_lookup = _paths._account_home
    root = tmp_path.resolve()
    top = root / "top"
    acct = top / "acct"
    work = acct / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    safe = root / "safe"
    safe.mkdir()
    monkeypatch.setattr(_paths, "_account_home", lambda: acct)
    monkeypatch.setenv("HOME", str(safe))
    monkeypatch.chdir(repo)
    return SimpleNamespace(
        root=root,
        top=top,
        acct=acct,
        work=work,
        repo=repo,
        safe=safe,
        account_home_lookup=account_home_lookup,
    )


@pytest.fixture
def opened_settings(monkeypatch):
    opened: list[Path] = []

    def _recording_open(path, *args, **kwargs):
        opened.append(Path(path))
        return open(path, *args, **kwargs)

    monkeypatch.setattr(settings_mod, "open", _recording_open, raising=False)
    return opened


def _found(layout) -> list[Path]:
    # Only what the test built: a stray `.lionagi` above the temporary root is not under test.
    return [d for d in _paths.find_lionagi_dirs() if layout.root in d.parents]


def _mcp(**spec) -> str | None:
    return _resolve_mcp_path(
        SimpleNamespace(mcp_config_path=spec.pop("mcp_config_path", None), cwd=None), **spec
    )


def _password_database_home() -> Path:
    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        pytest.skip("no password entry for uid")


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="no password database on this platform")
def test_the_account_home_comes_from_the_password_database_not_from_home():
    expected = _password_database_home()
    assert _paths._account_home() == expected
    # The suite runs under a HOME of its own, so the two must differ here.
    assert Path.home() != expected


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="no password database on this platform")
def test_password_database_home_skips_when_uid_is_missing(monkeypatch):
    import pwd

    def _no_entry(uid):
        raise KeyError(f"getpwuid(): uid not found: {uid}")

    monkeypatch.setattr(pwd, "getpwuid", _no_entry)

    with pytest.raises(pytest.skip.Exception, match="no password entry for uid"):
        _password_database_home()


@pytest.mark.parametrize(
    "failure",
    [
        "no entry for the uid",
        "no password database",
        "an empty home",
        "no home",
        "a relative home",
    ],
)
def test_an_unidentified_account_home_excludes_nothing(layout, monkeypatch, failure):
    import pwd

    if failure == "no entry for the uid":

        def _no_entry(uid):
            raise KeyError(f"getpwuid(): uid not found: {uid}")

        monkeypatch.setattr(pwd, "getpwuid", _no_entry)
    elif failure == "no password database":
        monkeypatch.setitem(sys.modules, "pwd", None)
    else:
        # A record that names no usable home. An empty one would read as the working
        # directory, and the walk below starts there.
        pw_dir = {"an empty home": "", "no home": None, "a relative home": "acct"}[failure]
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=pw_dir))
        monkeypatch.chdir(layout.acct)
    monkeypatch.setattr(_paths, "_account_home", layout.account_home_lookup)
    acct_dir = _lionagi(layout.acct)
    top_dir = _lionagi(layout.top)
    (layout.acct / ".mcp.json").write_text("{}")
    # HOME names the account's directory, so HOME standing in for the failed lookup would show:
    # the account `.lionagi` would move behind top's, and its bare MCP file would go unread.
    monkeypatch.setenv("HOME", str(layout.acct))

    assert _paths._account_home() is None
    assert _found(layout) == [acct_dir, top_dir]
    assert _mcp(trust_project_settings=True) == str(layout.acct / ".mcp.json")


def test_the_account_lionagi_above_the_cwd_is_read_by_no_walk(layout, opened_settings):
    _lionagi(layout.acct, mcp=True, settings="poison: true\n")
    (layout.acct / ".mcp.json").write_text("{}")
    user = _lionagi(layout.safe, mcp=True, settings="user: true\n")

    assert _found(layout) == [user]
    for trust in (True, False):
        assert _mcp(trust_project_settings=trust) == str(user / ".mcp.json")
    assert load_settings(include_project=True) == {"user": True}
    assert load_settings(include_project=False) == {"user": True}
    assert not [p for p in opened_settings if layout.acct in p.parents]


def test_without_a_user_directory_the_account_one_is_still_not_found(layout, opened_settings):
    _lionagi(layout.acct, mcp=True, settings="poison: true\n")

    assert _found(layout) == []
    assert _mcp(trust_project_settings=True) is None
    assert load_settings(include_project=True) == {}
    assert opened_settings == []


def test_an_account_home_that_is_the_git_root_adds_no_project_directory(layout, monkeypatch):
    _stub_git_root(monkeypatch, layout.acct, layout.repo)
    _lionagi(layout.acct)

    assert _paths._find_git_root(layout.repo) == layout.acct
    assert _found(layout) == []


def test_the_account_home_is_recognised_by_identity_not_spelling(layout, monkeypatch):
    acct_dir = _lionagi(layout.acct)
    alias = layout.root / "alias"
    alias.symlink_to(layout.acct, target_is_directory=True)
    # The password database names the home through a symlink; the walk meets the real path.
    monkeypatch.setattr(_paths, "_account_home", lambda: alias)

    assert acct_dir not in _found(layout)


def test_account_directories_are_recognised_by_case_variant(layout):
    _lionagi(layout.acct)
    home_variant = layout.acct.with_name(layout.acct.name.swapcase())
    if home_variant == layout.acct or not home_variant.is_dir():
        pytest.skip("filesystem does not resolve case variants to the same directory")

    assert _paths.is_account_home(home_variant)
    assert _paths.is_account_lionagi_dir(home_variant / ".LIONAGI")


def test_a_project_lionagi_that_links_to_the_account_one_is_excluded(layout):
    acct_dir = _lionagi(layout.acct)
    (layout.work / ".lionagi").symlink_to(acct_dir, target_is_directory=True)
    project = _lionagi(layout.repo)

    assert _found(layout) == [project]


def test_the_user_level_directory_follows_home_not_lionagi_home(layout, monkeypatch):
    first = _lionagi(layout.root / "first")
    second = _lionagi(layout.root / "second")
    monkeypatch.setenv("LIONAGI_HOME", str(_lionagi(layout.root / "runs")))

    monkeypatch.setenv("HOME", str(first.parent))
    assert _found(layout) == [first]
    monkeypatch.setenv("HOME", str(second.parent))
    assert _found(layout) == [second]


def test_under_the_real_home_the_account_lionagi_comes_once_and_last(layout, monkeypatch):
    monkeypatch.setenv("HOME", str(layout.acct))
    repo_dir = _lionagi(layout.repo)
    work_dir = _lionagi(layout.work)
    acct_dir = _lionagi(layout.acct)
    top_dir = _lionagi(layout.top)
    (layout.repo / "pkg").mkdir()
    monkeypatch.chdir(layout.repo / "pkg")
    _stub_git_root(monkeypatch, layout.repo, layout.repo / "pkg")

    assert _paths._find_git_root(Path.cwd()) == layout.repo

    # A directory above the account home now precedes it: the account's own is user-level.
    assert _found(layout) == [repo_dir, work_dir, top_dir, acct_dir]


def test_under_the_real_home_global_settings_are_loaded_once(layout, monkeypatch, opened_settings):
    account_dir = _lionagi(layout.acct, settings="items:\n  - x\n")
    monkeypatch.setenv("HOME", str(layout.acct))

    assert load_settings(include_project=True) == {"items": ["x"]}
    assert opened_settings == [account_dir / "settings.yaml"]


def test_the_account_lionagi_still_ends_the_mcp_walk(layout):
    _lionagi(layout.acct)
    _lionagi(layout.top, mcp=True)
    (layout.top / ".mcp.json").write_text("{}")

    assert _mcp(trust_project_settings=True) is None


def test_an_account_home_bare_mcp_file_needs_an_explicit_path(layout):
    bare = layout.acct / ".mcp.json"
    bare.write_text("{}")

    assert _mcp(trust_project_settings=True) is None
    assert _mcp(mcp_config_path=str(bare), trust_project_settings=True) == str(bare)


def test_a_project_bare_mcp_file_is_read_when_trusted(layout):
    (layout.work / ".mcp.json").write_text("{}")
    assert _mcp(trust_project_settings=True) == str(layout.work / ".mcp.json")
    assert _mcp(trust_project_settings=False) is None

    namespaced = _lionagi(layout.work, mcp=True) / ".mcp.json"
    assert _mcp(trust_project_settings=True) == str(namespaced)


def test_an_excluded_account_settings_file_promotes_no_farther_one(layout, opened_settings):
    _lionagi(layout.acct, settings="poison: true\n")
    _lionagi(layout.top, settings="far: true\n")
    _lionagi(layout.safe, settings="user: true\n")

    assert load_settings(include_project=True) == {"user": True}
    assert opened_settings == [layout.safe / ".lionagi" / "settings.yaml"]
