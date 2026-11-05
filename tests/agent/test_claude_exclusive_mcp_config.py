# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Exclusive Claude MCP handoffs render the selected set, not an older file."""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

import pytest

from lionagi.agent.factory import apply_forwarded_mcp_servers, create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.service.imodel import iModel
from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest


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
        and record.getMessage().startswith("Dropping mcp_config ")
    ]
    assert len(warnings) == (1 if inherited_file else 0)
    if inherited_file:
        assert config_arg in warnings[0]


@pytest.mark.parametrize("as_path", [False, True], ids=["string", "pathlike"])
def test_dropped_config_path_is_named_once(caplog, as_path):
    path = Path("agent-config.json") if as_path else "agent-config.json"
    kwargs = {"mcp_config": path}

    with caplog.at_level(logging.WARNING):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    assert kwargs["mcp_servers"] == {}
    assert kwargs["strict_mcp_config"] is True
    _assert_drop_warning(caplog, inherited_file=True, config_arg=str(path))


@pytest.mark.parametrize("prefix", ["", " \n\t"], ids=["plain", "whitespace"])
@pytest.mark.parametrize("malformed", [False, True], ids=["valid", "incomplete"])
def test_dropped_inline_config_never_logs_its_contents(caplog, prefix, malformed):
    marker = "fixture-value-do-not-log"
    inline = json.dumps({"mcpServers": {"demo": {"env": {"TOKEN": marker}}}})
    if malformed:
        inline = inline[:-1]
    kwargs = {"mcp_config": prefix + inline}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    assert kwargs["mcp_servers"] == {}
    assert kwargs["strict_mcp_config"] is True
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory"
        and record.levelno >= logging.WARNING
        and record.getMessage().startswith("Dropping mcp_config ")
    ]
    assert len(warnings) == 1
    assert "inline" in warnings[0].lower() and "json" in warnings[0].lower()
    assert all(marker not in record.getMessage() for record in caplog.records)
    assert marker not in caplog.text


@pytest.mark.parametrize(
    "inline",
    [json.dumps("fixture-value-do-not-log"), "null", "true", "123", "1.25"],
    ids=["string", "null", "boolean", "integer", "decimal"],
)
def test_dropped_json_scalar_is_not_logged_as_a_path(caplog, inline):
    kwargs = {"mcp_config": inline}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory"
        and record.levelno >= logging.WARNING
        and record.getMessage().startswith("Dropping mcp_config ")
    ]
    assert len(warnings) == 1
    assert "inline" in warnings[0].lower() and "json" in warnings[0].lower()
    assert all(inline not in record.getMessage() for record in caplog.records)
    assert "fixture-value-do-not-log" not in caplog.text


def test_incomplete_quoted_config_is_not_logged_as_a_path(caplog):
    marker = "fixture-value-do-not-log"
    kwargs = {"mcp_config": ' \n"' + marker}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory"
        and record.levelno >= logging.WARNING
        and record.getMessage().startswith("Dropping mcp_config ")
    ]
    assert len(warnings) == 1
    assert "inline" in warnings[0].lower() and "json" in warnings[0].lower()
    assert all(marker not in record.getMessage() for record in caplog.records)
    assert marker not in caplog.text


def test_bom_prefixed_inline_config_is_identified_without_logging_content(caplog):
    marker = "fixture-value-do-not-log"
    inline = "\ufeff \ufeff\t" + json.dumps({"mcpServers": {"demo": {"token": marker}}})
    kwargs = {"mcp_config": inline}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory" and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "inline JSON config" in warnings[0]
    assert marker not in caplog.text
    assert "mcp_config" not in kwargs


def test_bytes_path_with_surrogate_is_not_logged_or_unencodable(caplog):
    class BytesPath(os.PathLike):
        def __fspath__(self):
            return b"agent-\xff.json"

    path = BytesPath()
    assert "\udcff" in os.fsdecode(path)
    kwargs = {"mcp_config": path}
    output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    handler = logging.StreamHandler(output)
    errors = []
    handler.handleError = errors.append
    logger = logging.getLogger("lionagi.agent.factory")
    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG):
            assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)
        handler.flush()
        assert not errors
        assert "non-path config" in output.buffer.getvalue().decode("utf-8")
    finally:
        logger.removeHandler(handler)
        handler.close()
        output.close()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory" and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "non-path config" in warnings[0]
    assert "\udcff" not in warnings[0]
    assert "agent-" not in warnings[0]
    assert "mcp_config" not in kwargs


@pytest.mark.parametrize(
    "prefix",
    ["\v", "\f", "\x00", "\u0085", "\u00a0", "\u2028", "\u200b", "\ufeff"],
    ids=[
        "vertical-tab",
        "form-feed",
        "nul",
        "next-line",
        "nbsp",
        "line-separator",
        "zero-width",
        "bom",
    ],
)
def test_control_prefixed_inline_config_cannot_inject_log_content(caplog, prefix):
    marker = "fixture-value-do-not-log"
    inline = prefix + json.dumps({"mcpServers": {"demo": {"env": {"TOKEN": marker}}}})
    kwargs = {"mcp_config": inline}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory"
        and record.levelno >= logging.WARNING
        and record.getMessage().startswith("Dropping mcp_config ")
    ]
    assert len(warnings) == 1
    assert "inline" in warnings[0].lower() or "non-path" in warnings[0].lower()
    assert all(marker not in record.getMessage() for record in caplog.records)
    assert marker not in caplog.text
    assert len(caplog.text.splitlines()) == 1


@pytest.mark.parametrize(
    "separator",
    ["\v", "\f", "\x00", "\u0085", "\u00a0", "\u2028", "\u2029", "\u200b", "\ufeff", "\u2007"],
    ids=[
        "vertical-tab",
        "form-feed",
        "nul",
        "next-line",
        "nbsp",
        "line",
        "paragraph",
        "zero-width",
        "bom",
        "figure-space",
    ],
)
def test_unprintable_or_unsafe_separator_in_path_is_not_logged(caplog, separator):
    marker = "fixture-value-do-not-log"
    kwargs = {"mcp_config": "agent" + separator + marker + ".json"}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory"
        and record.levelno >= logging.WARNING
        and record.getMessage().startswith("Dropping mcp_config ")
    ]
    assert len(warnings) == 1
    assert "non-path" in warnings[0].lower()
    assert all(marker not in record.getMessage() for record in caplog.records)
    assert marker not in caplog.text
    assert len(caplog.text.splitlines()) == 1


@pytest.mark.parametrize("as_path", [False, True], ids=["string", "pathlike"])
def test_line_break_in_config_path_cannot_forge_a_log_record(caplog, as_path):
    marker = "fixture-forged-record"
    path = "ordinary.json\n" + marker
    kwargs = {"mcp_config": Path(path) if as_path else path}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.agent.factory"
        and record.levelno >= logging.WARNING
        and record.getMessage().startswith("Dropping mcp_config ")
    ]
    assert len(warnings) == 1
    assert "non-path" in warnings[0].lower()
    assert all(marker not in record.getMessage() for record in caplog.records)
    assert marker not in caplog.text
    assert len(caplog.text.splitlines()) == 1


@pytest.mark.parametrize("as_path", [False, True], ids=["string", "pathlike"])
def test_printable_unicode_config_path_is_named(caplog, as_path):
    path = "café/服务器 配置.json"
    kwargs = {"mcp_config": Path(path) if as_path else path}

    with caplog.at_level(logging.DEBUG):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    _assert_drop_warning(caplog, inherited_file=True, config_arg=path)


def test_none_config_is_removed_without_warning(caplog):
    kwargs = {"mcp_config": None}

    with caplog.at_level(logging.WARNING):
        assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)

    assert "mcp_config" not in kwargs
    assert kwargs["mcp_servers"] == {}
    assert kwargs["strict_mcp_config"] is True
    _assert_drop_warning(caplog, inherited_file=False, config_arg="unused.json")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False], ids=["named-file", "ambient"])
async def test_factory_caller_resolved_set_is_exclusive_only_when_named(
    tmp_path, monkeypatch, explicit
):
    """A set read from a file the caller named bounds the Claude child; an ambient one does not."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    selected = {"selected": {"command": "never-run-fixture"}}
    caller = iModel(provider="claude_code", model="sonnet", api_key="dummy")
    spec = AgentSpec.compose("reviewer", model="claude_code/sonnet", cwd=str(tmp_path))
    assert spec.mcp_servers is None
    branch = await create_agent(
        spec,
        load_settings=False,
        chat_model=caller,
        resolved_mcp_servers=selected,
        resolved_mcp_explicit=explicit,
        log_config={"auto_save_on_exit": False},
    )

    request = _caller_request(branch.chat_model)
    assert request.mcp_servers == selected
    assert request.strict_mcp_config is explicit
    args = request.as_cmd_args()
    assert ("--strict-mcp-config" in args) is explicit
    assert json.loads(_mcp_argument(args)) == {"mcpServers": selected}
