# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage-targeted tests for agent/settings.py — uncovered paths."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.agent.settings import (
    _import_hook,
    _make_shell_hook,
    _resolve_hook_spec,
    apply_hooks_from_settings,
)
from lionagi.agent.spec import AgentSpec

# ---------------------------------------------------------------------------
# _import_hook: untrusted module raises, no colon returns None, ImportError
# ---------------------------------------------------------------------------


def test_import_hook_no_colon_returns_none():
    result = _import_hook("lionagi.agent.hooks", trusted_hook_modules={"lionagi.agent.hooks"})
    assert result is None


def test_import_hook_untrusted_module_raises():
    with pytest.raises(PermissionError, match="Untrusted hook module"):
        _import_hook("evil.module:function", trusted_hook_modules={"lionagi.agent.hooks"})


def test_import_hook_trusted_module_import_error_returns_none():
    result = _import_hook(
        "lionagi.agent.hooks:nonexistent_function_xyz_abc",
        trusted_hook_modules={"lionagi.agent.hooks"},
    )
    assert result is None


def test_import_hook_trusted_module_returns_callable():
    result = _import_hook(
        "lionagi.agent.hooks:guard_destructive",
        trusted_hook_modules={"lionagi.agent.hooks"},
    )
    assert callable(result)


def test_import_hook_nonexistent_module_in_trusted_set_returns_none():
    result = _import_hook(
        "lionagi.agent.hooks_nonexistent:fn",
        trusted_hook_modules={"lionagi.agent.hooks_nonexistent"},
    )
    assert result is None


# ---------------------------------------------------------------------------
# _resolve_hook_spec: string shorthand, dict without python/command → None
# ---------------------------------------------------------------------------


def test_resolve_hook_spec_string_shorthand_trusted():
    result = _resolve_hook_spec(
        "lionagi.agent.hooks:guard_destructive",
        phase="pre",
        tool_name="bash",
        trusted_hook_modules={"lionagi.agent.hooks"},
    )
    assert callable(result)


def test_resolve_hook_spec_unknown_dict_returns_none():
    result = _resolve_hook_spec(
        {"unknown_key": "value"},
        phase="pre",
        tool_name="bash",
        trusted_hook_modules={"lionagi.agent.hooks"},
    )
    assert result is None


def test_resolve_hook_spec_dict_python_returns_callable():
    result = _resolve_hook_spec(
        {"python": "lionagi.agent.hooks:guard_destructive"},
        phase="pre",
        tool_name="bash",
        trusted_hook_modules={"lionagi.agent.hooks"},
    )
    assert callable(result)


def test_resolve_hook_spec_dict_command_returns_callable():
    result = _resolve_hook_spec(
        {"command": ["echo", "hello"]},
        phase="pre",
        tool_name="bash",
        trusted_hook_modules={"lionagi.agent.hooks"},
    )
    assert callable(result)


# ---------------------------------------------------------------------------
# _make_shell_hook: post hook path exercised
# ---------------------------------------------------------------------------


async def test_make_shell_post_hook_runs_and_returns_none(monkeypatch):
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    hook = _make_shell_hook(["echo", "{file_path}"], "post", "editor")

    result = await hook("editor", "write", {"file_path": "out.py"}, {"success": True})
    assert result is None


async def test_make_shell_post_hook_handles_timeout_silently(monkeypatch):
    async def raise_timeout(*_a, **_kw):
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_timeout)

    hook = _make_shell_hook(["slow-cmd"], "post", "editor")

    result = await hook("editor", "write", {}, {"success": True})
    assert result is None


async def test_make_shell_pre_hook_timeout_raises_permission_error(monkeypatch):
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    hook = _make_shell_hook(["slow-guard"], "pre", "bash")
    with pytest.raises(PermissionError, match="timed out"):
        await hook("bash", "run", {"command": "echo hi"})


async def test_make_shell_pre_hook_exec_error_raises_permission_error(monkeypatch):
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("not found")),
    )

    hook = _make_shell_hook(["guard"], "pre", "bash")
    with pytest.raises(PermissionError, match="Hook execution error"):
        await hook("bash", "run", {})


async def test_make_shell_pre_hook_nonzero_empty_stderr_uses_fallback(monkeypatch):
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

    hook = _make_shell_hook(["guard"], "pre", "bash")
    with pytest.raises(PermissionError, match="Hook blocked"):
        await hook("bash", "run", {})


# ---------------------------------------------------------------------------
# apply_hooks_from_settings: non-list hook_specs wrapped, all phases wired
# ---------------------------------------------------------------------------


def test_apply_hooks_from_settings_wraps_single_dict_in_list():
    settings = {
        "hooks": {
            "pre": {
                "bash": {"command": ["echo", "guard"]},  # dict, not list
            }
        }
    }
    config = AgentSpec.compose("implementer")
    apply_hooks_from_settings(config, settings)
    # AgentSpec stores hooks as "pre:bash" → list
    assert len(config.hook_handlers.get("pre:bash", [])) == 1


def test_apply_hooks_skips_none_handlers():
    settings = {
        "hooks": {
            "pre": {
                "bash": [{"unknown_key": "noop"}],
            }
        }
    }
    config = AgentSpec.compose("implementer")
    apply_hooks_from_settings(config, settings)
    assert config.hook_handlers.get("pre:bash", []) == []


def test_apply_hooks_all_phases_registered():
    settings = {
        "hooks": {
            "pre": {"reader": [{"command": ["cat", "{path}"]}]},
            "post": {"editor": [{"command": ["ruff", "format", "{file_path}"]}]},
            "on_error": {"bash": [{"command": ["logger", "error"]}]},
        }
    }
    config = AgentSpec.compose("implementer")
    apply_hooks_from_settings(config, settings)
    assert len(config.hook_handlers.get("pre:reader", [])) == 1
    assert len(config.hook_handlers.get("post:editor", [])) == 1
    assert len(config.hook_handlers.get("error:bash", [])) == 1


def test_apply_hooks_from_settings_loads_defaults_when_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = AgentSpec.compose("implementer")
    result = apply_hooks_from_settings(config, settings=None)
    assert result is config
