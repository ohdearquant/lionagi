# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Contained Codex working-directory refusals do not expose path values."""

import pytest

from lionagi._errors import ContainmentRefusedError
from lionhub.providers.openai import codex


@pytest.mark.parametrize("kind", ["loop", "missing", "unreadable"])
def test_containment_repo_refusal_keeps_type_without_retaining_path(tmp_path, monkeypatch, kind):
    marker = "sensitive fixture content"
    repo = tmp_path / marker
    if kind == "loop":
        repo.symlink_to(repo.name, target_is_directory=True)
    elif kind == "unreadable":
        repo.mkdir()
        original_access = codex.os.access
        monkeypatch.setattr(
            codex.os,
            "access",
            lambda path, mode: False if str(path) == str(repo) else original_access(path, mode),
        )
    error = None
    try:
        codex.CodexCodeRequest(prompt="hello", repo=repo, contain_account_integrations=True)
    except Exception as caught:
        error = caught
    if not isinstance(error, ContainmentRefusedError):
        pytest.fail("working directory lost its containment refusal type", pytrace=False)
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if marker in str(current) or marker in repr(current):
            pytest.fail("working-directory refusal retains the path", pytrace=False)
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
    assert error.__cause__ is None
    assert error.__context__ is None
