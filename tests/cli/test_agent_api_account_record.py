# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The account name a launcher hands a run is recorded in run.json, and a value that is not an
account name is refused before anything is written, without being shown (ADR-0126 Amendment 4)."""

import json

import pytest

from lionagi._errors import ConfigurationError
from lionagi.service.connections.endpoint import Endpoint
from lionhub.providers.anthropic.messages import AnthropicMessagesEndpoint
from tests.cli import test_agent_api_provider as api_provider
from tests.cli.test_agent_context_from import _wire_agent_stubs
from tests.cli.test_agent_run_json_error import _run
from tests.service.accounts import test_routed_endpoint as routed

# The scripted API run fixture: drives `_run_agent` against a stubbed Anthropic endpoint.
scripted_run = api_provider.leg


@pytest.mark.asyncio
async def test_the_launchers_account_name_lands_in_run_json(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    _settings(monkeypatch, tmp_path)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "acct-b")
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    assert json.loads(run.manifest_path.read_text())["api_account"] == "acct-b"


@pytest.mark.asyncio
async def test_without_the_variable_run_json_names_no_account(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    _settings(monkeypatch, tmp_path)
    monkeypatch.delenv("LIONAGI_API_ACCOUNT", raising=False)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    assert "api_account" not in json.loads(run.manifest_path.read_text())


def _real_runs_root(monkeypatch, tmp_path):
    """The real allocate_run under a scratch root, so a run that gets that far leaves run.json."""
    from lionagi.cli import _runs

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    return runs_root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["team_b-1", "a" * 32, "ask-an1"], ids=["name", "32 characters", "near the key marker"]
)
async def test_an_accepted_name_reaches_the_real_run_json(monkeypatch, tmp_path, value):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    _settings(monkeypatch, tmp_path)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", value)
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    (manifest,) = runs_root.rglob("run.json")
    assert json.loads(manifest.read_text())["api_account"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-api03-" + "x" * 90,
        "sk-short",
        "ask-ant1",
        "team-sk-ant",
        "Xy7q" * 10,
        "Team-B",
        "team.b",
        "team@b",
        "0team",
        "-team",
        "_team",
        "a" * 33,
        "two words",
        "",
    ],
    ids=[
        "key",
        "key prefix",
        "key marker inside",
        "key marker at the end",
        "mixed case",
        "capital",
        "dot",
        "at",
        "digit first",
        "dash first",
        "underscore first",
        "too long",
        "space",
        "empty",
    ],
)
async def test_a_value_that_is_not_an_account_name_is_refused_without_being_shown(
    monkeypatch, tmp_path, value
):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    _settings(monkeypatch, tmp_path)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", value)
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError, match="LIONAGI_API_ACCOUNT") as info:
        await _run_agent("claude/sonnet", "go")

    if value:
        assert value not in str(info.value)
    assert not runs_root.exists() or not list(runs_root.rglob("run.json"))


# A stand-in for a key that the name rule admits: lowercase hex after a letter.
_STAND_IN = "f3a9c27e1b84d56a"
_TEAM_B = [{"name": "team-b", "key_env": "ACCT_B", "budget_usd": "10"}]


def _settings(monkeypatch, tmp_path, *, accounts=(), dotenv=None):
    """Settings declaring ``accounts``, built from one dotenv file holding ``dotenv`` if given and
    from no dotenv file otherwise, so no test reads a file outside its own directory."""
    from lionagi import config
    from lionagi.config import AppSettings

    env_file = None
    if dotenv is not None:
        env_file = tmp_path / "account.env"
        env_file.write_text(dotenv, encoding="utf-8")
    settings = AppSettings(_env_file=env_file, ANTHROPIC_ACCOUNTS=list(accounts))
    monkeypatch.setattr(config, "settings", settings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder", "accounts", "in_dotenv"),
    [
        ("EXAMPLE_API_KEY", (), False),
        ("ACCT_B", _TEAM_B, False),
        ("EXAMPLE_API_KEY", (), True),
        ("ACCT_B", _TEAM_B, True),
        ("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY", (), False),
        ("webhooksigningkey", (), True),
        ("designing_key", (), False),
        ("codesigningkey", (), False),
        ("nodesigning_key", (), True),
    ],
    ids=[
        "credential variable",
        "configured account variable",
        "credential in dotenv",
        "configured account in dotenv",
        "hmac key in environment",
        "signing key in dotenv",
        "designing substring in environment",
        "code signing key in environment",
        "node signing key in dotenv",
    ],
)
async def test_a_key_the_process_holds_is_refused_without_being_shown(
    monkeypatch, tmp_path, holder, accounts, in_dotenv
):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    if in_dotenv:
        _settings(monkeypatch, tmp_path, accounts=accounts, dotenv=f"{holder}={_STAND_IN}\n")
    else:
        _settings(monkeypatch, tmp_path, accounts=accounts)
        monkeypatch.setenv(holder, _STAND_IN)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", _STAND_IN)
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError, match=f"value of {holder};") as info:
        await _run_agent("claude/sonnet", "go")

    assert _STAND_IN not in str(info.value)
    assert not runs_root.exists() or not list(runs_root.rglob("run.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder", "value", "in_dotenv"),
    [
        ("EXAMPLE_API_KEY", "a-different-value", False),
        ("EXAMPLE_TEAM", "team-b", False),
        ("EXAMPLE_TEAM", "team-b", True),
    ],
    ids=[
        "credential holds another value",
        "a plain variable holds the same value",
        "a plain dotenv entry holds the same value",
    ],
)
async def test_a_name_that_is_no_held_key_is_recorded(
    monkeypatch, tmp_path, holder, value, in_dotenv
):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    if in_dotenv:
        _settings(monkeypatch, tmp_path, accounts=_TEAM_B, dotenv=f"{holder}={value}\n")
    else:
        _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
        monkeypatch.setenv(holder, value)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "team-b")
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    (manifest,) = runs_root.rglob("run.json")
    assert json.loads(manifest.read_text())["api_account"] == "team-b"


@pytest.mark.asyncio
async def test_an_unreadable_dotenv_file_refuses_the_name_without_quoting_it(monkeypatch, tmp_path):
    import pydantic_settings

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="ok")
    _settings(monkeypatch, tmp_path, dotenv="EXAMPLE_TEAM=team-b\n")

    def unreadable(*args, **kwargs):
        raise ValueError(f"line 1 holds {_STAND_IN}")

    monkeypatch.setattr(pydantic_settings, "DotEnvSettingsSource", unreadable)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "team-b")
    runs_root = _real_runs_root(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError, match="could not be checked") as info:
        await _run_agent("claude/sonnet", "go")

    assert _STAND_IN not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert not runs_root.exists() or not list(runs_root.rglob("run.json"))


def _tool_loop_replies():
    """The replies of a run that reads one file, has nothing more to read, and then answers."""
    read_the_notes = api_provider._reply(
        {
            "analysis": "read the notes first",
            "action_requests": [
                {"function": "reader", "arguments": {"action": "read", "path": "notes.txt"}}
            ],
            "action_strategy": "sequential",
            "extension_needed": True,
            "reason": {"title": "read", "content": "the answer is in the file"},
        }
    )
    nothing_more_to_read = api_provider._reply(
        {
            "analysis": "the file says hello",
            "action_requests": [],
            "action_strategy": "sequential",
            "extension_needed": False,
            "reason": {"title": "done", "content": "nothing left to read"},
        }
    )
    return [read_the_notes, nothing_more_to_read, api_provider._answer("the notes say hello")]


async def _api_run_manifest(scripted_run, monkeypatch, tmp_path, *, accounts_declared):
    """Run a tool-using API run to its answer and return the run.json it left.

    With ``accounts_declared`` the Anthropic endpoint sends through the account router against a
    stubbed transport; without it the endpoint sends with the direct key against scripted replies.
    """
    replies = _tool_loop_replies()
    try:
        if accounts_declared:
            routed._install_accounts(monkeypatch, tmp_path)
            for reply in replies:
                reply["model"] = "claude-sonnet-5-5"
            transport = routed.StubTransport([routed.StubResponse(body=reply) for reply in replies])

            async def through_the_router(self, request, *args, **kwargs):
                return await Endpoint.call(self, request, *args, **kwargs)

            monkeypatch.setattr(AnthropicMessagesEndpoint, "call", through_the_router)
            monkeypatch.setattr(
                AnthropicMessagesEndpoint,
                "_create_http_session",
                lambda self, *args, **kwargs: transport.session(*args, **kwargs),
            )
            monkeypatch.setattr(
                AnthropicMessagesEndpoint, "_assert_ssrf_safe_url", lambda self: None
            )
        else:
            routed._install_unconfigured(monkeypatch, tmp_path)
            scripted_run.replies = replies
        await scripted_run.run(
            api_provider._profile(api_tools="read", max_input_tokens=50000), "read notes"
        )
    finally:
        routed._cleanup_router()
    (path,) = (tmp_path / "runs").glob("*/run.json")
    return json.loads(path.read_text())


@pytest.mark.asyncio
async def test_a_routed_run_json_does_not_name_an_account_the_router_never_used(
    scripted_run, monkeypatch, tmp_path
):
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "acct-b")

    manifest = await _api_run_manifest(scripted_run, monkeypatch, tmp_path, accounts_declared=True)

    assert "api_account" not in manifest
    usage = manifest["api_usage"]
    assert usage["account_labels"] == [{"provider": "anthropic", "label": "primary"}]
    paid = [
        attempt["account_label"]
        for row in usage["requests"]
        for attempt in row["selected_attempts"]
    ]
    assert paid and set(paid) == {"primary"}


@pytest.mark.asyncio
async def test_without_the_variable_a_routed_run_json_names_no_account(
    scripted_run, monkeypatch, tmp_path
):
    monkeypatch.delenv("LIONAGI_API_ACCOUNT", raising=False)

    manifest = await _api_run_manifest(scripted_run, monkeypatch, tmp_path, accounts_declared=True)

    assert "api_account" not in manifest
    assert manifest["api_usage"]["account_labels"] == [
        {"provider": "anthropic", "label": "primary"}
    ]


@pytest.mark.asyncio
async def test_an_api_run_without_declared_accounts_keeps_the_launchers_account_name(
    scripted_run, monkeypatch, tmp_path
):
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "acct-b")

    manifest = await _api_run_manifest(scripted_run, monkeypatch, tmp_path, accounts_declared=False)

    assert manifest["api_account"] == "acct-b"
    assert manifest["api_usage"]["account_labels"] == []
