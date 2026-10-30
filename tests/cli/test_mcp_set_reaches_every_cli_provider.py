# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A resolved MCP server set reaches every CLI provider that has a transport for one.

Three spawn shapes build a CLI request — a plain leg, a resumed leg, and a
flow/fanout worker — and each used to decide for itself whether the set could be
carried, by asking whether the provider was the Claude CLI. codex carries a set
too, over config overrides, so a caller who named one got it on one path and
silently lost it on three. These tests are per spawn shape rather than per
provider, because the shape is what differed.

The secret-field checks are the reason each shape is checked separately: `env`
and `http_headers` can hold API keys, and a path that forwards them as `-c`
overrides puts them in `ps` output and in every serialized request record.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import apply_forwarded_mcp_servers

CODEX_SPEC = "codex/gpt-5.3-codex"

SERVERS = {"khive": {"command": "kkernel", "args": ["serve"]}}
SECRET = "sk-live-do-not-put-me-on-argv"
SECRET_SERVERS = {"khive": {"command": "kkernel", "env": {"KHIVE_API_KEY": SECRET}}}

WARN_LOGGER = "lionagi.cli.warn"


async def _cmd_args(imodel) -> list[str]:
    """The argv the CLI child would be spawned with, off the live request."""
    api_call = await imodel.create_event(prompt="hi")
    return api_call.payload["request"].as_cmd_args()


@pytest.fixture
def codex_launch(monkeypatch, tmp_path):
    """Render the pre-spawn argv using the same final environment as the child."""
    import asyncio
    import socket
    import subprocess

    from lionagi.providers.openai import _codex_project_layers, codex

    async def launch(request):
        calls = []

        def forbid(*_args, **_kwargs):
            raise AssertionError("launch capture forbids processes, signals and network")

        async def fake_stream(cmd, **kwargs):
            prepare = kwargs.get("_prepare_argv")
            assert prepare is not None
            cmd = prepare(dict(kwargs.get("env") or os.environ))
            calls.append(list(cmd))
            if False:
                yield None

        with monkeypatch.context() as patch:
            patch.chdir(tmp_path)
            patch.setattr(_codex_project_layers, "_SYSTEM_CONFIG", tmp_path / "system.toml")
            patch.setattr(codex, "CODEX_CLI", "codex")
            patch.setattr(codex, "_linked_worktree_git_writable_roots", lambda _cwd: ())
            patch.setattr(codex, "ndjson_from_cli", fake_stream)
            for name in ("Popen", "run", "call", "check_call", "check_output"):
                patch.setattr(subprocess, name, forbid)
            for name in ("create_subprocess_exec", "create_subprocess_shell"):
                patch.setattr(asyncio, name, forbid)
            for name in ("kill", "killpg", "system"):
                patch.setattr(os, name, forbid, raising=False)
            for name in ("connect", "connect_ex", "sendto"):
                patch.setattr(socket.socket, name, forbid)
            patch.setattr(socket, "create_connection", forbid)
            patch.setattr(socket, "getaddrinfo", forbid)
            assert [item async for item in codex._ndjson_from_cli(request)] == []
        assert len(calls) == 1
        args = calls[0]
        return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-c"]

    return launch


def _assert_factory_selection(kwargs, names):
    assert kwargs["exclusive_mcp_names"] == names
    assert not any(
        key.startswith("mcp_servers.") and key.endswith(".enabled")
        for key in (kwargs.get("config_overrides") or {})
    )


def _override_values(kwargs: dict) -> list:
    return list((kwargs.get("config_overrides") or {}).values())


def _assert_secret_is_off_the_wire(kwargs: dict, args: list[str], codex_home) -> None:
    """The secret reaches the child through a private file, never through argv."""
    assert all(SECRET not in str(value) for value in _override_values(kwargs))
    assert all(SECRET not in arg for arg in args)

    profile = codex_home.path / f"{kwargs['profile']}.config.toml"
    assert profile in codex_home.profile_files()
    assert SECRET in profile.read_text()
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    # The child is pointed at the file; the file is how the value gets there.
    assert args[args.index("-p") + 1] == kwargs["profile"]


# The transports themselves: what an empty set means to each of them.


def test_an_empty_exclusive_set_is_strict_for_the_claude_transport():
    kwargs: dict = {}
    assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)
    assert kwargs["mcp_servers"] == {}
    assert kwargs["strict_mcp_config"] is True


def test_an_empty_exclusive_set_ignores_the_codex_user_config(codex_home):
    """codex has no wholesale clear: `-c mcp_servers={}` merges onto the existing
    table rather than replacing it. The empty set is the run that loads no user
    config at all, and a by-name disable of a server from a config the run does
    not load is a table with no server in it, which codex refuses to start with."""
    codex_home.write_config({"khive": {"command": "kkernel"}, "docs": {"url": "http://x"}})
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(kwargs, {}, provider="codex", exclusive=True)

    assert kwargs == {"ignore_user_config": True, "exclusive_mcp_names": []}


def test_an_empty_exclusive_set_with_a_named_profile_is_refused(codex_home):
    """A profile is user config too, and the by-name disables read config.toml alone, so a
    server the profile's file or a project layer declares would load. The empty set still
    takes the flag, and the codex request refuses the profile under it."""
    from lionagi.providers.openai.codex import CodexCodeRequest

    codex_home.write_config({"khive": {"command": "kkernel"}})
    kwargs: dict = {"profile": "work"}

    assert apply_forwarded_mcp_servers(kwargs, {}, provider="codex", exclusive=True)

    assert kwargs == {"profile": "work", "ignore_user_config": True, "exclusive_mcp_names": []}
    with pytest.raises(ConfigurationError, match="and profile='work'"):
        CodexCodeRequest(prompt="hi", **kwargs).as_cmd_args()


def test_an_exclusive_caller_with_no_set_is_strict_for_the_claude_transport():
    """No set from a caller that declares the whole set means no servers.

    `--no-mcp-config` resolves to a null map, the same map a failed search
    produces. Read as "no opinion", it left the request untouched and the Claude
    CLI loaded every server it finds for itself.
    """
    kwargs: dict = {}
    assert apply_forwarded_mcp_servers(kwargs, None, provider="claude_code", exclusive=True)
    assert kwargs["mcp_servers"] == {}
    assert kwargs["strict_mcp_config"] is True


def test_an_exclusive_caller_with_no_set_ignores_the_codex_user_config(codex_home):
    codex_home.write_config({"khive": {"command": "kkernel"}})
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(kwargs, None, provider="codex", exclusive=True)

    assert kwargs == {"ignore_user_config": True, "exclusive_mcp_names": []}


def test_a_request_that_ignores_the_user_config_disables_nothing_by_name(codex_home):
    """A name from a config the run does not load has no table behind it, so a
    by-name disable of it is an orphan codex refuses to start with."""
    codex_home.write_config({"ambient": {"command": "ambient-mcp"}})
    kwargs: dict = {"ignore_user_config": True}

    assert apply_forwarded_mcp_servers(
        kwargs, {"docs": {"command": "docs-mcp"}}, provider="codex", exclusive=True
    )

    assert kwargs["config_overrides"] == {"mcp_servers.docs.command": "docs-mcp"}


@pytest.mark.asyncio
async def test_an_allowlist_naming_a_server_only_codex_declares_keeps_that_server(
    codex_home, codex_launch, tmp_path
):
    """An allowed name the caller did not describe stays enabled from codex's own
    config, so an empty described set with such an allowlist is not the run that
    ignores the user config: only the names left out are disabled."""
    codex_home.write_config({"kept": {"command": "kept-mcp"}, "other": {"command": "other-mcp"}})
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(
        kwargs, {}, provider="codex", exclusive=True, allowed_names=["kept"]
    )

    assert "ignore_user_config" not in kwargs
    _assert_factory_selection(kwargs, ["kept"])
    from lionagi.providers.openai.codex import CodexCodeRequest

    config = await codex_launch(CodexCodeRequest(prompt="hi", repo=tmp_path, **kwargs))
    assert config == ["mcp_servers.other.enabled=false"]


@pytest.mark.parametrize(
    "config",
    [
        '"mcp\\u005fservers".kept.command = "kept-mcp"\n'
        '"mcp\\u005fservers".leak.command = "leak-mcp"\n',
        '[mcp_servers.kept]\ncommand = "kept-mcp"\n\n'
        '["mcp\\u005fservers".leak]\ncommand = "leak-mcp"\n',
    ],
    ids=["every-server-escaped", "one-server-escaped"],
)
@pytest.mark.asyncio
async def test_an_escaped_mcp_servers_key_declares_the_servers_it_spells(
    codex_home, monkeypatch, config, codex_launch, tmp_path
):
    """A quoted key holding an escape is the key it decodes to, so `"mcp\\u005fservers"`
    declares servers as `mcp_servers` does. Read with the escape left in, the kept server
    would be missing and the run would drop the user config with it, and a server declared
    that way beside a plain one would load with no disable."""
    import lionagi.providers.openai.codex as codex_mod

    # No codex binary to list the servers with, so the names can only come from the file.
    monkeypatch.setattr(codex_mod, "CODEX_CLI", None)
    (codex_home.path / "config.toml").write_text(config)
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(
        kwargs, {}, provider="codex", exclusive=True, allowed_names=["kept"]
    )

    assert "ignore_user_config" not in kwargs
    _assert_factory_selection(kwargs, ["kept"])
    from lionagi.providers.openai.codex import CodexCodeRequest

    argv_config = await codex_launch(CodexCodeRequest(prompt="hi", repo=tmp_path, **kwargs))
    assert argv_config == ["mcp_servers.leak.enabled=false"]


@pytest.mark.parametrize(
    ("servers", "allowed_names"),
    [({}, ["kept"]), (SERVERS, None)],
    ids=["allowlist-keeps-a-declared-server", "non-empty-set"],
)
def test_an_exclusive_set_that_loads_the_user_config_refuses_a_named_profile(
    codex_home, servers, allowed_names
):
    """These runs keep the user config, so the set is made exclusive by disabling by name
    the servers config.toml declares. A profile's file can declare servers no such disable
    reaches, so a profile the caller named is refused rather than passed on with `-p`."""
    codex_home.write_config({"kept": {"command": "kept-mcp"}, "other": {"command": "other-mcp"}})
    kwargs: dict = {"profile": "work"}

    with pytest.raises(ConfigurationError, match="profile='work'"):
        apply_forwarded_mcp_servers(
            kwargs, servers, provider="codex", exclusive=True, allowed_names=allowed_names
        )

    # the controls: the same set without a profile, and the profile on a set that is an addition
    plain: dict = {}
    assert apply_forwarded_mcp_servers(
        plain, servers, provider="codex", exclusive=True, allowed_names=allowed_names
    )
    assert "ignore_user_config" not in plain
    addition: dict = {"profile": "work"}
    assert apply_forwarded_mcp_servers(addition, SERVERS, provider="codex")
    assert addition["profile"] == "work"


@pytest.mark.parametrize("key", ["profile", " profile\t"])
@pytest.mark.parametrize(
    ("servers", "allowed_names"),
    [({}, ["kept"]), (SERVERS, None)],
    ids=["allowlist-keeps-a-declared-server", "non-empty-set"],
)
def test_an_exclusive_set_that_loads_the_user_config_refuses_a_profile_its_overrides_select(
    codex_home, servers, allowed_names, key
):
    """`-c profile=...` selects a profile as `-p` does: codex trims an override's key and
    reads it as the same `profile` key."""
    codex_home.write_config({"kept": {"command": "kept-mcp"}, "other": {"command": "other-mcp"}})
    kwargs: dict = {"config_overrides": {key: "work"}}

    with pytest.raises(ConfigurationError, match="profile='work'"):
        apply_forwarded_mcp_servers(
            kwargs, servers, provider="codex", exclusive=True, allowed_names=allowed_names
        )

    # the controls: overrides that only define a profile, and the selecting key on an addition
    defines: dict = {"config_overrides": {"profiles.work.model": "gpt-5.3-codex"}}
    assert apply_forwarded_mcp_servers(
        defines, servers, provider="codex", exclusive=True, allowed_names=allowed_names
    )
    addition: dict = {"config_overrides": {"profile": "work"}}
    assert apply_forwarded_mcp_servers(addition, SERVERS, provider="codex")
    assert addition["config_overrides"]["profile"] == "work"


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [
        ({"profile": ""}, "profile=''"),
        ({"profile": ["secret-name"]}, "a profile of type list"),
        ({"config_overrides": {"profile": {"secret": "name"}}}, "a profile of type dict"),
    ],
    ids=["empty-string", "list", "override-table"],
)
def test_an_empty_or_non_string_profile_on_such_a_set_is_refused(codex_home, kwargs, named):
    """An empty name still reaches the command line (`-p ''`), and a value that is not a
    string is named by its type, its content not echoed."""
    codex_home.write_config({"kept": {"command": "kept-mcp"}})

    with pytest.raises(ConfigurationError, match=re.escape(named)) as refused:
        apply_forwarded_mcp_servers(kwargs, SERVERS, provider="codex", exclusive=True)

    assert "secret" not in str(refused.value)


def test_a_string_flag_is_read_as_the_codex_request_reads_it(codex_home):
    """The request's field reads `"false"` as False, so the hand-off does too: the profile is
    refused, and an empty set takes the flag. A value the field refuses is left for it."""
    from pydantic import ValidationError

    from lionagi.providers.openai.codex import CodexCodeRequest

    codex_home.write_config({"kept": {"command": "kept-mcp"}})

    named: dict = {"ignore_user_config": "false", "profile": "work"}
    with pytest.raises(ConfigurationError, match="profile='work'"):
        apply_forwarded_mcp_servers(named, SERVERS, provider="codex", exclusive=True)

    empty: dict = {"ignore_user_config": "false"}
    assert apply_forwarded_mcp_servers(empty, {}, provider="codex", exclusive=True)
    assert empty == {"ignore_user_config": True, "exclusive_mcp_names": []}

    on: dict = {"ignore_user_config": "true"}
    assert apply_forwarded_mcp_servers(on, SERVERS, provider="codex", exclusive=True)
    assert on["ignore_user_config"] is True
    assert "mcp_servers.kept.enabled" not in on["config_overrides"]

    refused: dict = {"ignore_user_config": "maybe"}
    assert apply_forwarded_mcp_servers(refused, SERVERS, provider="codex", exclusive=True)
    assert refused["ignore_user_config"] == "maybe"
    with pytest.raises(ValidationError):
        CodexCodeRequest(prompt="hi", **refused)


@pytest.mark.parametrize("value", ["", "maybe", [], {}], ids=["empty", "maybe", "list", "dict"])
def test_a_flag_value_the_request_refuses_is_not_replaced_on_an_empty_set(codex_home, value):
    """A falsy value the request's field refuses is not the flag's absence: the empty set
    leaves it for the request to refuse rather than replacing it with the flag."""
    from pydantic import ValidationError

    from lionagi.providers.openai.codex import CodexCodeRequest

    codex_home.write_config({"kept": {"command": "kept-mcp"}})
    kwargs: dict = {"ignore_user_config": value}

    assert apply_forwarded_mcp_servers(kwargs, {}, provider="codex", exclusive=True)

    assert kwargs["ignore_user_config"] == value
    with pytest.raises(ValidationError):
        CodexCodeRequest(prompt="hi", **kwargs)


def test_an_override_key_is_read_as_an_exact_string(codex_home):
    """A `str` subclass key is read through `str.__str__`, as the codex request reads it, so
    an override of its own `strip` cannot hide the `profile` key."""

    class _Key(str):
        def strip(self, chars=None):
            return "x"

    codex_home.write_config({"kept": {"command": "kept-mcp"}})
    kwargs: dict = {"config_overrides": {_Key("profile"): "work"}}

    with pytest.raises(ConfigurationError, match="profile='work'"):
        apply_forwarded_mcp_servers(kwargs, SERVERS, provider="codex", exclusive=True)


_ROOT_PROFILE_CONFIG = """\
profile = "work"

[mcp_servers.kept]
command = "kept-mcp"

[profiles.work.mcp_servers.leak]
command = "leak-mcp"
"""


@pytest.mark.parametrize(
    ("servers", "allowed_names"),
    [({}, ["kept"]), (SERVERS, None)],
    ids=["allowlist-keeps-a-declared-server", "non-empty-set"],
)
def test_an_exclusive_set_that_loads_the_user_config_refuses_its_root_profile_key(
    codex_home, servers, allowed_names
):
    """With no `-p`, codex before 0.135 makes a root `profile` key the active profile, and
    a server that profile declares is out of reach of the by-name disables."""
    (codex_home.path / "config.toml").write_text(_ROOT_PROFILE_CONFIG)
    kwargs: dict = {}

    with pytest.raises(ConfigurationError, match="profile='work'") as refused:
        apply_forwarded_mcp_servers(
            kwargs, servers, provider="codex", exclusive=True, allowed_names=allowed_names
        )

    assert "Remove the root `profile` key from" in str(refused.value)
    # the controls: the same set when the key is gone, and the key under a set that is an addition
    codex_home.write_config({"kept": {"command": "kept-mcp"}})
    plain: dict = {}
    assert apply_forwarded_mcp_servers(
        plain, servers, provider="codex", exclusive=True, allowed_names=allowed_names
    )
    (codex_home.path / "config.toml").write_text(_ROOT_PROFILE_CONFIG)
    addition: dict = {}
    assert apply_forwarded_mcp_servers(addition, SERVERS, provider="codex")


def test_the_root_profile_key_is_read_from_the_config_the_childs_environment_names(
    codex_home, tmp_path
):
    """A request's env replaces the child's environment, so the config the child reads is
    under the `CODEX_HOME` that env names, not this process's."""
    codex_home.write_config({"kept": {"command": "kept-mcp"}})
    child_home = tmp_path / "child-codex-home"
    child_home.mkdir()
    (child_home / "config.toml").write_text(_ROOT_PROFILE_CONFIG)
    kwargs: dict = {"env": {"CODEX_HOME": str(child_home), "PATH": "/usr/bin:/bin"}}

    with pytest.raises(ConfigurationError, match=re.escape(str(child_home / "config.toml"))):
        apply_forwarded_mcp_servers(kwargs, SERVERS, provider="codex", exclusive=True)


def _codex_model_with_child_home(child_home: Path):
    from lionagi.service.imodel import iModel

    model = iModel(
        provider="codex",
        model="gpt-5.5",
        env={"CODEX_HOME": str(child_home), "PATH": "/usr/bin:/bin"},
    )
    # A CLI endpoint holds the child's env outside its config kwargs.
    assert "env" not in model.endpoint.config.kwargs
    return model


def test_an_agent_model_holding_the_child_env_is_judged_by_that_env(codex_home, tmp_path):
    """create_agent's hand-off reads the root `profile` key from the config the model's own
    child env names, though that env is not in the request kwargs."""
    from lionagi.agent.factory import _forward_mcp_to_cli_request
    from lionagi.agent.spec import AgentSpec
    from lionagi.session.branch import Branch

    codex_home.write_config({"kept": {"command": "kept-mcp"}})
    child_home = tmp_path / "child-codex-home"
    child_home.mkdir()
    (child_home / "config.toml").write_text(_ROOT_PROFILE_CONFIG)
    branch = Branch(chat_model=_codex_model_with_child_home(child_home))
    spec = AgentSpec.compose("implementer", model="codex/gpt-5.5")
    spec.mcp_servers = ["kept"]

    with pytest.raises(ConfigurationError, match=re.escape(str(child_home / "config.toml"))):
        _forward_mcp_to_cli_request(
            branch, spec, resolved_servers={"kept": {"command": "kept-mcp"}}
        )


@pytest.mark.asyncio
async def test_a_worker_model_disables_the_servers_its_child_config_declares(
    codex_home, tmp_path, codex_launch
):
    """The by-name disables name the servers of the config the child reads: one this
    process's config declares and the child's does not would be a table with no server,
    and one only the child's declares would load."""
    from lionagi.cli.orchestrate._orchestration import _hand_mcp_servers

    codex_home.write_config({"ambient": {"command": "ambient-mcp"}})
    child_home = tmp_path / "child-codex-home"
    child_home.mkdir()
    (child_home / "config.toml").write_text(
        '[mcp_servers.kept]\ncommand = "kept-mcp"\n\n[mcp_servers.leak]\ncommand = "leak-mcp"\n'
    )
    model = _codex_model_with_child_home(child_home)

    _hand_mcp_servers(model, {"kept": {"command": "kept-mcp"}}, label="w1", exclusive=True)

    kwargs = model.endpoint.config.kwargs
    _assert_factory_selection(kwargs, ["kept"])
    assert kwargs["config_overrides"] == {"mcp_servers.kept.command": "kept-mcp"}
    event = await model.create_event(prompt="hi", repo=tmp_path)
    config = await codex_launch(event.payload["request"])
    assert "mcp_servers.leak.enabled=false" in config
    assert not any(value.startswith("mcp_servers.ambient.") for value in config)


def test_secret_fields_are_refused_for_a_child_under_another_codex_home(codex_home, tmp_path):
    """The generated profile is written under this process's codex home; a child whose env
    names another would be handed `-p` for a file it cannot find. Refused, with no profile
    written and no value in the message. The control is a child env naming this home."""
    codex_home.write_config({})
    child_home = tmp_path / "child-codex-home"
    child_home.mkdir()
    (child_home / "config.toml").write_text("")
    kwargs: dict = {}

    with pytest.raises(ConfigurationError, match="names a different one") as refused:
        apply_forwarded_mcp_servers(
            kwargs,
            SECRET_SERVERS,
            provider="codex",
            env={"CODEX_HOME": str(child_home), "PATH": "/usr/bin:/bin"},
        )

    assert SECRET not in str(refused.value)
    assert codex_home.profile_files() == []
    same: dict = {}
    assert apply_forwarded_mcp_servers(
        same,
        SECRET_SERVERS,
        provider="codex",
        env={"CODEX_HOME": str(codex_home.path), "PATH": "/usr/bin:/bin"},
    )
    assert len(codex_home.profile_files()) == 1


@pytest.mark.parametrize("value", ["~/child-codex-home", "child-codex-home"])
@pytest.mark.parametrize("site", ["allowlist", "root profile", "secret profile"])
def test_a_child_codex_home_that_is_not_absolute_is_refused_before_its_config_is_read(
    codex_home, tmp_path, monkeypatch, value, site
):
    """Each read of the child's codex config refuses a `~` or relative CODEX_HOME, as the
    project-layer check does, with nothing started and no profile written. The config under
    each place the value could land declares a root profile, so the root-profile read
    would otherwise refuse for that instead. The control names the same home absolutely."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    child_home = tmp_path / "child-home"
    for landed in (tmp_path / "child-codex-home", child_home / "child-codex-home"):
        landed.mkdir(parents=True)
        if site == "root profile":
            (landed / "config.toml").write_text(_ROOT_PROFILE_CONFIG)
    codex_home.write_config({"kept": {"command": "kept-mcp"}})
    started: list = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: started.append(argv))

    def apply(codex_home_value: str) -> bool:
        env = {"CODEX_HOME": codex_home_value, "HOME": str(child_home), "PATH": "/usr/bin:/bin"}
        if site == "allowlist":
            return apply_forwarded_mcp_servers(
                {}, {}, provider="codex", exclusive=True, allowed_names=["kept"], env=env
            )
        if site == "root profile":
            return apply_forwarded_mcp_servers(
                {}, SERVERS, provider="codex", exclusive=True, env=env
            )
        return apply_forwarded_mcp_servers({}, SECRET_SERVERS, provider="codex", env=env)

    with pytest.raises(ConfigurationError, match="is not an absolute path"):
        apply(value)
    assert started == []
    assert codex_home.profile_files() == []
    assert apply(str(codex_home.path))


@pytest.mark.parametrize("key", ["profile", " profile ", "profile.x"])
def test_secret_fields_are_refused_beside_a_profile_override(codex_home, key):
    """The secret fields reach codex through a `-p` profile of their own; an override that
    selects a profile too would leave codex two, so the request is refused with no profile
    written and no value in the message. The control is the same set with no override."""
    codex_home.write_config({})
    kwargs: dict = {"config_overrides": {key: "work"}}

    with pytest.raises(ConfigurationError, match="overrides select a profile") as refused:
        apply_forwarded_mcp_servers(kwargs, SECRET_SERVERS, provider="codex")

    assert SECRET not in str(refused.value)
    assert codex_home.profile_files() == []
    plain: dict = {}
    assert apply_forwarded_mcp_servers(plain, SECRET_SERVERS, provider="codex")
    assert len(codex_home.profile_files()) == 1


@pytest.mark.asyncio
async def test_an_allowlist_naming_no_server_codex_declares_disables_declared_servers(
    codex_home, codex_launch, tmp_path
):
    """An undeclared allowed name keeps user config and disables its declared servers."""
    from lionagi.providers.openai.codex import CodexCodeRequest

    codex_home.write_config({"kept": {"command": "kept-mcp"}, "other": {"command": "other-mcp"}})
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(
        kwargs, {}, provider="codex", exclusive=True, allowed_names=["undeclared"]
    )

    assert "ignore_user_config" not in kwargs
    _assert_factory_selection(kwargs, ["undeclared"])
    config = await codex_launch(CodexCodeRequest(prompt="hi", repo=tmp_path, **kwargs))
    assert set(config) == {"mcp_servers.kept.enabled=false", "mcp_servers.other.enabled=false"}


def test_secret_fields_are_refused_on_a_request_that_ignores_the_user_config(codex_home):
    """The secret fields reach codex only through a generated `-p` profile, and a
    run that ignores the user config loads no profile: the server would start
    without them. Refused, with no profile written and no value in the message."""
    codex_home.write_config({})
    kwargs: dict = {"ignore_user_config": True}

    with pytest.raises(ConfigurationError, match="ignores the user config") as refused:
        apply_forwarded_mcp_servers(kwargs, SECRET_SERVERS, provider="codex", exclusive=True)

    assert SECRET not in str(refused.value)
    assert codex_home.profile_files() == []
    assert "profile" not in kwargs


def test_an_ignored_user_config_is_a_carried_set_and_a_disable_is_not():
    from lionagi.agent.factory import request_kwargs_carry_forwarded_mcp

    assert request_kwargs_carry_forwarded_mcp({"ignore_user_config": True})
    assert not request_kwargs_carry_forwarded_mcp({"ignore_user_config": False})
    # Read as the codex request's field reads the flag.
    assert request_kwargs_carry_forwarded_mcp({"ignore_user_config": "true"})
    assert request_kwargs_carry_forwarded_mcp({"ignore_user_config": 1})
    assert not request_kwargs_carry_forwarded_mcp({"ignore_user_config": "false"})
    assert not request_kwargs_carry_forwarded_mcp({"ignore_user_config": "neither"})
    assert not request_kwargs_carry_forwarded_mcp(
        {"config_overrides": {"mcp_servers.ambient.enabled": False}}
    )


def test_a_caller_with_no_set_and_no_claim_to_the_whole_set_changes_nothing(codex_home):
    codex_home.write_config({"khive": {"command": "kkernel"}})
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(kwargs, None, provider="codex") is False
    assert kwargs == {}


def test_an_exclusive_set_replaces_the_servers_a_request_already_carries(codex_home):
    """A resumed leg's request is the persisted one, holding the servers the
    previous run was handed; a set declared whole replaces them, both the
    per-field overrides and a whole `mcp_servers` table."""
    codex_home.write_config({})
    kwargs: dict = {
        "config_overrides": {
            "mcp_servers.old.command": "old-server",
            "mcp_servers": {"older": {"command": "older-server"}},
            "model_reasoning_effort": "high",
        }
    }

    assert apply_forwarded_mcp_servers(kwargs, {}, provider="codex", exclusive=True)

    assert kwargs["config_overrides"] == {"model_reasoning_effort": "high"}


def test_an_exclusive_set_leaves_no_emptied_override_map_behind(codex_home):
    codex_home.write_config({})
    kwargs: dict = {"config_overrides": {"mcp_servers.old.command": "old-server"}}

    assert apply_forwarded_mcp_servers(kwargs, {}, provider="codex", exclusive=True)

    assert "config_overrides" not in kwargs


def test_an_exclusive_set_drops_a_generated_profile_and_keeps_a_named_one(codex_home):
    """A generated profile holds nothing but a previous run's servers; a profile
    the caller named is theirs."""
    from lionagi.agent.factory import _CODEX_MCP_PROFILE_PREFIX

    codex_home.write_config({})
    generated = {"profile": _CODEX_MCP_PROFILE_PREFIX + "0" * 32}
    named = {"profile": "work"}

    apply_forwarded_mcp_servers(generated, {}, provider="codex", exclusive=True)
    apply_forwarded_mcp_servers(named, {}, provider="codex", exclusive=True)

    assert "profile" not in generated
    assert named["profile"] == "work"


@pytest.mark.asyncio
async def test_codex_disables_only_servers_its_own_config_declares(
    codex_home, tmp_path, monkeypatch, codex_launch
):
    """An allowlist excludes servers of the config it filters, and codex is told
    to disable only the names its own config declares: an
    `mcp_servers.<name>.enabled=false` override for any other name is a table
    with no server in it, which codex refuses to start with."""
    from lionagi.agent.factory import create_agent
    from lionagi.agent.spec import AgentSpec
    from lionagi.protocols.action.manager import ActionManager

    async def skip_native_registration(self, *args, **kwargs):
        return {}

    # The allowed server is handed to codex; registering it as a lionagi-native
    # tool would start it here, which is not what this test is about.
    monkeypatch.setattr(ActionManager, "load_mcp_config", skip_native_registration)

    codex_home.write_config({"ambient": {"command": "ambient-mcp"}})
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {"command": "docs-mcp"},
                    "notes": {"command": "notes-mcp-absent"},
                }
            }
        )
    )
    spec = AgentSpec.compose("reviewer", model=CODEX_SPEC)
    spec.mcp_config_path = str(config_path)
    spec.mcp_servers = ["docs"]

    branch = await create_agent(spec, load_settings=False)

    kwargs = branch.chat_model.endpoint.config.kwargs
    _assert_factory_selection(kwargs, ["docs"])
    assert kwargs["config_overrides"] == {"mcp_servers.docs.command": "docs-mcp"}
    event = await branch.chat_model.create_event(prompt="hi", repo=tmp_path)
    config = await codex_launch(event.payload["request"])
    assert set(config) == {
        'mcp_servers.docs.command="docs-mcp"',
        "mcp_servers.ambient.enabled=false",
    }
    assert not any(value.startswith("mcp_servers.notes.") for value in config)


@pytest.mark.parametrize("spelling", ["CLAUDE_CODE", " claude_code ", "Claude_Code"])
def test_a_claude_provider_spelling_takes_the_claude_transport(spelling):
    """A spelling that resolves to the Claude CLI must not fall through to the
    codex override form, which leaves the strict flag unset."""
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(kwargs, SERVERS, provider=spelling, exclusive=True)

    assert kwargs["mcp_servers"] == SERVERS
    assert kwargs["strict_mcp_config"] is True
    assert "config_overrides" not in kwargs


def test_a_provider_without_a_transport_reports_that_it_carried_nothing():
    kwargs: dict = {}
    assert apply_forwarded_mcp_servers(kwargs, SERVERS, provider="gemini_code") is False
    assert kwargs == {}


# Site 1 — the plain `li agent` leg (build_chat_model).


def test_plain_leg_hands_a_codex_request_the_resolved_set(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({})
    model = build_chat_model("codex", "gpt-5.3-codex", False, False, None, mcp_servers=SERVERS)

    assert model.endpoint.config.kwargs["config_overrides"] == {
        "mcp_servers.khive.command": "kkernel",
        "mcp_servers.khive.args": ["serve"],
    }


def test_plain_leg_still_hands_a_claude_request_the_resolved_set():
    from lionagi.cli._providers import build_chat_model

    model = build_chat_model(
        "claude_code", "claude-opus-4-5", False, False, None, mcp_servers=SERVERS
    )
    assert model.endpoint.config.kwargs["mcp_servers"] == SERVERS


def test_plain_leg_hands_nothing_to_a_provider_without_a_transport():
    from lionagi.cli._providers import build_chat_model

    model = build_chat_model(
        "gemini_code", "gemini-3.5-flash", False, False, None, mcp_servers=SERVERS
    )
    # No flags and nothing forwarded leaves a bare spec string, not a request.
    assert model == "gemini_code/gemini-3.5-flash"


@pytest.mark.asyncio
async def test_plain_leg_keeps_codex_secret_fields_off_the_command_line(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({})
    model = build_chat_model(
        "codex", "gpt-5.3-codex", False, False, None, mcp_servers=SECRET_SERVERS
    )

    kwargs = model.endpoint.config.kwargs
    assert kwargs["config_overrides"] == {"mcp_servers.khive.command": "kkernel"}
    _assert_secret_is_off_the_wire(kwargs, await _cmd_args(model), codex_home)


def test_plain_leg_empty_set_is_the_whole_set_on_both_transports(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({"khive": {"command": "kkernel"}})

    codex = build_chat_model("codex", "gpt-5.3-codex", False, False, None, mcp_servers={})
    assert codex.endpoint.config.kwargs["ignore_user_config"] is True
    assert "config_overrides" not in codex.endpoint.config.kwargs

    claude = build_chat_model("claude_code", "claude-opus-4-5", False, False, None, mcp_servers={})
    assert claude.endpoint.config.kwargs["mcp_servers"] == {}
    assert claude.endpoint.config.kwargs["strict_mcp_config"] is True


def test_plain_leg_with_a_declared_empty_set_is_contained_on_both_transports(codex_home):
    """`--no-mcp-config` reaches build_chat_model as a null map; the caller says
    it was declared, so the request is the whole (empty) set."""
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({"khive": {"command": "kkernel"}})

    codex = build_chat_model(
        "codex", "gpt-5.3-codex", False, False, None, mcp_servers=None, mcp_exclusive=True
    )
    assert codex.endpoint.config.kwargs["ignore_user_config"] is True
    assert "config_overrides" not in codex.endpoint.config.kwargs

    claude = build_chat_model(
        "claude_code", "claude-opus-4-5", False, False, None, mcp_servers=None, mcp_exclusive=True
    )
    assert claude.endpoint.config.kwargs["mcp_servers"] == {}
    assert claude.endpoint.config.kwargs["strict_mcp_config"] is True


@pytest.mark.asyncio
async def test_plain_leg_empty_set_reaches_the_codex_command_line(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({"ambient": {"command": "ambient-mcp"}})
    model = build_chat_model("codex", "gpt-5.3-codex", False, False, None, mcp_servers={})
    args = await _cmd_args(model)

    assert "--ignore-user-config" in args
    assert not [arg for arg in args if "ambient" in arg]


def test_plain_leg_derives_no_effort_from_a_user_config_it_ignores(codex_home):
    """The derived default keeps the user config's own effort under the model's
    ceiling; a leg that does not load that config has no such value."""
    from lionagi.cli._providers import build_chat_model

    (codex_home.path / "config.toml").write_text('model_reasoning_effort = "ultra"\n')

    ignores = build_chat_model("codex", "gpt-5.3-codex-spark", False, False, None, mcp_servers={})
    reads = build_chat_model(
        "codex", "gpt-5.3-codex-spark", False, False, None, mcp_servers=SERVERS
    )

    assert "reasoning_effort" not in ignores.endpoint.config.kwargs
    assert reads.endpoint.config.kwargs["reasoning_effort"] == "xhigh"


def test_plain_leg_with_no_set_and_no_declaration_is_left_alone(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({"khive": {"command": "kkernel"}})
    model = build_chat_model("codex", "gpt-5.3-codex", False, False, None, mcp_servers=None)
    assert model == "codex/gpt-5.3-codex"


# Site 2 — the resumed leg.


def _wire_resume_stubs(
    monkeypatch, tmp_path: Path, provider: str, model: str, **request_kwargs
) -> str:
    """Persist a branch on *provider* and stub every external I/O `_run_agent`
    does, so what the resumed leg's request ends up carrying is all that is
    left to observe. *request_kwargs* is what the previous run's request
    carried."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch, iModel
    from lionagi.service.manager import iModelManager

    branch = Branch(
        chat_model=iModel(
            provider=provider, endpoint="query_cli", model=model, api_key="dummy", **request_kwargs
        )
    )
    branch_path = tmp_path / f"{branch.id}.json"
    branch_path.write_text(json.dumps(branch.to_dict()))

    async def fake_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, **kw):
        return kw.get("status", "completed")

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )
    return str(branch.id)


def _mcp_config_file(tmp_path: Path, servers: dict) -> str:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return str(path)


@pytest.mark.asyncio
async def test_resumed_codex_leg_gets_the_set_the_resume_command_resolved(
    monkeypatch, tmp_path, codex_home, caplog
):
    """A resumed leg re-spawns a CLI child, so it needs the set as much as a new
    one does; the persisted branch carries the model, not the caller's directory.
    Having been given it, the leg is not also told it was not."""
    from lionagi.cli.agent import _run_agent

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "codex", "gpt-5.3-codex")

    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        await _run_agent(
            None,
            "follow up",
            resume=branch_id,
            mcp_config=_mcp_config_file(tmp_path, SERVERS),
        )

    assert "not carried" not in caplog.text


@pytest.mark.asyncio
async def test_resumed_leg_request_carries_the_codex_overrides(monkeypatch, tmp_path, codex_home):
    """Read the built request, not the report: the overrides are what the child
    is spawned with."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "codex", "gpt-5.3-codex")

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        seen["args"] = await _cmd_args(self.chat_model)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(
        None,
        "follow up",
        resume=branch_id,
        mcp_config=_mcp_config_file(tmp_path, SERVERS),
    )

    assert seen["kwargs"]["config_overrides"] == {
        "mcp_servers.khive.command": "kkernel",
        "mcp_servers.khive.args": ["serve"],
    }
    assert "-c" in seen["args"]
    assert 'mcp_servers.khive.command="kkernel"' in seen["args"]


@pytest.mark.asyncio
async def test_resumed_leg_keeps_codex_secret_fields_off_the_command_line(
    monkeypatch, tmp_path, codex_home
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "codex", "gpt-5.3-codex")

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        seen["args"] = await _cmd_args(self.chat_model)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(
        None,
        "follow up",
        resume=branch_id,
        mcp_config=_mcp_config_file(tmp_path, SECRET_SERVERS),
    )

    _assert_secret_is_off_the_wire(seen["kwargs"], seen["args"], codex_home)


@pytest.mark.asyncio
async def test_resumed_codex_leg_with_no_mcp_config_drops_the_previous_runs_servers(
    monkeypatch, tmp_path, codex_home
):
    """The persisted request holds the servers the previous run was handed;
    `--no-mcp-config` on the resume means none of them, and none of codex's own."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    codex_home.write_config({"khive": {"command": "kkernel"}})
    branch_id = _wire_resume_stubs(
        monkeypatch,
        tmp_path,
        "codex",
        "gpt-5.3-codex",
        config_overrides={"mcp_servers.old.command": "old-server"},
    )

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "follow up", resume=branch_id, no_mcp_config=True)

    assert seen["kwargs"]["ignore_user_config"] is True
    assert "config_overrides" not in seen["kwargs"]


@pytest.mark.asyncio
async def test_a_resumed_codex_leg_given_a_set_after_an_empty_one_reads_the_user_config(
    monkeypatch, tmp_path, codex_home
):
    """An earlier empty set was carried as the ignored user config. A set resolved for the
    resume replaces it, so the leg reads the user config as a new leg with that set does."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(
        monkeypatch, tmp_path, "codex", "gpt-5.3-codex", ignore_user_config=True
    )

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(
        None, "follow up", resume=branch_id, mcp_config=_mcp_config_file(tmp_path, SERVERS)
    )

    assert "ignore_user_config" not in seen["kwargs"]
    ((name, server),) = SERVERS.items()
    assert seen["kwargs"]["config_overrides"][f"mcp_servers.{name}.command"] == server["command"]


@pytest.mark.asyncio
async def test_resumed_claude_leg_with_no_mcp_config_is_strict_and_empty(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    branch_id = _wire_resume_stubs(
        monkeypatch, tmp_path, "claude_code", "sonnet", mcp_servers=dict(SERVERS)
    )

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "follow up", resume=branch_id, no_mcp_config=True)

    assert seen["kwargs"]["mcp_servers"] == {}
    assert seen["kwargs"]["strict_mcp_config"] is True


@pytest.mark.asyncio
async def test_resumed_leg_whose_search_finds_nothing_keeps_the_previous_runs_servers(
    monkeypatch, tmp_path, codex_home
):
    """Only a declared set replaces the persisted one. A search that finds no
    config declares nothing: it leaves a resumed leg the servers it had, as it
    leaves a new leg to what its CLI finds, and disables none of codex's own."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli._mcp_resolve import McpResolution

    codex_home.write_config({"khive": {"command": "kkernel"}})
    branch_id = _wire_resume_stubs(
        monkeypatch,
        tmp_path,
        "codex",
        "gpt-5.3-codex",
        config_overrides={"mcp_servers.old.command": "old-server"},
    )
    monkeypatch.setattr(
        agent_mod,
        "resolve_spawn_mcp_servers",
        lambda *a, **kw: McpResolution(None, "no_mcp_config_found", None, tmp_path),
    )

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "follow up", resume=branch_id)

    assert seen["kwargs"]["config_overrides"] == {"mcp_servers.old.command": "old-server"}


def _record_launch(tmp_path: Path, branch_id: str, launch: dict) -> None:
    import lionagi.cli.agent as agent_mod

    branch_path = tmp_path / f"{branch_id}.json"
    data = json.loads(branch_path.read_text())
    data.setdefault("metadata", {})[agent_mod._MCP_LAUNCH_KEY] = launch
    branch_path.write_text(json.dumps(data))


@pytest.mark.asyncio
async def test_a_resume_with_no_flag_searches_where_the_branch_was_launched(monkeypatch, tmp_path):
    """The launch directory decides, not the directory the resume runs in. That
    directory holds a config of its own, and its server must not reach the leg."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    launched = tmp_path / "launched"
    resumed = tmp_path / "resumed"
    launched.mkdir()
    resumed.mkdir()
    (launched / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"from-launch": {"command": "a"}}})
    )
    (resumed / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"from-resume": {"command": "b"}}})
    )
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": None, "no_mcp_config": False, "launch_dir": str(launched)},
    )
    monkeypatch.chdir(resumed)

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "follow up", resume=branch_id)

    assert set(seen["kwargs"]["mcp_servers"]) == {"from-launch"}


@pytest.mark.asyncio
async def test_a_branch_with_no_recorded_launch_keeps_its_servers_on_resume(
    monkeypatch, tmp_path, caplog
):
    """A branch from before the launch was recorded resolves nothing new: it keeps
    what its earlier run was handed, and the resuming directory adds nothing."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    resumed = tmp_path / "resumed"
    resumed.mkdir()
    (resumed / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"from-resume": {"command": "b"}}})
    )
    branch_id = _wire_resume_stubs(
        monkeypatch, tmp_path, "claude_code", "sonnet", mcp_servers=dict(SERVERS)
    )
    monkeypatch.chdir(resumed)

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        await agent_mod._run_agent(None, "follow up", resume=branch_id)

    assert seen["kwargs"]["mcp_servers"] == SERVERS
    assert "no MCP servers are being handed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("named_by", ["flag", "record"])
async def test_a_set_resolved_on_resume_replaces_the_one_the_codex_leg_had(
    monkeypatch, tmp_path, codex_home, named_by
):
    """codex takes a set as overrides merged into the persisted request, so the
    servers the earlier run was handed go first, or the leg starts both sets.
    Settings that are not servers stay."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(
        monkeypatch,
        tmp_path,
        "codex",
        "gpt-5.3-codex",
        config_overrides={"mcp_servers.old.command": "old-server", "model_provider": "vendor"},
    )
    config = _mcp_config_file(tmp_path, SERVERS)
    kwargs = {}
    if named_by == "flag":
        kwargs["mcp_config"] = config
    else:
        _record_launch(
            tmp_path,
            branch_id,
            {"mcp_config": config, "no_mcp_config": False, "launch_dir": str(tmp_path)},
        )

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "follow up", resume=branch_id, **kwargs)

    assert seen["kwargs"]["config_overrides"] == {
        "model_provider": "vendor",
        "mcp_servers.khive.command": "kkernel",
        "mcp_servers.khive.args": ["serve"],
    }


@pytest.mark.asyncio
async def test_a_resume_whose_recorded_config_is_gone_names_it(monkeypatch, tmp_path):
    """The caller passed no --mcp-config, so the refusal names the config the
    branch was launched with, and the two ways on."""
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._mcp_resolve import McpConfigError

    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    gone = tmp_path / "gone" / ".mcp.json"
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": str(gone), "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )

    with pytest.raises(McpConfigError) as raised:
        await agent_mod._run_agent(None, "follow up", resume=branch_id)

    message = str(raised.value)
    assert message.startswith(f"this branch was launched with the MCP config {gone}")
    assert "which is no longer a readable file" in message
    assert "--no-mcp-config" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["not_utf8", "unknown_user", "not_a_string"])
async def test_a_recorded_config_that_cannot_even_be_read_is_refused_by_name(
    monkeypatch, tmp_path, kind
):
    """A record naming a config whose bytes are not text, a home directory that
    does not exist, or something that is not a path at all still refuses the
    resume in its own words, before anything is spawned."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli._mcp_resolve import McpConfigError

    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    if kind == "not_utf8":
        path = tmp_path / "latin1" / ".mcp.json"
        path.parent.mkdir()
        path.write_bytes(b'{"mcpServers": {"caf\xe9": {"command": "x"}}}')
        recorded = str(path)
    elif kind == "unknown_user":
        recorded = "~no-such-user-for-this-test/.mcp.json"
    else:
        recorded = 5
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": recorded, "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)

    with pytest.raises(McpConfigError) as raised:
        await agent_mod._run_agent(None, "follow up", resume=branch_id)

    message = str(raised.value)
    assert message.startswith(f"this branch was launched with the MCP config {recorded}")
    assert "--no-mcp-config" in message
    assert seen == {}


@pytest.mark.asyncio
async def test_a_recorded_config_that_can_no_longer_be_used_names_it(monkeypatch, tmp_path):
    """The file is there but no longer reads as a config. The refusal still names
    the recorded config and the ways on, not a --mcp-config the caller never passed."""
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._mcp_resolve import McpConfigError

    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    broken = tmp_path / "broken" / ".mcp.json"
    broken.parent.mkdir()
    broken.write_text("{not json")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": str(broken), "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )

    with pytest.raises(McpConfigError) as raised:
        await agent_mod._run_agent(None, "follow up", resume=branch_id)

    message = str(raised.value)
    assert message.startswith(f"this branch was launched with the MCP config {broken}")
    assert "is not valid JSON" in message
    assert "--no-mcp-config" in message


@pytest.mark.asyncio
async def test_a_relative_recorded_config_is_read_from_the_launch_directory(monkeypatch, tmp_path):
    """A relative path in the record is taken from where the branch was launched,
    as the search is, not from the directory the resume runs in."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    launched = tmp_path / "launched"
    resumed = tmp_path / "resumed"
    (launched / "cfg").mkdir(parents=True)
    resumed.mkdir()
    (launched / "cfg" / "mcp.json").write_text(json.dumps({"mcpServers": SERVERS}))
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": "cfg/mcp.json", "no_mcp_config": False, "launch_dir": str(launched)},
    )
    monkeypatch.chdir(resumed)

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "follow up", resume=branch_id)

    assert seen["kwargs"]["mcp_servers"] == SERVERS


_GENERATED_PROFILE = "lionagi-mcp-" + "0" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_on_disk", [True, False])
async def test_a_resume_naming_a_removed_generated_codex_profile_is_refused(
    monkeypatch, tmp_path, codex_home, profile_on_disk
):
    """The earlier run gave codex its servers' env through a generated profile,
    which is removed when that run exits. A resume that resolves no new set keeps
    the request it had, so it would name a file that is gone: it is refused
    instead. While the file is there, the leg keeps it."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli._mcp_resolve import McpConfigError

    if profile_on_disk:
        (codex_home.path / f"{_GENERATED_PROFILE}.config.toml").write_text(
            '[mcp_servers.khive.env]\nKHIVE_API_KEY = "x"\n'
        )
    branch_id = _wire_resume_stubs(
        monkeypatch,
        tmp_path,
        "codex",
        "gpt-5.3-codex",
        profile=_GENERATED_PROFILE,
        config_overrides={"mcp_servers.khive.command": "kkernel"},
    )

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)

    if profile_on_disk:
        await agent_mod._run_agent(None, "follow up", resume=branch_id)
        assert seen["kwargs"]["profile"] == _GENERATED_PROFILE
        return

    with pytest.raises(McpConfigError) as raised:
        await agent_mod._run_agent(None, "follow up", resume=branch_id)
    message = str(raised.value)
    assert _GENERATED_PROFILE in message
    assert "--no-mcp-config" in message
    assert seen == {}


@pytest.mark.asyncio
async def test_resumed_antigravity_leg_rejects_an_explicit_server_set(monkeypatch, tmp_path):
    from lionagi.cli.agent import _run_agent

    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "gemini_code", "gemini-3.5-flash")

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
        await _run_agent(
            None,
            "follow up",
            resume=branch_id,
            mcp_config=_mcp_config_file(tmp_path, SERVERS),
        )


# Site 3 — the flow / fanout worker.


@pytest.fixture
def flow_run(monkeypatch, tmp_path):
    """A run launched from a directory whose .mcp.json is the run's server set."""
    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli._runs import RunDir

    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)

    def _allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="test-run",
            state_root=tmp_path / "state",
            artifact_root=tmp_path / "artifacts",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch_mod, "allocate_run", _allocate)
    return launch_dir


async def _codex_orchestration(flow_run, servers: dict):
    from lionagi.cli.orchestrate._orchestration import setup_orchestration

    (flow_run / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return await setup_orchestration(
        pattern_name="Fanout",
        model_spec=CODEX_SPEC,
        agent_name=None,
        save_dir=None,
        cwd=None,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
    )


async def _codex_worker(flow_run, servers: dict):
    from lionagi.cli.orchestrate._orchestration import build_worker_branch

    env = await _codex_orchestration(flow_run, servers)
    # A worker's role resolves through the agent profiles installed on the
    # machine, and a profile's model beats the run's default spec. Without a
    # pin, whichever model `implementer` happens to name locally is the one
    # these codex assertions run against, and the file silently tests another
    # provider. `model_override` is the only input that wins unconditionally.
    branch, _, _, _ = await build_worker_branch(
        env, agent_id="w1", role="implementer", model_override=CODEX_SPEC
    )
    return env, branch


@pytest.mark.asyncio
async def test_the_flow_worker_these_tests_build_is_a_codex_worker(flow_run, codex_home):
    """The worker the two cases below build resolves to the codex provider.

    Agent profiles live outside the repository, so an unpinned worker makes
    this file's provider a property of the machine running it. Asserting the
    provider directly means removing the pin fails here, saying which provider
    was built, rather than further down on an argv shape that no longer applies.
    """
    codex_home.write_config({})
    _env, branch = await _codex_worker(flow_run, SERVERS)

    assert branch.chat_model.endpoint.config.provider == "codex"


@pytest.mark.asyncio
async def test_a_codex_worker_is_spawned_with_the_runs_server_set(flow_run, codex_home):
    """The acceptance case: the `-c mcp_servers.*` arguments on the command line
    a codex worker of a flow run would actually be spawned with."""
    codex_home.write_config({})
    _env, branch = await _codex_worker(flow_run, SERVERS)

    args = await _cmd_args(branch.chat_model)
    forwarded = [args[i + 1] for i, a in enumerate(args) if a == "-c" and i + 1 < len(args)]
    assert 'mcp_servers.khive.command="kkernel"' in forwarded
    assert 'mcp_servers.khive.args=["serve"]' in forwarded


@pytest.mark.asyncio
async def test_a_codex_worker_refuses_secret_fields_it_cannot_carry(flow_run, codex_home):
    """A codex worker ignores the user config, so it loads no generated profile,
    the only carrier of `env` and `http_headers` that keeps them off argv. The
    worker is refused at build, writes no profile of its own and puts no value
    in the message. The orchestrator's own model is not a worker and keeps its
    profile."""
    from lionagi.cli.orchestrate._orchestration import build_worker_branch

    codex_home.write_config({})
    env = await _codex_orchestration(flow_run, SECRET_SERVERS)
    before = codex_home.profile_files()

    with pytest.raises(ConfigurationError, match="ignores the user config") as refused:
        await build_worker_branch(env, agent_id="w1", role="implementer", model_override=CODEX_SPEC)

    assert SECRET not in str(refused.value)
    assert codex_home.profile_files() == before
