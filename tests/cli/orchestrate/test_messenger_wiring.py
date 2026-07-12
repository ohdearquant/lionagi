# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""In-process team messaging wiring: build_worker_branch <-> Exchange/LionMessenger."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from lionagi import iModel
from lionagi.cli.orchestrate._common import (
    TEAM_COORD_SECTION,
    TEAM_COORD_SECTION_MESSENGER,
    _build_worker_operate_node,
)
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    build_worker_branch,
    team_worker_system,
    worker_is_cli,
)
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger


class _FakeSession:
    def __init__(self):
        self.branches: list = []

    def include_branches(self, branch):
        self.branches.append(branch)


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
    """Regression guard for the exact bug class this module protects against:
    if either fanout.py or flow.py stopped routing its static operate-node
    construction through the shared `_build_worker_operate_node` helper (e.g.
    reverting to an inline, independently-editable conditional), this fails."""
    import lionagi.cli.orchestrate.fanout as fanout_mod
    import lionagi.cli.orchestrate.flow as flow_mod
    from lionagi.cli.orchestrate._common import _build_worker_operate_node

    assert fanout_mod._build_worker_operate_node is _build_worker_operate_node
    assert flow_mod._build_worker_operate_node is _build_worker_operate_node


@pytest.mark.asyncio
async def test_bound_worker_operate_serializes_messenger_tool_schema(tmp_path):
    """End-to-end: build a real bound worker branch, construct its operate
    node through the real shared builder, then drive the EXACT request dict
    produced by production through Branch.operate() with a capturing middle.
    Confirms actions=True flows through Operation._invoke() -> operate() ->
    action_param construction -> get_tool_schema(), and that the messenger
    tool's schema is what gets serialized to the model."""
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
    """A bash-channel (messenger_bound=False) worker's prompt is unaffected by
    messenger_names — only messenger-bound workers need the reachability
    filter, since the bash `li team` channel's own reachability is unrelated
    to Exchange/roster registration."""
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
    """End-to-end: build_worker_branch's real system prompt for a messenger-bound
    worker in a mixed-provider team (env.messenger_names precomputed the way
    fanout.py/flow.py do it) never tells the worker to message a CLI-only
    teammate as if it were reachable."""
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
# persisted file, so prior messages must be surfaced as a static digest ────
#
# `--team-attach` loads an existing team's persisted messages (li team's
# file channel). A bash-channel worker can still `li team receive` live and
# see them; a messenger-bound worker's Exchange is fresh in-memory state for
# this run and never replays history sent before the messenger tool existed
# — so team_worker_system must inline that history into the prompt itself.


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


def test_team_worker_system_surfaces_prior_history_for_messenger_bound_worker():
    section = team_worker_system(
        _attached_team_data(),
        "alice",
        messenger_bound=True,
        messenger_names=frozenset({"alice", "bob"}),
    )
    assert "### Prior team messages" in section
    assert "kickoff broadcast" in section  # broadcast: everyone sees it
    assert "watch out for X" in section  # addressed to alice
    assert "private to bob only" not in section  # addressed to bob, not alice


def test_team_worker_system_omits_history_section_when_no_prior_messages():
    section = team_worker_system(
        _team_data(),  # no "messages" key at all
        "alice",
        messenger_bound=True,
        messenger_names=frozenset({"alice", "bob"}),
    )
    assert "### Prior team messages" not in section


def test_team_worker_system_bash_channel_worker_gets_no_history_digest():
    """Bash-channel workers already see history live via `li team receive` —
    the static digest is only needed (and only added) for messenger-bound
    workers, who have no other path to it."""
    section = team_worker_system(
        _attached_team_data(),
        "alice",
        messenger_bound=False,
    )
    assert "### Prior team messages" not in section
    assert "kickoff broadcast" not in section


@pytest.mark.asyncio
async def test_messenger_bound_worker_prompt_includes_attached_history_end_to_end(tmp_path):
    """End-to-end: build_worker_branch's real system prompt for a messenger-
    bound worker attaching to a team with prior messages includes those
    messages as static context."""
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
    assert "### Prior team messages" in prompt
    assert "kickoff broadcast" in prompt
    assert "watch out for X" in prompt
    assert "private to bob only" not in prompt
