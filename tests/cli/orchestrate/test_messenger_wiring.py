# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""In-process team messaging wiring: build_worker_branch <-> Exchange/LionMessenger."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from lionagi import iModel
from lionagi.cli.orchestrate._orchestration import OrchestrationEnv, build_worker_branch
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger


class _FakeSession:
    def __init__(self):
        self.branches: list = []

    def include_branches(self, branch):
        self.branches.append(branch)


def _make_env(tmp_path, *, exchange=None, messenger=None, roster=None):
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
    )
    env.assign_name = assign_name
    env.register_name = register_name
    env.exchange = exchange
    env.messenger = messenger
    env.roster = roster
    return env


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
        wb, _model, _profile = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )

    assert exchange.has(wb.id)
    assert roster["alice"] == wb.id
    assert any(t.function == "messenger" for t in wb.acts.registry.values())


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
        wb, _model, _profile = await build_worker_branch(
            env, agent_id="cli-worker", role="researcher", explicit_name="cli-worker"
        )

    assert not exchange.has(wb.id)
    assert "cli-worker" not in roster
    assert not any(t.function == "messenger" for t in wb.acts.registry.values())


@pytest.mark.asyncio
async def test_no_exchange_configured_skips_binding_entirely(tmp_path):
    """team mode inactive (env.exchange/messenger/roster all None): no-op, no crash."""
    env = _make_env(tmp_path)  # exchange/messenger/roster default None

    with patch(
        "lionagi.cli.orchestrate._orchestration.build_imodel_from_spec",
        side_effect=_api_imodel,
    ):
        wb, _model, _profile = await build_worker_branch(
            env, agent_id="solo", role="researcher", explicit_name="solo"
        )

    assert not any(t.function == "messenger" for t in wb.acts.registry.values())


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
        branch_a, _, _ = await build_worker_branch(
            env, agent_id="alice", role="researcher", explicit_name="alice"
        )
        branch_b, _, _ = await build_worker_branch(
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
