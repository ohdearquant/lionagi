# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests verifying documented agent and sandbox API matches source code."""

from __future__ import annotations

import inspect
from dataclasses import fields

# =============================================================================
# AgentSpec
# =============================================================================


class TestAgentSpecPresets:
    """Verify AgentSpec has exactly the documented presets."""

    def test_coding_preset_exists(self):
        """AgentSpec.coding() is a classmethod that returns AgentSpec."""
        from lionagi.agent.spec import AgentSpec

        assert hasattr(AgentSpec, "coding")
        spec = AgentSpec.coding()
        assert isinstance(spec, AgentSpec)

    def test_research_preset_does_not_exist(self):
        """AgentSpec.research() must NOT exist (removed; docs updated)."""
        from lionagi.agent.spec import AgentSpec

        assert not hasattr(AgentSpec, "research")

    def test_coding_tools_value(self):
        """AgentSpec.coding() sets tools to ('coding',) (CodingToolkit)."""
        from lionagi.agent.spec import AgentSpec

        spec = AgentSpec.coding()
        assert spec.tools == ("coding",)

    def test_coding_secure_default_hooks(self):
        """Secure mode (default) wires guard_destructive and guard_paths."""
        from lionagi.agent.spec import AgentSpec

        spec = AgentSpec.coding()
        # pre:bash should have guard_destructive
        assert "pre:bash" in spec.hook_handlers
        assert len(spec.hook_handlers["pre:bash"]) >= 1
        # pre:reader and pre:editor should have guard_paths
        assert "pre:reader" in spec.hook_handlers
        assert "pre:editor" in spec.hook_handlers

    def test_coding_insecure_no_hooks(self):
        """AgentSpec.coding(secure=False) does not add guard hooks."""
        from lionagi.agent.spec import AgentSpec

        spec = AgentSpec.coding(secure=False)
        assert len(spec.hook_handlers) == 0


# =============================================================================
# PermissionPolicy
# =============================================================================


class TestPermissionPolicyModes:
    """Verify PermissionPolicy modes match source (allow_all, deny_all, rules)."""

    def test_default_mode_is_allow_all(self):
        from lionagi.agent.permissions import PermissionPolicy

        policy = PermissionPolicy()
        assert policy.mode == "allow_all"

    def test_allow_all_classmethod(self):
        from lionagi.agent.permissions import PermissionPolicy

        policy = PermissionPolicy.allow_all()
        assert policy.mode == "allow_all"

    def test_deny_all_classmethod(self):
        from lionagi.agent.permissions import PermissionPolicy

        policy = PermissionPolicy.deny_all()
        assert policy.mode == "deny_all"

    def test_rules_mode_with_allow_deny(self):
        from lionagi.agent.permissions import PermissionPolicy

        policy = PermissionPolicy(
            mode="rules",
            allow={"reader": ["*"]},
            deny={"bash": ["rm *"]},
        )
        assert policy.mode == "rules"
        assert "reader" in policy.allow
        assert "bash" in policy.deny

    def test_no_allowlist_mode(self):
        """Mode 'allowlist' is NOT a valid mode (old docs were wrong)."""
        from lionagi.agent.permissions import PermissionPolicy

        policy = PermissionPolicy(mode="allowlist")
        decision = policy.check("bash", "run", {"command": "ls"})
        # 'allowlist' is not recognized; falls through to default deny
        assert decision.behavior == "deny"

    def test_no_confirm_mode(self):
        """Mode 'confirm' is NOT a valid mode (old docs were wrong)."""
        from lionagi.agent.permissions import PermissionPolicy

        policy = PermissionPolicy(mode="confirm")
        decision = policy.check("bash", "run", {"command": "ls"})
        assert decision.behavior == "deny"

    def test_constructor_uses_allow_deny_escalate_dicts(self):
        """PermissionPolicy takes allow/deny/escalate as dict params, not 'tools'."""
        from lionagi.agent.permissions import PermissionPolicy

        sig = inspect.signature(PermissionPolicy)
        param_names = set(sig.parameters.keys())
        assert "allow" in param_names
        assert "deny" in param_names
        assert "escalate" in param_names
        assert "tools" not in param_names

    def test_check_returns_permission_decision(self):
        from lionagi.agent.permissions import PermissionDecision, PermissionPolicy

        policy = PermissionPolicy(mode="allow_all")
        decision = policy.check("bash", "run", {"command": "ls"})
        assert isinstance(decision, PermissionDecision)
        assert decision.behavior == "allow"


# =============================================================================
# Hooks
# =============================================================================


class TestHookSignatures:
    """Verify hook function signatures match documented API."""

    def test_guard_destructive_is_async(self):
        from lionagi.agent.hooks import guard_destructive

        assert inspect.iscoroutinefunction(guard_destructive)

    def test_guard_destructive_signature(self):
        """guard_destructive(tool_name, action, args) — no factory."""
        from lionagi.agent.hooks import guard_destructive

        sig = inspect.signature(guard_destructive)
        params = list(sig.parameters.keys())
        assert params == ["tool_name", "action", "args"]

    def test_guard_paths_is_factory(self):
        """guard_paths is a factory returning a hook function."""
        from lionagi.agent.hooks import guard_paths

        assert not inspect.iscoroutinefunction(guard_paths)
        hook = guard_paths(allowed_paths=["/tmp"])
        assert inspect.iscoroutinefunction(hook)

    def test_guard_paths_kwarg_is_allowed_paths(self):
        """guard_paths takes allowed_paths=, not allowed=."""
        from lionagi.agent.hooks import guard_paths

        sig = inspect.signature(guard_paths)
        param_names = set(sig.parameters.keys())
        assert "allowed_paths" in param_names
        assert "allowed" not in param_names

    def test_log_tool_use_is_async(self):
        from lionagi.agent.hooks import log_tool_use

        assert inspect.iscoroutinefunction(log_tool_use)

    def test_log_tool_use_no_sink_param(self):
        """log_tool_use is a plain function, not a factory with a sink param."""
        from lionagi.agent.hooks import log_tool_use

        sig = inspect.signature(log_tool_use)
        param_names = set(sig.parameters.keys())
        assert "sink" not in param_names


# =============================================================================
# SandboxSession
# =============================================================================


class TestSandboxSessionAPI:
    """Verify SandboxSession is a dataclass with module-level async helpers."""

    def test_sandbox_session_is_dataclass(self):
        from lionagi.tools.sandbox import SandboxSession

        assert hasattr(SandboxSession, "__dataclass_fields__")

    def test_sandbox_session_fields(self):
        from lionagi.tools.sandbox import SandboxSession

        field_names = {f.name for f in fields(SandboxSession)}
        assert "worktree_path" in field_names
        assert "branch_name" in field_names
        assert "base_branch" in field_names
        assert "repo_root" in field_names
        assert "is_active" in field_names

    def test_no_create_classmethod(self):
        """SandboxSession does NOT have a .create() classmethod."""
        from lionagi.tools.sandbox import SandboxSession

        assert not hasattr(SandboxSession, "create")

    def test_no_diff_method(self):
        """SandboxSession does NOT have a .diff() instance method."""
        from lionagi.tools.sandbox import SandboxSession

        assert not hasattr(SandboxSession, "diff")

    def test_no_commit_method(self):
        """SandboxSession does NOT have a .commit() instance method."""
        from lionagi.tools.sandbox import SandboxSession

        assert not hasattr(SandboxSession, "commit")

    def test_no_merge_method(self):
        """SandboxSession does NOT have a .merge() instance method."""
        from lionagi.tools.sandbox import SandboxSession

        assert not hasattr(SandboxSession, "merge")

    def test_no_discard_method(self):
        """SandboxSession does NOT have a .discard() instance method."""
        from lionagi.tools.sandbox import SandboxSession

        assert not hasattr(SandboxSession, "discard")

    def test_module_level_create_sandbox_exists(self):
        from lionagi.tools.sandbox import create_sandbox

        assert inspect.iscoroutinefunction(create_sandbox)

    def test_module_level_sandbox_diff_exists(self):
        from lionagi.tools.sandbox import sandbox_diff

        assert inspect.iscoroutinefunction(sandbox_diff)

    def test_module_level_sandbox_commit_exists(self):
        from lionagi.tools.sandbox import sandbox_commit

        assert inspect.iscoroutinefunction(sandbox_commit)

    def test_module_level_sandbox_merge_exists(self):
        from lionagi.tools.sandbox import sandbox_merge

        assert inspect.iscoroutinefunction(sandbox_merge)

    def test_module_level_sandbox_discard_exists(self):
        from lionagi.tools.sandbox import sandbox_discard

        assert inspect.iscoroutinefunction(sandbox_discard)

    def test_create_sandbox_signature(self):
        """create_sandbox takes repo_root, base_branch, name."""
        from lionagi.tools.sandbox import create_sandbox

        sig = inspect.signature(create_sandbox)
        param_names = list(sig.parameters.keys())
        assert "repo_root" in param_names
        assert "base_branch" in param_names


# =============================================================================
# create_agent factory
# =============================================================================


class TestCreateAgentFactory:
    """Verify create_agent exists and accepts an AgentSpec."""

    def test_create_agent_is_async(self):
        from lionagi.agent.factory import create_agent

        assert inspect.iscoroutinefunction(create_agent)

    def test_create_agent_accepts_config_param(self):
        from lionagi.agent.factory import create_agent

        sig = inspect.signature(create_agent)
        param_names = list(sig.parameters.keys())
        assert "config" in param_names
