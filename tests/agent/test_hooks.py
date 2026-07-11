# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for built-in coding-agent hooks."""

import inspect

import pytest

from lionagi.agent.hooks import auto_format_python, guard_destructive, guard_paths
from lionagi.libs.path_safety import DENIED_NAMES


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
# Glob deny patterns
# ---------------------------------------------------------------------------


async def test_guard_paths_glob_deny_blocks_key_file(tmp_path):
    """'*.key' deny pattern must block files whose name matches via fnmatch component matching."""
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
    """A literal '.env' basename is denied by the DENIED_NAMES hard floor, even
    though a redundant caller '.env' deny pattern is also configured."""
    hook = guard_paths(denied_paths=[".env"])
    with pytest.raises(PermissionError, match="protected path"):
        await hook("reader", "read", {"path": str(tmp_path / ".env")})


# ---------------------------------------------------------------------------
# GLOB_CHARS security: only "*?[" trigger fnmatch; "~{}" stay in substring mode
# so patterns like "secret~" correctly block "mysecret~backup" via containment.
# ---------------------------------------------------------------------------


async def test_guard_paths_tilde_deny_uses_substring_mode(tmp_path):
    """'secret~' deny uses substring mode (not fnmatch), so it blocks paths containing 'secret~'."""
    hook = guard_paths(denied_paths=["secret~"])
    with pytest.raises(PermissionError, match="deny rule"):
        await hook("reader", "read", {"path": str(tmp_path / "mysecret~backup")})


async def test_guard_paths_brace_deny_uses_substring_mode(tmp_path):
    """'{secret}' in a deny rule must block paths containing that literal substring."""
    hook = guard_paths(denied_paths=["{secret}"])
    with pytest.raises(PermissionError, match="deny rule"):
        await hook("reader", "read", {"path": str(tmp_path / "my{secret}file")})


async def test_guard_paths_tilde_deny_allows_unrelated(tmp_path):
    """'secret~' deny must not block a path that does not contain 'secret~'."""
    hook = guard_paths(denied_paths=["secret~"])
    result = await hook("reader", "read", {"path": str(tmp_path / "config.py")})
    assert result is None


# ---------------------------------------------------------------------------
# Canonical workspace containment: symlink escapes and DENIED_NAMES floor
# ---------------------------------------------------------------------------


async def test_guard_paths_blocks_direct_symlink_escaping_workspace(tmp_path):
    """A direct file symlink inside the workspace pointing outside it is denied."""
    allowed = tmp_path / "project"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = allowed / "link.txt"
    link.symlink_to(outside)

    hook = guard_paths(allowed_paths=[str(allowed)])
    with pytest.raises(PermissionError, match="symlink"):
        await hook("reader", "read", {"path": str(link)})


async def test_guard_paths_blocks_intermediate_symlink_escaping_workspace(tmp_path):
    """An intermediate directory symlink inside the workspace pointing outside it is denied."""
    allowed = tmp_path / "project"
    allowed.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leaked.txt").write_text("secret")
    link_dir = allowed / "linkdir"
    link_dir.symlink_to(outside_dir)

    hook = guard_paths(allowed_paths=[str(allowed)])
    with pytest.raises(PermissionError, match="allowed list"):
        await hook("reader", "read", {"path": str(link_dir / "leaked.txt")})


async def test_guard_paths_denies_direct_symlink_to_in_workspace_target(tmp_path):
    """A direct symlink is denied by the pre-resolve symlink rule even when its
    target is inside the workspace."""
    allowed = tmp_path / "project"
    allowed.mkdir()
    real = allowed / "real.txt"
    real.write_text("ok")
    link = allowed / "link.txt"
    link.symlink_to(real)

    hook = guard_paths(allowed_paths=[str(allowed)])
    with pytest.raises(PermissionError, match="symlink"):
        await hook("reader", "read", {"path": str(link)})


async def test_guard_paths_denies_broken_symlink(tmp_path):
    """A broken direct symlink is denied before it is ever followed."""
    allowed = tmp_path / "project"
    allowed.mkdir()
    link = allowed / "broken.txt"
    link.symlink_to(allowed / "does-not-exist.txt")

    hook = guard_paths(allowed_paths=[str(allowed)])
    with pytest.raises(PermissionError, match="symlink"):
        await hook("reader", "read", {"path": str(link)})


@pytest.mark.parametrize("basename", [".env", ".netrc", "id_rsa"])
async def test_guard_paths_denies_protected_basenames_without_denied_paths(tmp_path, basename):
    """Protected basenames are denied even when the caller supplies no denied_paths,
    both with and without allowed roots configured."""
    protected = tmp_path / basename

    deny_only_hook = guard_paths()
    with pytest.raises(PermissionError, match="protected path"):
        await deny_only_hook("reader", "read", {"path": str(protected)})

    allowed = tmp_path / "project"
    allowed.mkdir()
    protected_in_root = allowed / basename
    allow_root_hook = guard_paths(allowed_paths=[str(allowed)])
    with pytest.raises(PermissionError, match="protected path"):
        await allow_root_hook("reader", "read", {"path": str(protected_in_root)})


@pytest.mark.parametrize("basename", sorted(DENIED_NAMES))
@pytest.mark.parametrize("casing", ["upper", "title"])
async def test_guard_paths_denies_protected_basenames_case_variants(tmp_path, basename, casing):
    """Every DENIED_NAMES member is denied under an uppercase/mixed-case spelling
    too, in both deny-only and allow-root modes — not just the exact lowercase
    spelling — so a case-insensitive filesystem alias (e.g. '.ENV' for '.env')
    cannot bypass the protected-basename floor."""
    variant = basename.upper() if casing == "upper" else basename.title()

    protected = tmp_path / variant
    deny_only_hook = guard_paths()
    with pytest.raises(PermissionError, match="protected path"):
        await deny_only_hook("reader", "read", {"path": str(protected)})

    allowed = tmp_path / "project"
    allowed.mkdir(exist_ok=True)
    protected_in_root = allowed / variant
    allow_root_hook = guard_paths(allowed_paths=[str(allowed)])
    with pytest.raises(PermissionError, match="protected path"):
        await allow_root_hook("reader", "read", {"path": str(protected_in_root)})


async def test_guard_paths_allows_relative_path_under_first_root(tmp_path):
    """A normal relative path resolves against and is allowed under the first root."""
    allowed = tmp_path / "project"
    allowed.mkdir()
    hook = guard_paths(allowed_paths=[str(allowed)])

    result = await hook("reader", "read", {"path": "src/main.py"})
    assert result is None


async def test_guard_paths_absolute_second_root_allowed_relative_resolves_first(tmp_path):
    """An absolute path under a second allowed root is allowed, while a relative
    path continues to resolve under the first allowed root."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    hook = guard_paths(allowed_paths=[str(first), str(second)])

    result = await hook("reader", "read", {"path": str(second / "under_second.py")})
    assert result is None

    result = await hook("reader", "read", {"path": "under_first.py"})
    assert result is None


async def test_guard_paths_cross_root_relative_path_accepted(tmp_path):
    """Pre-existing multi-root contract: a relative path is formed against the
    first allowed root, but the resulting candidate is accepted if it resolves
    under *any* configured root — not rejected outright just because it escapes
    the first root. With roots [src, docs], '../docs/guide.md' (relative to
    src) must resolve into docs and be allowed."""
    src = tmp_path / "src"
    docs = tmp_path / "docs"
    src.mkdir()
    docs.mkdir()
    (docs / "guide.md").write_text("hello")
    hook = guard_paths(allowed_paths=[str(src), str(docs)])

    result = await hook("reader", "read", {"path": "../docs/guide.md"})
    assert result is None


async def test_guard_paths_relative_path_outside_all_roots_still_denied(tmp_path):
    """A relative path that escapes every configured root remains denied."""
    src = tmp_path / "src"
    docs = tmp_path / "docs"
    outside = tmp_path / "outside"
    src.mkdir()
    docs.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    hook = guard_paths(allowed_paths=[str(src), str(docs)])

    with pytest.raises(PermissionError, match="allowed list"):
        await hook("reader", "read", {"path": "../outside/secret.txt"})
