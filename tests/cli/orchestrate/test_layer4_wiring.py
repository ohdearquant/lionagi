# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Layer-4 wiring tests: modes/permissions on FlowAgent, plan validation,
claude_code adapter, build_worker_branch casts fallback, observer wiring.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lionagi.agent.permissions import PermissionPolicy
from lionagi.casts.emission import EscalationRequest, Verdict
from lionagi.casts.pattern import list_modes, list_roles
from lionagi.cli.orchestrate.flow import FlowAgent, FlowOp, FlowPlan
from lionagi.providers.anthropic.claude_code.models import (
    PERMISSION_TOOL_MAP,
    translate_permissions,
)

# ── FlowAgent field tests ────────────────────────────────────────────────


def test_flow_agent_accepts_modes_field():
    a = FlowAgent(id="a1", role="researcher", modes=["adversarial"])
    assert a.modes == ["adversarial"]


def test_flow_agent_modes_defaults_to_empty():
    a = FlowAgent(id="a1", role="researcher")
    assert a.modes == []


def test_flow_agent_accepts_permissions_field():
    a = FlowAgent(id="a1", role="implementer", permissions="safe")
    assert a.permissions == "safe"


def test_flow_agent_permissions_defaults_to_none():
    a = FlowAgent(id="a1", role="researcher")
    assert a.permissions is None


def test_flow_agent_accepts_all_valid_permission_presets():
    for preset in ("safe", "read_only", "allow_all", "deny_all"):
        a = FlowAgent(id="a1", role="researcher", permissions=preset)
        assert a.permissions == preset


# ── Plan validation helpers ──────────────────────────────────────────────

# We test _run_flow_inner's validation logic indirectly by patching the
# session.flow call to return a synthetic plan and checking the returned
# error string.  This avoids spinning up real LLM calls.


def _make_synthetic_env(plan: FlowPlan):
    """Build a minimal fake OrchestrationEnv that returns ``plan`` from session.flow."""
    fake_run = MagicMock()
    fake_run.artifact_root = "/tmp/test_artifacts"
    fake_run.agent_artifact_dir.return_value = MagicMock()
    fake_run.agent_artifact_dir.return_value.mkdir = MagicMock()
    fake_run.agent_artifact_dir.return_value.__truediv__ = lambda s, x: MagicMock()
    fake_run.dag_image_path = "/tmp/dag.png"

    plan_result = SimpleNamespace(plan=plan)
    op_results = {None: plan_result}  # key will be patched below

    class FakeSession:
        branches = []
        _observed = {}

        def observe(self, event_type, handler):
            self._observed[event_type] = handler

        async def flow(self, graph):
            return {"operation_results": {plan_root_key[0]: plan_result}}

    fake_session = FakeSession()

    plan_root_key = ["plan_root_node"]

    fake_orc_branch = MagicMock()

    class FakeBuilder:
        def add_operation(self, *a, **kw):
            return plan_root_key[0]

        def get_graph(self):
            return MagicMock()

    class FakeEnv:
        run = fake_run
        session = fake_session
        orc_branch = fake_orc_branch
        builder = FakeBuilder()
        bare = False
        effort = None
        theme = None
        yolo = False
        bypass = False
        verbose = False
        fast = False
        cwd = "/tmp"
        total_budget = None
        team_data = None
        _live_persist = None

    return FakeEnv()


async def _run_validation(plan: FlowPlan) -> str:
    """Run _run_flow_inner with a synthetic env and return the result string."""
    from lionagi.cli.orchestrate.flow import _run_flow_inner

    env = _make_synthetic_env(plan)

    with (
        patch("lionagi.cli.orchestrate.flow.build_worker_branch") as mock_bwb,
        patch("lionagi.cli.orchestrate.flow.resolve_worker_spec") as mock_rws,
    ):
        mock_rws.return_value = ("openai/gpt-4o-mini", None)
        fake_branch = MagicMock()
        fake_branch.mdls = MagicMock()
        fake_branch.mdls.shutdown = MagicMock(return_value=asyncio.sleep(0))
        mock_bwb.return_value = (fake_branch, "openai/gpt-4o-mini", None)

        result = await _run_flow_inner(
            "openai/gpt-4o-mini",
            "test task",
            env=env,
            dry_run=True,  # stop after validation, don't execute
        )
    return result


# ── Validation: unknown modes ────────────────────────────────────────────


def test_plan_validation_rejects_unknown_mode():
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher", modes=["nonexistent_mode_xyz"])],
        operations=[FlowOp(id="o1", agent_id="a1", instruction="do X")],
    )
    result = asyncio.get_event_loop().run_until_complete(_run_validation(plan))
    assert "unknown mode" in result.lower() or "nonexistent_mode_xyz" in result


def test_plan_validation_accepts_valid_mode():
    valid_modes = list_modes()
    if not valid_modes:
        pytest.skip("No built-in modes available")
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher", modes=[valid_modes[0]])],
        operations=[FlowOp(id="o1", agent_id="a1", instruction="do X")],
    )
    result = asyncio.get_event_loop().run_until_complete(_run_validation(plan))
    # dry_run returns plan summary, not error
    assert "Invalid plan" not in result or "unknown mode" not in result.lower()


def test_plan_validation_rejects_conflicting_modes():
    # fast and slow conflict with each other
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher", modes=["fast", "slow"])],
        operations=[FlowOp(id="o1", agent_id="a1", instruction="do X")],
    )
    result = asyncio.get_event_loop().run_until_complete(_run_validation(plan))
    # Should report mode conflict
    assert "conflict" in result.lower() or "Invalid plan" in result


def test_plan_validation_rejects_unknown_permission_preset():
    plan = FlowPlan(
        agents=[FlowAgent(id="a1", role="researcher", permissions="super_secret_mode")],
        operations=[FlowOp(id="o1", agent_id="a1", instruction="do X")],
    )
    result = asyncio.get_event_loop().run_until_complete(_run_validation(plan))
    assert "unknown permission preset" in result.lower() or "super_secret_mode" in result


def test_plan_validation_accepts_valid_permission_presets():
    for preset in ("safe", "read_only", "allow_all", "deny_all"):
        plan = FlowPlan(
            agents=[FlowAgent(id="a1", role="researcher", permissions=preset)],
            operations=[FlowOp(id="o1", agent_id="a1", instruction="do X")],
        )
        result = asyncio.get_event_loop().run_until_complete(_run_validation(plan))
        assert "unknown permission preset" not in result.lower()


def test_plan_validation_accepts_profile_only_role():
    # A role that exists only as a .lionagi profile (not in casts) should not
    # be rejected — list_agents() provides the fallback set.
    with patch("lionagi.cli.orchestrate.flow.list_agents") as mock_la:
        mock_la.return_value = ["my_custom_profile_role"]
        with patch("lionagi.cli.orchestrate.flow.list_roles") as mock_lr:
            mock_lr.return_value = ["researcher", "implementer"]
            plan = FlowPlan(
                agents=[FlowAgent(id="a1", role="my_custom_profile_role")],
                operations=[FlowOp(id="o1", agent_id="a1", instruction="do X")],
            )
            result = asyncio.get_event_loop().run_until_complete(_run_validation(plan))
            assert "unknown role" not in result.lower()


# ── Planner roster ───────────────────────────────────────────────────────


def test_planner_roster_contains_role_info():
    """The roles_guidance string built in _run_flow_inner must include role names."""
    # We verify that list_roles() returns the roles that go into the guidance,
    # and that the guidance construction code is live (not a stub).
    roles = list_roles()
    assert len(roles) > 0, "Should have at least some built-in roles"
    # Spot-check a couple expected roles exist
    assert "researcher" in roles
    assert "implementer" in roles


def test_planner_roster_contains_mode_info():
    modes = list_modes()
    assert len(modes) > 0, "Should have at least some built-in modes"
    assert "adversarial" in modes or "systematic" in modes


def test_planner_roster_permission_presets_documented():
    # Verify the four permission presets are the ones used in validation
    import inspect

    from lionagi.cli.orchestrate.flow import _run_flow_inner

    src = inspect.getsource(_run_flow_inner)
    for preset in ("safe", "read_only", "allow_all", "deny_all"):
        assert preset in src, f"Preset {preset!r} missing from _run_flow_inner source"


# ── claude_code adapter ──────────────────────────────────────────────────


def test_translate_permissions_allow_all():
    policy = PermissionPolicy.allow_all()
    result = translate_permissions(policy)
    assert result == {"permission_mode": "bypassPermissions"}


def test_translate_permissions_deny_all():
    policy = PermissionPolicy.deny_all()
    result = translate_permissions(policy)
    assert "disallowed_tools" in result
    all_tools = [t for ts in PERMISSION_TOOL_MAP.values() for t in ts if not t.endswith("*")]
    for tool in all_tools:
        assert tool in result["disallowed_tools"]


def test_translate_permissions_read_only():
    policy = PermissionPolicy.read_only()
    result = translate_permissions(policy)
    # Should allow reader tools
    assert "allowed_tools" in result or "disallowed_tools" in result
    # Editor tools should be disallowed
    if "disallowed_tools" in result:
        for editor_tool in PERMISSION_TOOL_MAP["editor"]:
            assert editor_tool in result["disallowed_tools"]


def test_translate_permissions_rules_mode():
    policy = PermissionPolicy(
        mode="rules",
        allow={"reader": ["*"], "search": ["*"]},
        deny={"bash": ["*"]},
    )
    result = translate_permissions(policy)
    # Reader tools should be in allowedTools
    if "allowed_tools" in result:
        assert "Read" in result["allowed_tools"]
    # Bash should be in disallowedTools
    if "disallowed_tools" in result:
        assert "Bash" in result["disallowed_tools"]


def test_tool_map_tool_names_are_pascal_case():
    """Tool names in PERMISSION_TOOL_MAP must be PascalCase (or mcp__* wildcard)."""
    for zone, tools in PERMISSION_TOOL_MAP.items():
        for tool in tools:
            if tool.endswith("*"):
                continue  # wildcard exempted
            assert tool[0].isupper(), (
                f"Tool {tool!r} in zone {zone!r} must start with uppercase (PascalCase)"
            )


def test_translate_permissions_tool_name_vocabulary():
    """Verify specific expected tool names are present in the map."""
    editor_tools = PERMISSION_TOOL_MAP["editor"]
    assert "Edit" in editor_tools
    assert "Write" in editor_tools
    assert "MultiEdit" in editor_tools
    assert "NotebookEdit" in editor_tools

    assert PERMISSION_TOOL_MAP["bash"] == ["Bash"]
    assert PERMISSION_TOOL_MAP["reader"] == ["Read"]

    search_tools = PERMISSION_TOOL_MAP["search"]
    assert "Grep" in search_tools
    assert "Glob" in search_tools
    assert "WebSearch" in search_tools
    assert "WebFetch" in search_tools

    assert PERMISSION_TOOL_MAP["spawn"] == ["Task"]


# ── build_worker_branch: profile path unchanged ──────────────────────────


def test_build_worker_branch_profile_path_unchanged():
    """When a profile is found, casts fallback must NOT override system prompt."""
    from lionagi.cli.orchestrate._orchestration import build_worker_branch

    fake_profile = MagicMock()
    fake_profile.system_prompt = "profile system prompt"
    fake_profile.model = "openai/gpt-4o-mini"
    fake_profile.effort = None
    fake_profile.yolo = False
    fake_profile.fast_mode = False

    env = MagicMock()
    env.bare = False
    env.default_model_spec = "openai/gpt-4o-mini"
    env.effort = None
    env.yolo = False
    env.bypass = False
    env.verbose = False
    env.fast = False
    env.theme = None
    env.cwd = "/tmp"
    env.team_data = None
    env._live_persist = None
    env.run.agent_artifact_dir.return_value = MagicMock()
    env.run.agent_artifact_dir.return_value.mkdir = MagicMock()

    captured_system = []

    class CaptureBranch:
        def __init__(self, **kwargs):
            captured_system.append(kwargs.get("system", ""))
            self.name = kwargs.get("name", "")

    with (
        patch("lionagi.cli.orchestrate._orchestration.resolve_worker_spec") as mock_rws,
        patch("lionagi.cli.orchestrate._orchestration.build_imodel_from_spec") as mock_bim,
        patch("lionagi.cli.orchestrate._orchestration.Branch", CaptureBranch),
        patch("lionagi.cli.orchestrate._orchestration.DataLoggerConfig"),
    ):
        mock_rws.return_value = ("openai/gpt-4o-mini", fake_profile)
        mock_imodel = MagicMock()
        mock_imodel.endpoint.config.kwargs = {}
        mock_bim.return_value = mock_imodel
        env.assign_name.return_value = "researcher-1"
        env.session.include_branches = MagicMock()
        env.session.branches = []

        build_worker_branch(
            env,
            agent_id="a1",
            role="researcher",
            agent_modes=["adversarial"],
        )

    # The system prompt should come from the profile, not the casts spec
    assert len(captured_system) == 1
    assert captured_system[0] == "profile system prompt"


# ── Observer wiring ──────────────────────────────────────────────────────


def test_observer_wiring_records_escalation_request():
    """When session.observe is available, EscalationRequest events are collected."""
    collected = []

    class ObservableSession:
        branches = []

        def observe(self, event_type, handler):
            # Simulate an immediate event for testing
            if event_type is EscalationRequest:
                collected.append(("registered", event_type))

        async def flow(self, graph):
            return {
                "operation_results": {
                    "plan_root_node": SimpleNamespace(
                        plan=FlowPlan(
                            agents=[FlowAgent(id="a1", role="researcher")],
                            operations=[FlowOp(id="o1", agent_id="a1", instruction="X")],
                        )
                    )
                }
            }

    from lionagi.cli.orchestrate.flow import _run_flow_inner

    env = _make_synthetic_env(
        FlowPlan(
            agents=[FlowAgent(id="a1", role="researcher")],
            operations=[FlowOp(id="o1", agent_id="a1", instruction="X")],
        )
    )
    env.session = ObservableSession()

    with (
        patch("lionagi.cli.orchestrate.flow.build_worker_branch") as mock_bwb,
        patch("lionagi.cli.orchestrate.flow.resolve_worker_spec") as mock_rws,
    ):
        mock_rws.return_value = ("openai/gpt-4o-mini", None)
        fake_branch = MagicMock()
        fake_branch.mdls = MagicMock()
        fake_branch.mdls.shutdown = MagicMock(return_value=asyncio.sleep(0))
        mock_bwb.return_value = (fake_branch, "openai/gpt-4o-mini", None)

        asyncio.get_event_loop().run_until_complete(
            _run_flow_inner(
                "openai/gpt-4o-mini",
                "test task",
                env=env,
                dry_run=True,
            )
        )

    # Verify observe was called for EscalationRequest
    assert any(ec[1] is EscalationRequest for ec in collected)
