# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for built-in coding-agent hooks."""

import inspect

import pytest

from lionagi.agent.hooks import auto_format_python, guard_destructive, guard_paths


async def test_guard_paths_returns_callable_hook(tmp_path):
    allowed = tmp_path / "project"
    allowed.mkdir()

    hook = guard_paths(allowed_paths=[str(allowed)])

    assert callable(hook)
    assert not inspect.iscoroutine(hook)
    assert await hook("reader", "read", {"path": str(allowed / "ok.py")}) is None


async def test_guard_paths_blocks_prefix_sibling_escape(tmp_path):
    allowed = tmp_path / "project"
    sibling = tmp_path / "project-evil"
    allowed.mkdir()
    sibling.mkdir()
    hook = guard_paths(allowed_paths=[str(allowed)])

    with pytest.raises(PermissionError, match="allowed list"):
        await hook("reader", "read", {"path": str(sibling / "secret.py")})


async def test_auto_format_python_uses_argv_without_shell(monkeypatch):
    calls = []

    async def fake_run_sync(fn, cmd, shell, timeout, cwd):
        calls.append((cmd, shell, timeout, cwd))
        return {"returncode": 0}

    monkeypatch.setattr("lionagi.ln.concurrency.run_sync", fake_run_sync)

    result = await auto_format_python(
        "editor",
        "write",
        {"file_path": "src/weird;name.py"},
        {"success": True},
    )

    assert result is None
    assert calls == [(["ruff", "format", "src/weird;name.py"], False, 10.0, None)]


async def test_guard_destructive_blocks_dangerous_commands():
    with pytest.raises(PermissionError, match="Blocked destructive command"):
        await guard_destructive("bash", "run", {"command": "git reset --hard HEAD"})


async def test_guard_destructive_allows_safe_commands():
    result = await guard_destructive("bash", "run", {"command": "git status"})
    assert result is None


async def test_guard_paths_blocks_absolute_denied_path(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.touch()
    hook = guard_paths(denied_paths=[str(secret)])
    with pytest.raises(PermissionError, match="deny rule"):
        await hook("reader", "read", {"path": str(secret)})


async def test_guard_paths_blocks_relative_denied_name(tmp_path):
    hook = guard_paths(denied_paths=[".env"])
    with pytest.raises(PermissionError, match="deny rule"):
        await hook("reader", "read", {"path": str(tmp_path / ".env.local")})


async def test_guard_paths_allows_unrelated_path(tmp_path):
    allowed = tmp_path / "ok.py"
    allowed.touch()
    hook = guard_paths(denied_paths=[".env"])
    result = await hook("reader", "read", {"path": str(allowed)})
    assert result is None


# ---------------------------------------------------------------------------
# LIONAGI-AUDIT-001 (agent-standards 2026-06-06): glob deny patterns
# ---------------------------------------------------------------------------


async def test_guard_paths_glob_deny_blocks_key_file(tmp_path):
    """'*.key' pattern must block /tmp/api.key (audit LIONAGI-AUDIT-001).

    Before the fix, '*.key' was tested with raw substring containment
    (``denied in raw_path or denied in resolved.name``).  The fnmatch-per-
    component approach now used here correctly blocks files whose name matches
    the glob pattern.
    """
    hook = guard_paths(denied_paths=["*.key"])
    with pytest.raises(PermissionError, match="deny rule"):
        await hook("reader", "read", {"path": str(tmp_path / "api.key")})


async def test_guard_paths_glob_deny_blocks_nested_key_file(tmp_path):
    """Glob deny '*.key' blocks a file nested inside an otherwise allowed tree."""
    allowed = tmp_path / "project"
    allowed.mkdir()
    hook = guard_paths(allowed_paths=[str(allowed)], denied_paths=["*.key"])
    nested = allowed / "subdir" / "secrets.key"
    nested.parent.mkdir(parents=True)
    with pytest.raises(PermissionError, match="deny rule"):
        await hook("reader", "read", {"path": str(nested)})


async def test_guard_paths_glob_deny_allows_non_matching(tmp_path):
    """'*.key' must NOT block a file whose name merely *contains* 'key' but doesn't match."""
    hook = guard_paths(denied_paths=["*.key"])
    # 'keystone.py' contains 'key' as a substring but does not match '*.key'.
    result = await hook("reader", "read", {"path": str(tmp_path / "keystone.py")})
    assert result is None


async def test_guard_paths_glob_deny_blocks_dotenv(tmp_path):
    """'.env' pattern blocks /project/.env via fnmatch component matching."""
    hook = guard_paths(denied_paths=[".env"])
    with pytest.raises(PermissionError, match="deny rule"):
        await hook("reader", "read", {"path": str(tmp_path / ".env")})
