# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A ``light`` CLI agent is held to what its engine can enforce: codex in its
workspace-write sandbox with no network and no extra writable roots, agy under
``--sandbox``, Claude Code without its file-writing tools. A setting that would undo
that is refused, and Pi, which has no sandbox to pin, refuses ``light`` outright. A
``heavy`` agent's command line is the one it had before workloads existed."""

from __future__ import annotations

import subprocess

import pytest

from lionagi._errors import CLIInputRefusedError
from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
from lionagi.providers.google.gemini_code import GeminiCodeRequest
from lionagi.providers.openai.codex import CodexCodeRequest


def _codex(tmp_path, **kw) -> list[str]:
    return CodexCodeRequest(prompt="hi", repo=tmp_path, **kw).as_cmd_args()


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


@pytest.mark.parametrize(
    "kw", [{}, {"full_auto": True}, {"sandbox": "workspace-write"}, {"ask_for_approval": "never"}]
)
def test_a_light_codex_agent_is_sandboxed_to_its_cwd_with_no_network(tmp_path, kw):
    args = _codex(tmp_path, workload="light", **kw)
    assert _pairs(args, "--sandbox") == ["workspace-write"]
    assert "-s" not in args
    overrides = _pairs(args, "-c")
    assert "sandbox_workspace_write.writable_roots=[]" in overrides
    assert "sandbox_workspace_write.network_access=false" in overrides
    assert "--add-dir" not in args


def test_a_light_codex_agent_asked_for_read_only_stays_read_only(tmp_path):
    args = _codex(tmp_path, workload="light", sandbox="read-only")
    assert _pairs(args, "--sandbox") == ["read-only"]


def test_a_light_codex_agent_in_a_linked_worktree_gets_no_writable_git_dir(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *cmd], cwd=main, check=True
        )
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=main, check=True)
    heavy = CodexCodeRequest(prompt="hi", repo=wt, full_auto=True).as_cmd_args()
    light = CodexCodeRequest(prompt="hi", repo=wt, workload="light").as_cmd_args()
    assert "--add-dir" in heavy
    assert "--add-dir" not in light


@pytest.mark.parametrize(
    ("kw", "named"),
    [
        ({"bypass_approvals": True}, "bypass_approvals"),
        ({"sandbox": "danger-full-access"}, "danger-full-access"),
        ({"add_dir": ["/tmp/elsewhere"]}, "add_dir"),
        ({"config_overrides": {"sandbox_workspace_write.network_access": True}}, "network_access"),
        ({"config_overrides": {"sandbox_workspace_write": {"writable_roots": ["/"]}}}, "sandbox_"),
        ({"config_overrides": {"sandbox_mode": "danger-full-access"}}, "sandbox_mode"),
    ],
)
def test_a_light_codex_agent_that_could_write_elsewhere_is_refused_at_construction(
    tmp_path, kw, named
):
    with pytest.raises(CLIInputRefusedError, match=named) as refused:
        CodexCodeRequest(prompt="hi", repo=tmp_path, workload="light", **kw)
    assert "Declare the agent's workload 'heavy'" in str(refused.value)


def test_a_copied_request_cannot_slip_a_light_agent_past_the_refusal(tmp_path):
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, workload="light")
    copied = request.model_copy(update={"bypass_approvals": True})
    with pytest.raises(CLIInputRefusedError, match="bypass_approvals"):
        copied.as_cmd_args()


@pytest.mark.parametrize("kw", [{}, {"full_auto": True}, {"bypass_approvals": True}])
def test_a_heavy_codex_agent_keeps_its_command_line(tmp_path, kw):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert _codex(tmp_path, workload="heavy", **kw) == _codex(tmp_path, **kw)


def test_a_light_claude_agent_runs_without_its_file_writing_tools(tmp_path):
    args = ClaudeCodeRequest(
        prompt="hi", repo=tmp_path, workload="light", disallowed_tools=["WebFetch", "Write"]
    ).as_cmd_args()
    assert args.count("--disallowedTools") == 1
    start = args.index("--disallowedTools") + 1
    denied = []
    for token in args[start:]:
        if token.startswith("-"):
            break
        denied.append(token)
    assert denied == ["WebFetch", "Write", "Edit", "NotebookEdit"]
    assert "Bash" not in denied


def test_a_heavy_claude_agent_keeps_its_command_line(tmp_path):
    kw = {"prompt": "hi", "repo": tmp_path, "disallowed_tools": ["WebFetch"]}
    assert (
        ClaudeCodeRequest(**kw, workload="heavy").as_cmd_args()
        == ClaudeCodeRequest(**kw).as_cmd_args()
    )


def test_a_light_gemini_agent_always_runs_sandboxed(tmp_path):
    light = GeminiCodeRequest(prompt="hi", repo=tmp_path, workload="light").as_cmd_args()
    heavy = GeminiCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()
    assert light.count("--sandbox") == 1
    assert "--sandbox" not in heavy


@pytest.mark.asyncio
async def test_a_light_pi_agent_is_refused_before_anything_starts(tmp_path, monkeypatch):
    import lionagi.providers.pi.cli as pi_cli

    monkeypatch.setattr(pi_cli, "PI_CLI", str(tmp_path / "pi-must-not-run"))
    request = pi_cli.PiCodeRequest(prompt="hi", repo=tmp_path, workload="light")
    with pytest.raises(CLIInputRefusedError, match="Pi has none"):
        async for _ in pi_cli._ndjson_from_cli(request):
            pass
