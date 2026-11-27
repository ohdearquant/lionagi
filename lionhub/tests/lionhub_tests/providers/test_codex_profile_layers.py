# SPDX-License-Identifier: Apache-2.0

"""Under the permission profile, every layer that can name a profile or a sandbox setting is refused.

The layers are the profile file ``CODEX_HOME/<name>.config.toml``, the ``[profiles.<name>]`` table
in ``config.toml``, and a profile chosen by the request, by a ``profile`` override or by a root
``profile`` key in ``config.toml``. A profile that sets ``sandbox_mode``, ``default_permissions``
or ``permissions`` would be a second sandbox under the one lionagi installs, so the launch refuses.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from lionagi._errors import CLIInputRefusedError
from lionhub.providers.openai import codex as codex_module
from lionhub.providers.openai import codex_sandbox_probe as probe_module
from lionhub.providers.openai._codex_permissions import REQUIRED_ARMS
from lionhub.providers.openai.codex import CodexCodeRequest


def _home(tmp_path: Path, config: str = "", **profile_files: str) -> dict[str, str]:
    """A codex home holding ``config.toml`` and, per keyword, ``<name>.config.toml``."""
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(config)
    for name, text in profile_files.items():
        (home / f"{name}.config.toml").write_text(text)
    return {"CODEX_HOME": str(home)}


@pytest.fixture
def sandbox_on(monkeypatch):
    """The child sandbox on with the default policy (the suite switches it off by default)."""
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})


@pytest.fixture
def passing_binary(tmp_path, monkeypatch):
    """A codex binary with a passing probe record, as the launch's ``CODEX_CLI``."""
    import lionagi._paths as paths

    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path / "lionagi-home")
    directory = tmp_path / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "codex"
    binary.write_text('#!/bin/sh\necho "codex-cli 0.162.1"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    arms = {name: {"passed": True, "evidence": "rc=0 ok"} for name in REQUIRED_ARMS}
    arms["deny_canary"].update(opened=1, of=1)
    probe_module.write_record(str(binary), arms)
    monkeypatch.setattr(codex_module, "CODEX_CLI", str(binary))
    return binary


def _gate(cwd: Path, env: dict[str, str], **kw):
    request = CodexCodeRequest(prompt="hi", repo=cwd, env=env, **kw)
    plain = codex_module._plain_request(request)
    return codex_module._gate_a_profiled_launch(plain, Path(cwd), env)


_PROFILE_FILES = {
    "sandbox_mode": 'sandbox_mode = "workspace-write"\n',
    "default_permissions": 'default_permissions = "wide"\n',
    "permissions": "[permissions.p.network]\nenabled = true\n",
}


@pytest.mark.parametrize("key", sorted(_PROFILE_FILES))
def test_a_profile_file_that_sets_a_sandbox_key_is_refused(
    tmp_path, sandbox_on, passing_binary, key
):
    env = _home(tmp_path, p=_PROFILE_FILES[key])
    with pytest.raises(CLIInputRefusedError, match="profile 'p'"):
        _gate(tmp_path, env, profile="p")


def test_a_profile_file_of_another_name_is_not_consulted(tmp_path, sandbox_on, passing_binary):
    env = _home(tmp_path, p=_PROFILE_FILES["sandbox_mode"])
    assert _gate(tmp_path, env, profile="q") is None


def test_a_root_profile_key_selects_a_profile_table_that_sets_the_sandbox(
    tmp_path, sandbox_on, passing_binary
):
    config = 'profile = "p"\n\n[profiles.p]\nsandbox_mode = "workspace-write"\n'
    with pytest.raises(CLIInputRefusedError, match="profile 'p'"):
        _gate(tmp_path, _home(tmp_path, config))


def test_a_root_profile_key_selects_a_profile_file_that_sets_the_sandbox(
    tmp_path, sandbox_on, passing_binary
):
    env = _home(tmp_path, 'profile = "p"\n', p=_PROFILE_FILES["sandbox_mode"])
    with pytest.raises(CLIInputRefusedError, match="profile 'p'"):
        _gate(tmp_path, env)


def test_a_root_profile_key_that_names_a_harmless_profile_is_not_refused(
    tmp_path, sandbox_on, passing_binary
):
    config = 'profile = "p"\n\n[profiles.p]\nmodel = "gpt-x"\n'
    assert _gate(tmp_path, _home(tmp_path, config)) is None


def test_a_profile_override_naming_a_table_that_sets_the_sandbox_is_refused(
    tmp_path, sandbox_on, passing_binary
):
    config = '[profiles.p]\nsandbox_mode = "workspace-write"\n'
    with pytest.raises(CLIInputRefusedError):
        _gate(tmp_path, _home(tmp_path, config), config_overrides={"profile": "p"})


def test_rendering_the_command_line_reaches_the_same_refusals_as_a_launch(
    tmp_path, sandbox_on, passing_binary
):
    env = _home(tmp_path)
    request = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        env=env,
        config_overrides={"permissions.wide.network.enabled": True},
    )
    with pytest.raises(CLIInputRefusedError, match="nothing else may set its sandbox"):
        request.as_cmd_args()
