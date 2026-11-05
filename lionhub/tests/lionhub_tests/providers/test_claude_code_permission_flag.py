# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The Claude CLI argv names a permission mode only when the request does.

An explicit flag outranks the ``defaultMode`` in the user's Claude settings, so a
request that names no mode passes no flag and the CLI's own settings decide. A
request that names one gets exactly one flag for it.
"""

from __future__ import annotations

from typing import get_args

import pytest

from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest, ClaudePermissionMode

_FLAGS = ("--permission-mode", "--dangerously-skip-permissions")


def _permission_flags(req: ClaudeCodeRequest) -> list[str]:
    args = req.as_cmd_args()
    found = [a for a in args if a in _FLAGS or a.startswith("--permission-mode=")]
    if found == ["--permission-mode"]:
        return ["--permission-mode", args[args.index("--permission-mode") + 1]]
    return found


def test_a_request_that_names_no_mode_passes_no_permission_flag():
    assert _permission_flags(ClaudeCodeRequest(prompt="hi")) == []


def test_an_inline_permission_mode_is_detected():
    class RequestWithInlineMode:
        def as_cmd_args(self):
            return ["exec", "--permission-mode=default"]

    assert _permission_flags(RequestWithInlineMode()) == ["--permission-mode=default"]


@pytest.mark.parametrize(
    "mode", [m for m in get_args(ClaudePermissionMode) if m != "bypassPermissions"]
)
def test_a_named_mode_is_passed_as_it_is(mode):
    assert _permission_flags(ClaudeCodeRequest(prompt="hi", permission_mode=mode)) == [
        "--permission-mode",
        mode,
    ]


def test_bypass_is_the_skip_flag_alone(tmp_path):
    req = ClaudeCodeRequest(prompt="hi", permission_mode="bypassPermissions", repo=tmp_path)
    assert _permission_flags(req) == ["--dangerously-skip-permissions"]


def test_allowing_bypass_later_names_no_mode_now():
    req = ClaudeCodeRequest(prompt="hi", allow_dangerously_skip_permissions=True)
    assert "--allow-dangerously-skip-permissions" in req.as_cmd_args()
    assert _permission_flags(req) == []
