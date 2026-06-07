import pytest

from lionagi.operations.fields import ActionRequestModel
from lionagi.protocols.action.manager import (
    ActionManager,
    ActionRequest,
    FunctionCalling,
    Tool,
)
from lionagi.protocols.generic.event import EventStatus


# Helper functions for testing
async def helper_func(x: int = 0, y: str = "default") -> str:
    """Test function."""
    return f"{x}-{y}"


def another_helper_func(x: int = 0) -> int:
    """Another test function."""
    return x + 1


@pytest.fixture
def action_manager():
    """Fixture for creating an ActionManager instance."""
    return ActionManager()


@pytest.fixture
def populated_manager():
    """Fixture for creating an ActionManager with pre-registered tools."""
    manager = ActionManager()
    manager.register_tools([helper_func, another_helper_func])
    return manager


def test_action_manager_init():
    """Test ActionManager initialization."""
    manager = ActionManager()
    assert isinstance(manager.registry, dict)
    assert len(manager.registry) == 0

    # Test with pre-registered tools
    tool = Tool(func_callable=helper_func)
    manager = ActionManager()
    manager.register_tool(tool)
    assert len(manager.registry) == 1
    assert "helper_func" in manager.registry


def test_tool_registration(action_manager):
    """Test tool registration functionality."""
    # Test registering a callable
    action_manager.register_tool(helper_func)
    assert helper_func.__name__ in action_manager.registry

    # Test registering a Tool object
    tool = Tool(func_callable=another_helper_func)
    action_manager.register_tool(tool)
    assert tool.function in action_manager.registry

    # Test duplicate registration
    with pytest.raises(ValueError):
        action_manager.register_tool(helper_func)

    # Test update existing tool
    action_manager.register_tool(helper_func, update=True)
    assert helper_func.__name__ in action_manager.registry


def test_tool_registration_validation(action_manager):
    """Test tool registration validation."""
    # Test invalid tool type
    with pytest.raises(TypeError):
        action_manager.register_tool("not_a_tool")

    # Test registering multiple tools
    tools = [helper_func, another_helper_func]
    action_manager.register_tools(tools)
    assert all(func.__name__ in action_manager.registry for func in tools)


def test_contains_check(populated_manager):
    """Test tool existence checking."""
    # Test with function name
    assert "helper_func" in populated_manager

    # Test with callable
    assert helper_func in populated_manager

    # Test with Tool object
    tool = populated_manager.registry["helper_func"]
    assert tool in populated_manager

    # Test non-existent tool
    assert "non_existent" not in populated_manager


@pytest.mark.asyncio
async def test_match_tool_action_request(populated_manager):
    """Test matching tool from ActionRequest."""
    # Test with ActionRequest
    request = ActionRequest(content={"function": "helper_func", "arguments": {"x": 1}})
    result = populated_manager.match_tool(request)
    assert isinstance(result, FunctionCalling)
    assert result.function == "helper_func"

    # Test with ActionRequestModel
    model = ActionRequestModel(function="helper_func", arguments={"x": 1})
    result = populated_manager.match_tool(model)
    assert isinstance(result, FunctionCalling)
    assert result.function == "helper_func"

    # Test invalid function name
    with pytest.raises(ValueError):
        populated_manager.match_tool(
            ActionRequest(content={"function": "invalid_func", "arguments": {}})
        )


@pytest.mark.asyncio
async def test_invoke(populated_manager):
    """Test tool invocation."""

    # Test with ActionRequest
    request = ActionRequest(content={"function": "helper_func", "arguments": {"x": 3, "y": "test"}})
    result = await populated_manager.invoke(request)
    assert result.response == "3-test"


def test_schema_list(populated_manager):
    """Test retrieving tool schemas."""
    schemas = populated_manager.schema_list
    assert isinstance(schemas, list)
    assert len(schemas) == 2
    assert all("function" in schema for schema in schemas)


def test_get_tool_schema(populated_manager):
    """Test retrieving specific tool schemas."""
    # Test with boolean=True (all tools)
    result = populated_manager.get_tool_schema(True)
    assert "tools" in result
    assert len(result["tools"]) == 2

    # Test with boolean=False (no tools)
    result = populated_manager.get_tool_schema(False)
    assert "tools" not in result

    # Test with specific tool
    result = populated_manager.get_tool_schema("helper_func")
    assert "tools" in result
    assert isinstance(result["tools"], dict)
    assert result["tools"]["function"]["name"] == "helper_func"

    # Test with list of tools
    result = populated_manager.get_tool_schema(["helper_func", "another_helper_func"])
    assert "tools" in result
    assert isinstance(result["tools"], list)
    assert len(result["tools"]) == 2

    # Test with invalid tool
    with pytest.raises(ValueError):
        populated_manager.get_tool_schema("invalid_func")

    # Test with invalid type
    with pytest.raises(TypeError):
        populated_manager.get_tool_schema(123)


def test_get_tool_schema_with_kwargs(populated_manager):
    """Test retrieving tool schemas with additional kwargs."""
    result = populated_manager.get_tool_schema(True)
    assert "tools" in result


##########################
# Additional Test Helpers
##########################


def duplicate_name_func1(x: int) -> int:
    """Function #1 with name collision."""
    return x + 10


def duplicate_name_func1(x: int, y: int) -> int:  # noqa: F811
    """Function #2 with the same name as #1 in Python scope (overrides #1)."""
    return x + y


async def failing_func():
    """An async function that always raises an error."""
    raise RuntimeError("This function always fails")


###############################
# Additional Test Scenarios
###############################


def test_register_duplicate_name_different_functions():
    """
    Test registering two different callables that Python sees as having
    the same __name__ (due to redefinition).
    In a real code base, you typically wouldn't do this, but let's ensure
    the manager handles it gracefully. The second definition overrides
    the first at the Python level, so effectively only one gets registered.
    """
    manager = ActionManager()
    manager.register_tool(duplicate_name_func1)  # This references the second definition

    # The name that ends up registered is the last one in Python scope
    assert "duplicate_name_func1" in manager.registry
    tool = manager.registry["duplicate_name_func1"]
    assert isinstance(tool, Tool)
    # The second definition is effectively what's stored
    assert tool.func_callable(5, 10) == 15  # uses x + y


def test_register_tool_update_with_different_callable():
    """
    Test that if you register a tool with name X, then attempt to register
    a different function with the same name (Python-level name collision),
    update=True will allow the override.
    """

    def func_original(x: int) -> str:
        return f"Original {x}"

    def func_updated(x: int) -> str:
        return f"Updated {x}"

    manager = ActionManager()
    manager.register_tool(func_original)
    assert "func_original" in manager.registry
    original_tool = manager.registry["func_original"]

    # Attempt to register a new callable with the same name at Python level
    # The function object is different, but has the same `__name__`.
    func_updated.__name__ = "func_original"

    # Without update=True, this should fail
    with pytest.raises(ValueError) as exc_info:
        manager.register_tool(func_updated)
    assert "already registered" in str(exc_info.value)

    # With update=True, it should replace the existing one
    manager.register_tool(func_updated, update=True)
    updated_tool = manager.registry["func_original"]
    assert updated_tool is not original_tool
    assert updated_tool.func_callable("Test") == "Updated Test"


@pytest.mark.asyncio
async def test_invoke_with_missing_arguments(populated_manager):
    """
    Test invoking a tool with missing required arguments
    to confirm that the `FunctionCalling` logic raises errors at instantiation.
    (Or if the tool has defaults, it might succeed.)
    """
    # another_helper_func(x: int=0) -> int
    # 'x' has a default, so missing 'x' should be okay => x=0
    request = ActionRequest(content={"function": "another_helper_func", "arguments": {}})
    result = await populated_manager.invoke(request)
    # The default x=0 -> returns 1
    assert result.response == 1

    # helper_func(x: int=0, y: str="default") -> str
    # This also has all defaults, so missing is also okay => "0-default"
    request = ActionRequest(content={"function": "helper_func", "arguments": {}})
    result = await populated_manager.invoke(request)
    assert result.response == "0-default"


@pytest.mark.asyncio
async def test_invoke_failure_scenario(action_manager):
    """
    Test that if a tool's callable raises an Exception,
    ActionManager.invoke returns a FunctionCalling with FAILED status.
    """
    # Register a function that always fails
    action_manager.register_tool(failing_func)

    request = ActionRequest(content={"function": "failing_func", "arguments": {}})
    result = await action_manager.invoke(request)

    assert isinstance(result, FunctionCalling)
    assert result.status == EventStatus.FAILED
    assert "This function always fails" in str(result.execution.error)


def test_get_tool_schema_with_auto_register():
    """
    Test the auto_register=True behavior in get_tool_schema().
    If the tool isn't registered, the manager should register it on the fly.
    """
    manager = ActionManager()

    def unregistered_tool(x: int) -> int:
        return x * 2

    # Attempt to retrieve schema for an unregistered tool, but auto_register=True
    schema_result = manager.get_tool_schema(unregistered_tool, auto_register=True)
    assert "tools" in schema_result
    schema = schema_result["tools"]
    assert schema["function"]["name"] == "unregistered_tool"
    # Confirm it's now in the registry
    assert "unregistered_tool" in manager.registry


def test_get_tool_schema_without_auto_register():
    """
    Test the auto_register=False behavior in get_tool_schema().
    If the tool isn't registered, it should raise a ValueError.
    """
    manager = ActionManager()

    def unregistered_tool(x: int) -> int:
        return x * 2

    with pytest.raises(ValueError) as exc_info:
        manager.get_tool_schema(unregistered_tool, auto_register=False)
    assert "is not registered" in str(exc_info.value)
    # Confirm it's still not in the registry
    assert "unregistered_tool" not in manager.registry


def test_get_tool_schema_partial_list(populated_manager):
    """
    Test passing a list that contains a mix of valid and invalid tools
    to get_tool_schema(). The first invalid should raise an error.
    """

    def brand_new_tool():
        return "I'm not in the registry yet"

    # The manager does not have brand_new_tool
    tools = ["helper_func", brand_new_tool, "another_helper_func"]

    # By default, auto_register=True, so brand_new_tool would get registered automatically.
    # Let's check that it doesn't raise an error in that case.
    schema_result = populated_manager.get_tool_schema(tools, auto_register=True)
    assert isinstance(schema_result["tools"], list)
    assert len(schema_result["tools"]) == 3
    # Confirm brand_new_tool got auto-registered
    assert "brand_new_tool" in populated_manager.registry
    populated_manager.registry.pop("brand_new_tool")
    assert "brand_new_tool" not in populated_manager.registry

    # Now if auto_register=False, it will raise ValueError at brand_new_tool
    with pytest.raises(ValueError) as exc_info:
        populated_manager.get_tool_schema(tools, auto_register=False)
    assert "Tool brand_new_tool is not registered" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Coverage gap: line 86 — duplicate Tool registration → name = tool.function
# ---------------------------------------------------------------------------


class TestRegisterToolDuplicateToolObject:
    """Line 86: register_tool with a duplicate Tool object (update=False)."""

    def test_duplicate_tool_object_raises_with_function_name(self):
        """Line 86: tool.function extracted for error message when Tool duplicated."""
        manager = ActionManager()

        def my_func(x: int) -> int:
            return x

        tool = Tool(func_callable=my_func)
        manager.register_tool(tool)

        # Second registration with same function name raises ValueError
        # __contains__ checks tool.function in registry → True
        # → enters if block → isinstance(tool, Tool) → name = tool.function (line 86)
        with pytest.raises(ValueError, match="my_func"):
            manager.register_tool(tool, update=False)

    def test_duplicate_callable_tool_registered_as_name(self):
        """Line 88: callable.__name__ extracted when function already registered."""
        manager = ActionManager()

        def another_fn(y: str) -> str:
            return y

        # Register by callable
        manager.register_tool(another_fn)

        # Second callable registration with update=False → ValueError via __name__
        with pytest.raises(ValueError, match="another_fn"):
            manager.register_tool(another_fn, update=False)
