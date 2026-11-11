# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent` on an Anthropic API model moves to the subscription route when the account router
refuses the run's first request.

The router refuses before anything is sent when every configured account is blocked or out of
budget. A run whose first request is refused for spend (`budget_exhausted`, or `account_blocked`
with every blocked account at its spend ceiling) continues on `claude_code/<model>`, at most
once, and says so on the console and in run.json. A run pinned to an account with
`LIONAGI_API_ACCOUNT`, a run refused for any other block reason, a refusal after a reply, any
other router code and any other provider keep failing with the routing error. The endpoint's
call and the subscription route's turn are scripted here, so no request leaves the process.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lionagi._errors import ExecutionError
from lionagi.service._api_telemetry import record_selected_attempt
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    ProvenNotSent,
    Quote,
    RouterErrorCode,
    Scope,
)
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


def _protected_denials(root) -> list[str]:
    """The deny rules the switch builds for the protected names under *root*."""
    from lionagi.cli.agent import _subscription_read_rules

    return _subscription_read_rules("Read,Grep,Glob", str(root))[2]


def _flag_value(args: list[str], flag: str) -> str | None:
    """The value after *flag*, None when the flag is absent."""
    return args[args.index(flag) + 1] if flag in args else None


def _request_env_names(chat_model) -> frozenset[str] | None:
    """The names in the child process environment of the Claude Code request that the model's
    endpoint builds; None when the request carries none, so that the child would inherit this
    process's whole environment. Names only: the values are never read."""
    payload, _ = chat_model.endpoint.create_payload({"prompt": "x"})
    env = payload["request"].env
    return None if env is None else frozenset(env)


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
                request_env_names=_request_env_names(self.chat_model),
                request_fill_skip=_request_fill_skip(self.chat_model),
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


def _not_moving_notes(console_warnings) -> list[str]:
    """What the run printed when it stayed on the API route and said why."""
    return [line for line in console_warnings if "not moving to the subscription route" in line]


_TWO_TEAMS = [
    {"name": "team-a", "key_env": "ACCT_A", "budget_usd": "10"},
    {"name": "team-b", "key_env": "ACCT_B", "budget_usd": "10"},
]


@pytest.fixture
def account_ledger(monkeypatch, tmp_path):
    """The account router pointed at a ledger file of its own, so the blocked accounts a run reads
    at its refusal are the ones a test wrote through the ledger. No account is pinned."""
    from lionagi.service.accounts import _router as router_module

    monkeypatch.delenv("LIONAGI_API_ACCOUNT", raising=False)
    router_module._reset_router_test_dependencies()
    router_module._set_router_test_dependencies(ledger_path=tmp_path / "ledger.sqlite3")
    yield
    router_module._reset_router_test_dependencies()


async def _block(reasons: dict[str, str]) -> None:
    """Give every configured account a ledger row, then block each named account for its reason,
    through the ledger the router hands the run."""
    from lionagi import config
    from lionagi.service.accounts._router import get_router

    specs = tuple(
        AccountSpec(name=item.name, key_env=item.key_env, budget_nusd=item.budget_nusd)
        for item in config.settings.ANTHROPIC_ACCOUNTS
    )
    ledger = get_router().ledger
    first = await ledger.reserve(
        Scope("branch", uuid4(), "anthropic"),
        uuid4(),
        Quote("model-a", 1, "price-a", "a" * 64),
        specs,
    )
    await ledger.settle(first.attempt_id, ProvenNotSent())
    for name, reason in reasons.items():
        await ledger.block("anthropic", name, reason)


def _assert_stays_on_the_api_route(info, code, leg, subscription, console_warnings, tmp_path):
    """The run failed with the routing error it was refused with, sent nothing on the
    subscription route, kept its API model and wrote no fallback record."""
    assert isinstance(info.value.__cause__, AccountRoutingError)
    assert info.value.__cause__.code is code
    assert subscription.calls == []
    assert len(leg.payloads) == 1
    manifest = _manifest(tmp_path)
    assert manifest["status"] == "failed"
    assert "subscription_fallback" not in manifest
    assert _fallback_notes(console_warnings) == []
    assert leg.branch.chat_model.endpoint.config.provider == "anthropic"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [RouterErrorCode.BUDGET_EXHAUSTED])
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
    record = dict(manifest["subscription_fallback"])
    removed = record.pop("env_removed")
    assert removed == sorted(removed)
    skipped = record.pop("env_fill_skipped")
    assert skipped == sorted(skipped)
    assert record == {
        "original_model": provider.MODEL,
        "model_used": FALLBACK_MODEL,
        "refusal_code": code.value,
        "tools": "Read,Grep,Glob",
        "allowed_tools": [
            f"{tool}(/{leg.workspace.resolve()}/**)" for tool in ("Read", "Grep", "Glob")
        ],
        "disallowed_tools": _protected_denials(leg.workspace.resolve()),
        "permission_mode": "dontAsk",
    }
    assert not {"prompt_caching", "tool_result_max_chars", "context_fold"} & set(manifest)
    (note,) = _fallback_notes(console_warnings)
    assert code.value in note and FALLBACK_MODEL in note
    assert "api_launch" not in leg.branch.metadata
    assert leg.branch.chat_model.endpoint.config.provider == "claude_code"
    assert leg.branch.parse_model is leg.branch.chat_model


@pytest.mark.asyncio
async def test_a_pinned_run_does_not_move_to_the_subscription_route(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """A run pinned to an account asked for that account and no other route: a spend refusal
    that would move an unpinned run ends it with the routing error and says why."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    monkeypatch.setenv("LIONAGI_API_ACCOUNT", "team-b")
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    _assert_stays_on_the_api_route(
        info, RouterErrorCode.BUDGET_EXHAUSTED, leg, subscription, console_warnings, tmp_path
    )
    (note,) = _not_moving_notes(console_warnings)
    assert "LIONAGI_API_ACCOUNT pins it to account team-b" in note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accounts", "reasons"),
    [
        (_TEAM_B, {"team-b": "ceiling_exceeded"}),
        (_TWO_TEAMS, {"team-a": "ceiling_exceeded", "team-b": "ceiling_exceeded"}),
    ],
    ids=["one account", "two accounts"],
)
async def test_account_blocked_at_the_spend_ceiling_continues_on_the_subscription_route(
    leg, subscription, account_ledger, console_warnings, monkeypatch, tmp_path, accounts, reasons
):
    _settings(monkeypatch, tmp_path, accounts=accounts)
    await _block(reasons)
    _refuse(monkeypatch, leg, RouterErrorCode.ACCOUNT_BLOCKED)

    result, provider_used, _, status, _ = await leg.run(_PROFILE(api_tools="read"))

    assert (result, provider_used, status) == (ANSWER, "claude_code", "completed")
    assert len(leg.payloads) == 1
    (turn,) = subscription.calls
    assert (turn.provider, turn.model) == ("claude_code", "claude-haiku-5-5")
    fallback = _manifest(tmp_path)["subscription_fallback"]
    assert fallback["refusal_code"] == "account_blocked"
    assert fallback["model_used"] == FALLBACK_MODEL
    (note,) = _fallback_notes(console_warnings)
    assert "account_blocked" in note and FALLBACK_MODEL in note
    assert _not_moving_notes(console_warnings) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasons", "names", "reason_text"),
    [
        (
            {"team-a": "settlement_conflict", "team-b": "ceiling_exceeded"},
            "team-a",
            "settlement_conflict",
        ),
        (
            {"team-a": "auth_required", "team-b": "auth_required"},
            "team-a, team-b",
            "auth_required",
        ),
        ({"team-a": "manual", "team-b": "ceiling_exceeded"}, "team-a", "manual"),
    ],
    ids=["settlement conflict beside a ceiling", "auth reason", "reason set by hand"],
)
async def test_account_blocked_for_any_other_reason_does_not_move_the_run(
    leg,
    subscription,
    account_ledger,
    console_warnings,
    monkeypatch,
    tmp_path,
    reasons,
    names,
    reason_text,
):
    """A block that is not about spend is not something the subscription route solves: the run
    names the accounts and reasons that keep it where it is and fails with the routing error."""
    _settings(monkeypatch, tmp_path, accounts=_TWO_TEAMS)
    await _block(reasons)
    _refuse(monkeypatch, leg, RouterErrorCode.ACCOUNT_BLOCKED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    _assert_stays_on_the_api_route(
        info, RouterErrorCode.ACCOUNT_BLOCKED, leg, subscription, console_warnings, tmp_path
    )
    (note,) = _not_moving_notes(console_warnings)
    assert f"account(s) {names} are blocked for {reason_text}" in note
    assert "ACCT_A" not in note and "ACCT_B" not in note


@pytest.mark.asyncio
async def test_account_blocked_with_no_blocked_account_in_the_ledger_does_not_move_the_run(
    leg, subscription, account_ledger, console_warnings, monkeypatch, tmp_path
):
    _settings(monkeypatch, tmp_path, accounts=_TWO_TEAMS)
    await _block({})
    _refuse(monkeypatch, leg, RouterErrorCode.ACCOUNT_BLOCKED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    _assert_stays_on_the_api_route(
        info, RouterErrorCode.ACCOUNT_BLOCKED, leg, subscription, console_warnings, tmp_path
    )
    (note,) = _not_moving_notes(console_warnings)
    assert "no account is reported blocked" in note


@pytest.mark.asyncio
async def test_account_blocked_with_a_status_that_cannot_be_read_does_not_move_the_run(
    leg, subscription, account_ledger, console_warnings, monkeypatch, tmp_path
):
    from lionagi.service.accounts._ledger import CredentialLedger

    async def unreadable(self, provider, month_utc=None):
        raise RuntimeError("ledger offline")

    _settings(monkeypatch, tmp_path, accounts=_TWO_TEAMS)
    monkeypatch.setattr(CredentialLedger, "status", unreadable)
    _refuse(monkeypatch, leg, RouterErrorCode.ACCOUNT_BLOCKED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    _assert_stays_on_the_api_route(
        info, RouterErrorCode.ACCOUNT_BLOCKED, leg, subscription, console_warnings, tmp_path
    )
    (note,) = _not_moving_notes(console_warnings)
    assert "could not be read (RuntimeError)" in note
    assert "ledger offline" not in note


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
    assert "sandbox_dropped" not in record
    # No user, project or local Claude Code settings are read by the switched leg.
    assert "--setting-sources" in turn.cli_args
    assert turn.cli_args[turn.cli_args.index("--setting-sources") + 1] == ""
    # Each read tool is granted only under the workspace root the API reader was held to, and
    # every call no rule grants is refused.
    root = str(leg.workspace.resolve())
    rules = [f"Read(/{root}/**)", f"Grep(/{root}/**)", f"Glob(/{root}/**)"]
    start = turn.cli_args.index("--allowedTools") + 1
    assert turn.cli_args[start : start + 3] == rules
    assert _flag_value(turn.cli_args, "--permission-mode") == "dontAsk"
    assert record["allowed_tools"] == rules
    assert record["permission_mode"] == "dontAsk"
    # The names the API reader refuses inside the root are denied to each tool, and a deny
    # rule wins over a grant.
    denials = _protected_denials(root)
    start = turn.cli_args.index("--disallowedTools") + 1
    assert set(denials) <= set(turn.cli_args[start:])
    assert record["disallowed_tools"] == denials


@pytest.fixture
def seatbelt(monkeypatch, tmp_path):
    """The macOS sandbox the write set needs is reported available, as on a Mac."""
    from lionhub.tools import _seatbelt

    monkeypatch.setattr(_seatbelt, "seatbelt_available", lambda: True)
    monkeypatch.setattr(
        _seatbelt, "_uv_cache_dir", lambda *, _command_env_names=None: tmp_path / "uv-cache"
    )


@pytest.mark.asyncio
async def test_the_switched_run_gets_the_environment_without_the_api_routes_credential_names(
    leg, subscription, monkeypatch, tmp_path
):
    """The Claude Code request carries a child environment of its own instead of leaving the CLI
    to inherit this process's whole one: the API key name and the configured account key name are
    gone, an unrelated name is kept, and run.json lists exactly the names that were removed."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.setenv("ACCT_B", "placeholder")
    monkeypatch.setenv("LIONAGI_TEST_PLAIN", "placeholder")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read"))

    (turn,) = subscription.calls
    child_names = turn.request_env_names
    assert child_names is not None
    assert "ANTHROPIC_API_KEY" not in child_names
    assert "ACCT_B" not in child_names
    assert "LIONAGI_TEST_PLAIN" in child_names
    removed = _manifest(tmp_path)["subscription_fallback"]["env_removed"]
    assert removed == sorted(removed)
    assert {"ANTHROPIC_API_KEY", "ACCT_B"} <= set(removed)
    assert "LIONAGI_TEST_PLAIN" not in removed
    assert set(removed) == set(os.environ) - child_names


@pytest.mark.asyncio
async def test_the_switched_run_keeps_claude_codes_own_login_token_and_no_other_token(
    leg, subscription, monkeypatch, tmp_path
):
    """Claude Code can read its own login token from its environment, so that one name is kept;
    the API key stays removed (it would be billed in place of the subscription) and so does any
    other name that matches a sensitive-name pattern."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "placeholder")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.setenv("LIONAGI_TEST_AUTH_TOKEN", "placeholder")
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read"))

    (turn,) = subscription.calls
    child_names = turn.request_env_names
    assert child_names is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN" in child_names
    assert "ANTHROPIC_API_KEY" not in child_names
    assert "LIONAGI_TEST_AUTH_TOKEN" not in child_names
    removed = _manifest(tmp_path)["subscription_fallback"]["env_removed"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in removed
    assert {"ANTHROPIC_API_KEY", "LIONAGI_TEST_AUTH_TOKEN"} <= set(removed)


def _lookup_declaring(monkeypatch, *names):
    """The configured secret lookup declares *names*. Its command is never run here: the
    switched turn is a stand-in, so only the names reach the code under test."""
    import sys

    from lionagi.service.accounts import _secret_resolution

    real = _secret_resolution.resolve_secret_lookup_config
    block = {
        "secrets": {
            "lookup": {"argv": [sys.executable, "-c", "pass", "{name}"], "names": list(names)}
        }
    }
    monkeypatch.setattr(
        _secret_resolution, "resolve_secret_lookup_config", lambda **_: real(settings=block)
    )


def _request_fill_skip(chat_model) -> frozenset[str]:
    """The names the Claude Code request the model's endpoint builds keeps the secret lookup
    from filling into its child."""
    payload, _ = chat_model.endpoint.create_payload({"prompt": "x"})
    return payload["request"].secret_fill_skip


@pytest.mark.asyncio
async def test_the_secret_lookup_cannot_fill_a_removed_name_back_into_the_switched_child(
    leg, subscription, monkeypatch, tmp_path
):
    """A configured secret lookup fills each name it declares that a child lacks. The switched
    request names every such name except Claude Code's own login token, so a removed API key
    is not filled back in, and run.json lists the skipped names."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _lookup_declaring(monkeypatch, "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read"))

    (turn,) = subscription.calls
    assert turn.request_fill_skip == frozenset({"ANTHROPIC_API_KEY"})
    record = _manifest(tmp_path)["subscription_fallback"]
    assert record["env_fill_skipped"] == ["ANTHROPIC_API_KEY"]


@pytest.mark.asyncio
async def test_a_switch_whose_request_drops_the_fill_skip_set_does_not_happen(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """An endpoint that did not keep the skip set would let the lookup fill the removed names
    back in. The run does not switch and fails with the routing error."""
    import lionagi.cli.agent as agent_mod

    build_chat_model = agent_mod.build_chat_model

    def build_without_the_skip_set(*args, **kwargs):
        model = build_chat_model(*args, **kwargs)
        state = getattr(getattr(model, "endpoint", None), "_runtime_state", None)
        if isinstance(state, dict):
            state.pop("secret_fill_skip", None)
        return model

    monkeypatch.setattr(agent_mod, "build_chat_model", build_without_the_skip_set)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _lookup_declaring(monkeypatch, "ANTHROPIC_API_KEY")
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert subscription.calls == []
    assert "subscription_fallback" not in _manifest(tmp_path)
    (note,) = _not_moving_notes(console_warnings)
    assert "child environment" in note


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["yolo", "bypass"])
async def test_a_read_set_run_switches_without_a_permission_bypass(
    leg, subscription, console_warnings, monkeypatch, tmp_path, flag
):
    """The read set's switch carries no permission bypass even when the run was started with
    `yolo` or `bypass`: it runs in `dontAsk`, which refuses every call its rules do not grant."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read"), **{flag: True})

    (turn,) = subscription.calls
    assert "--dangerously-skip-permissions" not in turn.cli_args
    assert _flag_value(turn.cli_args, "--permission-mode") == "dontAsk"
    assert _manifest(tmp_path)["subscription_fallback"]["permission_mode"] == "dontAsk"
    (note,) = _fallback_notes(console_warnings)
    assert "without asking" not in note


@pytest.mark.asyncio
async def test_the_warning_names_the_root_the_read_tools_are_granted_under(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """The switched run is told where its read tools reach and that every other call is
    refused."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    await leg.run(_PROFILE(api_tools="read"))

    (note,) = _fallback_notes(console_warnings)
    assert f"Read, Grep, Glob are granted only under {leg.workspace.resolve()}" in note
    assert "every other tool call is refused" in note


def test_only_sets_without_a_sandbox_move_to_the_subscription_route():
    """A set whose editor and bash ran under the macOS sandbox has nothing on the subscription
    route to run them under, so the subscription table names none of those sets."""
    import lionagi.cli.agent as agent_mod

    assert set(agent_mod._SUBSCRIPTION_API_SET_TOOLS) <= set(agent_mod._API_TOOL_SETS)
    assert not set(agent_mod._SUBSCRIPTION_API_SET_TOOLS) & set(agent_mod._API_CONFINED_TOOL_SETS)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "yolo", "bypass"])
async def test_a_write_set_run_does_not_move_to_the_subscription_route(
    leg, subscription, seatbelt, console_warnings, monkeypatch, tmp_path, flag
):
    """The write set's editor and bash ran under the macOS sandbox and the API route's command
    checks. The run stays: it prints why and fails with the routing error, and nothing runs on
    the subscription route, with or without `yolo` and `bypass`."""
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="write"), **({flag: True} if flag else {}))

    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert subscription.calls == []
    assert len(leg.payloads) == 1
    manifest = _manifest(tmp_path)
    assert manifest["provider"] == "anthropic"
    assert "subscription_fallback" not in manifest
    (note,) = _not_moving_notes(console_warnings)
    assert "only a read-set run moves" in note
    assert leg.branch.chat_model.endpoint.config.provider == "anthropic"


@pytest.mark.asyncio
async def test_a_run_whose_api_set_has_no_subscription_tools_fails_with_the_routing_error(
    leg, subscription, seatbelt, console_warnings, monkeypatch, tmp_path
):
    """The subscription CLI has every built-in tool unless the request names some. A set with no
    tool list to name does not switch: the run fails with the routing error, nothing is sent on
    the subscription route, and the branch keeps its API model."""
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "_SUBSCRIPTION_API_SET_TOOLS",
        {k: v for k, v in agent_mod._SUBSCRIPTION_API_SET_TOOLS.items() if k != "read"},
    )
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    assert isinstance(info.value.__cause__, AccountRoutingError)
    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert subscription.calls == []
    assert len(leg.payloads) == 1
    manifest = _manifest(tmp_path)
    assert (manifest["provider"], manifest["model"]) == ("anthropic", provider.MODEL)
    assert manifest["status"] == "failed"
    assert "subscription_fallback" not in manifest
    assert _fallback_notes(console_warnings) == []
    assert leg.branch.chat_model.endpoint.config.provider == "anthropic"


@pytest.mark.asyncio
async def test_a_switch_whose_request_does_not_take_the_child_environment_does_not_happen(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """A Claude Code endpoint that did not take the child environment would leave the CLI to
    inherit this process's whole one. The run does not switch: it prints one warning saying why,
    fails with the routing error, and the branch keeps its API model."""
    import lionagi.cli.agent as agent_mod

    build_chat_model = agent_mod.build_chat_model

    def build_without_the_environment(*args, **kwargs):
        model = build_chat_model(*args, **kwargs)
        state = getattr(getattr(model, "endpoint", None), "_runtime_state", None)
        if isinstance(state, dict):
            state.pop("env", None)
        return model

    monkeypatch.setattr(agent_mod, "build_chat_model", build_without_the_environment)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    assert isinstance(info.value.__cause__, AccountRoutingError)
    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert subscription.calls == []
    assert len(leg.payloads) == 1
    manifest = _manifest(tmp_path)
    assert (manifest["provider"], manifest["model"]) == ("anthropic", provider.MODEL)
    assert manifest["status"] == "failed"
    assert "subscription_fallback" not in manifest
    assert _fallback_notes(console_warnings) == []
    (note,) = _not_moving_notes(console_warnings)
    assert "child environment" in note
    assert leg.branch.chat_model.endpoint.config.provider == "anthropic"


@pytest.mark.asyncio
@pytest.mark.parametrize("dropped", ["permission_mode", "allowed_tools", "disallowed_tools"])
async def test_a_switch_whose_request_drops_the_workspace_permission_rules_does_not_happen(
    leg, subscription, console_warnings, monkeypatch, tmp_path, dropped
):
    """Without `dontAsk` and the workspace grants, the CLI's own defaults would decide where
    its reads reach. The run does not switch: it prints one warning saying why, fails with the
    routing error, and the branch keeps its API model."""
    import lionagi.cli.agent as agent_mod

    build_chat_model = agent_mod.build_chat_model

    def build_without_the_rule(*args, **kwargs):
        model = build_chat_model(*args, **kwargs)
        endpoint = getattr(model, "endpoint", None)
        if endpoint is not None:
            endpoint.config.kwargs.pop(dropped, None)
        return model

    monkeypatch.setattr(agent_mod, "build_chat_model", build_without_the_rule)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    _assert_stays_on_the_api_route(
        info, RouterErrorCode.BUDGET_EXHAUSTED, leg, subscription, console_warnings, tmp_path
    )
    (note,) = _not_moving_notes(console_warnings)
    assert "workspace permission rules" in note


@pytest.mark.asyncio
async def test_a_workspace_whose_path_a_rule_cannot_carry_does_not_move(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """A space in the workspace path would change how the permission rule parses, so the run
    stays on the API route and says why."""
    import lionagi.cli.agent as agent_mod

    spaced = tmp_path / "work space"
    spaced.mkdir()
    profile = _PROFILE(api_tools="read")
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await agent_mod._run_agent(
            provider.MODEL, "what do the notes say?", agent_name=profile.name, cwd=str(spaced)
        )

    _assert_stays_on_the_api_route(
        info, RouterErrorCode.BUDGET_EXHAUSTED, leg, subscription, console_warnings, tmp_path
    )
    (note,) = _not_moving_notes(console_warnings)
    assert "permission rule" in note


@pytest.mark.parametrize(
    "root", ["/work space", "/work/*", "/work/[ab]", "/work/a(b)", "/work/a?", "/work/~a", "/"]
)
def test_a_root_with_any_character_a_rule_reads_specially_gets_no_rules(root):
    from lionagi.cli.agent import _subscription_read_rules

    assert _subscription_read_rules("Read,Grep,Glob", root) is None


def test_the_rules_name_the_resolved_workspace_root(tmp_path, monkeypatch):
    from lionagi.cli.agent import _subscription_read_rules

    (tmp_path / "ws").mkdir()
    monkeypatch.chdir(tmp_path)
    root = str((tmp_path / "ws").resolve())
    expected = (root, [f"Read(/{root}/**)", f"Grep(/{root}/**)", f"Glob(/{root}/**)"])

    assert _subscription_read_rules("Read,Grep,Glob", "ws/../ws")[:2] == expected
    monkeypatch.chdir(tmp_path / "ws")
    assert _subscription_read_rules("Read,Grep,Glob", None)[:2] == expected


def test_every_protected_name_is_denied_to_every_read_tool_in_any_case(tmp_path):
    from fnmatch import fnmatchcase

    from lionagi.cli.agent import _subscription_read_rules
    from lionagi.libs.path_safety import DENIED_NAMES, is_protected_name

    root = str(tmp_path.resolve())
    _, _, denials = _subscription_read_rules("Read,Grep,Glob", root)

    def patterns(tool, below):
        prefix, suffix = f"{tool}(/{root}/**/", f"{below})"
        return [
            rule[len(prefix) : -len(suffix)]
            for rule in denials
            if rule.startswith(prefix)
            and rule.endswith(suffix)
            and "/" not in rule[len(prefix) : -len(suffix)]
        ]

    # Spellings the API reader refuses: each folds to a protected name.
    spellings = set()
    for name in DENIED_NAMES:
        spellings |= {name, name.upper(), name.title(), name.swapcase()}
        spellings |= {name.replace("s", "ſ"), name.replace("ss", "ß"), name.replace("ss", "ẞ")}
    spellings = {s for s in spellings if is_protected_name(s)}
    assert {".htpaßwd", "id_rſa", ".ENV"} <= spellings
    for tool in ("Read", "Grep", "Glob"):
        for below in ("", "/**"):
            found = patterns(tool, below)
            for spelling in spellings:
                assert any(fnmatchcase(spelling, pattern) for pattern in found), (
                    tool,
                    below,
                    spelling,
                )
            for ordinary in ("notes.txt", "env", ".envrc", "id_rsa.pub", "htpasswd"):
                assert not any(fnmatchcase(ordinary, pattern) for pattern in found), ordinary
    assert f"Read(/{root}/**/.[Ee][Nn][Vv])" in denials
    assert f"Glob(/{root}/**/.[Ee][Nn][Vv]/**)" in denials
    assert f"Grep(/{root}/**/[Ii][Dd]_[Rr][Ssſ][Aa])" in denials
    assert f"Read(/{root}/**/.[Hh][Tt][Pp][Aa][ßẞ][Ww][Dd])" in denials


@pytest.mark.parametrize("protected", ["id_rsa", ".ENV", "id_ed25519"])
def test_a_root_with_a_protected_name_in_its_path_gets_no_rules(tmp_path, protected):
    from lionagi.cli.agent import _subscription_read_rules

    root = tmp_path / protected / "project"
    root.mkdir(parents=True)

    assert _subscription_read_rules("Read,Grep,Glob", str(root)) is None
    assert _subscription_read_rules("Read,Grep,Glob", str(tmp_path / protected)) is None


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


@pytest.mark.asyncio
async def test_a_branch_that_moved_to_the_subscription_route_is_not_resumed(
    leg, subscription, monkeypatch, tmp_path
):
    """The reduced environment the switched Claude Code process ran with is not saved with the
    branch, so a restored branch would spawn Claude Code with this process's whole environment.
    The saved branch records the move, and resuming it is refused before any turn runs."""
    from lionagi._errors import ConfigurationError
    from lionhub.orchestration.runs import find_branch

    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)
    _, _, branch_id, _, _ = await leg.run(_PROFILE(api_tools="read"))
    _, path = find_branch(branch_id)
    saved = json.loads(path.read_text())["metadata"]
    assert saved["subscription_switch"] == {"from": provider.MODEL}
    assert "api_launch" not in saved

    with pytest.raises(ConfigurationError, match="cannot be resumed"):
        await leg.run(_PROFILE(api_tools="read"), "continue", resume=branch_id)
    assert len(subscription.calls) == 1, "no turn ran on the resumed branch"


@pytest.mark.asyncio
async def test_unreadable_credential_settings_keep_the_run_on_the_api_route(
    leg, subscription, console_warnings, monkeypatch, tmp_path
):
    """When the names to remove from the child environment cannot be read, the run does not move:
    it prints why and fails with the routing error, not the settings error."""
    import lionagi._command_env_metadata as metadata
    from lionagi._errors import ConfigurationError

    real = metadata.configured_command_env_names

    def unreadable(**kwargs):
        # Only the switch's own read fails; the launch reads the same names through the agent
        # factory, which must still succeed for the first request to be sent and refused.
        if sys._getframe(1).f_code.co_name == "_run_agent":
            raise ConfigurationError("settings could not be read")
        return real(**kwargs)

    monkeypatch.setattr(metadata, "configured_command_env_names", unreadable)
    _settings(monkeypatch, tmp_path, accounts=_TEAM_B)
    _refuse(monkeypatch, leg, RouterErrorCode.BUDGET_EXHAUSTED)

    with pytest.raises(ExecutionError) as info:
        await leg.run(_PROFILE(api_tools="read"))

    assert isinstance(info.value.__cause__, AccountRoutingError)
    assert info.value.__cause__.code is RouterErrorCode.BUDGET_EXHAUSTED
    assert subscription.calls == []
    assert "subscription_fallback" not in _manifest(tmp_path)
    (note,) = _not_moving_notes(console_warnings)
    assert "could not be read (ConfigurationError)" in note
    assert leg.branch.chat_model.endpoint.config.provider == "anthropic"


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
