# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A config override key is read two ways, and the permission profile's refusals check both.

The command-line layer splits a key at every ``.``, the way codex's loader is documented to read
it. The key-path reader reads it as a TOML dotted key, where a part may be quoted, spaced around
its dots or written with escapes. A guarded key (``sandbox_mode``, ``default_permissions``,
``permissions.*``, ``profile``, ``sandbox_workspace_write.*``) named by either reading is refused.
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


def _home(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text("")
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
    probe_module.write_record(str(binary), arms)
    monkeypatch.setattr(codex_module, "CODEX_CLI", str(binary))
    return binary


def _plain(cwd: Path, env: dict[str, str], **kw):
    request = CodexCodeRequest(prompt="hi", repo=cwd, env=env, **kw)
    return codex_module._plain_request(request)


def _gate(cwd: Path, env: dict[str, str], **kw):
    return codex_module._gate_a_profiled_launch(_plain(cwd, env, **kw), Path(cwd), env)


GUARDED_KEYS = [
    pytest.param('"sandbox_mode"', "workspace-write", id="quoted-sandbox_mode"),
    pytest.param('"default_permissions"', "wide", id="quoted-default_permissions"),
    pytest.param("permissions . wide . network . enabled", True, id="spaced-permissions-path"),
    pytest.param(
        "sandbox_workspace_write . writable_roots", ["/tmp/x"], id="spaced-writable_roots"
    ),
    pytest.param('"sandbox\\u005fmode"', "workspace-write", id="unicode-escaped-part"),
]


@pytest.mark.parametrize(("key", "value"), GUARDED_KEYS)
def test_a_guarded_key_written_the_other_way_is_refused(
    tmp_path, sandbox_on, passing_binary, key, value
):
    with pytest.raises(CLIInputRefusedError, match="nothing else may set its sandbox"):
        _gate(tmp_path, _home(tmp_path), config_overrides={key: value})


def test_a_quoted_key_that_names_nothing_guarded_is_not_refused(
    tmp_path, sandbox_on, passing_binary
):
    assert _gate(tmp_path, _home(tmp_path), config_overrides={'"model"': "gpt-x"}) is None


def test_the_sandbox_off_refusal_reads_a_quoted_key_too(tmp_path, sandbox_on):
    plain = _plain(
        tmp_path, _home(tmp_path), config_overrides={'"sandbox_mode"': "danger-full-access"}
    )
    with pytest.raises(CLIInputRefusedError, match="danger-full-access"):
        codex_module._refuse_sandbox_off_while_confined(plain, tmp_path)


def test_the_decoded_reading_is_the_toml_one_and_only_where_it_differs(tmp_path, sandbox_on):
    plain = _plain(
        tmp_path,
        _home(tmp_path),
        config_overrides={
            "model": "gpt-x",
            '"sandbox_mode"': "workspace-write",
            "permissions . wide . network . enabled": True,
        },
    )
    decoded = codex_module._command_line_layer_decoded(plain)
    assert decoded == {
        "sandbox_mode": "workspace-write",
        "permissions": {"wide": {"network": {"enabled": True}}},
    }
