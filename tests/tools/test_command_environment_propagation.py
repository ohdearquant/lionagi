# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Declared command names reach legacy, static and bound tool execution."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest


@pytest.fixture
def command_programs(monkeypatch, tmp_path):
    directory = tmp_path / "commands"
    directory.mkdir()
    capture = tmp_path / "command-environment.json"
    program = (
        "#!" + sys.executable + "\n"
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['PUBLIC_COMMAND_CAPTURE']).write_text(json.dumps({\n"
        "    'declared': 'GH_TOKEN' in os.environ,\n"
        "    'granted': 'SOMEVENDOR_API_KEY' in os.environ,\n"
        "    'alias': 'somevendor_api_key' in os.environ,\n"
        "    'fresh': os.environ.get('PUBLIC_REFRESH_TAG'),\n"
        "}))\n"
        "print('[]')\n"
    )
    for name in ["command-check", "grep", "find", "ruff", "sg"]:
        path = directory / name
        path.write_text(program)
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PUBLIC_COMMAND_CAPTURE", str(capture))
    monkeypatch.setenv("GH_TOKEN", "synthetic-selected-reference")
    monkeypatch.setenv("SOMEVENDOR_API_KEY", "synthetic-exact-grant")
    monkeypatch.setenv("somevendor_api_key", "synthetic-case-alias")
    monkeypatch.setenv("PUBLIC_REFRESH_TAG", "first")
    return directory, capture


def _observed(capture, fresh):
    assert json.loads(capture.read_text()) == {
        "declared": False,
        "granted": True,
        "alias": False,
        "fresh": fresh,
    }, "commands must keep selected names private and honor only the exact grant"


@pytest.mark.parametrize("context", ["declared", "owned"])
@pytest.mark.parametrize("consumer", ["bash", "grep", "find", "ruff", "ast"])
async def test_legacy_and_static_commands_use_fresh_declared_environments(
    monkeypatch, tmp_path, command_programs, context, consumer
):
    from lionagi import _command_env_metadata
    from lionagi.tools.code.ast_search import AstSearchRequest, AstSearchTool
    from lionagi.tools.code.bash import BashRequest, BashTool
    from lionagi.tools.code.check import CodeCheckRequest, CodeCheckTool
    from lionagi.tools.code.search import SearchRequest, SearchTool

    directory, capture = command_programs
    # Make the selected name's source explicit; it matches no default pattern.
    owned = frozenset({"GH_TOKEN"}) if context == "owned" else frozenset()
    monkeypatch.setattr(_command_env_metadata, "configured_command_env_names", lambda: owned)
    names = None if context == "owned" else frozenset({"GH_TOKEN"})
    grant = ("SOMEVENDOR_API_KEY",)
    if consumer == "bash":
        tool = BashTool(command_env_allowlist=grant, _command_env_names=names)
        request = BashRequest(command=shlex.join([str(directory / "command-check")]))
    elif consumer in {"grep", "find"}:
        tool = SearchTool(str(tmp_path), _command_env_names=names, _command_env_allowlist=grant)
        request = SearchRequest(action=consumer, pattern="sample", path=".")
    elif consumer == "ruff":
        tool = CodeCheckTool(tmp_path, _command_env_names=names, _command_env_allowlist=grant)
        request = CodeCheckRequest(paths=["."])
    else:
        tool = AstSearchTool(tmp_path, _command_env_names=names, _command_env_allowlist=grant)
        request = AstSearchRequest(pattern="$X", path=".")

    for fresh in ["first", "second"]:
        monkeypatch.setenv("PUBLIC_REFRESH_TAG", fresh)
        response = await tool.handle_request(request)
        if consumer == "bash":
            assert response.return_code == 0
        elif consumer in {"grep", "find"}:
            assert response.success
        else:
            assert response.status == "ok"
        _observed(capture, fresh)


def _bound_tools(tmp_path, *, tools, seatbelt=None, post_hooks=()):
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch
    from lionagi.tools.coding import CodingToolkit

    branch = Branch(
        chat_model=iModel(provider="openai", model="no-request", api_key="dummy-key-for-testing")
    )
    toolkit = CodingToolkit(
        notify=False,
        workspace_root=tmp_path,
        tools=tools,
        seatbelt=seatbelt,
        command_env_allowlist=("SOMEVENDOR_API_KEY",),
    )
    for name, handler in post_hooks:
        toolkit.post(name, handler)
    bound = toolkit.bind(branch, _command_env_names=frozenset({"GH_TOKEN"}))
    return toolkit, {tool.func_callable.__name__: tool for tool in bound}


@pytest.mark.parametrize("consumer", ["code_check", "ast_search"])
async def test_bound_static_commands_receive_the_selected_name_snapshot(
    monkeypatch, tmp_path, command_programs, consumer
):
    from lionagi import _command_env_metadata

    monkeypatch.setattr(_command_env_metadata, "configured_command_env_names", lambda: frozenset())
    _, capture = command_programs
    _, tools = _bound_tools(tmp_path, tools=(consumer,))
    arguments = {"paths": ["."]} if consumer == "code_check" else {"pattern": "$X"}
    response = await tools[consumer].func_callable(**arguments)
    assert response["status"] == "ok"
    _observed(capture, "first")


@pytest.mark.parametrize("action", ["write", "edit"])
@pytest.mark.parametrize("confined", [False, True], ids=["direct", "confined"])
async def test_bound_file_actions_use_empty_grants_only_for_a_confined_child(
    monkeypatch, tmp_path, action, confined
):
    from lionagi import _command_env_metadata
    from lionagi.tools import coding

    monkeypatch.setattr(_command_env_metadata, "configured_command_env_names", lambda: frozenset())
    monkeypatch.setenv("GH_TOKEN", "synthetic-selected-reference")
    monkeypatch.setenv("SOMEVENDOR_API_KEY", "synthetic-granted-command")
    seen = []

    class Writer:
        def write_text(self, path, content, timeout=30.0, *, _command_env=None):
            seen.append(dict(_command_env))
            Path(path).write_text(content)
            return None

    _, tools = _bound_tools(
        tmp_path, tools=("reader", "editor"), seatbelt=Writer() if confined else None
    )
    target = tmp_path / "catalog.txt"
    if action == "edit":
        target.write_text("old catalog")
        assert (await tools["reader"].func_callable(action="read", path=str(target)))["success"]

    if not confined:

        def no_environment_read(**kwargs):
            pytest.fail("a direct file action must not materialize a command environment")

        monkeypatch.setattr(coding, "command_environment", no_environment_read)
    arguments = (
        {"content": "new catalog"}
        if action == "write"
        else {
            "old_string": "old",
            "new_string": "new",
        }
    )
    response = await tools["editor"].func_callable(
        action=action, file_path=str(target), **arguments
    )
    assert response["success"]
    assert target.read_text() == "new catalog"
    if confined:
        assert len(seen) == 1
        assert "GH_TOKEN" not in seen[0] and "SOMEVENDOR_API_KEY" not in seen[0], (
            "fixed writer children must deny declared names and ignore command grants"
        )
    else:
        assert seen == []


async def test_the_builtin_post_hook_keeps_empty_arguments_without_launching_a_formatter(
    monkeypatch, tmp_path
):
    from lionagi.agent.hooks import auto_format_python
    from lionagi.ln import concurrency
    from lionagi.tools import coding

    observed = []

    def environment(**kwargs):
        observed.append(kwargs)
        return {"PUBLIC_TAG": "prepared"}

    monkeypatch.setattr(coding, "command_environment", environment)

    def no_formatter_launch(*args, **kwargs):
        pytest.fail("an empty-argument post hook must not launch a formatter")

    monkeypatch.setattr(concurrency, "run_sync", no_formatter_launch)
    _, tools = _bound_tools(tmp_path, tools=("bash",), post_hooks=(("bash", auto_format_python),))
    callback = tools["bash"].postprocessor
    assert callback is not None
    result = {"success": True, "file_path": "result-is-not-an-invocation.py"}
    assert await callback(result) == result
    assert observed == [
        {
            "declared_names": frozenset({"GH_TOKEN"}),
            "command_env_allowlist": ("SOMEVENDOR_API_KEY",),
        }
    ]


def test_command_context_stays_out_of_tool_request_schemas():
    from lionagi.tools.code.ast_search import AstSearchRequest
    from lionagi.tools.code.bash import BashRequest
    from lionagi.tools.code.check import CodeCheckRequest
    from lionagi.tools.code.search import SearchRequest
    from lionagi.tools.coding import SandboxRequest, SubagentRequest
    from lionagi.tools.file.editor import EditorRequest

    private = {
        "_command_env",
        "_command_env_names",
        "_command_env_allowlist",
        "command_env_allowlist",
    }
    for request in [
        BashRequest,
        SearchRequest,
        CodeCheckRequest,
        AstSearchRequest,
        SandboxRequest,
        SubagentRequest,
        EditorRequest,
    ]:
        assert private.isdisjoint(request.model_fields)
