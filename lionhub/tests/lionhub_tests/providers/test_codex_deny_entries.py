# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A profile's deny entries are written in the form codex applies.

A ``~`` names the child's home, not the parent's. A link under a linked directory is denied at
the place the Seatbelt rule puts it, which is the resolved directory plus the entry's own name.
"""

from __future__ import annotations

import os

import pytest

from lionhub.providers.openai._codex_permissions import deny_entries

pwd = pytest.importorskip("pwd")


@pytest.fixture
def parent_home(tmp_path, monkeypatch):
    """This process's own HOME, set to a directory no child environment names."""
    home = tmp_path / "parent-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _under(entries: list[str], root) -> list[str]:
    return [entry for entry in entries if entry.startswith(str(root))]


def test_tilde_expands_against_the_child_home(tmp_path, parent_home):
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    entries = deny_entries(["~/.secret"], tmp_path / "codex-home", {"HOME": str(child_home)})
    assert os.path.join(child_home, ".secret") in entries
    assert _under(entries, parent_home) == []


def test_a_bare_tilde_is_the_child_home(tmp_path, parent_home):
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    entries = deny_entries(["~"], tmp_path / "codex-home", {"HOME": str(child_home)})
    assert str(child_home) in entries
    assert _under(entries, parent_home) == []


@pytest.mark.parametrize("env", [None, {}, {"HOME": "relative/home"}])
def test_tilde_falls_back_to_the_account_home(tmp_path, parent_home, env):
    account = pwd.getpwuid(os.getuid()).pw_dir
    entries = deny_entries(["~/.secret"], tmp_path / "codex-home", env)
    assert os.path.join(account, ".secret") in entries
    assert _under(entries, parent_home) == []


def test_a_link_in_a_linked_directory_is_denied_where_it_sits(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "target").write_text("x")
    (real / "secret").symlink_to(real / "target")
    (tmp_path / "via").symlink_to(real, target_is_directory=True)
    written = str(tmp_path / "via" / "secret")
    placed = os.path.join(os.path.realpath(tmp_path / "via"), "secret")
    assert placed != written
    entries = deny_entries([written], tmp_path / "codex-home")
    assert written in entries
    assert placed in entries


def test_a_plain_path_is_listed_once(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    entries = deny_entries([str(plain)], tmp_path / "codex-home")
    assert entries.count(str(plain)) == 1
    assert len(entries) == len(set(entries))
