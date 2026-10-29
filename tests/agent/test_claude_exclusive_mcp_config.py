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
    if inherited_file:
        assert config_arg in warnings[0]


def _caller_request(model: iModel) -> ClaudeCodeRequest:
    payload, _ = model.endpoint.create_payload({"prompt": "hello"})
    request = payload["request"]
    assert isinstance(request, ClaudeCodeRequest)
    return request


def _assert_same_request(before: ClaudeCodeRequest, after: ClaudeCodeRequest) -> None:
    assert after.model_dump() == before.model_dump()
    assert after.mcp_servers == before.mcp_servers
    assert after.as_cmd_args() == before.as_cmd_args()
    assert after.env == before.env
    assert after.on_spawn is before.on_spawn


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
    caller_before = _caller_request(caller)
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
    _assert_same_request(caller_before, _caller_request(caller))
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
    caller_before = _caller_request(caller)
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
    _assert_same_request(caller_before, _caller_request(caller))
    _assert_drop_warning(caplog, inherited_file=inherited_file, config_arg=config_arg)


@pytest.mark.asyncio
async def test_ambient_factory_preserves_caller_config(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_arg = "caller-mcp.json"
    (tmp_path / config_arg).write_text(json.dumps({"mcpServers": {}}))
    caller = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    caller.endpoint.config.kwargs["mcp_config"] = config_arg
    caller_before = _caller_request(caller)
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
    _assert_same_request(caller_before, _caller_request(caller))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("override_at", ["request", "kwargs"])
@pytest.mark.parametrize("override_kind", ["file", "servers"])
async def test_per_call_mcp_override_keeps_normal_request_precedence(
    tmp_path, monkeypatch, override_at, override_kind
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_arg = "per-call-mcp.json"
    selected = {"per-call": {"command": "never-run-fixture"}}
    (tmp_path / config_arg).write_text(json.dumps({"mcpServers": selected}))
    caller = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    caller_before = _caller_request(caller)
    spec = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(tmp_path))
    spec.mcp_servers = []
    branch = await create_agent(
        spec,
        load_settings=False,
        chat_model=caller,
        resolved_mcp_servers={},
        resolved_mcp_explicit=True,
        log_config={"auto_save_on_exit": False},
    )

    endpoint = branch.chat_model.endpoint
    initial, _ = endpoint.create_payload({"prompt": "hello"})
    assert initial["request"].strict_mcp_config is True
    assert json.loads(_mcp_argument(initial["request"].as_cmd_args())) == {"mcpServers": {}}

    key, value = (
        ("mcp_config", config_arg) if override_kind == "file" else ("mcp_servers", selected)
    )
    request = {"prompt": "hello"}
    kwargs = {}
    if override_at == "request":
        request[key] = value
    else:
        kwargs[key] = value
    payload, _ = endpoint.create_payload(request, **kwargs)
    overridden = payload["request"]
    assert overridden.strict_mcp_config is True
    rendered = _mcp_argument(overridden.as_cmd_args())
    if override_kind == "file":
        assert rendered == config_arg
    else:
        assert json.loads(rendered) == {"mcpServers": selected}

    _assert_same_request(caller_before, _caller_request(caller))
    after, _ = endpoint.create_payload({"prompt": "hello"})
    assert json.loads(_mcp_argument(after["request"].as_cmd_args())) == {"mcpServers": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["env", "on_spawn"])
async def test_factory_copy_preserves_caller_runtime_behavior(tmp_path, monkeypatch, field):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_arg = "caller-mcp.json"
    (tmp_path / config_arg).write_text(json.dumps({"mcpServers": {}}))
    caller = iModel(provider="claude_code", model="sonnet", api_key="dummy")

    def fake_spawn_callback(_process):
        raise AssertionError("no subprocess should be spawned")

    marker = {"FAKE_ENV": "fixture-value"} if field == "env" else fake_spawn_callback
    caller.endpoint.config.kwargs[field] = marker
    caller.endpoint.config.kwargs["mcp_config"] = config_arg
    before = _caller_request(caller)
    if field == "env":
        assert before.env == marker
    else:
        assert before.on_spawn is marker

    spec = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(tmp_path))
    spec.mcp_servers = []
    branch = await create_agent(
        spec,
        load_settings=False,
        chat_model=caller,
        resolved_mcp_servers={},
        resolved_mcp_explicit=True,
        log_config={"auto_save_on_exit": False},
    )

    assert branch.chat_model is not caller
    _assert_same_request(before, _caller_request(caller))
    forwarded = _caller_request(branch.chat_model)
    assert forwarded.strict_mcp_config is True
    assert json.loads(_mcp_argument(forwarded.as_cmd_args())) == {"mcpServers": {}}
    assert forwarded.env == before.env
    assert forwarded.on_spawn is before.on_spawn
