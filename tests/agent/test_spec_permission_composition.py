# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterization: what an AgentSpec installs on its tools, and how permission
policy, tool processors and the session gate compose today.

These tests pin current behavior for a later change to preserve, not to argue
for. A few pinned facts are surprising or look wrong; each is documented where
it is asserted rather than fixed here.
"""

from __future__ import annotations

import pytest

from lionagi.agent.factory import (
    _apply_permissions,
    _attach_hooks,
    _chain_post_hooks,
    _chain_pre_hooks,
    _compose_postprocessor,
    _compose_preprocessor,
    _tool_hooks,
    create_agent,
)
from lionagi.agent.gate import ControlUnavailableError, GateDeniedError
from lionagi.agent.permissions import PermissionPolicy
from lionagi.agent.spec import AgentSpec
from lionagi.operations.act.act import _act
from lionagi.protocols.action.function_calling import FunctionCalling
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.action.tool_hooks import ActionGovernanceDeniedError
from lionagi.protocols.generic.event import EventStatus
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied

# ── Composed preprocessor: tool's own first, then the spec chain ──────────


async def test_tool_preprocessor_runs_before_the_spec_chain():
    order: list[str] = []

    async def tool_pre(args, **kw):
        order.append("tool")
        return {**args, "seen": [*args.get("seen", []), "tool"]}

    async def security(name, action, args):
        order.append("security")
        return None

    async def user(name, action, args):
        order.append("user")
        return {**args, "seen": [*args["seen"], "user"]}

    spec = AgentSpec.compose("analyst")
    spec.security_pre("add", security)
    spec.pre("add", user)

    tool = Tool(func_callable=lambda **kw: 0, preprocessor=tool_pre)
    _attach_hooks(tool, spec, "add")

    args = await tool.preprocessor({"seen": []})
    assert order == ["tool", "security", "user", "security"]
    assert args["seen"] == ["tool", "user"]


async def test_security_pass_runs_twice_around_a_user_pre_hook():
    order: list[str] = []

    async def security(name, action, args):
        order.append("security")

    async def user(name, action, args):
        order.append("user")

    chained = _chain_pre_hooks("bash", [security], [user])
    await chained({"action": "run"})
    assert order == ["security", "user", "security"]


async def test_a_user_rewrite_is_seen_by_the_second_security_pass():
    seen: list[dict] = []

    async def security(name, action, args):
        seen.append(dict(args))

    async def rewrite(name, action, args):
        return {"command": "rm -rf /"}

    chained = _chain_pre_hooks("bash", [security], [rewrite])
    args = await chained({"command": "ls"})
    assert seen == [{"command": "ls"}, {"command": "rm -rf /"}]
    assert args == {"command": "rm -rf /"}


async def test_the_second_security_pass_can_deny_a_user_rewrite():
    calls = {"n": 0}

    async def security(name, action, args):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("recheck refused")

    async def rewrite(name, action, args):
        return {"command": "rm -rf /"}

    chained = _chain_pre_hooks("bash", [security], [rewrite])
    with pytest.raises(GateDeniedError, match="recheck refused") as exc:
        await chained({"command": "ls"})
    assert exc.value.result.control == "security"


async def test_security_only_chain_runs_the_control_once():
    calls: list[str] = []

    async def security(name, action, args):
        calls.append(name)

    chained = _chain_pre_hooks("bash", [security], [])
    await chained({"action": "run"})
    assert calls == ["bash"]


async def test_composed_preprocessor_runs_a_sync_tools_own_preprocessor_first():
    def sync_pre(args, **kw):
        return {**args, "sync": True}

    async def spec_pre(args, **kw):
        assert args["sync"] is True
        return {**args, "spec": True}

    composed = _compose_preprocessor(sync_pre, spec_pre)
    assert await composed({"a": 1}) == {"a": 1, "sync": True, "spec": True}


# ── Composed postprocessor: spec chain first, then the tool's own ─────────


async def test_spec_postprocessor_runs_before_the_tools_own_postprocessor():
    order: list[tuple[str, dict]] = []

    async def tool_post(result, **kw):
        order.append(("tool", dict(result)))
        return {**result, "tool": True}

    async def spec_post(name, action, args, result):
        order.append(("spec", dict(result)))
        return {**result, "spec": True}

    spec = AgentSpec.compose("analyst")
    spec.post("add", spec_post)

    tool = Tool(func_callable=lambda **kw: 0, postprocessor=tool_post)
    _attach_hooks(tool, spec, "add")

    result = await tool.postprocessor({"value": 1})
    assert result == {"value": 1, "spec": True, "tool": True}
    assert order == [("spec", {"value": 1}), ("tool", {"value": 1, "spec": True})]


async def test_the_spec_post_chain_is_skipped_for_a_non_dict_result():
    """The spec post chain only runs for dict results; an int/str/None result
    passes through the chain untouched."""
    calls: list[str] = []

    async def spec_post(name, action, args, result):
        calls.append("spec")
        return result

    spec = AgentSpec.compose("analyst")
    spec.post("add", spec_post)

    tool = Tool(func_callable=lambda **kw: 0)
    _attach_hooks(tool, spec, "add")

    assert await tool.postprocessor(7) == 7
    assert calls == []


async def test_post_hook_receives_the_tool_name_empty_action_and_empty_args():
    seen: list[tuple] = []

    async def hook(name, action, args, result):
        seen.append((name, action, args, result))

    chained = _chain_post_hooks("reader", [hook])
    assert await chained({"ok": 1}) == {"ok": 1}
    assert seen == [("reader", "", {}, {"ok": 1})]


async def test_compose_postprocessor_without_an_original_returns_the_spec_chain_bare():
    """The dict guard lives in _chain_post_hooks; _compose_postprocessor with
    no original is just the spec chain, unwrapped."""

    async def spec_post(result, **kw):
        return {**result, "spec": True}

    composed = _compose_postprocessor(None, spec_post)
    assert composed is spec_post


# ── Where the security controls sit in the chain ──────────────────────────


def test_security_bucket_order_is_star_then_name_then_name_tool():
    spec = AgentSpec.compose("analyst")

    async def star(name, action, args): ...
    async def named(name, action, args): ...
    async def suffixed(name, action, args): ...

    spec.security_pre("*", star)
    spec.security_pre("reader", named)
    spec.security_pre("reader_tool", suffixed)

    hooks = _tool_hooks(spec, "security_pre", "reader")
    assert [h.__name__ for h in hooks] == ["star", "named", "suffixed"]


def test_attach_hooks_includes_the_tool_suffixed_bucket_for_a_canonical_name():
    spec = AgentSpec.compose("analyst")

    async def alias_guard(name, action, args): ...

    spec.security_pre("reader_tool", alias_guard)
    tool = Tool(func_callable=lambda **kw: 0)
    _attach_hooks(tool, spec, "reader")
    assert tool.preprocessor is not None
    assert alias_guard in _tool_hooks(spec, "security_pre", "reader")


def test_permission_policy_is_inserted_at_the_front_of_the_security_bucket():
    spec = AgentSpec.compose("analyst")

    async def wildcard(name, action, args): ...

    spec.security_pre("*", wildcard)
    spec.permissions = PermissionPolicy.deny_all()
    _apply_permissions(spec)

    names = [h.__name__ for h in spec.hook_handlers["security_pre:*"]]
    assert names == ["permission_check", "wildcard"]


async def test_coding_preset_permission_denial_wins_over_the_destructive_guard(tmp_path):
    """A read-only policy denies bash before guard_destructive is reached, so
    the denial that surfaces names the permission policy, not the guard."""
    spec = AgentSpec.coding(cwd=str(tmp_path))
    spec.permissions = PermissionPolicy.read_only()
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)

    with pytest.raises(GateDeniedError) as exc:
        await branch.acts.registry["bash"].preprocessor({"action": "run", "command": "echo hi"})
    assert exc.value.result.control == "permission_check"


async def test_coding_preset_without_a_policy_still_wires_the_destructive_guard(tmp_path):
    spec = AgentSpec.coding(cwd=str(tmp_path))
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)

    bash = branch.acts.registry["bash"]
    assert bash.preprocessor is not None
    with pytest.raises(GateDeniedError) as exc:
        await bash.preprocessor({"action": "run", "command": "rm -rf /tmp/x"})
    assert exc.value.result.control == "guard_destructive"


def test_omitting_a_policy_installs_no_security_hook():
    spec = AgentSpec.compose("analyst")
    _apply_permissions(spec)
    assert spec.hook_handlers == {}


def test_a_policy_assigned_as_a_plain_dict_is_silently_ignored():
    """compose() resolves a dict/str into a PermissionPolicy, but a policy
    assigned directly to the field as a dict is not resolved: _apply_permissions
    only accepts a PermissionPolicy and returns early, so no hook is installed."""
    from lionagi.casts.profile import Profile

    spec = AgentSpec(profile=Profile.compose("analyst"), permissions={"mode": "deny_all"})
    _apply_permissions(spec)
    assert spec.hook_handlers == {}


# ── The governance error a processor raises and where it surfaces ─────────


async def test_a_permission_denial_raises_a_gate_denied_permission_error():
    spec = AgentSpec.compose("analyst", tools=["reader"], permissions=PermissionPolicy.deny_all())
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)
    tool = branch.acts.registry["reader_tool"]

    with pytest.raises(GateDeniedError) as exc:
        await tool.preprocessor({"action": "read", "path": "/tmp/x"})
    assert isinstance(exc.value, PermissionError)
    assert exc.value.result.allowed is False
    assert exc.value.result.control == "permission_check"
    assert exc.value.result.errored is False
    assert "mode=deny_all" in exc.value.result.reason


async def test_the_governance_error_is_not_an_action_governance_denied_error():
    """A processor denial is a GateDeniedError, not the ActionGovernanceDeniedError
    that a manager tool-pre hook denial raises; the act path treats the two
    differently (see the suppression tests below)."""
    spec = AgentSpec.compose("analyst", tools=["reader"], permissions=PermissionPolicy.deny_all())
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)

    with pytest.raises(GateDeniedError) as exc:
        await branch.acts.registry["reader_tool"].preprocessor({"action": "read", "path": "/tmp/x"})
    assert not isinstance(exc.value, ActionGovernanceDeniedError)


async def test_denial_surfaces_as_a_failed_function_calling_with_the_error_attached():
    spec = AgentSpec.compose("analyst")
    spec.permissions = PermissionPolicy.deny_all()
    _apply_permissions(spec)

    tool = Tool(func_callable=lambda **kw: 0)
    _attach_hooks(tool, spec, "add")
    fc = FunctionCalling(func_tool=tool, arguments={})
    await fc.invoke()

    assert fc.status == EventStatus.FAILED
    assert isinstance(fc.execution.error, GateDeniedError)
    assert fc.response is None


async def test_act_path_raises_a_processor_denial(plugin_home):
    spec = AgentSpec.compose("analyst", tools=["reader"], permissions=PermissionPolicy.deny_all())
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)
    Session(default_branch=branch)

    with pytest.raises(GateDeniedError):
        await _act(
            branch, {"function": "reader_tool", "arguments": {"action": "read", "path": "/x"}}
        )


async def test_act_path_suppresses_a_processor_denial_to_a_none_output(plugin_home):
    """With suppress_errors the act path returns a response whose output is
    None for a processor denial -- the error dict branch belongs to the
    ActionGovernanceDeniedError type, which a GateDeniedError is not."""
    spec = AgentSpec.compose("analyst", tools=["reader"], permissions=PermissionPolicy.deny_all())
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)
    Session(default_branch=branch)

    result = await _act(
        branch,
        {"function": "reader_tool", "arguments": {"action": "read", "path": "/x"}},
        suppress_errors=True,
    )
    assert result.output is None


# ── PermissionPolicy: judging arguments, escalation, denial ───────────────


def test_check_judges_the_argument_string_per_tool():
    policy = PermissionPolicy(
        mode="rules",
        allow={"editor": ["*.py"], "reader": ["/tmp/*"], "search": ["*.py *"], "bash": ["git *"]},
    )
    assert policy.check("editor", "write", {"file_path": "/x/y.py"}).behavior == "allow"
    assert policy.check("editor", "write", {"file_path": "/x/y.txt"}).behavior == "deny"
    assert policy.check("reader", "read", {"path": "/tmp/a"}).behavior == "allow"
    assert policy.check("reader", "read", {"path": "/etc/a"}).behavior == "deny"
    assert policy.check("search", "grep", {"pattern": "mod.py", "path": "src"}).behavior == "allow"
    assert policy.check("bash", "run", {"command": "git status"}).behavior == "allow"
    assert policy.check("bash", "run", {"command": "ls"}).behavior == "deny"


def test_check_reports_the_matched_rule_and_a_reason():
    policy = PermissionPolicy(mode="rules", deny={"bash": ["rm *"]})
    denied = policy.check("bash", "run", {"command": "rm -rf /"})
    assert denied.behavior == "deny"
    assert denied.matched_rule == "rm *"
    assert denied.reason == "denied by rule: rm *"
    assert (denied.tool_name, denied.action) == ("bash", "run")

    unmatched = policy.check("bash", "run", {"command": "ls"})
    assert unmatched.matched_rule is None
    assert unmatched.reason == "no matching rule, default deny"


def test_check_normalizes_an_alias_and_case_at_the_call_site():
    """Both the exact-case alias "Reader_Tool" and the upper-case "BASH_TOOL"
    match rules written for the canonical lower-case name; without alias and
    case normalization neither rule could match."""
    policy = PermissionPolicy(mode="rules", allow={"reader": ["*"], "bash": ["*"]})
    assert policy.check("Reader_Tool", "read", {"path": "/tmp/x"}).behavior == "allow"
    assert policy.check("BASH_TOOL", "run", {"command": "ls"}).behavior == "allow"


def test_a_shell_control_operator_is_denied_before_an_allow_all_rule():
    policy = PermissionPolicy(mode="rules", allow={"bash": ["*"]})
    denied = policy.check("bash", "run", {"command": "git status && rm -rf /"})
    assert denied.behavior == "deny"
    assert "Shell control operator" in denied.reason


def test_an_empty_rule_set_default_denies_every_tool():
    policy = PermissionPolicy(mode="rules")
    for tool, args in [
        ("reader", {"path": "/tmp/x"}),
        ("bash", {"command": "ls"}),
        ("anything", {"x": 1}),
    ]:
        decision = policy.check(tool, "act", args)
        assert decision.behavior == "deny"
        assert decision.reason == "no matching rule, default deny"


def test_deny_is_taken_before_escalate_when_both_match():
    policy = PermissionPolicy(mode="rules", deny={"bash": ["git *"]}, escalate={"bash": ["*"]})
    assert policy.check("bash", "run", {"command": "git log"}).behavior == "deny"


async def test_to_pre_hook_returns_none_when_the_call_is_allowed():
    hook = PermissionPolicy.allow_all().to_pre_hook()
    assert await hook("bash", "run", {"command": "anything"}) is None


async def test_deny_all_raises_a_permission_error_naming_the_mode():
    hook = PermissionPolicy.deny_all().to_pre_hook()
    with pytest.raises(PermissionError, match="mode=deny_all"):
        await hook("bash", "run", {"command": "ls"})


async def test_an_escalation_handler_returning_true_allows_the_call():
    async def allow(decision, args):
        return True

    hook = PermissionPolicy(mode="rules", escalate={"bash": ["*"]}, on_escalate=allow).to_pre_hook()
    assert await hook("bash", "run", {"command": "deploy"}) is None


async def test_an_escalation_handler_returning_a_dict_rewrites_the_arguments():
    async def rewrite(decision, args):
        return {**args, "command": "ls"}

    hook = PermissionPolicy(
        mode="rules", escalate={"bash": ["*"]}, on_escalate=rewrite
    ).to_pre_hook()
    assert await hook("bash", "run", {"command": "danger"}) == {"command": "ls"}


async def test_an_escalation_handler_declining_is_a_verdict_not_a_fault():
    async def decline(decision, args):
        return False

    hook = PermissionPolicy(
        mode="rules", escalate={"bash": ["*"]}, on_escalate=decline
    ).to_pre_hook()
    with pytest.raises(PermissionError, match="was declined") as exc:
        await hook("bash", "run", {"command": "danger"})
    assert not isinstance(exc.value, ControlUnavailableError)


async def test_an_escalation_handler_returning_a_truthy_non_true_value_is_declined():
    """Only the literal True allows; any other non-dict return, truthy included,
    is a declined verdict rather than a fault."""

    async def vague(decision, args):
        return "yes"

    hook = PermissionPolicy(mode="rules", escalate={"bash": ["*"]}, on_escalate=vague).to_pre_hook()
    with pytest.raises(PermissionError, match="was declined") as exc:
        await hook("bash", "run", {"command": "danger"})
    assert not isinstance(exc.value, ControlUnavailableError)


async def test_the_policy_pre_hook_raises_control_unavailable_without_an_escalation_handler():
    hook = PermissionPolicy(mode="rules", escalate={"bash": ["*"]}).to_pre_hook()
    with pytest.raises(ControlUnavailableError, match="No escalation handler configured"):
        await hook("bash", "run", {"command": "danger"})


async def test_the_policy_pre_hook_requires_an_awaitable_escalation_handler():
    """The policy awaits on_escalate directly; a sync handler is a TypeError,
    not a denial."""
    hook = PermissionPolicy(
        mode="rules", escalate={"bash": ["*"]}, on_escalate=lambda decision, args: True
    ).to_pre_hook()
    with pytest.raises(TypeError):
        await hook("bash", "run", {"command": "danger"})


async def test_installed_chain_turns_a_missing_escalation_handler_into_an_errored_denial():
    """Through create_agent the policy hook is wrapped by adapt_legacy_hook: the
    ControlUnavailableError fault becomes GateDeniedError with errored=True and the
    control named permission_check, not a raised fault type."""
    spec = AgentSpec.compose("analyst", tools=["bash"])
    spec.permissions = PermissionPolicy(mode="rules", escalate={"bash": ["*"]})
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)

    bash = branch.acts.registry["bash_tool"]
    with pytest.raises(GateDeniedError) as exc:
        await bash.preprocessor({"action": "run", "command": "deploy"})
    assert exc.value.result.errored is True
    assert exc.value.result.control == "permission_check"
    assert "No escalation handler configured" in exc.value.result.reason


async def test_installed_chain_turns_a_synchronous_escalation_handler_into_an_errored_denial():
    """A sync on_escalate raises TypeError inside the wrapped hook; the chain
    fails closed with a GateDeniedError whose reason is the evaluator error."""
    spec = AgentSpec.compose("analyst", tools=["bash"])
    spec.permissions = PermissionPolicy(
        mode="rules", escalate={"bash": ["*"]}, on_escalate=lambda decision, args: True
    )
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)

    bash = branch.acts.registry["bash_tool"]
    with pytest.raises(GateDeniedError) as exc:
        await bash.preprocessor({"action": "run", "command": "deploy"})
    assert exc.value.result.errored is True
    assert exc.value.result.control == "permission_check"
    assert (
        exc.value.result.reason
        == "evaluator error: object bool can't be used in 'await' expression"
    )


# ── Omitted, empty and resolved policies ──────────────────────────────────


async def test_an_omitted_policy_leaves_the_tool_unprocessed():
    spec = AgentSpec.compose("analyst", tools=["reader"])
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)
    assert branch.acts.registry["reader_tool"].preprocessor is None


async def test_an_explicit_empty_dict_policy_resolves_to_allow_all():
    spec = AgentSpec.compose("analyst", permissions={})
    assert spec.permissions.mode == "allow_all"
    _apply_permissions(spec)
    assert spec.hook_handlers["security_pre:*"]


async def test_an_empty_rule_policy_denies_a_tool_that_an_omitted_policy_would_allow():
    spec = AgentSpec.compose("analyst", tools=["reader"])
    spec.permissions = PermissionPolicy(mode="rules")
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)

    with pytest.raises(GateDeniedError, match="no matching rule"):
        await branch.acts.registry["reader_tool"].preprocessor({"action": "read", "path": "/tmp/x"})


async def test_a_child_branch_is_governed_by_the_same_session_gate_as_its_parent():
    session = Session()
    parent = session.default_branch
    child = session.new_branch()

    session.gate(lambda inv: inv.function != "danger")
    assert parent._observer is session.observer
    assert child._observer is session.observer
    assert await parent.authorize(ToolInvocation(function="danger")) is False
    assert await child.authorize(ToolInvocation(function="danger")) is False
    assert await child.authorize(ToolInvocation(function="safe")) is True


async def test_a_child_branch_removed_from_the_session_allows_everything():
    """Authority is the session observer, not a property of the branch: once a
    branch is detached its authorize() returns True unconditionally."""
    session = Session()
    child = session.new_branch()
    session.gate(lambda inv: False)

    session.remove_branch(child)
    assert child._observer is None
    assert await child.authorize(ToolInvocation(function="danger")) is True


async def test_a_branch_outside_any_session_allows_everything():
    """A branch with no session observer has no gate to consult and allows."""
    from lionagi.session.branch import Branch

    standalone = Branch()
    assert standalone._observer is None
    assert await standalone.authorize(ToolInvocation(function="danger")) is True


async def test_spec_permission_denies_even_when_the_session_gate_allows(plugin_home):
    spec = AgentSpec.compose("analyst", tools=["reader"], permissions=PermissionPolicy.deny_all())
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)
    session = Session(default_branch=branch)
    session.gate(lambda inv: True)

    result = await _act(
        branch,
        {"function": "reader_tool", "arguments": {"action": "read", "path": "/x"}},
        suppress_errors=True,
    )
    assert result.output is None
    assert session.observer.by_type(GateDenied) == []


async def test_the_gate_is_consulted_before_the_spec_processor_runs(plugin_home):
    """The recorded call order proves the gate precedes the spec security chain:
    a denied gate stops the call before the processor, and an allowed one lets the
    processor run after it. The denial output alone cannot show whether the spec
    chain was reached, so only the call order does."""
    order: list[str] = []

    spec = AgentSpec.compose("analyst", tools=["reader"], permissions=PermissionPolicy.allow_all())

    async def processor(name, action, args):
        order.append("processor")

    spec.security_pre("reader", processor)
    branch = await create_agent(spec, load_settings=False, resolved_mcp_servers=None)
    session = Session(default_branch=branch)

    verdict = {"allow": False}

    def gate(inv):
        order.append("gate")
        return verdict["allow"]

    session.gate(gate)

    denied = await _act(
        branch, {"function": "reader_tool", "arguments": {"action": "read", "path": "/x"}}
    )
    assert "processor" not in order, "a denied gate must stop the call before the spec chain"
    assert denied.output == {"error": "denied by governance gate", "function": "reader_tool"}
    assert len(session.observer.by_type(GateDenied)) == 1

    order.clear()
    verdict["allow"] = True
    await _act(branch, {"function": "reader_tool", "arguments": {"action": "read", "path": "/x"}})

    assert "processor" in order, "an allowed gate must let the spec chain run"
    assert order.index("gate") < order.index("processor")
