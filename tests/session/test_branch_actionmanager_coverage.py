# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Coverage-targeted tests for:
  - lionagi/session/branch.py          (78%, ~53 uncovered lines)
  - lionagi/protocols/action/manager.py (82%, ~32 uncovered lines)
"""

import pytest

from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.messages.action_request import (
    ActionRequest,
    ActionRequestContent,
)
from lionagi.protocols.messages.manager import MessageManager
from lionagi.service.manager import iModelManager
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# Test tools
# ---------------------------------------------------------------------------


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def multiply(x: int, y: int) -> int:
    """Multiply two integers."""
    return x * y


def greet(name: str) -> str:
    """Return a greeting string."""
    return f"Hello, {name}!"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plain_branch():
    """Minimal Branch with no system message."""
    return Branch()


@pytest.fixture
def system_branch():
    """Branch with a system message."""
    return Branch(system="You are helpful.")


@pytest.fixture
def branch_with_tools():
    """Branch pre-loaded with two tools."""
    return Branch(tools=[add, multiply])


@pytest.fixture
def action_manager():
    """Empty ActionManager."""
    return ActionManager()


@pytest.fixture
def populated_manager():
    """ActionManager with 'add' pre-registered."""
    m = ActionManager()
    m.register_tool(add)
    return m


# ===========================================================================
# Branch tests
# ===========================================================================


class TestBranchSystemMessage:
    """Branch(system=...) stores the system message correctly."""

    def test_system_message_stored(self, system_branch):
        assert system_branch.system is not None

    def test_system_message_content(self, system_branch):
        assert "You are helpful." in system_branch.system.rendered

    def test_system_message_in_messages(self, system_branch):
        # The system message is part of the messages pile
        assert len(system_branch.messages) == 1

    def test_no_system_message_by_default(self, plain_branch):
        assert plain_branch.system is None
        assert len(plain_branch.messages) == 0


class TestBranchProperties:
    """Branch exposes the three manager properties."""

    def test_msgs_returns_message_manager(self, plain_branch):
        assert isinstance(plain_branch.msgs, MessageManager)

    def test_acts_returns_action_manager(self, plain_branch):
        assert isinstance(plain_branch.acts, ActionManager)

    def test_mdls_returns_imodel_manager(self, plain_branch):
        assert isinstance(plain_branch.mdls, iModelManager)

    def test_tools_dict(self, branch_with_tools):
        assert isinstance(plain_branch := branch_with_tools, Branch)
        assert isinstance(branch_with_tools.tools, dict)


class TestBranchMessageCount:
    """len(branch.messages) reflects actual message count."""

    def test_empty_branch_message_count(self, plain_branch):
        assert len(plain_branch.messages) == 0

    def test_system_branch_message_count(self, system_branch):
        assert len(system_branch.messages) == 1

    def test_messages_pile_has_correct_length(self, system_branch):
        msgs = system_branch.messages
        assert len(msgs) == 1


class TestBranchMessages:
    """branch.messages returns the messages Pile."""

    def test_messages_is_iterable(self, system_branch):
        msgs = list(system_branch.messages)
        assert len(msgs) == 1

    def test_message_has_role(self, system_branch):
        msg = list(system_branch.messages)[0]
        from lionagi.protocols.messages.base import MessageRole

        assert msg.role == MessageRole.SYSTEM

    def test_empty_messages(self, plain_branch):
        msgs = list(plain_branch.messages)
        assert msgs == []


class TestBranchRegisterTools:
    """register_tools([fn1, fn2]) registers multiple callables at once."""

    def test_register_single_tool(self, plain_branch):
        plain_branch.register_tools(add)
        assert "add" in plain_branch.acts.registry

    def test_register_list_of_tools(self, plain_branch):
        plain_branch.register_tools([add, multiply])
        assert "add" in plain_branch.acts.registry
        assert "multiply" in plain_branch.acts.registry

    def test_register_three_tools(self, plain_branch):
        plain_branch.register_tools([add, multiply, greet])
        assert len(plain_branch.acts.registry) == 3

    def test_tools_param_in_init(self, branch_with_tools):
        assert "add" in branch_with_tools.acts.registry
        assert "multiply" in branch_with_tools.acts.registry

    def test_register_duplicate_raises(self, plain_branch):
        plain_branch.register_tools(add)
        with pytest.raises(ValueError, match="already registered"):
            plain_branch.register_tools(add)

    def test_register_duplicate_with_update(self, plain_branch):
        plain_branch.register_tools(add)
        # update=True should not raise
        plain_branch.register_tools(add, update=True)
        assert "add" in plain_branch.acts.registry


class TestBranchClone:
    """Branch.clone() creates an independent copy."""

    def test_clone_returns_branch(self, system_branch):
        cloned = system_branch.clone()
        assert isinstance(cloned, Branch)

    def test_clone_has_different_id(self, system_branch):
        cloned = system_branch.clone()
        assert cloned.id != system_branch.id

    def test_clone_messages_are_independent(self, system_branch):
        cloned = system_branch.clone()
        # Adding a message to original should not affect clone
        original_count = len(cloned.messages)
        system_branch.msgs.add_message(instruction="extra instruction")
        assert len(cloned.messages) == original_count

    def test_clone_has_system_message(self, system_branch):
        cloned = system_branch.clone()
        assert cloned.system is not None

    def test_clone_of_branch_with_tools(self, branch_with_tools):
        cloned = branch_with_tools.clone()
        # Cloned branch should also have the tools
        assert "add" in cloned.acts.registry
        assert "multiply" in cloned.acts.registry

    def test_clone_invalid_sender_raises(self, system_branch):
        with pytest.raises(ValueError):
            system_branch.clone(sender="not-a-valid-id")


class TestBranchSerialization:
    """to_dict() / from_dict() serialization roundtrip."""

    def test_to_dict_returns_dict(self, plain_branch):
        d = plain_branch.to_dict()
        assert isinstance(d, dict)

    def test_to_dict_has_messages_key(self, plain_branch):
        d = plain_branch.to_dict()
        assert "messages" in d

    def test_to_dict_has_model_keys(self, plain_branch):
        d = plain_branch.to_dict()
        assert "chat_model" in d
        # parse_model is only emitted when it differs from chat_model
        if plain_branch.parse_model is not plain_branch.chat_model:
            assert "parse_model" in d

    def test_roundtrip_plain_branch(self, plain_branch):
        d = plain_branch.to_dict()
        restored = Branch.from_dict(d)
        assert isinstance(restored, Branch)
        assert len(restored.messages) == len(plain_branch.messages)

    def test_roundtrip_system_branch(self, system_branch):
        d = system_branch.to_dict()
        restored = Branch.from_dict(d)
        assert isinstance(restored, Branch)
        # System message should be present (already in messages pile)
        assert len(restored.messages) >= 1

    def test_to_dict_with_system_includes_system(self, system_branch):
        d = system_branch.to_dict()
        assert "system" in d

    def test_to_dict_metadata_field(self, plain_branch):
        d = plain_branch.to_dict()
        assert "metadata" in d


# ===========================================================================
# ActionManager tests
# ===========================================================================


class TestActionManagerInit:
    """ActionManager() empty and populated initialization."""

    def test_empty_init(self):
        m = ActionManager()
        assert isinstance(m.registry, dict)
        assert len(m.registry) == 0

    def test_init_with_tool(self):
        m = ActionManager(add)
        assert "add" in m.registry

    def test_init_with_multiple_tools(self):
        m = ActionManager(add, multiply)
        assert "add" in m.registry
        assert "multiply" in m.registry

    def test_registry_is_dict(self, action_manager):
        assert isinstance(action_manager.registry, dict)


class TestActionManagerRegisterTool:
    """register_tool() with annotated functions."""

    def test_register_callable(self, action_manager):
        action_manager.register_tool(add)
        assert "add" in action_manager.registry

    def test_tool_name_matches_function_name(self, action_manager):
        action_manager.register_tool(add)
        tool = action_manager.registry["add"]
        assert tool.function == "add"

    def test_register_returns_none(self, action_manager):
        result = action_manager.register_tool(add)
        assert result is None

    def test_registered_tool_is_tool_instance(self, action_manager):
        action_manager.register_tool(add)
        assert isinstance(action_manager.registry["add"], Tool)

    def test_duplicate_raises_value_error(self, action_manager):
        action_manager.register_tool(add)
        with pytest.raises(ValueError, match="already registered"):
            action_manager.register_tool(add)

    def test_duplicate_with_update_overwrites(self, action_manager):
        action_manager.register_tool(add)
        action_manager.register_tool(add, update=True)
        assert "add" in action_manager.registry

    def test_invalid_type_raises_type_error(self, action_manager):
        with pytest.raises(TypeError):
            action_manager.register_tool(123)  # not a callable, Tool, or dict


class TestActionManagerRegisterTools:
    """register_tools([fn1, fn2]) batch registration."""

    def test_register_list_of_two(self, action_manager):
        action_manager.register_tools([add, multiply])
        assert "add" in action_manager.registry
        assert "multiply" in action_manager.registry

    def test_register_list_of_three(self, action_manager):
        action_manager.register_tools([add, multiply, greet])
        assert len(action_manager.registry) == 3

    def test_register_single_item_list(self, action_manager):
        action_manager.register_tools([add])
        assert "add" in action_manager.registry

    def test_register_single_non_list(self, action_manager):
        action_manager.register_tools(add)
        assert "add" in action_manager.registry


class TestActionManagerContains:
    """__contains__ checks by name, callable, and Tool object."""

    def test_contains_by_string(self, populated_manager):
        assert "add" in populated_manager

    def test_not_contains_by_string(self, populated_manager):
        assert "nonexistent" not in populated_manager

    def test_contains_by_callable(self, populated_manager):
        assert add in populated_manager

    def test_contains_by_tool_object(self, populated_manager):
        tool = populated_manager.registry["add"]
        assert tool in populated_manager

    def test_not_contains_unregistered_callable(self, populated_manager):
        assert multiply not in populated_manager


class TestActionManagerMatchTool:
    """match_tool() converts ActionRequest/dict to FunctionCalling."""

    def test_match_tool_with_dict(self, populated_manager):
        fc = populated_manager.match_tool({"function": "add", "arguments": {"a": 1, "b": 2}})
        assert fc is not None

    def test_match_tool_returns_function_calling(self, populated_manager):
        from lionagi.protocols.action.function_calling import FunctionCalling

        fc = populated_manager.match_tool({"function": "add", "arguments": {"a": 1, "b": 2}})
        assert isinstance(fc, FunctionCalling)

    def test_match_tool_function_name(self, populated_manager):
        fc = populated_manager.match_tool({"function": "add", "arguments": {"a": 1, "b": 2}})
        assert fc.function == "add"

    def test_match_unknown_raises(self, populated_manager):
        with pytest.raises(ValueError, match="not registered"):
            populated_manager.match_tool({"function": "unknown", "arguments": {}})

    def test_match_tool_with_action_request(self, populated_manager):
        from lionagi.protocols.action.function_calling import FunctionCalling

        content = ActionRequestContent(function="add", arguments={"a": 3, "b": 4})
        req = ActionRequest(content=content)
        fc = populated_manager.match_tool(req)
        assert isinstance(fc, FunctionCalling)
        assert fc.function == "add"

    def test_match_tool_unsupported_type_raises(self, populated_manager):
        with pytest.raises(TypeError):
            populated_manager.match_tool("add")  # string not accepted


class TestActionManagerGetToolSchema:
    """get_tool_schema() returns correct OpenAI-compatible schema."""

    def test_get_schema_true_returns_all(self, populated_manager):
        result = populated_manager.get_tool_schema(True)
        assert "tools" in result
        assert isinstance(result["tools"], list)
        assert len(result["tools"]) == 1

    def test_get_schema_false_returns_empty(self, populated_manager):
        result = populated_manager.get_tool_schema(False)
        assert result == []

    def test_get_schema_by_string_has_function_key(self, populated_manager):
        result = populated_manager.get_tool_schema("add")
        assert "tools" in result
        # _get_tool_schema returns a single dict for a string lookup
        schema = result["tools"]
        assert "function" in schema

    def test_get_schema_function_has_name(self, populated_manager):
        result = populated_manager.get_tool_schema("add")
        schema = result["tools"]
        assert schema["function"]["name"] == "add"

    def test_get_schema_function_has_parameters(self, populated_manager):
        result = populated_manager.get_tool_schema("add")
        schema = result["tools"]
        assert "parameters" in schema["function"]

    def test_schema_list_property(self, populated_manager):
        schemas = populated_manager.schema_list
        assert isinstance(schemas, list)
        assert len(schemas) == 1

    def test_get_schema_unknown_string_raises(self, populated_manager):
        with pytest.raises(ValueError):
            populated_manager.get_tool_schema("nonexistent")

    def test_get_schema_auto_register_callable(self, action_manager):
        # Passing an unregistered callable with auto_register=True registers it
        result = action_manager.get_tool_schema(add, auto_register=True)
        assert "tools" in result
        assert "add" in action_manager.registry

    def test_get_schema_no_auto_register_raises(self, action_manager):
        with pytest.raises(ValueError):
            action_manager.get_tool_schema(add, auto_register=False)


class TestActionManagerInvoke:
    """invoke() executes tools and returns FunctionCalling result."""

    @pytest.mark.asyncio
    async def test_invoke_with_dict(self, populated_manager):
        fc = await populated_manager.invoke({"function": "add", "arguments": {"a": 2, "b": 3}})
        assert fc.execution is not None
        assert fc.execution.response == 5

    @pytest.mark.asyncio
    async def test_invoke_add_result(self, populated_manager):
        fc = await populated_manager.invoke({"function": "add", "arguments": {"a": 10, "b": 20}})
        assert fc.execution.response == 30

    @pytest.mark.asyncio
    async def test_invoke_with_action_request(self, populated_manager):
        content = ActionRequestContent(function="add", arguments={"a": 2, "b": 3})
        req = ActionRequest(content=content)
        fc = await populated_manager.invoke(req)
        assert fc.execution.response == 5

    @pytest.mark.asyncio
    async def test_invoke_multiply(self):
        m = ActionManager()
        m.register_tool(multiply)
        fc = await m.invoke({"function": "multiply", "arguments": {"x": 4, "y": 5}})
        assert fc.execution.response == 20

    @pytest.mark.asyncio
    async def test_invoke_returns_function_calling(self, populated_manager):
        from lionagi.protocols.action.function_calling import FunctionCalling

        fc = await populated_manager.invoke({"function": "add", "arguments": {"a": 1, "b": 1}})
        assert isinstance(fc, FunctionCalling)

    @pytest.mark.asyncio
    async def test_invoke_unknown_tool_raises_or_errors(self, populated_manager):
        # match_tool raises ValueError for unknown tools
        with pytest.raises(ValueError):
            await populated_manager.invoke({"function": "unknown", "arguments": {}})
