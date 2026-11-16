# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""No CLI child is handed the Studio daemon's bearer or a remote Studio's token.

With either one a child could ask a daemon to launch further runs, here or on another
machine. The spawn removes them whatever environment the caller passed, and the
command-environment scrub removes the remote tokens from tool and MCP server
environments too.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

import pytest

from lionagi._command_env import scrub_command_env
from lionhub.providers._cli_subprocess import ndjson_from_cli

TOKEN = "example-bearer-value"  # noqa: S105 — a test value, never a real credential
_NAMES = ("LIONAGI_STUDIO_AUTH_TOKEN", "LIONAGI_REMOTE_LEGBOX_TOKEN", "lionagi_remote_other")
_PRINT_ENV = (
    "import json, os, sys; "
    "names = json.loads(sys.argv[1]); "
    "sys.stdout.write(json.dumps({'seen': sorted(n for n in names if n in os.environ), "
    "'control': os.environ.get('LIONAGI_TEST_CONTROL')}) + '\\n')"
)


async def _child_sees(**kwargs) -> dict:
    stream = ndjson_from_cli(
        [sys.executable, "-c", _PRINT_ENV, json.dumps([*_NAMES])], engine="codex", **kwargs
    )
    async with contextlib.aclosing(stream):
        [line] = [obj async for obj in stream]
    return line


@pytest.mark.asyncio
async def test_an_inherited_studio_credential_does_not_reach_the_child(monkeypatch):
    for name in _NAMES:
        monkeypatch.setenv(name, TOKEN)
    monkeypatch.setenv("LIONAGI_TEST_CONTROL", "kept")

    seen = await _child_sees()

    assert seen == {"seen": [], "control": "kept"}


@pytest.mark.asyncio
async def test_a_studio_credential_in_a_passed_environment_does_not_reach_the_child():
    env = {**os.environ, **dict.fromkeys(_NAMES, TOKEN), "LIONAGI_TEST_CONTROL": "kept"}

    seen = await _child_sees(env=env)

    assert seen == {"seen": [], "control": "kept"}


def test_the_command_scrub_removes_remote_tokens():
    parent = {
        "LIONAGI_REMOTE_LEGBOX_TOKEN": TOKEN,
        "LIONAGI_STUDIO_AUTH_TOKEN": TOKEN,
        "PATH": "/bin",
    }
    assert scrub_command_env(parent, declared_names=()) == {"PATH": "/bin"}


# ── Every other credential: a child keeps its own provider's login and nothing else ──────────

_CREDENTIALS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GEMINI_API_KEY",
    "DATABASE_URL",
    "stripe_secret",
    "SERVICE_PASSWORD",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SIGNING_KEY",
    "NPMTOKEN",
    "SSH_AUTH_SOCK",
    "DOCKER_AUTH_CONFIG",
    "REGISTRY_BASIC_AUTH",
    "OAUTH_CLIENT",
    "GITHUB_PAT",
)
#: Names a substring rule would wrongly take; a child must still get them.
_LOOKALIKES = (
    "TOKENIZERS_PARALLELISM",
    "KEYCHAIN_PATH",
    "MONKEYPATCH_MODE",
    "GIT_AUTHOR_NAME",
    "XAUTHORITY",
    "MANPATH",
    "COMPAT_MODE",
)
_PRINT_NAMES = (
    "import json, os, sys; "
    "names = json.loads(sys.argv[1]); "
    "sys.stdout.write(json.dumps({'seen': sorted(n for n in names if n in os.environ)}) + '\\n')"
)


async def _names_seen(names, **kwargs) -> list[str]:
    stream = ndjson_from_cli(
        [sys.executable, "-c", _PRINT_NAMES, json.dumps(list(names))], engine="codex", **kwargs
    )
    async with contextlib.aclosing(stream):
        [line] = [obj async for obj in stream]
    return line["seen"]


def _set_all(monkeypatch, names) -> None:
    for name in names:
        monkeypatch.setenv(name, TOKEN)


@pytest.mark.asyncio
async def test_a_child_keeps_only_the_named_auth_variables(monkeypatch):
    _set_all(monkeypatch, (*_CREDENTIALS, *_LOOKALIKES, "LIONAGI_TEST_CONTROL"))
    allowed = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"})

    seen = await _names_seen(
        (*_CREDENTIALS, *_LOOKALIKES, "LIONAGI_TEST_CONTROL"), auth_env=allowed
    )

    assert seen == sorted({*allowed, *_LOOKALIKES, "LIONAGI_TEST_CONTROL"})


@pytest.mark.asyncio
async def test_no_auth_names_means_every_credential_is_withheld(monkeypatch):
    """The default is the strict one: a caller that names nothing passes nothing."""
    _set_all(monkeypatch, (*_CREDENTIALS, "LIONAGI_TEST_CONTROL"))

    assert await _names_seen((*_CREDENTIALS, "LIONAGI_TEST_CONTROL")) == ["LIONAGI_TEST_CONTROL"]


@pytest.mark.asyncio
async def test_a_passed_environment_is_stripped_the_same_way():
    env = {**os.environ, **dict.fromkeys(_CREDENTIALS, TOKEN), "LIONAGI_TEST_CONTROL": "kept"}

    seen = await _names_seen(
        (*_CREDENTIALS, "LIONAGI_TEST_CONTROL"), env=env, auth_env=frozenset({"GEMINI_API_KEY"})
    )

    assert seen == ["GEMINI_API_KEY", "LIONAGI_TEST_CONTROL"]


@pytest.mark.asyncio
async def test_an_allowed_name_never_admits_a_studio_credential(monkeypatch):
    _set_all(monkeypatch, _NAMES)

    assert await _names_seen(_NAMES, auth_env=frozenset(_NAMES)) == []


@pytest.mark.asyncio
async def test_the_process_group_marker_survives_the_strip():
    """The spawn's own group marker reads like a token; losing it would orphan the child."""
    from lionagi.ln._proc import _PROCESS_GROUP_MARKER

    assert await _names_seen((_PROCESS_GROUP_MARKER,)) == [_PROCESS_GROUP_MARKER]


def test_a_cloud_backend_adds_its_names_only_when_selected():
    from lionhub.providers._cli_subprocess import CLAUDE_CODE_AUTH_ENV, claude_code_auth_env

    assert claude_code_auth_env({}) == CLAUDE_CODE_AUTH_ENV
    assert claude_code_auth_env({"CLAUDE_CODE_USE_BEDROCK": "0"}) == CLAUDE_CODE_AUTH_ENV
    bedrock = claude_code_auth_env({"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert {"AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} <= bedrock
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in bedrock
    assert "GOOGLE_APPLICATION_CREDENTIALS" in claude_code_auth_env({"CLAUDE_CODE_USE_VERTEX": "1"})


@pytest.mark.asyncio
async def test_a_claude_child_gets_its_login_and_no_other_credential(tmp_path, monkeypatch):
    """Launched through the Claude Code provider itself, with a stub ``claude`` on the path."""
    from lionhub.providers.anthropic import claude_code as cc

    names = (*_CREDENTIALS, *_NAMES, "LIONAGI_TEST_CONTROL")
    stub = tmp_path / "stub-cli"
    stub.write_text(
        f"#!{sys.executable} -I\n"
        "import json, os\n"
        f"names = {list(names)!r}\n"
        "seen = sorted(n for n in names if n in os.environ)\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': json.dumps(seen)}))\n"
    )
    stub.chmod(0o755)
    _set_all(monkeypatch, names)
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: str(stub))

    request = cc.ClaudeCodeRequest(prompt="hi", repo=tmp_path)
    async with contextlib.aclosing(cc._ndjson_from_cli(request)) as stream:
        results = [obj async for obj in stream if obj.get("type") == "result"]

    assert json.loads(results[-1]["result"]) == [
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "LIONAGI_TEST_CONTROL",
    ]


def _capture_spawn_kwargs(monkeypatch, module) -> dict:
    """Replace a provider's spawn with one that records its keyword arguments and yields nothing."""
    captured: dict = {}

    async def fake(cmd, **kwargs):
        captured.update(kwargs)
        return
        yield

    monkeypatch.setattr(module, "ndjson_from_cli", fake)
    return captured


async def _drain(stream) -> None:
    async with contextlib.aclosing(stream):
        async for _ in stream:
            pass


@pytest.mark.asyncio
async def test_each_provider_names_its_own_login_to_the_spawn(tmp_path, monkeypatch):
    from lionhub.providers._cli_subprocess import GEMINI_AUTH_ENV
    from lionhub.providers.google import gemini_code
    from lionhub.providers.openai import codex
    from lionhub.providers.pi import cli as pi

    monkeypatch.setattr(gemini_code, "AGY_CLI", "agy")
    seen = _capture_spawn_kwargs(monkeypatch, gemini_code)
    await _drain(
        gemini_code._ndjson_from_cli(gemini_code.GeminiCodeRequest(prompt="hi", repo=tmp_path))
    )
    assert seen["auth_env"] == GEMINI_AUTH_ENV

    monkeypatch.setattr(pi, "PI_CLI", "pi")
    monkeypatch.setattr(pi, "_refuse_while_the_child_sandbox_is_enabled", lambda: None)
    seen = _capture_spawn_kwargs(monkeypatch, pi)
    request = pi.PiCodeRequest(prompt="hi", provider="openrouter", workload="heavy", repo=tmp_path)
    await _drain(pi._ndjson_from_cli(request))
    assert seen["auth_env"] == frozenset({"OPENROUTER_API_KEY"})

    monkeypatch.setattr(codex, "CODEX_CLI", "codex")
    seen = _capture_spawn_kwargs(monkeypatch, codex)
    await _drain(codex._ndjson_from_cli(codex.CodexCodeRequest(prompt="hi", repo=tmp_path)))
    # Codex names its login against the child's environment after the secret fill.
    codex_auth_env = seen["auth_env"]({"HOME": str(tmp_path)})
    assert "OPENAI_API_KEY" in codex_auth_env
    assert "GH_TOKEN" not in codex_auth_env


@pytest.mark.asyncio
async def test_a_withheld_value_stays_redacted_when_the_command_is_built_late(monkeypatch):
    """A provider that renders its command against the final environment re-reads the redactor's
    values; a credential withheld from the child is still one of them."""
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    leak = f"import sys; sys.stderr.write('child saw ' + {TOKEN!r}); sys.exit(3)"

    stream = ndjson_from_cli(
        [sys.executable], engine="codex", _prepare_argv=lambda env: [sys.executable, "-c", leak]
    )
    with pytest.raises(RuntimeError) as raised:
        await _drain(stream)

    assert "child saw" in str(raised.value)
    assert TOKEN not in str(raised.value)
