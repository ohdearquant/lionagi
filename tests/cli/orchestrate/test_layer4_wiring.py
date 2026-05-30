# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for Layer 4 — CLI orchestration wiring (ADR-0073)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lionagi import Branch, Session
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    build_worker_branch,
    resolve_worker_spec,
)
from lionagi.cli.orchestrate.flow import FlowAgent, FlowPlan, _run_flow_inner

# ── FlowAgent field parsing ──────────────────────────────────────────────────


def test_flowagent_accepts_modes_and_permissions():
    a = FlowAgent(id="i1", role="implementer", modes=["adversarial"], permissions="read_only")
    assert a.modes == ["adversarial"]
    assert a.permissions == "read_only"


def test_flowagent_modes_default_empty():
    a = FlowAgent(id="r1", role="researcher")
    assert a.modes == []
    assert a.permissions is None


# ── Plan validation: unknown role ────────────────────────────────────────────


class _FakeBuilder:
    def __init__(self):
        self.added = []

    def add_operation(self, operation, **kwargs):
        node_id = f"node-{len(self.added) + 1}"
        self.added.append({"id": node_id, "operation": operation, "kwargs": kwargs})
        return node_id

    def get_graph(self):
        return object()


class _FakeSession:
    def __init__(self, builder, plan):
        self.builder = builder
        self.plan = plan

    def observe(self, event_type, handler):
        pass  # no-op for testing

    async def flow(self, _graph, **_kwargs):
        plan_root = self.builder.added[0]["id"]
        return {"operation_results": {plan_root: SimpleNamespace(plan=self.plan)}}


def _make_env(builder, plan, tmp_path):
    return SimpleNamespace(
        run=SimpleNamespace(
            artifact_root=tmp_path,
            dag_image_path=tmp_path / "dag.png",
        ),
        session=_FakeSession(builder, plan),
        orc_branch=SimpleNamespace(id=uuid4()),
        builder=builder,
        bare=True,
        effort=None,
        verbose=False,
        team_data=None,
    )


@pytest.mark.asyncio
async def test_plan_rejects_unknown_mode(tmp_path):
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher", modes=["nonexistent_mode_xyz"])],
        operations=[],
    )
    # Need at least one op for the plan to be non-empty
    from lionagi.cli.orchestrate.flow import FlowOp

    plan.operations = [FlowOp(id="o1", agent_id="a1", instruction="do it")]
    builder = _FakeBuilder()
    env = _make_env(builder, plan, tmp_path)
    result = await _run_flow_inner("codex/gpt-5.5", "task", env=env, dry_run=True)
    assert "Invalid plan" in result
    assert "unknown mode" in result.lower() or "nonexistent_mode_xyz" in result


@pytest.mark.asyncio
async def test_plan_rejects_conflicting_modes(tmp_path):
    # fast and slow conflict with each other
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher", modes=["fast", "slow"])],
        operations=[],
    )
    from lionagi.cli.orchestrate.flow import FlowOp

    plan.operations = [FlowOp(id="o1", agent_id="a1", instruction="do it")]
    builder = _FakeBuilder()
    env = _make_env(builder, plan, tmp_path)
    result = await _run_flow_inner("codex/gpt-5.5", "task", env=env, dry_run=True)
    assert "Invalid plan" in result
    assert "conflict" in result.lower() or "mode" in result.lower()


@pytest.mark.asyncio
async def test_plan_rejects_unknown_permissions_preset(tmp_path):
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher", permissions="super_yolo")],
        operations=[],
    )
    from lionagi.cli.orchestrate.flow import FlowOp

    plan.operations = [FlowOp(id="o1", agent_id="a1", instruction="do it")]
    builder = _FakeBuilder()
    env = _make_env(builder, plan, tmp_path)
    result = await _run_flow_inner("codex/gpt-5.5", "task", env=env, dry_run=True)
    assert "Invalid plan" in result
    assert "permissions" in result.lower()


@pytest.mark.asyncio
async def test_plan_accepts_valid_agent_with_modes_and_permissions(tmp_path):
    plan = FlowPlan(
        agents=[
            FlowAgent(id="a1", role="researcher", modes=["systematic"], permissions="read_only")
        ],
        operations=[],
    )
    from lionagi.cli.orchestrate.flow import FlowOp

    plan.operations = [FlowOp(id="o1", agent_id="a1", instruction="research topic")]
    builder = _FakeBuilder()
    env = _make_env(builder, plan, tmp_path)
    result = await _run_flow_inner("codex/gpt-5.5", "task", env=env, dry_run=True)
    # Should NOT be an "Invalid plan" error
    assert "Invalid plan" not in result


# ── Planner roster includes roles, modes, permissions ───────────────────────


@pytest.mark.asyncio
async def test_planner_roster_contains_roles_modes_permissions(tmp_path):
    """The plan root guidance should contain casts role/mode names and presets."""
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher")],
        operations=[],
    )
    from lionagi.cli.orchestrate.flow import FlowOp

    plan.operations = [FlowOp(id="o1", agent_id="a1", instruction="research")]
    builder = _FakeBuilder()
    env = _make_env(builder, plan, tmp_path)
    # Run dry-run — we just need the builder to have been called with guidance
    await _run_flow_inner("codex/gpt-5.5", "task", env=env, dry_run=True)

    # Inspect the guidance passed to the planner operation
    assert len(builder.added) >= 1
    guidance = builder.added[0]["kwargs"].get("instruct", SimpleNamespace(guidance="")).guidance
    assert "researcher" in guidance
    assert "adversarial" in guidance  # a known mode
    assert "allow_all" in guidance  # a known permission preset


# ── claude_code adapter ──────────────────────────────────────────────────────


def test_translate_permissions_allow_all():
    from lionagi.agent.adapters.claude_code import translate_permissions
    from lionagi.agent.permissions import PermissionPolicy

    result = translate_permissions(PermissionPolicy.allow_all())
    assert result == {"permission_mode": "bypassPermissions"}


def test_translate_permissions_deny_all():
    from lionagi.agent.adapters.claude_code import translate_permissions
    from lionagi.agent.permissions import PermissionPolicy

    result = translate_permissions(PermissionPolicy.deny_all())
    assert result["permission_mode"] == "default"
    assert "disallowed_tools" in result
    assert len(result["disallowed_tools"]) > 0


def test_translate_permissions_read_only():
    from lionagi.agent.adapters.claude_code import translate_permissions
    from lionagi.agent.permissions import PermissionPolicy

    result = translate_permissions(PermissionPolicy.read_only())
    assert result["permission_mode"] == "default"
    # editor and bash should be denied
    denied = result.get("disallowed_tools", [])
    assert "edit" in denied or "bash" in denied


def test_translate_permissions_rules_custom():
    from lionagi.agent.adapters.claude_code import translate_permissions
    from lionagi.agent.permissions import PermissionPolicy

    policy = PermissionPolicy(
        mode="rules",
        allow={"reader": ["*"]},
        deny={"bash": ["*"]},
    )
    result = translate_permissions(policy)
    assert result["permission_mode"] == "default"
    allowed = result.get("allowed_tools", [])
    denied = result.get("disallowed_tools", [])
    assert "read" in allowed
    assert "bash" in denied


# ── build_worker_branch: profile path unchanged ──────────────────────────────


def test_build_worker_branch_profile_path_unchanged(tmp_path):
    """When resolve_worker_spec finds a profile, the profile path must be used."""
    from unittest.mock import patch

    from lionagi.cli._agents import AgentProfile

    fake_profile = AgentProfile(
        name="myagent",
        system_prompt="Custom system prompt from profile",
        model="claude_code/opus",
        effort=None,
        yolo=False,
        fast_mode=False,
        lion_system=False,
        artifact_defaults=None,
        extra={},
    )

    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    run_mock = MagicMock()
    run_mock.agent_artifact_dir.return_value = tmp_path / "artifacts" / "a1"
    (tmp_path / "artifacts" / "a1").mkdir(parents=True, exist_ok=True)

    env = OrchestrationEnv(
        run=run_mock,
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        default_model_spec="claude_code/sonnet",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )

    with patch(
        "lionagi.cli.orchestrate._orchestration.load_agent_profile",
        return_value=fake_profile,
    ):
        wb, w_model, w_profile = build_worker_branch(
            env,
            agent_id="a1",
            role="myagent",
            model_override=None,
            explicit_name="myagent-1",
        )

    assert w_profile is fake_profile
    assert "Custom system prompt from profile" in wb.system.rendered


# ── build_worker_branch: casts fallback path ────────────────────────────────


def test_build_worker_branch_casts_path_composes_spec(tmp_path):
    """When no profile file exists but the role is a known casts role,
    AgentSpec should be composed and capabilities granted."""
    from unittest.mock import patch

    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    run_mock = MagicMock()
    run_mock.agent_artifact_dir.return_value = tmp_path / "artifacts" / "a1"
    (tmp_path / "artifacts" / "a1").mkdir(parents=True, exist_ok=True)

    env = OrchestrationEnv(
        run=run_mock,
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        default_model_spec="claude_code/sonnet",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )

    # Force load_agent_profile to raise FileNotFoundError so casts path is taken
    with patch(
        "lionagi.cli.orchestrate._orchestration.load_agent_profile",
        side_effect=FileNotFoundError("no profile"),
    ):
        wb, w_model, w_profile = build_worker_branch(
            env,
            agent_id="a1",
            role="critic",
            model_override=None,
            explicit_name="critic-1",
        )

    assert w_profile is None  # no profile file
    # System message should contain role body (from casts critic role)
    sys_text = wb.system.rendered
    assert sys_text  # non-empty, not bare
    # Capabilities should be granted (critic emits Verdict)
    assert wb.capabilities is not None


def test_build_worker_branch_casts_path_applies_permissions(tmp_path):
    """Permissions preset should be translated and applied to endpoint kwargs."""
    from unittest.mock import patch

    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    run_mock = MagicMock()
    run_mock.agent_artifact_dir.return_value = tmp_path / "artifacts" / "a1"
    (tmp_path / "artifacts" / "a1").mkdir(parents=True, exist_ok=True)

    env = OrchestrationEnv(
        run=run_mock,
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        default_model_spec="claude_code/sonnet",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )

    with patch(
        "lionagi.cli.orchestrate._orchestration.load_agent_profile",
        side_effect=FileNotFoundError("no profile"),
    ):
        wb, _, _ = build_worker_branch(
            env,
            agent_id="a1",
            role="researcher",
            model_override=None,
            explicit_name="researcher-1",
            agent_permissions="allow_all",
        )

    # allow_all → permission_mode: bypassPermissions in endpoint kwargs
    kwargs = wb.chat_model.endpoint.config.kwargs
    assert kwargs.get("permission_mode") == "bypassPermissions"


# ── Observer wiring ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observer_wiring_records_escalation_request(tmp_path):
    """EscalationRequest wrapped in a Signal should be recorded by the observer."""
    from lionagi.casts.capabilities import EscalationRequest
    from lionagi.session.signal import StructuredOutput

    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)

    escalations: list = []
    session.observe(EscalationRequest, lambda e, _ctx: escalations.append(e))

    # Capability events are emitted as StructuredOutput signals (the observer's
    # TypeFilter unwraps the payload to match the inner EscalationRequest).
    req = EscalationRequest(
        reason="test escalation",
        context={"detail": "unit test"},
        blocking=False,
        from_role="critic",
    )
    await session.emit(StructuredOutput(data=req))

    assert len(escalations) == 1
    assert escalations[0].reason == "test escalation"
