# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The write set of an API-model leg: editor and bash under the macOS Seatbelt sandbox.

The tool functions are called directly, so no security pre-hook runs: `guard_paths`
and `guard_destructive` are absent, and a refusal here comes from the sandbox. The
temporary directory and the uv cache are moved under the test's own directory, so
the only writable roots are the workspace and those two, and a sibling of the
workspace is outside all three.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionagi.session.branch import Branch
from lionagi.tools import _seatbelt
from lionagi.tools._seatbelt import SeatbeltPolicy, seatbelt_available
from lionagi.tools.coding import CodingToolkit
from tests.cli import test_agent_api_provider as _provider_tests
from tests.cli.test_agent_api_provider import _answer, _last_round, _profile, _reply

leg = _provider_tests.leg  # the scripted-endpoint fixture the provider tests run on

needs_seatbelt = pytest.mark.skipif(
    not seatbelt_available(), reason="needs macOS with /usr/bin/sandbox-exec"
)


@pytest.fixture
def roots(monkeypatch, tmp_path):
    """Workspace, outside directory and the policy's own writable roots, all resolved."""
    base = tmp_path.resolve()
    for name in ("workspace", "outside", "tmp", "uv-cache"):
        (base / name).mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base / "tmp"))
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda: base / "uv-cache")
    return base


def _bound_tools(workspace: Path, *, network: bool = False) -> dict:
    policy = SeatbeltPolicy.for_workspace(workspace, network=network)
    toolkit = CodingToolkit(
        notify=False,
        workspace_root=str(workspace),
        tools=("reader", "editor", "bash"),
        seatbelt=policy,
    )
    return {t.func_callable.__name__: t.func_callable for t in toolkit.bind(Branch())}


def test_the_policy_writes_to_the_workspace_the_uv_cache_and_the_temporary_directory(roots):
    policy = SeatbeltPolicy.for_workspace(roots / "workspace")

    assert policy.writable_roots == (
        str(roots / "workspace"),
        str(roots / "uv-cache"),
        str(roots / "tmp"),
    )
    assert policy.read_only == (str(roots / "workspace" / ".git"),)
    assert policy.network is False


def test_the_network_rules_are_in_the_policy_only_when_asked_for(roots):
    off = SeatbeltPolicy.for_workspace(roots / "workspace").profile()
    on = SeatbeltPolicy.for_workspace(roots / "workspace", network=True).profile()

    assert off.startswith("(version 1)\n(deny default)\n")
    assert "network-outbound" not in off
    assert "(allow network-outbound)" in on


def test_paths_travel_as_parameters_and_never_inside_the_policy_text(roots):
    workspace = roots / "workspace"
    argv = SeatbeltPolicy.for_workspace(workspace).wrap(["/bin/echo", "-n"])

    assert argv[0] == "/usr/bin/sandbox-exec"
    assert argv[-3:] == ["--", "/bin/echo", "-n"]
    assert f"WRITABLE_ROOT_0={workspace}" in argv
    assert f"READ_ONLY_0={workspace / '.git'}" in argv
    assert str(workspace) not in argv[argv.index("-p") + 1]


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_outside_the_working_directory_is_refused_by_the_sandbox(roots):
    tools = _bound_tools(roots / "workspace")
    target = roots / "outside" / "escaped.txt"

    result = await tools["bash"](f"touch {target}", cwd=str(roots / "workspace"))
    refused = SeatbeltPolicy.for_workspace(roots / "workspace").write_text(target, "x")

    assert result["return_code"] != 0
    assert "Operation not permitted" in result["stderr"]
    assert refused is not None and "Operation not permitted" in refused
    assert not target.exists()


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_through_a_symlink_that_points_outside_is_refused(roots):
    workspace = roots / "workspace"
    (workspace / "link").symlink_to(roots / "outside", target_is_directory=True)
    tools = _bound_tools(workspace)

    through_bash = await tools["bash"]("touch link/via-bash.txt", cwd=str(workspace))
    through_editor = await tools["editor"]("write", "link/via-editor.txt", content="x")
    through_writer = SeatbeltPolicy.for_workspace(workspace).write_text(
        workspace / "link" / "via-writer.txt", "x"
    )

    assert through_bash["return_code"] != 0
    assert "Operation not permitted" in through_bash["stderr"]
    assert through_editor["success"] is False
    assert through_writer is not None and "Operation not permitted" in through_writer
    assert list((roots / "outside").iterdir()) == []


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_inside_the_working_directory_succeeds_and_reads_back(roots):
    workspace = roots / "workspace"
    tools = _bound_tools(workspace)

    written = await tools["editor"]("write", "pkg/notes.txt", content="first line\n")
    read = await tools["reader"]("read", "pkg/notes.txt")
    edited = await tools["editor"](
        "edit", "pkg/notes.txt", old_string="first line", new_string="second line"
    )
    ran = await tools["bash"]("touch made-by-bash.txt", cwd=str(workspace))
    temp = await tools["bash"](f"touch {roots / 'tmp' / 'scratch.txt'}", cwd=str(workspace))

    assert written["success"] is True, written
    assert "first line" in read["content"]
    assert edited["success"] is True, edited
    assert (workspace / "pkg" / "notes.txt").read_text() == "second line\n"
    assert ran["return_code"] == 0, ran
    assert (workspace / "made-by-bash.txt").is_file()
    assert temp["return_code"] == 0, temp
    assert (roots / "tmp" / "scratch.txt").is_file()


@needs_seatbelt
@pytest.mark.asyncio
async def test_the_workspace_git_directory_stays_read_only(roots):
    workspace = roots / "workspace"
    (workspace / ".git" / "hooks").mkdir(parents=True)
    tools = _bound_tools(workspace)

    hook = await tools["bash"]("touch .git/hooks/pre-commit", cwd=str(workspace))
    ignore = await tools["editor"]("write", ".gitignore", content="dist/\n")

    assert hook["return_code"] != 0
    assert "Operation not permitted" in hook["stderr"]
    assert not (workspace / ".git" / "hooks" / "pre-commit").exists()
    assert ignore["success"] is True, ignore


@pytest.mark.parametrize("host", ["no-sandbox-exec", "not-macos"])
@pytest.mark.parametrize("resumed", [False, True], ids=["new", "resumed"])
@pytest.mark.asyncio
async def test_the_write_set_on_a_host_without_sandbox_exec_is_refused_before_a_run_exists(
    leg, monkeypatch, tmp_path, host, resumed
):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._runs import find_branch

    branch_id = None
    if resumed:
        leg.replies = [_last_round(), _answer("first")]
        _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
        _, path = find_branch(branch_id)
        stored = json.loads(path.read_text())
        stored["metadata"]["api_launch"]["tools"] = "write"
        path.write_text(json.dumps(stored))
    if host == "no-sandbox-exec":
        monkeypatch.setattr(_seatbelt, "SANDBOX_EXEC", str(tmp_path / "missing" / "sandbox-exec"))
    else:
        monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="Seatbelt sandbox"):
        if resumed:
            await leg.run(_profile(api_tools="write"), "again", resume=branch_id)
        else:
            await leg.run(_profile(api_tools="write"))


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_api_network_must_be_a_boolean(value):
    import lionagi.cli.agent as agent_mod

    with pytest.raises(ConfigurationError, match="api_network"):
        agent_mod._profile_api_launch(_profile(api_tools="write", api_network=value))


def test_api_network_applies_only_to_the_write_set():
    import lionagi.cli.agent as agent_mod

    with pytest.raises(ConfigurationError, match="only to the write set"):
        agent_mod._profile_api_launch(_profile(api_tools="read", api_network=True))
    with pytest.raises(ConfigurationError, match="but no api_tools"):
        agent_mod._profile_api_launch(_profile(api_network=False))


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_set_leg_writes_inside_its_working_directory_through_the_loop(
    leg, monkeypatch
):
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda: leg.workspace.parent / "uv-cache")
    write = _reply(
        {
            "analysis": "write the summary",
            "action_requests": [
                {
                    "function": "editor",
                    "arguments": {
                        "action": "write",
                        "file_path": "summary.txt",
                        "content": "the notes say hello\n",
                    },
                }
            ],
            "action_strategy": "sequential",
            "extension_needed": True,
            "reason": {"title": "write", "content": "record the answer"},
        }
    )
    leg.replies = [write, _last_round(), _answer("written")]

    _, _, _, status, _ = await leg.run(_profile(api_tools="write"))

    assert status == "completed"
    assert set(leg.branch.acts.registry) == {"reader", "search", "context", "editor", "bash"}
    record = leg.branch.metadata["api_launch"]
    assert (record["tools"], record["network"]) == ("write", False)
    assert (leg.workspace / "summary.txt").read_text() == "the notes say hello\n"
