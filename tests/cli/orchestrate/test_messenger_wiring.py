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
)
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger


class _FakeSession:
    def __init__(self):
        self.branches: list = []

    def include_branches(self, branch):
        self.branches.append(branch)


def _make_env(tmp_path, *, exchange=None, messenger=None, roster=None, team_data=None):
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
    return env


def _team_data(team_id="t1", team_name="the-team"):
    return {"id": team_id, "name": team_name, "members": ["orchestrator", "alice", "bob"]}


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
