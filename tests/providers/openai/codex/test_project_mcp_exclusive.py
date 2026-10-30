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

from lionagi._errors import ConfigurationError, _ExclusiveMcpSelectionError
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
    with (codex_home / "config.toml").open("a", encoding="utf-8") as user_config:
        user_config.write(f'\n[projects."{project.parent}"]\ntrust_level = "trusted"\n')
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home)}
    monkeypatch.setattr(project_layers, "_SYSTEM_CONFIG", tmp_path / "system-config.toml")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    # Do not let a host-specific ancestor supply a project layer unnoticed.
    assert list(project_layers.project_layers(tmp_path, env)) == []
    calls = []

    async def fake_stream(cmd, **kwargs):
        prepare = kwargs.get("_prepare_argv")
        if prepare is not None:
            cmd = prepare(dict(kwargs.get("env") or os.environ))
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


@pytest.mark.asyncio
async def test_missing_cli_refuses_before_secret_fill_or_census(tmp_path, monkeypatch):
    import lionagi.providers._cli_subprocess as cli_sub

    seen = []

    async def fill(*args, **kwargs):
        seen.append("secret fill")
        raise AssertionError("secret fill must not run")

    def census(*args, **kwargs):
        seen.append("census")
        raise AssertionError("census must not run")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", None)
    monkeypatch.setattr(cli_sub, "fill_declared_secrets_and_names", fill)
    monkeypatch.setattr(codex_mod, "_exclusive_cmd_args", census)
    request = CodexCodeRequest(prompt="x", repo=tmp_path, exclusive_mcp_names=["selected"])
    with pytest.raises(RuntimeError, match="Codex CLI not found"):
        await _run(request)
    assert seen == []


def _config_args(call):
    args = call[0]
    return [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-c"]


@pytest.mark.parametrize("names", [["selected"], []])
def test_exclusive_request_refuses_public_argv_without_launch_check(fixture, names):
    project, env, _ = fixture
    request = CodexCodeRequest(prompt="synthetic", repo=project, env=env, exclusive_mcp_names=names)
    with pytest.raises(_ExclusiveMcpSelectionError, match="launch-time configuration check"):
        request.as_cmd_args()


def test_exclusive_public_argv_refuses_a_nonexclusive_supplied_snapshot(fixture):
    project, env, _ = fixture
    request = CodexCodeRequest(prompt="synthetic", repo=project, env=env, exclusive_mcp_names=[])
    other = CodexCodeRequest(prompt="synthetic", repo=project, env=env)
    with pytest.raises(_ExclusiveMcpSelectionError, match="launch-time configuration check"):
        request.as_cmd_args(plain=codex_mod._plain_request(other))


@pytest.mark.asyncio
async def test_exclusive_project_trust_override_has_selection_refusal_type(fixture):
    from lionagi._errors import _ExclusiveMcpSelectionError

    project, env, calls = fixture
    request = _request(project, env)
    request.config_overrides["project_root_markers"] = ["marker"]
    with pytest.raises(_ExclusiveMcpSelectionError, match="project trust or root markers"):
        await _run(request)
    assert not calls


def test_exclusive_names_field_is_sorted_unique_and_serializable(fixture):
    project, env, _ = fixture
    request = CodexCodeRequest(
        prompt="x", repo=project, env=env, exclusive_mcp_names=["z", "a", "z"]
    )
    assert request.exclusive_mcp_names == ["a", "z"]
    assert request.model_dump()["exclusive_mcp_names"] == ["a", "z"]
    with pytest.raises(TypeError, match="list of strings"):
        CodexCodeRequest(prompt="x", repo=project, env=env, exclusive_mcp_names=[1])


def test_endpoint_preserves_request_workspace_where_dev_dropped_it(fixture, monkeypatch):
    project, env, _ = fixture
    request = _request(project, env)
    endpoint = codex_mod.CodexCLIEndpoint()
    assert endpoint.create_payload(request)[0]["request"].cwd() == project
    monkeypatch.setattr(
        codex_mod.CodexCLIEndpoint,
        "_carried_runtime_state",
        codex_mod.AgenticHandlersMixin._carried_runtime_state,
    )
    assert endpoint.create_payload(request)[0]["request"].cwd() != project


def test_census_proven_loaded_and_allowed(fixture):
    project, env, _ = fixture
    _project_config(
        project.parent,
        '[mcp_servers.selected]\ncommand = "selected"\n[mcp_servers.extra]\ncommand = "extra"\n',
    )
    assert project_layers.project_mcp_server_names(project, env, {"selected"}) == {"extra"}


def test_census_skips_proven_untrusted(fixture):
    project, env, _ = fixture
    with (Path(env["CODEX_HOME"]) / "config.toml").open("a", encoding="utf-8") as config:
        config.write(f'\n[projects."{project}"]\ntrust_level = "untrusted"\n')
    _project_config(project, '[mcp_servers.extra]\ncommand = "extra"\n')
    assert project_layers.project_mcp_server_names(project, env, set()) == set()


def test_census_refuses_extra_above_project_root(fixture):
    project, env, _ = fixture
    path = _project_config(project.parent.parent, '[mcp_servers.extra]\ncommand = "extra"\n')
    with pytest.raises(ConfigurationError, match="above the proven project root") as error:
        project_layers.project_mcp_server_names(project, env, set())
    assert str(path) in str(error.value)
    assert "extra" in str(error.value)


def test_allowed_only_layer_above_root_needs_no_override(fixture):
    project, env, _ = fixture
    _project_config(project.parent.parent, '[mcp_servers.selected]\ncommand = "selected"\n')
    assert project_layers.project_mcp_server_names(project, env, {"selected"}) == set()


def test_census_refuses_unproven_linked_repository_trust(fixture):
    project, env, _ = fixture
    git = project.parent / ".git"
    git.rename(project.parent / "git-metadata")
    git.write_text("gitdir: ../git-metadata\n")
    config = Path(env["CODEX_HOME"]) / "config.toml"
    config.write_text('[mcp_servers.keep]\ncommand = "keep"\n')
    _project_config(project.parent, '[mcp_servers.extra]\ncommand = "extra"\n')
    with pytest.raises(ConfigurationError, match="repository trust cannot be determined"):
        project_layers.project_mcp_server_names(project, env, set())


def test_census_refuses_git_directory_without_head(fixture):
    project, env, _ = fixture
    fake_git = project / ".git"
    fake_git.mkdir()
    _project_config(project, '[mcp_servers.extra]\ncommand = "extra"\n')
    with (Path(env["CODEX_HOME"]) / "config.toml").open("a") as config:
        config.write(f'\n[projects."{project}"]\ntrust_level = "trusted"\n')
    with pytest.raises(ConfigurationError, match="has no HEAD") as error:
        project_layers.project_mcp_server_names(project, env, set())
    assert str(fake_git) in str(error.value)


def test_census_uses_canonical_trust_for_linked_workspace(fixture):
    project, env, _ = fixture
    link = project.parent.parent / "linked-repo"
    link.symlink_to(project.parent, target_is_directory=True)
    _project_config(project.parent, '[mcp_servers.extra]\ncommand = "extra"\n')
    assert project_layers.project_mcp_server_names(link / "work", env, set()) == {"extra"}


@pytest.mark.parametrize(
    "content", ["[mcp_servers]\nextra = 3\n", "[mcp_servers.extra]\nenabled = true\n"]
)
def test_census_refuses_unproven_server_definitions(fixture, content):
    project, env, _ = fixture
    path = _project_config(project.parent, content)
    with pytest.raises(ConfigurationError, match="definitions cannot be proven") as error:
        project_layers.project_mcp_server_names(project, env, set())
    assert str(path) in str(error.value)


def test_census_root_markers_change_the_verdict(fixture):
    project, env, _ = fixture
    (project / ".custom-root").mkdir()
    _project_config(project.parent, '[mcp_servers.extra]\ncommand = "extra"\n')
    assert project_layers.project_mcp_server_names(project, env, set()) == {"extra"}
    path = Path(env["CODEX_HOME"]) / "config.toml"
    path.write_text('project_root_markers = [".custom-root"]\n' + path.read_text())
    with pytest.raises(ConfigurationError, match="above the proven project root"):
        project_layers.project_mcp_server_names(project, env, set())


@pytest.mark.parametrize("content", ["bad =", b"\xff"])
def test_census_unreadable_project_is_refused(fixture, content):
    project, env, _ = fixture
    path = _project_config(project.parent, content)
    with pytest.raises(ConfigurationError) as error:
        project_layers.project_mcp_server_names(project, env, set())
    assert str(path) in str(error.value)


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
async def test_selected_project_fields_remain_available_to_codex_merge(fixture):
    project, env, calls = fixture
    config = _project_config(
        project.parent, '[mcp_servers.selected]\ncommand = "project"\nargs = ["project-arg"]\n'
    )
    await _run(_request(project, env))
    args = _config_args(calls[-1])
    assert 'mcp_servers.selected.command="selected-mcp"' in args
    assert not any(arg.startswith("mcp_servers.selected.args=") for arg in args)
    assert 'args = ["project-arg"]' in config.read_text()


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
async def test_empty_described_set_keeps_project_only_allowed_name(fixture):
    project, env, calls = fixture
    _project_config(project.parent, '[mcp_servers.permitted]\ncommand = "project-permitted"\n')
    request = _request(project, env, servers={}, allowed_names=["permitted"])
    assert not request.ignore_user_config
    assert request.exclusive_mcp_names == ["permitted"]
    await _run(request)
    args = _config_args(calls[-1])
    assert "mcp_servers.permitted.enabled=false" not in args
    assert "mcp_servers.ambient.enabled=false" in args


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", ["keep", "absent"])
async def test_empty_described_set_keeps_user_or_absent_allowlist(fixture, allowed):
    project, env, calls = fixture
    request = _request(project, env, servers={}, allowed_names=[allowed])
    assert not request.ignore_user_config
    assert request.exclusive_mcp_names == [allowed]
    await _run(request)
    args = _config_args(calls[-1])
    assert f"mcp_servers.{allowed}.enabled=false" not in args
    assert "mcp_servers.ambient.enabled=false" in args
    if allowed == "absent":
        assert not any(arg.startswith("mcp_servers.absent.") for arg in args)


@pytest.mark.asyncio
async def test_explicit_empty_allowlist_takes_ignore_user_shortcut(fixture):
    project, env, calls = fixture
    request = _request(project, env, servers={}, allowed_names=[])
    assert request.ignore_user_config
    assert request.exclusive_mcp_names == []
    await _run(request)
    assert "--ignore-user-config" in calls[-1][0]


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
    assert request.exclusive_mcp_names == []
    await _run(request)
    assert "--ignore-user-config" in calls[-1][0]
    path = _project_config(project.parent, '[mcp_servers.leak]\ncommand = "leak-mcp"\n')
    with pytest.raises(ConfigurationError) as refusal:
        await _run(request)
    assert str(path) in str(refusal.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_exclusive_renders_system_disable_override(fixture):
    """Check argv only; no Codex process starts here.

    Codex rust-v0.162.1 loads the system layer even when exec ignores user config:
    https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/loader/mod.rs#L256-L274
    https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/loader/mod.rs#L485-L501
    """
    project, env, calls = fixture
    project_layers._SYSTEM_CONFIG.write_text('[mcp_servers.system]\ncommand = "system"\n')
    request = _request(project, env, servers={})
    await _run(request)
    assert "mcp_servers.system.enabled=false" in _config_args(calls[-1])


@pytest.mark.asyncio
async def test_empty_exclusive_refuses_system_root_profile(fixture):
    project, env, calls = fixture
    project_layers._SYSTEM_CONFIG.write_text('profile = "late"\n')
    request = _request(project, env, servers={})
    with pytest.raises(ConfigurationError, match="root profile"):
        await _run(request)
    assert not calls


@pytest.mark.asyncio
async def test_ignore_user_without_exclusive_carrier_leaves_system_unchanged(fixture):
    project, env, calls = fixture
    project_layers._SYSTEM_CONFIG.write_text('[mcp_servers.system]\ncommand = "system"\n')
    request = CodexCodeRequest(prompt="x", repo=project, env=env, ignore_user_config=True)
    await _run(request)
    assert "mcp_servers.system.enabled=false" not in _config_args(calls[-1])


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
    with pytest.raises(ConfigurationError, match="above the proven project root"):
        await _run(request)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["projects", "projects./tmp.trust_level", "project_root_markers"])
async def test_trust_and_root_overrides_refused_before_launch(fixture, key):
    project, env, calls = fixture
    request = _request(project, env)
    request.config_overrides[key] = "untrusted"
    with pytest.raises(ConfigurationError, match="project trust or root markers"):
        await _run(request)
    assert not calls


@pytest.mark.asyncio
async def test_trust_is_rechecked_for_each_launch(fixture):
    project, env, calls = fixture
    path = _project_config(project.parent, '[mcp_servers.extra]\ncommand = "extra"\n')
    request = _request(project, env)
    await _run(request)
    assert "mcp_servers.extra.enabled=false" in _config_args(calls[-1])
    config = Path(env["CODEX_HOME"]) / "config.toml"
    config.write_text(
        config.read_text().replace('trust_level = "trusted"', 'trust_level = "untrusted"')
    )
    await _run(request.model_copy(deep=True))
    assert "mcp_servers.extra.enabled=false" not in _config_args(calls[-1])
    assert path.exists()


@pytest.mark.asyncio
async def test_user_config_names_are_rechecked_on_each_launch(fixture):
    project, env, calls = fixture
    request = _request(project, env, allowed_names=["keep"])
    await _run(request)
    assert "mcp_servers.ambient.enabled=false" in _config_args(calls[-1])
    config = Path(env["CODEX_HOME"]) / "config.toml"
    text = config.read_text().replace(
        '[mcp_servers.ambient]\ncommand = "ambient-mcp"\n',
        '[mcp_servers.late]\ncommand = "late"\n',
    )
    config.write_text(text)
    await _run(request.model_copy(deep=True))
    args = _config_args(calls[-1])
    assert "mcp_servers.late.enabled=false" in args
    assert "mcp_servers.ambient.enabled=false" not in args


@pytest.mark.asyncio
async def test_system_config_server_is_disabled_at_launch(fixture):
    project, env, calls = fixture
    project_layers._SYSTEM_CONFIG.write_text('[mcp_servers.system]\ncommand = "system"\n')
    await _run(_request(project, env))
    assert "mcp_servers.system.enabled=false" in _config_args(calls[-1])


@pytest.mark.asyncio
async def test_system_profile_table_is_checked(fixture):
    project, env, calls = fixture
    project_layers._SYSTEM_CONFIG.write_text(
        '[profiles.custom.mcp_servers.leak]\ncommand = "secret"\n'
    )
    request = _request(project, env).model_copy(update={"profile": "custom"})
    with pytest.raises(ConfigurationError):
        await _run(request)
    assert not calls


@pytest.mark.asyncio
async def test_system_root_profile_is_refused(fixture):
    project, env, calls = fixture
    request = _request(project, env)
    path = project_layers._SYSTEM_CONFIG
    path.write_text('profile = "late"\n')
    with pytest.raises(ConfigurationError, match="root profile") as error:
        await _run(request)
    assert str(path) in str(error.value)
    assert not calls


@pytest.mark.asyncio
async def test_census_and_argv_use_one_cwd_snapshot(fixture, monkeypatch):
    project, env, calls = fixture
    _project_config(project.parent, '[mcp_servers.extra]\ncommand = "extra"\n')
    request = _request(project, env)
    observed = []

    def changing_cwd(_request):
        observed.append(None)
        return project if len(observed) == 1 else project.parent.parent

    monkeypatch.setattr(CodexCodeRequest, "cwd", changing_cwd)
    await _run(request)
    args = calls[-1][0]
    assert args[args.index("-C") + 1] == str(project)
    assert "mcp_servers.extra.enabled=false" in _config_args(calls[-1])
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "project"])
async def test_root_profile_written_after_request_refuses_before_launch(fixture, source):
    project, env, calls = fixture
    request = _request(project, env)
    if source == "user":
        path = Path(env["CODEX_HOME"]) / "config.toml"
        path.write_text('profile = "late"\n' + path.read_text())
    else:
        path = _project_config(project.parent, 'profile = "late"\n')
    with pytest.raises(ConfigurationError, match="root profile") as error:
        await _run(request)
    assert str(path) in str(error.value)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["mcp_servers", "projects", "project_root_markers"])
async def test_selected_profile_with_policy_keys_refuses(fixture, unsafe):
    project, env, calls = fixture
    request = _request(project, env).model_copy(update={"profile": "custom"})
    path = Path(env["CODEX_HOME"]) / "custom.config.toml"
    path.write_text(
        f"{unsafe} = {{}}\n" if unsafe != "project_root_markers" else f"{unsafe} = []\n"
    )
    with pytest.raises(ConfigurationError) as error:
        await _run(request)
    assert str(path) in str(error.value)
    assert not calls


@pytest.mark.asyncio
async def test_selected_profile_without_policy_keys_is_allowed(fixture):
    project, env, calls = fixture
    request = _request(project, env).model_copy(update={"profile": "custom"})
    (Path(env["CODEX_HOME"]) / "custom.config.toml").write_text('model = "example"\n')
    await _run(request)
    assert "-p" in calls[-1][0]


@pytest.mark.asyncio
async def test_profile_selected_by_override_is_checked_at_launch(fixture):
    project, env, calls = fixture
    request = _request(project, env)
    request.config_overrides["profile"] = "custom"
    path = Path(env["CODEX_HOME"]) / "custom.config.toml"
    path.write_text("project_root_markers = []\n")
    with pytest.raises(ConfigurationError) as error:
        await _run(request)
    assert str(path) in str(error.value)
    assert not calls


@pytest.mark.asyncio
async def test_generated_profile_rechecked_after_request(fixture):
    project, env, calls = fixture
    name = "lionagi-mcp-" + "a" * 32
    path = Path(env["CODEX_HOME"]) / f"{name}.config.toml"
    path.write_text('[mcp_servers.selected.env]\nTOKEN = "secret"\n')
    request = _request(project, env).model_copy(update={"profile": name})
    await _run(request)
    path.write_text('[mcp_servers.extra.env]\nTOKEN = "secret"\n')
    with pytest.raises(ConfigurationError, match="outside the selected set") as error:
        await _run(request)
    assert str(path) in str(error.value)
    assert "secret" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "ancestor"])
async def test_generated_profile_same_name_legacy_table_refuses(fixture, source):
    project, env, calls = fixture
    name = "lionagi-mcp-" + "a" * 32
    (Path(env["CODEX_HOME"]) / f"{name}.config.toml").write_text(
        '[mcp_servers.selected.env]\nTOKEN = "secret"\n'
    )
    if source == "user":
        config = Path(env["CODEX_HOME"]) / "config.toml"
        with config.open("a") as file:
            file.write(f'\n[profiles.{name}]\nmodel = "x"\n')
    else:
        _project_config(project.parent.parent, f'[profiles.{name}]\nmodel = "x"\n')
    request = _request(project, env).model_copy(update={"profile": name})
    with pytest.raises(ConfigurationError, match="defined"):
        await _run(request)
    assert not calls


@pytest.mark.asyncio
async def test_generated_profile_same_name_override_refuses(fixture):
    project, env, calls = fixture
    name = "lionagi-mcp-" + "a" * 32
    (Path(env["CODEX_HOME"]) / f"{name}.config.toml").write_text(
        '[mcp_servers.selected.env]\nTOKEN = "secret"\n'
    )
    request = _request(project, env).model_copy(update={"profile": name})
    request.config_overrides[f"profiles.{name}.model"] = "example"
    with pytest.raises(ConfigurationError, match="selected profile override"):
        await _run(request)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "projects = {}\n",
        "project_root_markers = []\n",
        'model = "leak-never"\n',
        '[mcp_servers.selected]\ncommand = "leak-never"\n',
    ],
)
async def test_generated_profile_forbids_other_keys(fixture, text):
    project, env, calls = fixture
    name = "lionagi-mcp-" + "a" * 32
    path = Path(env["CODEX_HOME"]) / f"{name}.config.toml"
    path.write_text(text)
    request = _request(project, env).model_copy(update={"profile": name})
    with pytest.raises(ConfigurationError) as error:
        await _run(request)
    assert str(path) in str(error.value)
    assert "leak-never" not in str(error.value)
    assert not calls


@pytest.mark.asyncio
async def test_generated_profile_missing_refuses(fixture):
    project, env, calls = fixture
    name = "lionagi-mcp-" + "a" * 32
    request = _request(project, env).model_copy(update={"profile": name})
    with pytest.raises(ConfigurationError, match="cannot be read or parsed"):
        await _run(request)
    assert not calls


@pytest.mark.asyncio
async def test_factory_written_generated_secret_profile_passes(fixture):
    from lionagi.agent.factory import _write_codex_mcp_secret_profile

    project, env, calls = fixture
    kwargs = {}
    _write_codex_mcp_secret_profile(
        kwargs, {"selected": {"env": {"TOKEN": "secret"}, "http_headers": {"X": "secret"}}}
    )
    request = _request(project, env).model_copy(update={"profile": kwargs["profile"]})
    await _run(request)
    assert "-p" in calls[-1][0]


@pytest.mark.asyncio
async def test_selected_profile_legacy_policy_table_refuses(fixture):
    project, env, calls = fixture
    config = Path(env["CODEX_HOME"]) / "config.toml"
    with config.open("a") as file:
        file.write('\n[profiles.custom.mcp_servers.leak]\ncommand = "secret"\n')
    request = _request(project, env).model_copy(update={"profile": "custom"})
    with pytest.raises(ConfigurationError) as error:
        await _run(request)
    assert str(config) in str(error.value)
    assert "secret" not in str(error.value)
    assert not calls


@pytest.mark.asyncio
async def test_nonexclusive_request_does_not_apply_profile_check(fixture):
    project, env, calls = fixture
    request = _request(project, env, exclusive=False)
    path = Path(env["CODEX_HOME"]) / "config.toml"
    path.write_text('profile = "late"\n' + path.read_text())
    await _run(request)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_request_override_cannot_reenable_unselected_server(fixture):
    project, env, calls = fixture
    _project_config(project.parent, '[mcp_servers.extra]\ncommand = "extra"\n')
    request = _request(project, env)
    request.config_overrides["mcp_servers.extra.enabled"] = True
    with pytest.raises(_ExclusiveMcpSelectionError, match="unselected"):
        await _run(request)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["mapping", "request"])
@pytest.mark.parametrize("ignore_user_config", [False, True])
@pytest.mark.parametrize(
    "overrides",
    [
        {"mcp_servers.extra.enabled": True},
        {"mcp_servers": {"extra": {"command": "other"}}},
        {'mcp_servers."extra".enabled': True},
        {'mcp_servers."ex\\u0074ra".enabled': True},
    ],
)
async def test_request_selection_refuses_unselected_mcp_override(
    fixture, shape, overrides, ignore_user_config
):
    project, env, calls = fixture
    values = {
        "prompt": "synthetic",
        "repo": project,
        "env": env,
        "exclusive_mcp_names": ["allowed"],
        "config_overrides": overrides,
        "ignore_user_config": ignore_user_config,
    }
    request = values if shape == "mapping" else CodexCodeRequest(**values)
    payload, _ = codex_mod.CodexCLIEndpoint().create_payload(request)
    with pytest.raises(_ExclusiveMcpSelectionError, match="unselected"):
        await _run(payload["request"])
    assert calls == []


@pytest.mark.asyncio
async def test_request_selection_allows_selected_mcp_override(fixture):
    project, env, calls = fixture
    request = CodexCodeRequest(
        prompt="synthetic",
        repo=project,
        env=env,
        exclusive_mcp_names=["allowed"],
        config_overrides={"mcp_servers.allowed.command": "allowed-mcp"},
    )
    await _run(request)
    args = _config_args(calls[-1])
    assert 'mcp_servers.allowed.command="allowed-mcp"' in args
    assert "mcp_servers.allowed.enabled=false" not in args


@pytest.mark.asyncio
async def test_request_selection_allows_selected_whole_table_and_other_overrides(fixture):
    project, env, calls = fixture
    request = CodexCodeRequest(
        prompt="synthetic",
        repo=project,
        env=env,
        exclusive_mcp_names=["allowed"],
        config_overrides={
            "mcp_servers": {"allowed": {"command": "allowed-mcp"}},
            "features.test": True,
        },
    )
    await _run(request)
    args = _config_args(calls[-1])
    assert any(arg.startswith("mcp_servers=") and "allowed-mcp" in arg for arg in args)
    assert "features.test=true" in args


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["bad.name", "bad=name", " bad", "bad ", "bad\\u0000name"])
async def test_unrepresentable_project_server_name_fails_closed(fixture, name):
    project, env, calls = fixture
    request = _request(project, env)
    path = _project_config(project.parent, f'[mcp_servers."{name}"]\ncommand = "bad-mcp"\n')
    with pytest.raises(ConfigurationError) as refusal:
        await _run(request)
    assert str(path) in str(refusal.value)
    assert calls == []


@pytest.mark.asyncio
async def test_endpoint_per_call_selection_replaces_configured_names(fixture):
    project, env, calls = fixture
    _project_config(
        project.parent,
        '[mcp_servers.selected]\ncommand = "old"\n'
        '[mcp_servers.new]\ncommand = "new"\n'
        '[mcp_servers.extra]\ncommand = "extra"\n',
    )
    endpoint = codex_mod.CodexCLIEndpoint()
    request = _request(project, env)
    payload, _ = endpoint.create_payload(
        request, config_overrides={"mcp_servers.new.command": "per-call"}
    )
    call = payload["request"]
    assert call.cwd() == project
    await _run(call)
    args = _config_args(calls[-1])
    assert "mcp_servers.selected.enabled=false" in args
    assert "mcp_servers.extra.enabled=false" in args
    assert "mcp_servers.new.enabled=false" not in args
    assert 'mcp_servers.new.command="per-call"' in args


@pytest.mark.asyncio
async def test_endpoint_kwargs_override_request_selection(fixture):
    project, env, calls = fixture
    _project_config(
        project.parent,
        '[mcp_servers.requested]\ncommand = "old"\n[mcp_servers.called]\ncommand = "new"\n',
    )
    endpoint = codex_mod.CodexCLIEndpoint(
        exclusive_mcp_names=["configured"],
        config_overrides={"mcp_servers.configured.command": "configured"},
    )
    request = {
        "prompt": "synthetic",
        "repo": project,
        "env": env,
        "config_overrides": {"mcp_servers.requested.command": "requested"},
    }
    payload, _ = endpoint.create_payload(
        request, config_overrides={"mcp_servers.called.command": "called"}
    )
    await _run(payload["request"])
    args = _config_args(calls[-1])
    assert "mcp_servers.requested.enabled=false" in args
    assert "mcp_servers.called.enabled=false" not in args
    assert 'mcp_servers.called.command="called"' in args


@pytest.mark.asyncio
async def test_endpoint_request_override_beats_configured_selection(fixture):
    project, env, calls = fixture
    _project_config(project.parent, '[mcp_servers.configured]\ncommand = "configured"\n')
    endpoint = codex_mod.CodexCLIEndpoint(
        exclusive_mcp_names=["configured"],
        config_overrides={"mcp_servers.configured.command": "configured"},
    )
    payload, _ = endpoint.create_payload(
        {
            "prompt": "synthetic",
            "repo": project,
            "env": env,
            "config_overrides": {"mcp_servers.requested.command": "requested"},
        }
    )
    await _run(payload["request"])
    args = _config_args(calls[-1])
    assert "mcp_servers.configured.enabled=false" in args
    assert 'mcp_servers.requested.command="requested"' in args


@pytest.mark.asyncio
async def test_endpoint_empty_mcp_override_uses_empty_selection_guard(fixture):
    project, env, calls = fixture
    endpoint = codex_mod.CodexCLIEndpoint()
    request = _request(project, env)
    payload, _ = endpoint.create_payload(request, config_overrides={"mcp_servers": {}})
    call = payload["request"]
    assert call.ignore_user_config
    assert call.exclusive_mcp_names == []
    await _run(call)
    assert "--ignore-user-config" in calls[-1][0]


@pytest.mark.asyncio
async def test_factory_model_roundtrip_checks_project_layer_added_later(fixture):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.spec import AgentSpec
    from lionagi.service.imodel import iModel

    project, env, calls = fixture
    spec = AgentSpec.compose("reviewer", model="codex/gpt-5.5", cwd=str(project))
    spec.mcp_servers = ["selected"]
    branch = await create_agent(
        spec,
        load_settings=False,
        resolved_mcp_servers={"selected": {"command": "selected-mcp"}},
        resolved_mcp_explicit=True,
        log_config={"auto_save_on_exit": False},
    )
    restored = iModel.from_dict(branch.chat_model.to_dict())
    assert restored.endpoint.config.kwargs["exclusive_mcp_names"] == ["selected"]
    _project_config(project.parent, '[mcp_servers.late]\ncommand = "late"\n')
    payload, _ = restored.endpoint.create_payload({"prompt": "x", "repo": project, "env": env})
    await _run(payload["request"])
    assert "mcp_servers.late.enabled=false" in _config_args(calls[-1])
