# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Path resolution refusals use stable exception categories and clean chains."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lionagi.libs.path_safety import contain_path_in_root
from lionhub.providers import _cli_subprocess
from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest


class UnusualOSError(OSError):
    def __init__(self, code, message):
        super().__init__(code, message)


class UnusualRuntimeError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(code, message)


def _exception(kind):
    if kind == "value":
        return UnicodeEncodeError("utf-8", "\ud800", 0, 1, "unencodable")
    if kind == "os":
        return UnusualOSError(7, "private fixture")
    return UnusualRuntimeError(7, "private fixture")


@pytest.mark.parametrize("site", ["workspace", "resolve", "inspect", "contain"])
@pytest.mark.parametrize("kind", ["value", "os", "runtime"])
def test_unusual_error_constructor_uses_fixed_category(tmp_path, monkeypatch, site, kind):
    original = _exception(kind)
    expected = {"value": ValueError, "os": OSError, "runtime": RuntimeError}[kind]

    def fail(*_args, **_kwargs):
        raise original

    with monkeypatch.context() as patch:
        if site == "workspace":
            patch.setattr(_cli_subprocess, "contain_and_resolve", fail)
            operation = lambda: _cli_subprocess.resolve_cli_workspace(tmp_path, "child")
        elif site == "resolve":
            patch.setattr(Path, "resolve", fail)
            operation = lambda: _cli_subprocess._resolve_cli_path(tmp_path, "repo")
        elif site == "inspect":
            patch.setattr(Path, "is_dir", fail)
            operation = lambda: _cli_subprocess.resolve_cli_workspace(tmp_path, None)
        else:
            patch.setattr(Path, "resolve", fail)
            operation = lambda: contain_path_in_root("child", tmp_path, "file")
        caught = None
        try:
            operation()
        except Exception as exc:
            caught = exc
    if type(caught) is not expected:
        pytest.fail("resolution did not raise the fixed exception category", pytrace=False)
    if caught.__cause__ is not None or caught.__context__ is not None:
        pytest.fail("resolution retained the original exception", pytrace=False)
    if "private fixture" in str(caught) or "private fixture" in repr(caught):
        pytest.fail("resolution retained the input", pytrace=False)


@pytest.mark.parametrize("site", ["workspace", "resolve", "inspect", "contain"])
def test_lone_surrogate_resolution_is_a_clean_value_error(tmp_path, site):
    value = "\ud800x"
    caught = None
    try:
        if site == "workspace":
            _cli_subprocess.resolve_cli_workspace(tmp_path, value)
        elif site == "resolve":
            _cli_subprocess._resolve_cli_path(tmp_path / value, "repo")
        elif site == "inspect":
            _cli_subprocess.resolve_cli_workspace(tmp_path / value, None)
        else:
            contain_path_in_root(value, tmp_path, "file")
    except Exception as exc:
        caught = exc
    if type(caught) is not ValueError:
        pytest.fail("surrogate resolution did not raise fixed ValueError", pytrace=False)
    if caught.__cause__ is not None or caught.__context__ is not None:
        pytest.fail("surrogate resolution retained the original exception", pytrace=False)


def test_claude_bypass_with_surrogate_workspace_is_a_validation_error(tmp_path):
    caught = None
    try:
        ClaudeCodeRequest(
            prompt="hello", repo=tmp_path, ws="\ud800x", permission_mode="bypassPermissions"
        )
    except Exception as exc:
        caught = exc
    if not isinstance(caught, ValidationError):
        pytest.fail("surrogate workspace did not produce a validation error", pytrace=False)
