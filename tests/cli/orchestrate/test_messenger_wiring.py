# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""In-process team messaging wiring: build_worker_branch <-> Exchange/LionMessenger."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from lionagi import iModel
from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate._common import (
    TEAM_COORD_SECTION,
    TEAM_COORD_SECTION_MESSENGER,
    _build_worker_operate_node,
)
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    build_worker_branch,
    team_history_context,
    team_worker_system,
    worker_is_cli,
)
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger


class _FakeObserver:
    def __init__(self):
        self.registered: list = []

    def observe(self, *keys, handler=None, role=None):
        self.registered.append((keys, handler, role))
        return handler

    def unobserve(self, handler):
        self.registered = [r for r in self.registered if r[1] is not handler]


class _FakeSession:
    def __init__(self):
        self.branches: list = []
        self.observer = _FakeObserver()

    def include_branches(self, branch):
        self.branches.append(branch)

    def observe(self, *keys, handler=None, role=None):
        # Mirror Session.observe -> self.observer.observe so the fanout
        # node-completion subscription (and its later unobserve cleanup) run
        # under the fake.
        return self.observer.observe(*keys, handler=handler, role=role)


def _make_env(
    tmp_path,
    *,
    exchange=None,
    messenger=None,
    roster=None,
    team_data=None,
    messenger_names=None,
):
    name_counts: dict = {}

    def assign_name(role: str) -> str:
        name_counts[role] = name_counts.get(role, 0) + 1
        n = name_counts[role]
        return f"{role}-{n}" if n > 1 else role

    def register_name(name: str) -> None:
        pass

    env = OrchestrationEnv(
        run=SimpleNamespace(agent_artifact_dir=lambda a: tmp_path / a),
        session=_FakeSession(),
        orc_branch=SimpleNamespace(),
        builder=SimpleNamespace(),
        orc_profile=None,
        default_model_spec="openai/gpt-4o-mini",
        bare=True,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=str(tmp_path),
        team_data=team_data,
    )
    env.assign_name = assign_name
    env.register_name = register_name
    env.exchange = exchange
    env.messenger = messenger
    env.roster = roster
    env.messenger_names = messenger_names
    return env


def _team_data(team_id="t1", team_name="the-team"):
    return {"id": team_id, "name": team_name, "members": ["orchestrator", "alice", "bob"]}


def _mixed_team_data(team_id="t1", team_name="the-team"):
    """A team with a third member ('cli-carl') that is never messenger-bound —
    for mixed-provider-team tests exercising the messenger_names filter."""
    return {
        "id": team_id,
        "name": team_name,
        "members": ["orchestrator", "alice", "bob", "cli-carl"],
    }


def _api_imodel(*_a, **_kw):
    return iModel(provider="openai", model="gpt-4o-mini", api_key="dummy-key")


def _cli_imodel(*_a, **_kw):
    return iModel(provider="claude_code", api_key="dummy-key")


@pytest.mark.asyncio
async def test_api_worker_gets_registered_and_bound(tmp_path):
    """Non-CLI worker: exchange.register + roster entry + messenger tool on branch.acts."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(tmp_path, exchange=exchange, messenger=messenger, roster=roster)

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )

    assert exchange.has(wb.id)
    assert roster["alice"] == wb.id
    assert any(t.function == "messenger" for t in wb.acts.registry.values())
    assert messenger_bound is True


@pytest.mark.asyncio
async def test_cli_worker_skips_messenger_binding(tmp_path):
    """CLI worker: no exchange registration, no roster entry, no messenger tool."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(tmp_path, exchange=exchange, messenger=messenger, roster=roster)

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_cli_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="cli-worker", role="researcher", explicit_name="cli-worker"
        )

    assert not exchange.has(wb.id)
    assert "cli-worker" not in roster
    assert not any(t.function == "messenger" for t in wb.acts.registry.values())
    assert messenger_bound is False


@pytest.mark.asyncio
async def test_no_exchange_configured_skips_binding_entirely(tmp_path):
    """team mode inactive (env.exchange/messenger/roster all None): no-op, no crash."""
    env = _make_env(tmp_path)  # exchange/messenger/roster default None

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="solo", role="researcher", explicit_name="solo"
        )

    assert not any(t.function == "messenger" for t in wb.acts.registry.values())
    assert messenger_bound is False


@pytest.mark.asyncio
async def test_send_collect_receive_roundtrip_renders_sender_name(tmp_path):
    """Worker A sends -> collect_all() routes it -> worker B's receive renders A by name."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(tmp_path, exchange=exchange, messenger=messenger, roster=roster)

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        branch_a, _, _, _ = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )
        branch_b, _, _, _ = await build_worker_branch(
            env, agent_id="bob", role="implementer", explicit_name="bob"
        )

    tool_a = next(t for t in branch_a.acts.registry.values() if t.function == "messenger")
    tool_b = next(t for t in branch_b.acts.registry.values() if t.function == "messenger")

    send_result = tool_a.func_callable(action="send", to="bob", content="ping from alice")
    assert "Sent to bob" in send_result

    await exchange.collect_all()

    receive_result = tool_b.func_callable(action="receive")
    assert receive_result == "[alice] ping from alice"

    # roster is the SAME shared dict passed to both binds — later-registered
    # bob is visible to alice's already-bound tool without rebinding.
    assert roster == {"alice": branch_a.id, "bob": branch_b.id}


@pytest.mark.asyncio
async def test_operate_node_carries_actions_kwarg_when_team_messaging_active(tmp_path):
    """API worker + active team messaging: the REAL static-node builder shared
    by fanout.py and flow.py (`_build_worker_operate_node`) produces a request
    with actions=True, so Branch.operate() serializes branch.acts."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(tmp_path, exchange=exchange, messenger=messenger, roster=roster)
    builder = OperationGraphBuilder()

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )

    node_id = _build_worker_operate_node(
        builder,
        branch=wb,
        instruction="do the task",
        context=[{"overall_task": "t"}],
        messenger_bound=messenger_bound,
    )
    node = builder._operations[node_id]

    assert messenger_bound is True
    assert node.request.get("actions") is True


@pytest.mark.asyncio
async def test_operate_node_omits_actions_kwarg_when_team_mode_off(tmp_path):
    """No exchange/messenger configured (team mode off): the REAL static-node
    builder's request has no actions kwarg — unchanged default behavior."""
    env = _make_env(tmp_path)  # exchange/messenger/roster default None
    builder = OperationGraphBuilder()

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="solo", role="researcher", explicit_name="solo"
        )

    node_id = _build_worker_operate_node(
        builder,
        branch=wb,
        instruction="do the task",
        context=[{"overall_task": "t"}],
        messenger_bound=messenger_bound,
    )
    node = builder._operations[node_id]

    assert messenger_bound is False
    assert "actions" not in node.request


@pytest.mark.asyncio
async def test_operate_node_omits_actions_kwarg_for_cli_worker(tmp_path):
    """Team messaging active but this worker is a CLI provider: no messenger
    binding, so the REAL static-node builder's request must not carry
    actions=True either."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(tmp_path, exchange=exchange, messenger=messenger, roster=roster)
    builder = OperationGraphBuilder()

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_cli_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="cli-worker", role="researcher", explicit_name="cli-worker"
        )

    node_id = _build_worker_operate_node(
        builder,
        branch=wb,
        instruction="do the task",
        context=[{"overall_task": "t"}],
        messenger_bound=messenger_bound,
    )
    node = builder._operations[node_id]

    assert messenger_bound is False
    assert "actions" not in node.request


@pytest.mark.asyncio
async def test_fanout_and_flow_call_sites_use_the_same_shared_node_builder():
    """Regression guard: if either fanout.py or flow.py stopped routing its static operate-node construction through the shared `_build_worker_operate_node` helper (e.g. reverting to an inline, independently-editable conditional), this fails."""
    import lionagi.cli.orchestrate.fanout as fanout_mod
    import lionagi.cli.orchestrate.flow as flow_mod
    from lionagi.cli.orchestrate._common import _build_worker_operate_node

    assert fanout_mod._build_worker_operate_node is _build_worker_operate_node
    assert flow_mod._build_worker_operate_node is _build_worker_operate_node


@pytest.mark.asyncio
async def test_bound_worker_operate_serializes_messenger_tool_schema(tmp_path):
    """End-to-end: build a real bound worker branch, construct its operate node through the real shared builder, then drive the exact request dict production produces through Branch.operate() with a capturing middle -- confirms actions=True flows through to get_tool_schema() and the messenger tool's schema is what gets serialized to the model."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(tmp_path, exchange=exchange, messenger=messenger, roster=roster)
    builder = OperationGraphBuilder()

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )
    assert messenger_bound is True

    node_id = _build_worker_operate_node(
        builder,
        branch=wb,
        instruction="do the task",
        context=[{"overall_task": "t"}],
        messenger_bound=messenger_bound,
    )
    node = builder._operations[node_id]
    node._branch = wb

    captured: dict = {}

    async def capturing_middle(b, ins, cctx, pctx, clear, **kw):
        captured["tool_schemas"] = cctx.tool_schemas
        return "ok"

    await wb.operate(**node.request, middle=capturing_middle, skip_validation=True)

    schemas = captured.get("tool_schemas") or []
    assert any(s.get("function", {}).get("name") == "messenger" for s in schemas)


@pytest.mark.asyncio
async def test_unbound_worker_operate_does_not_serialize_any_tool_schema(tmp_path):
    """Team mode off: the real shared builder's request carries no actions
    kwarg, so Branch.operate() never touches branch.acts at all — no tool
    schemas get serialized regardless of what is registered on the branch."""
    env = _make_env(tmp_path)  # exchange/messenger/roster default None
    builder = OperationGraphBuilder()

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="solo", role="researcher", explicit_name="solo"
        )
    assert messenger_bound is False

    node_id = _build_worker_operate_node(
        builder,
        branch=wb,
        instruction="do the task",
        context=[{"overall_task": "t"}],
        messenger_bound=messenger_bound,
    )
    node = builder._operations[node_id]
    node._branch = wb

    captured: dict = {}

    async def capturing_middle(b, ins, cctx, pctx, clear, **kw):
        captured["tool_schemas"] = cctx.tool_schemas
        return "ok"

    await wb.operate(**node.request, middle=capturing_middle, skip_validation=True)

    assert not captured.get("tool_schemas")


# ── Team-coordination prompt selection ─────────────────────────────────────
#
# Regression coverage for the two-channel contradiction: a team-mode worker
# prompt must describe exactly one coordination channel — the in-process
# `messenger` tool (messenger_bound=True) or the bash `li team` CLI
# (messenger_bound=False) — never both, and never the wrong one.

_BASH_MARKERS = ("li team receive", "li team send")
_MESSENGER_MARKERS = ('action="receive"', 'action="send"', "messenger tool")


def test_bash_and_messenger_templates_are_distinct():
    assert TEAM_COORD_SECTION != TEAM_COORD_SECTION_MESSENGER


def test_team_worker_system_messenger_bound_selects_tool_channel_only():
    section = team_worker_system(_team_data(), "alice", messenger_bound=True)

    for marker in _MESSENGER_MARKERS:
        assert marker in section
    for marker in _BASH_MARKERS:
        assert marker not in section


def test_team_worker_system_unbound_selects_bash_channel_only():
    section = team_worker_system(_team_data(), "alice", messenger_bound=False)

    for marker in _BASH_MARKERS:
        assert marker in section
    for marker in _MESSENGER_MARKERS:
        assert marker not in section


def test_team_worker_system_default_messenger_bound_is_false():
    """Callers that don't pass messenger_bound get the bash-channel section
    (matches build_worker_branch's pre-fix behavior for CLI workers)."""
    section = team_worker_system(_team_data(), "alice")
    assert "li team receive" in section
    assert 'action="receive"' not in section


def test_team_worker_system_none_team_data_returns_none():
    assert team_worker_system(None, "alice", messenger_bound=True) is None
    assert team_worker_system(None, "alice", messenger_bound=False) is None


@pytest.mark.asyncio
async def test_messenger_bound_worker_branch_prompt_has_tool_channel_only(tmp_path):
    """End-to-end: an API-model worker in team mode gets the messenger-tool
    coordination section on its actual assembled system prompt, and NOT the
    bash `li team` instructions — the two channels must never coexist."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(
        tmp_path,
        exchange=exchange,
        messenger=messenger,
        roster=roster,
        team_data=_team_data(),
    )

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )

    assert messenger_bound is True
    prompt = wb.system.rendered
    for marker in _MESSENGER_MARKERS:
        assert marker in prompt
    for marker in _BASH_MARKERS:
        assert marker not in prompt


@pytest.mark.asyncio
async def test_cli_worker_branch_prompt_has_bash_channel_only(tmp_path):
    """End-to-end: a CLI-provider worker in team mode (no tool-calling
    surface, no messenger binding) gets the bash `li team` coordination
    section, and NOT instructions for a tool it was never given."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(
        tmp_path,
        exchange=exchange,
        messenger=messenger,
        roster=roster,
        team_data=_team_data(),
    )

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_cli_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="cli-worker", role="researcher", explicit_name="cli-worker"
        )

    assert messenger_bound is False
    prompt = wb.system.rendered
    for marker in _BASH_MARKERS:
        assert marker in prompt
    for marker in _MESSENGER_MARKERS:
        assert marker not in prompt


# ── Mixed-provider teams: messenger roster must match actual reachability ──
#
# In a heterogeneous --workers pool (some CLI-provider specs, some API
# specs) under team mode, only the API workers end up messenger-bound. A
# messenger-bound worker's prompt must never advertise a CLI-only teammate
# as a valid `messenger(action="send", to=...)` target — LionMessenger.send
# rejects any name never registered in env.roster, and CLI-only teammates
# never get registered (see test_cli_worker_skips_messenger_binding above).


def test_worker_is_cli_true_for_cli_provider_spec(tmp_path):
    env = _make_env(tmp_path)
    assert worker_is_cli(env, "researcher", model_override="claude_code/opus") is True
    assert worker_is_cli(env, "researcher", model_override="codex/gpt-5.5") is True


def test_worker_is_cli_false_for_api_provider_spec(tmp_path):
    env = _make_env(tmp_path)
    assert worker_is_cli(env, "researcher", model_override="openai/gpt-4o-mini") is False


def test_worker_is_cli_falls_back_to_env_default_model_spec(tmp_path):
    """bare env, no override: resolves env.default_model_spec (an API spec here)."""
    env = _make_env(tmp_path)
    assert worker_is_cli(env, "researcher") is False


def test_team_worker_system_flags_cli_teammates_as_unreachable_via_messenger():
    """messenger-bound worker + messenger_names excluding cli-carl: cli-carl
    is annotated in the roster and explicitly called out as unreachable —
    the section must never instruct sending it a messenger message."""
    section = team_worker_system(
        _mixed_team_data(),
        "alice",
        messenger_bound=True,
        messenger_names=frozenset({"alice", "bob"}),
    )
    assert "cli-carl" in section
    assert "no messenger channel" in section
    assert "### Messenger reach" in section
    assert "Unknown recipient" in section
    # bob IS messenger-bound: no unreachable annotation on bob's own line.
    assert "bob (no messenger channel" not in section


def test_team_worker_system_no_unreachable_note_when_all_teammates_bound():
    """messenger_names covers every teammate: no unreachable flag, no extra
    section — matches the plain messenger-only template exactly."""
    section = team_worker_system(
        _team_data(),
        "alice",
        messenger_bound=True,
        messenger_names=frozenset({"alice", "bob"}),
    )
    assert "no messenger channel" not in section
    assert "### Messenger reach" not in section


def test_team_worker_system_bash_channel_ignores_messenger_names():
    """A bash-channel (messenger_bound=False) worker's prompt is unaffected by messenger_names -- only messenger-bound workers need the reachability filter, since the bash `li team` channel's own reachability is unrelated to Exchange/roster registration."""
    section = team_worker_system(
        _mixed_team_data(),
        "alice",
        messenger_bound=False,
        messenger_names=frozenset({"alice", "bob"}),
    )
    assert "no messenger channel" not in section
    assert "### Messenger reach" not in section
    assert "cli-carl" in section  # still listed as a plain teammate


@pytest.mark.asyncio
async def test_messenger_bound_worker_prompt_flags_cli_teammate_end_to_end(tmp_path):
    """End-to-end: build_worker_branch's real system prompt for a messenger-bound worker in a mixed-provider team (env.messenger_names precomputed the way fanout.py/flow.py do it) never tells the worker to message a CLI-only teammate as if it were reachable."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(
        tmp_path,
        exchange=exchange,
        messenger=messenger,
        roster=roster,
        team_data=_mixed_team_data(),
        # Precomputed exactly like fanout.py/flow.py: only alice/bob resolve
        # to API specs; cli-carl resolves to a CLI spec and is excluded.
        messenger_names=frozenset({"alice", "bob"}),
    )

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )

    assert messenger_bound is True
    prompt = wb.system.rendered
    assert "cli-carl (no messenger channel" in prompt
    assert "### Messenger reach" in prompt
    assert "Unknown recipient" in prompt


# ── Attached-team history: messenger-bound workers can't live-poll the
# persisted file, so prior messages must be surfaced to them some other way
# ─────────────────────────────────────────────────────────────────────────
#
# `--team-attach` loads an existing team's persisted messages (li team's
# file channel). A bash-channel worker can still `li team receive` live and
# see them; a messenger-bound worker's Exchange is fresh in-memory state for
# this run and never replays history sent before the messenger tool existed.
#
# That history is DATA (arbitrary prior user/agent text — potentially
# containing forged headings or "ignore the task"-style content), not a
# vetted instruction, so it must never be promoted into the system prompt
# (which carries the same authority as the coordination instructions
# themselves). team_history_context() shapes it for operation CONTEXT
# instead (what fanout.py/flow.py pass into `operate(context=...)`), and
# team_worker_system()'s system-prompt output must never contain it.


def _attached_team_data(team_id="t1", team_name="the-team"):
    return {
        "id": team_id,
        "name": team_name,
        "members": ["orchestrator", "alice", "bob"],
        "messages": [
            {"id": "m1", "from": "orchestrator", "to": ["*"], "content": "kickoff broadcast"},
            {"id": "m2", "from": "bob", "to": ["alice"], "content": "watch out for X"},
            {"id": "m3", "from": "orchestrator", "to": ["bob"], "content": "private to bob only"},
        ],
    }


def test_team_history_context_surfaces_prior_messages_for_messenger_bound_worker():
    ctx = team_history_context(_attached_team_data(), "alice", messenger_bound=True)
    assert ctx is not None
    digest = ctx["prior_team_messages"]
    assert "TRANSCRIPT DATA" in digest["note"]
    assert "not an instruction" in digest["note"].lower() or "not a command" in digest["note"]
    contents = [m["content"] for m in digest["messages"]]
    assert "kickoff broadcast" in contents  # broadcast: everyone sees it
    assert "watch out for X" in contents  # addressed to alice
    assert "private to bob only" not in contents  # addressed to bob, not alice


def test_team_history_context_none_when_no_prior_messages():
    assert team_history_context(_team_data(), "alice", messenger_bound=True) is None


def test_team_history_context_none_for_bash_channel_worker():
    """Bash-channel workers already see history live via `li team receive` —
    the digest is only needed (and only produced) for messenger-bound
    workers, who have no other path to it."""
    assert team_history_context(_attached_team_data(), "alice", messenger_bound=False) is None


def test_team_history_context_none_without_team_data():
    assert team_history_context(None, "alice", messenger_bound=True) is None


def test_team_worker_system_never_contains_prior_message_content():
    """The system prompt is the one place attached-team history must NEVER appear -- regression guard for the injection-surface concern: message content from a prior (potentially untrusted) sender must not be promoted to the same authority as coordination instructions."""
    section = team_worker_system(
        _attached_team_data(),
        "alice",
        messenger_bound=True,
        messenger_names=frozenset({"alice", "bob"}),
    )
    assert "### Prior team messages" not in section
    assert "kickoff broadcast" not in section
    assert "watch out for X" not in section
    assert "private to bob only" not in section


@pytest.mark.asyncio
async def test_messenger_bound_worker_system_prompt_excludes_attached_history_end_to_end(
    tmp_path,
):
    """End-to-end: build_worker_branch's real system prompt for a messenger-bound worker attaching to a team with prior messages never contains that history -- it must only reach the worker via operation context, which build_worker_branch itself does not construct."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(
        tmp_path,
        exchange=exchange,
        messenger=messenger,
        roster=roster,
        team_data=_attached_team_data(),
        messenger_names=frozenset({"alice", "bob"}),
    )

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile, messenger_bound = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )

    assert messenger_bound is True
    prompt = wb.system.rendered
    assert "### Prior team messages" not in prompt
    assert "kickoff broadcast" not in prompt
    assert "watch out for X" not in prompt
    assert "private to bob only" not in prompt

    # The content DOES exist, correctly shaped — just not here. Confirms the
    # end-to-end picture: build_worker_branch's prompt is clean, and the
    # caller-side context helper independently has the real data.
    ctx = team_history_context(env.team_data, "alice", messenger_bound=messenger_bound)
    assert ctx is not None
    assert "kickoff broadcast" in [m["content"] for m in ctx["prior_team_messages"]["messages"]]


# ── Coordinator reachability: the orchestrator is not a messenger target ───
#
# The roster line for "orchestrator" is the one entry team_worker_system()
# always emits without going through the messenger_names reachability
# filter (it isn't a teammate, so it never appears in `teammates`). Nothing
# in build_worker_branch (see its exchange-registration block) ever
# registers the orchestrator branch into env.roster/exchange — coordinator
# escalation goes through `action="help"` instead — so a messenger-bound
# worker's roster line for it must be flagged the same way an unreachable
# CLI teammate is, not left looking like a plain, sendable `to=` target.


def test_team_worker_system_flags_orchestrator_as_unreachable_for_messenger_bound_worker():
    section = team_worker_system(
        _team_data(),
        "alice",
        messenger_bound=True,
        messenger_names=frozenset({"alice", "bob"}),
    )
    assert (
        '- orchestrator (coordinator) (not a messenger recipient — use action="help" instead)'
        in section
    )
    assert "### Coordinator reach" in section
    assert 'action="help"' in section.split("### Coordinator reach", 1)[1]


def test_team_worker_system_orchestrator_line_stays_plain_for_bash_channel_worker():
    """li team send --to orchestrator always succeeds against the shared file
    channel (li team's own `to` validation only warns, never rejects), so
    the bash-channel prompt is unaffected by this fix."""
    section = team_worker_system(_team_data(), "alice", messenger_bound=False)
    assert "- orchestrator (coordinator)" in section
    assert "not a messenger recipient" not in section
    assert "### Coordinator reach" not in section


def _parse_advertised_roster(section: str) -> list[tuple[str, bool]]:
    """Parse the '### Your team' roster block of a rendered coordination section into (name, flagged_unreachable) pairs, driven entirely by the rendered text, never a hardcoded name list -- a line is 'flagged' when its own annotation says it isn't a valid messenger `to=` target."""
    block = section.split("### Your team", 1)[1].split("\n###", 1)[0]
    parsed = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:]
        name = body.split(" (", 1)[0].strip().strip("*")
        flagged = "no messenger channel" in body or "not a messenger recipient" in body
        parsed.append((name, flagged))
    return parsed


@pytest.mark.asyncio
async def test_every_advertised_messenger_recipient_is_actually_reachable(tmp_path):
    """Structural guard: parse the roster a real messenger-bound worker's OWN rendered prompt advertises, then check each parsed name against the live roster/tool it ships with, never hardcoding which names should work -- exactly the assertion that would have failed pre-fix, when 'orchestrator' appeared as a plain roster line while never being registered in env.roster."""
    exchange = Exchange()
    messenger = LionMessenger(exchange)
    roster: dict = {}
    env = _make_env(
        tmp_path,
        exchange=exchange,
        messenger=messenger,
        roster=roster,
        team_data=_mixed_team_data(),
        messenger_names=frozenset({"alice", "bob"}),
    )

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb_alice, _, _, mb_alice = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )
        await build_worker_branch(env, agent_id="bob", role="researcher", explicit_name="bob")
    assert mb_alice is True

    prompt = wb_alice.system.rendered
    tool = next(t for t in wb_alice.acts.registry.values() if t.function == "messenger")
    advertised = _parse_advertised_roster(prompt)
    assert advertised  # sanity: parsing actually found roster lines

    checked_reachable = 0
    checked_unreachable = 0
    for name, flagged in advertised:
        if name == "alice":  # this worker itself, not a send target
            continue
        result = tool.func_callable(action="send", to=name, content="ping")
        if flagged:
            assert "Unknown recipient" in result, (
                f"{name!r} is flagged unreachable in the prompt but send succeeded: {result!r}"
            )
            checked_unreachable += 1
        else:
            assert "Unknown recipient" not in result, (
                f"{name!r} is advertised as reachable but the tool rejected it: {result!r}"
            )
            checked_reachable += 1

    # sanity: the fixture actually exercises both branches of the assertion
    # (a real reachable target — bob — and real unreachable ones — cli-carl,
    # and pre-fix, orchestrator).
    assert checked_reachable > 0
    assert checked_unreachable > 0


@pytest.mark.asyncio
async def test_reactive_worker_prompt_renders_spawn_affordance_and_roster(tmp_path):
    env = _make_env(tmp_path)

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        branch, *_ = await build_worker_branch(
            env,
            agent_id="researcher",
            role="researcher",
            explicit_name="researcher",
            grant_spawn=True,
            spawn_assignees=["researcher", "reviewer"],
        )

    prompt = branch.system.rendered
    assert "Do NOT spawn sub-agents" not in prompt
    assert "## Workflow expansion" in prompt
    assert "## Spawn-request guidance" in prompt
    assert "Valid assignees: researcher, reviewer." in prompt
    assert "Allowed operations:" in prompt
    assert '"spawn_request"' in prompt


@pytest.mark.asyncio
async def test_non_reactive_worker_prompt_keeps_leaf_executor_rule(tmp_path):
    env = _make_env(tmp_path)

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        branch, *_ = await build_worker_branch(
            env,
            agent_id="researcher",
            role="researcher",
            explicit_name="researcher",
        )

    assert "Do NOT spawn sub-agents or delegate further" in branch.system.rendered


class _EntrypointSession(_FakeSession):
    async def flow(self, _graph, **_kwargs):
        return {"operation_results": {}}


def _entrypoint_env(tmp_path):
    env = _make_env(tmp_path)
    env.session = _EntrypointSession()
    env.builder = OperationGraphBuilder()
    env.run = SimpleNamespace(
        artifact_root=tmp_path,
        dag_image_path=tmp_path / "dag.png",
        synthesis_path=tmp_path / "synthesis.md",
        agent_artifact_dir=lambda name: tmp_path / name,
    )
    return env


def _model_for_mixed_pool(spec, *_args, **_kwargs):
    if str(spec).startswith("codex/"):
        return _cli_imodel()
    return _api_imodel()


@pytest.mark.asyncio
async def test_fanout_entrypoint_prepass_matches_worker_binding(tmp_path, monkeypatch):
    import lionagi.cli.orchestrate.fanout as fanout_module

    assignments = [
        TaskAssignment(task="api work", assignee="researcher"),
        TaskAssignment(task="cli work", assignee="reviewer"),
    ]
    worker_names = ["researcher", "reviewer"]
    env = _entrypoint_env(tmp_path)
    observed: dict[str, bool] = {}

    async def fake_plan(*_args, **_kwargs):
        return assignments

    async def tracked_build(env, **kwargs):
        result = await build_worker_branch(env, **kwargs)
        name = kwargs["explicit_name"]
        observed[name] = result[3]
        assert (name in env.messenger_names) is result[3]
        return result

    monkeypatch.setattr(fanout_module, "plan", fake_plan)
    monkeypatch.setattr(fanout_module, "build_worker_branch", tracked_build)
    monkeypatch.setattr(
        fanout_module,
        "_create_fanout_team",
        lambda name, members: {
            "id": "team-id",
            "name": name,
            "members": ["orchestrator", *members],
        },
    )
    monkeypatch.setattr(fanout_module, "_post_results_to_team", lambda *_a, **_kw: None)
    monkeypatch.setattr(fanout_module, "finalize_orchestration", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        _model_for_mixed_pool,
    )

    await fanout_module._run_fanout_inner(
        "openai/gpt-4o-mini",
        "mixed work",
        env=env,
        workers_str="openai/gpt-4o-mini,codex/gpt-5.5",
        team_name="mixed",
    )

    assert env.messenger_names == frozenset({"researcher"})
    assert observed == dict(zip(worker_names, (True, False), strict=True))


@pytest.mark.asyncio
async def test_flow_entrypoint_prepass_matches_worker_binding(tmp_path, monkeypatch):
    import lionagi.cli.orchestrate.flow as flow_module

    assignments = [
        TaskAssignment(task="api work", assignee="researcher"),
        TaskAssignment(task="cli work", assignee="reviewer"),
    ]
    worker_names = ["researcher", "reviewer"]
    env = _entrypoint_env(tmp_path)
    observed: dict[str, bool] = {}

    async def fake_plan(*_args, **_kwargs):
        return assignments

    async def tracked_build(env, **kwargs):
        result = await build_worker_branch(env, **kwargs)
        name = kwargs["explicit_name"]
        observed[name] = result[3]
        assert (name in env.messenger_names) is result[3]
        return result

    async def fake_execute(*_args, **_kwargs):
        return flow_module._ExecResult(agent_results=[], n_spawned=0, t_exec_elapsed=0.0)

    monkeypatch.setattr(flow_module, "plan", fake_plan)
    monkeypatch.setattr(flow_module, "build_worker_branch", tracked_build)
    monkeypatch.setattr(flow_module, "_execute_dag", fake_execute)
    monkeypatch.setattr(flow_module, "_finalize_flow", lambda *_a, **_kw: "")
    monkeypatch.setattr(
        flow_module,
        "_create_fanout_team",
        lambda name, members: {
            "id": "team-id",
            "name": name,
            "members": ["orchestrator", *members],
        },
    )
    monkeypatch.setattr(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        _model_for_mixed_pool,
    )

    await flow_module._run_flow_inner(
        "openai/gpt-4o-mini",
        "mixed work",
        env=env,
        workers_str="openai/gpt-4o-mini,codex/gpt-5.5",
        team_name="mixed",
        reactive_spec="off",
    )

    assert env.messenger_names == frozenset({"researcher"})
    assert observed == dict(zip(worker_names, (True, False), strict=True))
