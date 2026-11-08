# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent` on an Anthropic API model moves to the subscription route when the account router
refuses the run's first request.

The router refuses before anything is sent when every configured account is blocked or out of
budget. A run whose first request is refused that way continues on `claude_code/<model>`, at
most once, and says so on the console and in run.json. A refusal after a reply, any other router
code and any other provider keep failing with the routing error. The endpoint's call and the
subscription route's turn are scripted here, so no request leaves the process.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lionagi._errors import ExecutionError
from lionagi.service._api_telemetry import record_selected_attempt
from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode
from lionhub.orchestration.console import _WARN_LOGGER_NAME
from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest
from lionhub.providers.anthropic.messages import AnthropicMessagesEndpoint
from tests.cli import test_agent_api_provider as provider
from tests.cli.test_agent_api_account_record import _TEAM_B, _settings

leg = provider.leg

ANSWER = "answered on the subscription route"
FALLBACK_MODEL = "claude_code/claude-haiku-5-5"
_PROFILE = provider._profile


def _cli_args(kwargs: dict) -> list[str]:
    """The command line the Claude Code CLI would be started with for a model's request kwargs."""
    return ClaudeCodeRequest(prompt="x", **kwargs).as_cmd_args()


def _flag_value(args: list[str], flag: str) -> str | None:
    """The value after *flag*, None when the flag is absent."""
    return args[args.index(flag) + 1] if flag in args else None


@pytest.fixture
def subscription(monkeypatch):
    """The subscription route's turn: what `Branch.operate` is handed and what it answers."""
    from lionagi import Branch

    seen = SimpleNamespace(calls=[])

    async def operate(self, instruction=None, **kw):
        config = self.chat_model.endpoint.config
        args = _cli_args(config.kwargs)
        seen.calls.append(
            SimpleNamespace(
                instruction=instruction,
                provider=config.provider,
                model=config.kwargs.get("model"),
                cli_args=args,
                cli_tools=_flag_value(args, "--tools"),
            )
        )
        return ANSWER

    monkeypatch.setattr(Branch, "operate", operate)
    return seen


@pytest.fixture
def console_warnings():
    """What `warn()` printed, without depending on propagation to the root logger."""
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger(_WARN_LOGGER_NAME)
    handler = _Collect()
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def _refuse(monkeypatch, leg, code, *, account=None, after_selecting_an_account=False):
    """Script the endpoint: it answers each queued reply, then the router refuses the request."""

    async def call(self, request, *args, **kwargs):
        leg.payloads.append(request)
        if leg.replies:
            return leg.replies.pop(0)
        if after_selecting_an_account:
            record_selected_attempt(
                attempt_id=str(uuid4()), provider="anthropic", account_label="team-b"
            )
        raise AccountRoutingError(code, account=account, attempt_id=uuid4())

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)


def _manifest(tmp_path) -> dict:
    (path,) = (tmp_path / "runs").rglob("run.json")
    return json.loads(path.read_text())


def _fallback_notes(console_warnings) -> list[str]:
    return [line for line in console_warnings if "refused the first request" in line]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", [RouterErrorCode.BUDGET_EXHAUSTED, RouterErrorCode.ACCOUNT_BLOCKED]
)
async def test_a_first_request_refusal_continues_on_the_subscription_route(
    leg, subscription, console_warnings, monkeypatch, tmp_path, code
):
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, code)

    result, provider_used, _, status, _ = await leg.run(_PROFILE(api_tools="read"))

    assert (result, provider_used, status) == (ANSWER, "claude_code", "completed")
    assert len(leg.payloads) == 1, "the refused request is the only one the API route saw"
    (turn,) = subscription.calls
    assert (turn.provider, turn.model) == ("claude_code", "claude-haiku-5-5")
    assert "what do the notes say?" in turn.instruction
    manifest = _manifest(tmp_path)
    assert manifest["provider"] == "claude_code"
    assert manifest["model"] == FALLBACK_MODEL
    assert manifest["status"] == "completed"
    assert manifest["subscription_fallback"] == {
        "original_model": provider.MODEL,
        "model_used": FALLBACK_MODEL,
        "refusal_code": code.value,
        "tools": "Read,Grep,Glob",
        "sandbox_dropped": False,
    }
    assert not {"prompt_caching", "tool_result_max_chars", "context_fold"} & set(manifest)
    (note,) = _fallback_notes(console_warnings)
    assert code.value in note and FALLBACK_MODEL in note
    assert "api_launch" not in leg.branch.metadata
    assert leg.branch.chat_model.endpoint.config.provider == "claude_code"
    assert leg.branch.parse_model is leg.branch.chat_model


@pytest.mark.asyncio
async def test_a_read_set_run_continues_with_only_the_read_tools(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """The API route gave the read set's tools and nothing that writes; the subscription route's
    CLI keeps every built-in tool unless the request names some, so the request names the reads."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read"))

    (turn,) = subscription.calls
    assert turn.cli_tools == "Read,Grep,Glob"
    (note,) = _fallback_notes(console_warnings)
    assert "sandbox" not in note
    record = _manifest(tmp_path)["subscription_fallback"]
    assert record["tools"] == "Read,Grep,Glob"
    assert record["sandbox_dropped"] is False


@pytest.mark.asyncio
async def test_a_profile_that_names_its_own_tools_keeps_them_on_the_subscription_route(
    leg, subscription, monkeypatch, tmp_path
):
    """The API route refuses a profile's `tools` when the run launches, so the first model is let
    through here to reach the switch with the profile's own list in hand."""
    import lionagi.cli.agent as agent_mod

    build_chat_model = agent_mod.build_chat_model

    def launch_then_switch(provider_name, *args, **kwargs):
        if provider_name == "anthropic":
            kwargs["tool_settings"] = None
        return build_chat_model(provider_name, *args, **kwargs)

    monkeypatch.setattr(agent_mod, "build_chat_model", launch_then_switch)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read", tools=["Read", "Grep"]))

    (turn,) = subscription.calls
    assert turn.cli_tools == "Read,Grep"


@pytest.fixture
def seatbelt(monkeypatch, tmp_path):
    """The macOS sandbox the write set needs is reported available, as on a Mac."""
    from lionagi.tools import _seatbelt

    monkeypatch.setattr(_seatbelt, "seatbelt_available", lambda: True)
    monkeypatch.setattr(
        _seatbelt, "_uv_cache_dir", lambda *, _command_env_names=None: tmp_path / "uv-cache"
    )


@pytest.mark.asyncio
async def test_a_write_set_run_continues_with_the_tools_its_api_set_had(
    leg, subscription, seatbelt, monkeypatch, tmp_path
):
    """The API write set had a reader, search, an editor and bash; the switched leg names the
    built-ins that stand for them, and run.json records that and the dropped sandbox."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="write"))

    (turn,) = subscription.calls
    assert turn.cli_tools == "Read,Grep,Glob,Edit,Write,Bash"
    record = _manifest(tmp_path)["subscription_fallback"]
    assert record["tools"] == "Read,Grep,Glob,Edit,Write,Bash"
    assert record["sandbox_dropped"] is True


@pytest.mark.asyncio
async def test_a_write_set_run_is_told_the_subscription_route_has_no_sandbox(
    leg, subscription, seatbelt, console_warnings, monkeypatch, tmp_path
):
    """The write set's editor and bash ran under the macOS sandbox on the API route; nothing on
    the subscription route confines them, and the one warning line says so."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    result, provider_used, _, status, _ = await leg.run(_PROFILE(api_tools="write"))

    assert (result, provider_used, status) == (ANSWER, "claude_code", "completed")
    (note,) = _fallback_notes(console_warnings)
    assert FALLBACK_MODEL in note
    assert "macOS sandbox" in note and "no filesystem sandbox" in note


@pytest.mark.asyncio
async def test_the_switched_run_is_handed_no_mcp_server(leg, subscription, monkeypatch, tmp_path):
    """The API leg was handed no MCP server. A server the project configures is not handed to the
    switched leg either, and the CLI is kept from loading a config of its own."""
    (leg.workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"notes": {"command": "notes-server"}}})
    )
    monkeypatch.chdir(leg.workspace)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read"))

    (turn,) = subscription.calls
    assert "--strict-mcp-config" in turn.cli_args
    assert json.loads(_flag_value(turn.cli_args, "--mcp-config")) == {"mcpServers": {}}


@pytest.mark.asyncio
async def test_the_session_rows_name_the_route_that_ran(leg, subscription, monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    import lionhub.orchestration.runs as runs_mod
    from lionagi.state.db import StateDB

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    contexts = []

    async def setup(branch, **kw):
        ctx = await runs_mod.setup_agent_persist(branch, **kw)
        contexts.append(ctx)
        return ctx

    async def teardown(ctx, *, status="completed", **kw):
        return await runs_mod.teardown_agent_persist(ctx, status=status, **kw)

    monkeypatch.setattr(agent_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    _, _, branch_id, _, _ = await leg.run(_PROFILE(api_tools="read"))

    async with StateDB() as db:
        session = await db.get_session(contexts[0]["session_id"])
        branch_row = await db.get_branch(branch_id)
    for row in (session, branch_row):
        assert (row["provider"], row["model"]) == ("claude_code", FALLBACK_MODEL)


@pytest.mark.asyncio
async def test_a_refusal_after_a_reply_still_fails_with_the_routing_error(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    leg.replies = [provider._read_round("notes.txt")]
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    assert isinstance(info.value.__cause__, AccountRoutingError)
    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert len(leg.payloads) == 2
    assert subscription.calls == []
    manifest = _manifest(tmp_path)
    assert (manifest["provider"], manifest["model"]) == ("anthropic", provider.MODEL)
    assert manifest["status"] == "failed"
    assert "subscription_fallback" not in manifest
    assert _fallback_notes(console_warnings) == []


@pytest.mark.asyncio
async def test_a_refusal_after_the_router_chose_an_account_still_fails(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """Settling a reply can also raise budget_exhausted, after the provider has the request."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(
        monkeypatch,
        leg,
        RouterErrorCode.BUDGET_EXHAUSTED,
        account="team-b",
        after_selecting_an_account=True,
    )

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    assert isinstance(info.value.__cause__, AccountRoutingError)
    assert subscription.calls == []
    manifest = _manifest(tmp_path)
    assert (manifest["provider"], manifest["model"]) == ("anthropic", provider.MODEL)
    assert "subscription_fallback" not in manifest
    assert _fallback_notes(console_warnings) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        RouterErrorCode.CREDENTIAL_MISSING,
        RouterErrorCode.CREDENTIAL_INVALID,
        RouterErrorCode.UNSUPPORTED_PRICING,
        RouterErrorCode.LEDGER_BUSY,
        RouterErrorCode.LEDGER_UNAVAILABLE,
        RouterErrorCode.PROVIDER_ERROR,
    ],
)
async def test_any_other_router_code_still_fails_with_the_routing_error(
    leg, subscription, console_warnings, monkeypatch, tmp_path, code
):
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, code)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    assert info.value.__cause__.code is code
    assert subscription.calls == []
    manifest = _manifest(tmp_path)
    assert (manifest["provider"], manifest["model"]) == ("anthropic", provider.MODEL)
    assert "subscription_fallback" not in manifest
    assert _fallback_notes(console_warnings) == []


@pytest.mark.asyncio
async def test_another_provider_is_not_moved_to_the_subscription_route(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    from lionhub.providers.openai.chat import OpenaiChatEndpoint

    async def call(self, request, *args, **kwargs):
        raise AccountRoutingError(RouterErrorCode.BUDGET_EXHAUSTED)

    monkeypatch.setattr(OpenaiChatEndpoint, "call", call)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"), model="openai/gpt-6-luna")

    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert subscription.calls == []
    manifest = _manifest(tmp_path)
    assert manifest["provider"] == "openai"
    assert "subscription_fallback" not in manifest
    assert _fallback_notes(console_warnings) == []


@pytest.mark.asyncio
async def test_a_resumed_branch_is_not_moved_to_the_subscription_route(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """A branch reopened with `-r` lives on the API route, even before it holds a reply."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.CREDENTIAL_MISSING)
    with pytest.raises(ExecutionError):
        await leg.run(_PROFILE(api_tools="read"))
    (first_run,) = (tmp_path / "runs").rglob("run.json")
    branch_id = json.loads(first_run.read_text())["branch_id"]
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"), "continue", resume=branch_id)

    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert subscription.calls == []
    assert _fallback_notes(console_warnings) == []


def test_li_agent_exits_zero_and_prints_the_subscription_answer(
    leg, subscription, monkeypatch, tmp_path, capsys
):
    import lionagi.cli.agent as agent_mod

    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)
    profile = _PROFILE(api_tools="read")
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
    args = SimpleNamespace(
        query=[provider.MODEL, "what do the notes say?"],
        prompt_flag=None,
        prompt_file=None,
        yolo=False,
        verbose=False,
        theme=None,
        resume=None,
        continue_last=False,
        effort=None,
        agent=profile.name,
        cwd=str(leg.workspace),
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        preset=None,
        resume_on_timeout=False,
        form=None,
        context_from=None,
        context_budget=None,
    )

    rc = agent_mod.run_agent(args)

    assert rc == 0
    assert ANSWER in capsys.readouterr().out
    assert _manifest(tmp_path)["subscription_fallback"]["model_used"] == FALLBACK_MODEL
