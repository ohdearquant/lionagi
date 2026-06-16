"""Tests that documented code examples in core-concepts docs are structurally correct."""

import pytest

from lionagi.testing import LionAGIMockFactory

# ---------------------------------------------------------------------------
# A -- Imports & Construction (no mocks, no LLM calls)
# ---------------------------------------------------------------------------


class TestImportsAndTopLevelExports:
    """Verify that all documented import paths resolve correctly."""

    def test_top_level_session_branch_imodel(self):
        from lionagi import Branch, Session, iModel

        assert Branch is not None
        assert Session is not None
        assert iModel is not None

    def test_top_level_primitives(self):
        from lionagi import Edge, Element, Graph, Node, Pile, Progression

        assert Element is not None
        assert Pile is not None
        assert Progression is not None
        assert Node is not None
        assert Edge is not None
        assert Graph is not None

    def test_top_level_event_and_hooks(self):
        from lionagi import Event, HookRegistry

        assert Event is not None
        assert HookRegistry is not None

    def test_top_level_model_utilities(self):
        from lionagi import FieldModel, OperableModel, Undefined, Unset

        assert FieldModel is not None
        assert OperableModel is not None
        assert Undefined is not None
        assert Unset is not None

    def test_top_level_misc(self):
        from lionagi import BaseModel, Field, __version__, ln, load_mcp_tools

        assert __version__ is not None
        assert ln is not None
        assert load_mcp_tools is not None
        assert BaseModel is not None
        assert Field is not None

    def test_message_types_import(self):
        from lionagi.protocols.messages import (
            ActionRequest,
            ActionResponse,
            AssistantResponse,
            Instruction,
            MessageManager,
            MessageRole,
            RoledMessage,
            SenderRecipient,
            System,
        )

        for cls in (
            ActionRequest,
            ActionResponse,
            AssistantResponse,
            Instruction,
            System,
            RoledMessage,
            MessageManager,
            MessageRole,
            SenderRecipient,
        ):
            assert cls is not None

    def test_function_to_schema_import(self):
        from lionagi.libs.schema.function_to_schema import function_to_schema

        assert callable(function_to_schema)

    def test_tool_class_import(self):
        from lionagi.protocols.action.tool import Tool

        assert Tool is not None


class TestBranchConstruction:
    """Branch() with various documented constructor patterns."""

    def test_minimal_branch(self):
        from lionagi import Branch

        branch = Branch()
        assert branch is not None
        assert branch.id is not None

    def test_branch_with_system_and_name(self):
        from lionagi import Branch

        branch = Branch(system="You are a research assistant.", name="researcher")
        assert branch.name == "researcher"
        assert branch.system is not None

    def test_branch_with_user(self):
        from lionagi import Branch

        branch = Branch(user="alice", name="test")
        assert branch.user == "alice"

    def test_branch_with_imodel(self):
        from lionagi import Branch, iModel

        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test")
        branch = Branch(chat_model=model, system="Hello")
        assert branch.chat_model is model


class TestIModelConstruction:
    """iModel() with documented provider configurations."""

    def test_openai_provider(self):
        from lionagi import iModel

        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        assert model is not None
        assert model.endpoint is not None

    def test_anthropic_provider(self):
        from lionagi import iModel

        model = iModel(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key="test-key",
        )
        assert model is not None


class TestPrimitiveConstruction:
    """Element, Pile, Progression basic construction."""

    def test_element_construction(self):
        from lionagi import Element

        elem = Element()
        assert elem.id is not None
        assert elem.created_at is not None
        assert isinstance(elem.metadata, dict)

    def test_pile_construction(self):
        from lionagi import Element, Pile

        pile = Pile()
        assert len(pile) == 0

        e1 = Element()
        e2 = Element()
        pile = Pile(collections=[e1, e2])
        assert len(pile) == 2
        # O(1) UUID-keyed access
        assert pile[e1.id] is e1

    def test_progression_construction(self):
        from lionagi import Progression

        prog = Progression()
        assert len(prog) == 0

    def test_node_construction(self):
        from lionagi import Node

        node = Node(content="test content")
        assert node.content == "test content"
        assert node.id is not None


class TestSessionConstruction:
    """Session() documented construction patterns."""

    def test_session_minimal(self):
        from lionagi import Session

        session = Session()
        assert session is not None
        assert session.default_branch is not None

    def test_session_new_branch(self):
        from lionagi import Session

        session = Session()
        branch = session.new_branch(name="analyst", system="Analyze data")
        assert branch.name == "analyst"
        assert branch in session.branches

    def test_session_default_branch_exists(self):
        from lionagi import Session

        session = Session()
        assert session.default_branch is not None
        assert len(session.branches) >= 1


# ---------------------------------------------------------------------------
# B -- Schema & Tool Registration (no mocks)
# ---------------------------------------------------------------------------


class TestSchemaAndToolRegistration:
    """function_to_schema and tool registration patterns from docs."""

    def test_function_to_schema_basic(self):
        from lionagi.libs.schema.function_to_schema import function_to_schema

        def greet(name: str, greeting: str) -> str:
            """Greet someone.

            Args:
                name: The person's name.
                greeting: The greeting to use.
            """
            return f"{greeting}, {name}!"

        schema = function_to_schema(greet)
        assert isinstance(schema, dict)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "greet"
        assert "parameters" in schema["function"]
        params = schema["function"]["parameters"]
        assert "name" in params["properties"]
        assert "greeting" in params["properties"]
        assert "name" in params["required"]

    def test_function_to_schema_types_mapped(self):
        from lionagi.libs.schema.function_to_schema import function_to_schema

        def compute(x: int, y: float, flag: bool) -> dict:
            """Compute something.

            Args:
                x: An integer.
                y: A float.
                flag: A boolean.
            """
            return {}

        schema = function_to_schema(compute)
        props = schema["function"]["parameters"]["properties"]
        assert props["x"]["type"] == "number"
        assert props["y"]["type"] == "number"
        assert props["flag"]["type"] == "boolean"

    def test_branch_register_tools(self):
        from lionagi import Branch

        def add(a: int, b: int) -> int:
            """Add two numbers.

            Args:
                a: First number.
                b: Second number.
            """
            return a + b

        branch = Branch()
        branch.register_tools([add])
        assert "add" in branch.tools

    def test_branch_tools_in_constructor(self):
        from lionagi import Branch

        def multiply(x: int, y: int) -> int:
            """Multiply two numbers.

            Args:
                x: First.
                y: Second.
            """
            return x * y

        def divide(x: float, y: float) -> float:
            """Divide two numbers.

            Args:
                x: Numerator.
                y: Denominator.
            """
            return x / y

        branch = Branch(tools=[multiply, divide])
        assert "multiply" in branch.tools
        assert "divide" in branch.tools

    def test_tool_construction_direct(self):
        from lionagi.protocols.action.tool import Tool

        def search(query: str) -> str:
            """Search for something.

            Args:
                query: The search query.
            """
            return f"results for {query}"

        tool = Tool(func_callable=search)
        assert tool.function == "search"
        assert tool.tool_schema is not None
        assert tool.tool_schema["type"] == "function"
        assert tool.tool_schema["function"]["name"] == "search"


# ---------------------------------------------------------------------------
# C -- API/Method Existence (no mocks, no calls)
# ---------------------------------------------------------------------------


class TestBranchMethodsExist:
    """Branch methods documented in operations.md are callable."""

    def test_branch_has_core_operations(self):
        from lionagi import Branch

        branch = Branch()
        assert callable(branch.chat)
        assert callable(branch.communicate)
        assert callable(branch.parse)
        assert callable(branch.operate)
        assert callable(branch.ReAct)

    def test_branch_properties_exist(self):
        from lionagi import Branch

        branch = Branch(system="test system")
        # .msgs returns MessageManager
        assert branch.msgs is not None
        # .messages returns Pile of messages
        assert branch.messages is not None
        # .system returns System msg
        assert branch.system is not None
        # .tools returns dict
        assert isinstance(branch.tools, dict)
        # .chat_model and .parse_model
        assert branch.chat_model is not None
        assert branch.parse_model is not None
        # .logs returns Pile
        assert branch.logs is not None

    def test_branch_to_dict_exists(self):
        from lionagi import Branch

        branch = Branch()
        result = branch.to_dict()
        assert isinstance(result, dict)

    def test_branch_from_dict_exists(self):
        from lionagi import Branch

        assert callable(Branch.from_dict)


class TestMessageManagerProperties:
    """MessageManager convenience properties from messages-and-memory.md."""

    def test_message_manager_has_last_response(self):
        from lionagi import Branch

        branch = Branch()
        # Should return None when no responses exist
        assert branch.msgs.last_response is None

    def test_message_manager_has_last_instruction(self):
        from lionagi import Branch

        branch = Branch()
        assert branch.msgs.last_instruction is None

    def test_message_manager_has_collection_properties(self):
        from lionagi import Branch

        branch = Branch()
        # These return Pile instances (possibly empty)
        assert branch.msgs.assistant_responses is not None
        assert branch.msgs.instructions is not None
        assert branch.msgs.action_requests is not None
        assert branch.msgs.action_responses is not None

    def test_message_manager_progression(self):
        from lionagi import Branch

        branch = Branch(system="test")
        # progression tracks message ordering
        assert branch.msgs.progression is not None
        assert len(branch.msgs.progression) >= 1  # at least the system msg


class TestMessageTypeConstruction:
    """Constructing message types directly, from messages-and-memory.md."""

    def test_system_message_construct(self):
        from lionagi.protocols.messages import System

        sys_msg = System(content={"system_message": "You are helpful."})
        assert sys_msg.role.value == "system"

    def test_instruction_construct(self):
        from lionagi.protocols.messages import Instruction

        inst = Instruction(content={"instruction": "Summarize this text."})
        assert inst.role.value == "user"

    def test_action_request_construct(self):
        from lionagi.protocols.messages import ActionRequest

        ar = ActionRequest(content={"function": "search", "arguments": {"query": "test"}})
        assert ar.function == "search"
        assert ar.arguments == {"query": "test"}

    def test_assistant_response_construct(self):
        from lionagi.protocols.messages import AssistantResponse

        resp = AssistantResponse(content={"assistant_response": "Here is the answer."})
        assert resp.role.value == "assistant"

    def test_message_role_enum(self):
        from lionagi.protocols.messages import MessageRole

        assert MessageRole.SYSTEM.value == "system"
        assert MessageRole.USER.value == "user"
        assert MessageRole.ASSISTANT.value == "assistant"


# ---------------------------------------------------------------------------
# D -- LLM Integration (mocked, uses fixtures from conftest)
# ---------------------------------------------------------------------------


class TestMockedLLMOperations:
    """Test LLM-calling methods with mocked iModel (no real API calls)."""

    @pytest.mark.asyncio
    async def test_communicate_returns_response(self, mocked_branch):
        result = await mocked_branch.communicate("What is AI?")
        assert result is not None

    @pytest.mark.asyncio
    async def test_chat_returns_response(self, mocked_branch):
        result = await mocked_branch.chat("Hello, world!")
        assert result is not None

    @pytest.mark.asyncio
    async def test_communicate_multiple_calls(self, mocked_branch):
        r1 = await mocked_branch.communicate("First question")
        r2 = await mocked_branch.communicate("Second question")
        assert r1 is not None
        assert r2 is not None

    def test_clone_branch(self, mocked_branch):
        cloned = mocked_branch.clone()
        assert cloned is not None
        assert cloned.id != mocked_branch.id
        # Clone should have its own message pile
        assert cloned.messages is not mocked_branch.messages

    def test_register_tools_after_construction(self, mocked_branch):
        def helper(text: str) -> str:
            """A helper function.

            Args:
                text: Input text.
            """
            return text.upper()

        assert "helper" not in mocked_branch.tools
        mocked_branch.register_tools([helper])
        assert "helper" in mocked_branch.tools


class TestMockedSession:
    """Test Session operations with mocked branches."""

    @pytest.fixture
    def session_with_branches(self):
        """Build a Session with mocked branches using new_branch + mock iModel."""
        from lionagi.session.session import Session

        session = Session()
        mock_model = LionAGIMockFactory.create_mocked_imodel(response="mocked session response")
        for branch_name in ("researcher", "writer", "reviewer"):
            branch = session.new_branch(name=branch_name)
            branch.chat_model = mock_model
            branch.parse_model = mock_model
        return session

    def test_session_has_named_branches(self, session_with_branches):
        # default_branch + 3 named branches
        assert len(session_with_branches.branches) >= 3

    def test_session_get_branch_by_name(self, session_with_branches):
        researcher = session_with_branches.get_branch("researcher")
        assert researcher is not None
        assert researcher.name == "researcher"

    def test_session_split_creates_clone(self, session_with_branches):
        original = session_with_branches.get_branch("writer")
        cloned = session_with_branches.split(original)
        assert cloned is not None
        assert cloned.id != original.id
        assert cloned in session_with_branches.branches

    @pytest.mark.asyncio
    async def test_session_branch_communicate(self, session_with_branches):
        branch = session_with_branches.get_branch("reviewer")
        result = await branch.communicate("Review this code")
        assert result is not None
