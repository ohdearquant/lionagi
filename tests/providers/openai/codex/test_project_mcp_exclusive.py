# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exclusive MCP selections cover Codex project configuration at launch."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import apply_forwarded_mcp_servers
from lionagi.providers.openai import _codex_project_layers as project_layers
from lionagi.providers.openai import codex as codex_mod
from lionagi.providers.openai.codex import CodexCodeRequest


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline test forbids process, signal, and network operations")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.keep]\ncommand = "keep-mcp"\n'
        '[mcp_servers.ambient]\ncommand = "ambient-mcp"\n',
        encoding="utf-8",
    )
    project = tmp_path / "outer" / "repo" / "work"
    project.mkdir(parents=True)
    git_dir = project.parent / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home)}
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    # Do not let a host-specific ancestor supply a project layer unnoticed.
    assert list(project_layers.project_layers(tmp_path, env)) == []
    calls = []

    async def fake_stream(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if False:
            yield None

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    # The synthetic repository has no linked Git worktrees to discover.
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", fake_stream)
    return project, env, calls


def _project_config(directory: Path, content: str | bytes) -> Path:
    parent = directory / ".codex"
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / "config.toml"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _request(project, env, *, servers=None, allowed_names=None, exclusive=True):
    if servers is None:
        servers = {"selected": {"command": "selected-mcp"}}
    kwargs = {}
    assert apply_forwarded_mcp_servers(
        kwargs,
        servers,
        provider="codex",
        exclusive=exclusive,
        allowed_names=allowed_names,
        env=env,
    )
    return CodexCodeRequest(prompt="synthetic", repo=project, env=env, **kwargs)


async def _run(request):
    return [item async for item in codex_mod._ndjson_from_cli(request)]


def _config_args(call):
    args = call[0]
    return [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-c"]


@pytest.mark.asyncio
async def test_new_project_server_is_disabled_on_each_launch_and_copy(fixture):
    project, env, calls = fixture
    request = _request(project, env)
    await _run(request)
    assert len(calls) == 1
    assert not any(arg.startswith("mcp_servers.project_only.") for arg in _config_args(calls[-1]))

    # This layer appears after the request was created; both it and a later change
    # must be inspected where the process would actually start.
    _project_config(project.parent, '[mcp_servers.project_only]\ncommand = "project-mcp"\n')
    await _run(request)
    assert "mcp_servers.project_only.enabled=false" in _config_args(calls[-1])
    assert any(arg.startswith("mcp_servers.selected.command=") for arg in _config_args(calls[-1]))

    _project_config(
        project.parent,
        '[mcp_servers.project_only]\ncommand = "project-mcp"\n'
        '[mcp_servers.later]\ncommand = "later-mcp"\n',
    )
    await _run(request.model_copy(deep=True))
    assert "mcp_servers.project_only.enabled=false" in _config_args(calls[-1])
    assert "mcp_servers.later.enabled=false" in _config_args(calls[-1])
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_selected_and_allowed_project_names_are_retained(fixture):
    project, env, calls = fixture
    _project_config(
        project.parent,
        '[mcp_servers.selected]\ncommand = "project-selected"\n'
        '[mcp_servers.permitted]\ncommand = "project-permitted"\n'
        '[mcp_servers.excluded]\ncommand = "project-excluded"\n',
    )
    request = _request(project, env, allowed_names=["permitted", "keep"])
    await _run(request)
    args = _config_args(calls[-1])
    assert any(arg.startswith("mcp_servers.selected.command=") for arg in args)
    assert "mcp_servers.excluded.enabled=false" in args
    assert "mcp_servers.selected.enabled=false" not in args
    assert "mcp_servers.permitted.enabled=false" not in args
    assert "mcp_servers.keep.enabled=false" not in args


@pytest.mark.asyncio
async def test_empty_described_set_with_user_allowlist_retains_project_allowlist(fixture):
    project, env, calls = fixture
    _project_config(
        project.parent,
        '[mcp_servers.permitted]\ncommand = "project-permitted"\n'
        '[mcp_servers.excluded]\ncommand = "project-excluded"\n',
    )
    request = _request(project, env, servers={}, allowed_names=["keep", "permitted"])
    assert not request.ignore_user_config
    await _run(request)
    args = _config_args(calls[-1])
    assert "mcp_servers.excluded.enabled=false" in args
    assert "mcp_servers.permitted.enabled=false" not in args
    assert "mcp_servers.keep.enabled=false" not in args


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["malformed", "invalid-utf8", "not-a-file"])
async def test_unreadable_or_unparseable_project_layer_refuses_before_spawn(fixture, bad):
    project, env, calls = fixture
    request = _request(project, env)
    path = _project_config(project.parent, "model = ")
    if bad == "invalid-utf8":
        path.write_bytes(b"\xff")
    elif bad == "not-a-file":
        path.unlink()
        path.mkdir()
    with pytest.raises(ConfigurationError) as refusal:
        await _run(request)
    assert str(path) in str(refusal.value)
    assert calls == []


@pytest.mark.asyncio
async def test_empty_exclusive_set_keeps_existing_ignore_user_config_guard(fixture):
    project, env, calls = fixture
    request = _request(project, env, servers={})
    assert request.ignore_user_config is True
    await _run(request)
    assert "--ignore-user-config" in calls[-1][0]
    path = _project_config(project.parent, '[mcp_servers.leak]\ncommand = "leak-mcp"\n')
    with pytest.raises(ConfigurationError) as refusal:
        await _run(request)
    assert str(path) in str(refusal.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_nonexclusive_request_is_unaffected_by_later_project_layer(fixture):
    project, env, calls = fixture
    request = _request(project, env, exclusive=False)
    await _run(request)
    before = calls[-1][0]
    _project_config(project.parent, '[mcp_servers.project_only]\ncommand = "project-mcp"\n')
    await _run(request)
    assert calls[-1][0] == before
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_outer_ancestor_project_layer_is_checked(fixture):
    project, env, calls = fixture
    request = _request(project, env)
    _project_config(project.parent.parent, '[mcp_servers.ancestor]\ncommand = "ancestor-mcp"\n')
    await _run(request)
    assert "mcp_servers.ancestor.enabled=false" in _config_args(calls[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["bad.name", "bad=name"])
async def test_unrepresentable_project_server_name_fails_closed(fixture, name):
    project, env, calls = fixture
    request = _request(project, env)
    path = _project_config(project.parent, f'[mcp_servers."{name}"]\ncommand = "bad-mcp"\n')
    with pytest.raises(ConfigurationError) as refusal:
        await _run(request)
    assert str(path) in str(refusal.value)
    assert calls == []
