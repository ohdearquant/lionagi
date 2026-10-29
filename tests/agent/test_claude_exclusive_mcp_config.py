# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Exclusive Claude MCP handoffs render the selected set, not an older file."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from lionagi.agent.factory import apply_forwarded_mcp_servers, create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
from lionagi.service.imodel import iModel


def _mcp_argument(args: list[str]) -> str:
    indices = [index for index, arg in enumerate(args) if arg == "--mcp-config"]
    assert len(indices) == 1
    return args[indices[0] + 1]


def _assert_drop_warning(caplog, *, inherited_file: bool, config_arg: str) -> None:
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory"
        and record.levelno >= logging.WARNING
        and "mcp_config" in record.getMessage()
    ]
    assert len(warnings) == (1 if inherited_file else 0)
    assert all(config_arg not in warning for warning in warnings)


@pytest.mark.parametrize(
    "selected", [{}, {"allowed": {"command": "never-run-fixture"}}], ids=["empty", "selected"]
)
@pytest.mark.parametrize("inherited_file", [False, True], ids=["no-config", "caller-config"])
def test_exclusive_forwarding_renders_selected_set(
    tmp_path, monkeypatch, caplog, selected, inherited_file
):
    monkeypatch.chdir(tmp_path)
    config_arg = "caller-mcp.json"
    (tmp_path / config_arg).write_text(
        json.dumps({"mcpServers": {"excluded": {"command": "never-run-fixture"}}})
    )
    kwargs = {"model": "sonnet"}
    if inherited_file:
        kwargs["mcp_config"] = config_arg
    with caplog.at_level(logging.WARNING, logger="lionagi.agent.factory"):
        assert (
            apply_forwarded_mcp_servers(kwargs, selected, provider="claude_code", exclusive=True)
            is True
        )
    assert "mcp_config" not in kwargs
    assert kwargs["mcp_servers"] == selected
    assert kwargs["strict_mcp_config"] is True
    assert kwargs["model"] == "sonnet"
    _assert_drop_warning(caplog, inherited_file=inherited_file, config_arg=config_arg)

    args = ClaudeCodeRequest(prompt="hello", repo=tmp_path, **kwargs).as_cmd_args()
    assert "--strict-mcp-config" in args
    assert json.loads(_mcp_argument(args)) == {"mcpServers": selected}


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited_file", [False, True], ids=["no-config", "caller-config"])
@pytest.mark.parametrize("resolved", [False, True], ids=["default", "resolved-empty"])
async def test_factory_empty_selection_preserves_caller_and_forwards_empty_set(
    tmp_path, monkeypatch, caplog, inherited_file, resolved
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_arg = "caller-mcp.json"
    (tmp_path / config_arg).write_text(
        json.dumps({"mcpServers": {"excluded": {"command": "never-run-fixture"}}})
    )
    caller = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    if inherited_file:
        caller.endpoint.config.kwargs["mcp_config"] = config_arg
    spec = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(tmp_path))
    spec.mcp_servers = []
    resolved_kwargs = (
        {"resolved_mcp_servers": {}, "resolved_mcp_explicit": True} if resolved else {}
    )
    with caplog.at_level(logging.WARNING, logger="lionagi.agent.factory"):
        branch = await create_agent(
            spec,
            load_settings=False,
            chat_model=caller,
            log_config={"auto_save_on_exit": False},
            **resolved_kwargs,
        )

    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hello"})
    request = payload["request"]
    assert isinstance(request, ClaudeCodeRequest)
    assert request.mcp_servers == {}
    assert request.strict_mcp_config is True
    assert json.loads(_mcp_argument(request.as_cmd_args())) == {"mcpServers": {}}
    assert caller.endpoint.config.kwargs.get("mcp_config") == (
        config_arg if inherited_file else None
    )
    assert "mcp_servers" not in caller.endpoint.config.kwargs
    assert "strict_mcp_config" not in caller.endpoint.config.kwargs
    _assert_drop_warning(caplog, inherited_file=inherited_file, config_arg=config_arg)


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited_file", [False, True], ids=["no-config", "caller-config"])
async def test_factory_named_allowlist_forwards_only_selected_servers(
    tmp_path, monkeypatch, caplog, inherited_file
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_arg = "caller-mcp.json"
    (tmp_path / config_arg).write_text(
        json.dumps({"mcpServers": {"excluded": {"command": "never-run-fixture"}}})
    )
    selected = {"allowed": {"command": "allowed-fixture"}}
    handed = {**selected, "excluded": {"command": "excluded-fixture"}}
    caller = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    if inherited_file:
        caller.endpoint.config.kwargs["mcp_config"] = config_arg
    spec = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(tmp_path))
    spec.mcp_servers = ["allowed"]
    with caplog.at_level(logging.WARNING, logger="lionagi.agent.factory"):
        branch = await create_agent(
            spec,
            load_settings=False,
            chat_model=caller,
            resolved_mcp_servers=handed,
            resolved_mcp_explicit=True,
            log_config={"auto_save_on_exit": False},
        )

    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hello"})
    request = payload["request"]
    assert isinstance(request, ClaudeCodeRequest)
    assert request.mcp_servers == selected
    assert request.strict_mcp_config is True
    assert json.loads(_mcp_argument(request.as_cmd_args())) == {"mcpServers": selected}
    assert caller.endpoint.config.kwargs.get("mcp_config") == (
        config_arg if inherited_file else None
    )
    assert "mcp_servers" not in caller.endpoint.config.kwargs
    assert "strict_mcp_config" not in caller.endpoint.config.kwargs
    _assert_drop_warning(caplog, inherited_file=inherited_file, config_arg=config_arg)


@pytest.mark.asyncio
async def test_ambient_factory_preserves_caller_config(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_arg = "caller-mcp.json"
    (tmp_path / config_arg).write_text(json.dumps({"mcpServers": {}}))
    caller = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    caller.endpoint.config.kwargs["mcp_config"] = config_arg
    spec = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(tmp_path))
    assert spec.mcp_servers is None
    with caplog.at_level(logging.WARNING, logger="lionagi.agent.factory"):
        branch = await create_agent(
            spec,
            load_settings=False,
            chat_model=caller,
            log_config={"auto_save_on_exit": False},
        )
    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hello"})
    request = payload["request"]
    assert _mcp_argument(request.as_cmd_args()) == config_arg
    assert request.strict_mcp_config is False
    assert caller.endpoint.config.kwargs["mcp_config"] == config_arg
    _assert_drop_warning(caplog, inherited_file=False, config_arg=config_arg)


def test_standalone_request_keeps_config_file_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_arg = "caller-mcp.json"
    (tmp_path / config_arg).write_text(json.dumps({"mcpServers": {}}))
    request = ClaudeCodeRequest(
        prompt="hello",
        repo=Path(tmp_path),
        mcp_config=config_arg,
        mcp_servers={},
        strict_mcp_config=True,
    )
    args = request.as_cmd_args()
    assert "--strict-mcp-config" in args
    assert _mcp_argument(args) == config_arg
