# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A codex child keeps the key variables its model providers name and no other credential.

Codex reads a key from the variable a model provider's ``env_key`` names. The child keeps
``OPENAI_API_KEY``, every ``CODEX_*`` name, and every ``env_key`` the user config or the run's
``-c`` overrides define; every other credential-named variable is removed before the spawn.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

from lionhub.providers.openai import codex

TOKEN = "example-credential-value"  # noqa: S105 — a test value, never a real credential
_CONFIG = """
[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://example.invalid/v1"
env_key = "OPENROUTER_API_KEY"
"""


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _plain(tmp_path: Path, **fields) -> codex._PlainRequest:
    return codex._plain_request(codex.CodexCodeRequest(prompt="hi", repo=tmp_path, **fields))


def test_the_keep_set_reads_the_user_config_the_overrides_and_codex_names(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", TOKEN)
    plain = _plain(
        tmp_path,
        config_overrides={
            "model_providers.local.env_key": "LOCAL_LLM_KEY",
            "model_providers.lab": {"name": "Lab", "env_key": "LAB_TOKEN"},
            "model": "gpt-5.3-codex-spark",
        },
    )

    keep = codex._codex_auth_env(plain, os.environ)

    assert {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENROUTER_API_KEY",
        "LOCAL_LLM_KEY",
        "LAB_TOKEN",
    } <= keep
    assert "GH_TOKEN" not in keep


def test_an_unreadable_user_config_names_no_key(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (codex_home / "config.toml").write_text("[model_providers.openrouter\n")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    assert "OPENROUTER_API_KEY" not in codex._codex_auth_env(_plain(tmp_path), os.environ)


@pytest.mark.asyncio
async def test_a_codex_child_gets_its_provider_key_and_no_other_credential(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launched through the Codex provider itself, with a stub ``codex`` on the path."""
    names = ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
    for name in names:
        monkeypatch.setenv(name, TOKEN)
    monkeypatch.setenv("LIONAGI_TEST_CONTROL", "kept")
    stub = tmp_path / "stub-cli"
    stub.write_text(
        f"#!{sys.executable} -I\n"
        "import json, os\n"
        f"names = {[*names, 'LIONAGI_TEST_CONTROL']!r}\n"
        "print(json.dumps({'seen': sorted(n for n in names if n in os.environ)}))\n"
    )
    stub.chmod(0o755)
    monkeypatch.setattr(codex, "CODEX_CLI", str(stub))

    request = codex.CodexCodeRequest(prompt="hi", repo=tmp_path)
    async with contextlib.aclosing(codex._ndjson_from_cli(request)) as stream:
        lines = [obj async for obj in stream]

    assert lines == [{"seen": ["LIONAGI_TEST_CONTROL", "OPENAI_API_KEY", "OPENROUTER_API_KEY"]}]
    assert json.dumps(lines).count(TOKEN) == 0


@pytest.mark.asyncio
async def test_a_codex_home_the_secret_lookup_supplies_is_kept_and_its_config_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keep set is read from the child's environment after the fill, not before it."""
    from lionhub.providers import _cli_subprocess

    home = tmp_path / "filled-codex-home"
    home.mkdir()
    (home / "config.toml").write_text(_CONFIG)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", TOKEN)

    async def fill(env, **_):
        return {**(os.environ if env is None else env), "CODEX_HOME": str(home)}, ("CODEX_HOME",)

    monkeypatch.setattr(_cli_subprocess, "fill_declared_secrets_and_names", fill)
    names = ("CODEX_HOME", "OPENROUTER_API_KEY")
    stub = tmp_path / "stub-cli"
    stub.write_text(
        f"#!{sys.executable} -I\n"
        "import json, os\n"
        f"names = {list(names)!r}\n"
        "print(json.dumps({'seen': sorted(n for n in names if n in os.environ)}))\n"
    )
    stub.chmod(0o755)
    monkeypatch.setattr(codex, "CODEX_CLI", str(stub))

    repo = tmp_path / "repo"
    repo.mkdir()
    request = codex.CodexCodeRequest(prompt="hi", repo=repo)
    async with contextlib.aclosing(codex._ndjson_from_cli(request)) as stream:
        lines = [obj async for obj in stream]

    assert lines == [{"seen": ["CODEX_HOME", "OPENROUTER_API_KEY"]}]


def test_a_later_override_that_replaces_a_provider_drops_its_earlier_env_key(
    codex_home: Path, tmp_path: Path
) -> None:
    plain = _plain(
        tmp_path,
        config_overrides={
            "model_providers.lab.env_key": "OLD_LAB_TOKEN",
            "model_providers.lab": {"name": "Lab", "env_key": "NEW_LAB_TOKEN"},
        },
    )

    keep = codex._codex_auth_env(plain, os.environ)

    assert "NEW_LAB_TOKEN" in keep
    assert "OLD_LAB_TOKEN" not in keep
