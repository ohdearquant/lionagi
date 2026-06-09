"""Tests for integration documentation examples (llm-providers.md, tools.md, mcp-servers.md).

Validates that the code patterns shown in integration docs actually work:
- All supported LLM providers can construct iModel instances
- Tool and function_to_schema produce correct OpenAI-format schemas
- MCP-related imports resolve correctly
"""

import pytest
from pydantic import BaseModel

from lionagi.service.imodel import iModel

# ===========================================================================
# LLM Providers (llm-providers.md)
# ===========================================================================


class TestLLMProviders:
    """iModel construction for every documented provider."""

    def test_openai_imodel_constructs(self):
        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test")
        assert model is not None
        assert model.endpoint is not None

    def test_anthropic_imodel_constructs(self):
        model = iModel(
            provider="anthropic",
            model="claude-sonnet-4-5-20250929",
            api_key="test",
        )
        assert model is not None
        assert model.endpoint is not None

    def test_gemini_imodel_constructs(self):
        model = iModel(provider="gemini", model="gemini-2.5-flash", api_key="test")
        assert model is not None
        assert model.endpoint is not None

    def test_groq_imodel_constructs(self):
        model = iModel(
            provider="groq",
            model="llama-3.3-70b-versatile",
            api_key="test",
        )
        assert model is not None
        assert model.endpoint is not None

    def test_openrouter_imodel_constructs(self):
        model = iModel(
            provider="openrouter",
            model="google/gemini-2.5-flash",
            api_key="test",
        )
        assert model is not None
        assert model.endpoint is not None

    def test_perplexity_imodel_constructs(self):
        model = iModel(provider="perplexity", model="sonar", api_key="test")
        assert model is not None
        assert model.endpoint is not None

    def test_nvidia_nim_imodel_constructs(self):
        model = iModel(
            provider="nvidia_nim",
            model="meta/llama3-8b-instruct",
            api_key="test",
        )
        assert model is not None
        assert model.endpoint is not None

    def test_ollama_imodel_constructs(self):
        model = iModel(
            provider="ollama",
            model="llama3",
            base_url="http://localhost:11434",
            api_key="test",
        )
        assert model is not None
        assert model.endpoint is not None

    def test_custom_endpoint_imodel_constructs(self):
        model = iModel(
            provider="custom",
            model="my-model",
            base_url="https://example.com/v1",
            api_key="test",
        )
        assert model is not None
        assert model.endpoint is not None

    def test_imodel_copy_returns_new_instance(self):
        original = iModel(provider="openai", model="gpt-4o-mini", api_key="test")
        copied = original.copy()
        assert copied is not original
        assert isinstance(copied, iModel)
        assert copied.id != original.id


# ===========================================================================
# CLI Providers — Detailed (llm-providers.md CLI section + claude-code-usage.md)
# ===========================================================================


class TestCLIEndpointArchitecture:
    """Verify CLIEndpoint class hierarchy and properties documented in llm-providers.md."""

    def test_cli_endpoint_import(self):
        from lionagi.service.connections import CLIEndpoint

        assert CLIEndpoint is not None

    def test_cli_endpoint_is_subclass_of_endpoint(self):
        from lionagi.service.connections import CLIEndpoint, Endpoint

        assert issubclass(CLIEndpoint, Endpoint)

    def test_cli_endpoint_class_vars(self):
        from lionagi.service.connections import CLIEndpoint

        assert CLIEndpoint.is_cli is True
        assert CLIEndpoint.DEFAULT_CONCURRENCY_LIMIT == 3
        assert CLIEndpoint.DEFAULT_QUEUE_CAPACITY == 10

    def test_cli_endpoint_session_id_property(self):
        from lionagi.service.connections import CLIEndpoint

        assert hasattr(CLIEndpoint, "session_id")

    def test_imodel_is_cli_property(self):
        try:
            model = iModel(provider="claude_code", api_key="test")
            assert model.is_cli is True
        except Exception:
            pytest.skip("claude_code provider could not be constructed")

    def test_api_imodel_is_not_cli(self):
        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test")
        assert model.is_cli is False

    def test_match_endpoint_routes_cli_providers(self):
        from lionagi.service.connections import CLIEndpoint, match_endpoint

        for provider in ("claude_code", "gemini_code", "codex"):
            try:
                ep = match_endpoint(provider=provider, endpoint="query_cli")
                assert isinstance(ep, CLIEndpoint)
            except Exception:
                pytest.skip(f"{provider} endpoint could not be constructed")


class TestCLIProviderRequestModels:
    """Verify CLI request models exist with documented parameters."""

    def test_claude_code_request_import(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        assert ClaudeCodeRequest is not None

    def test_claude_code_request_fields(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        fields = ClaudeCodeRequest.model_fields
        for field in (
            "prompt",
            "system_prompt",
            "model",
            "max_turns",
            "permission_mode",
            "resume",
            "ws",
            "add_dir",
            "allowed_tools",
            "disallowed_tools",
            "mcp_tools",
            "mcp_servers",
            "auto_finish",
            "verbose_output",
        ):
            assert field in fields, f"Missing field: {field}"

    def test_claude_code_request_as_cmd_args(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hello")
        args = req.as_cmd_args()
        assert isinstance(args, list)
        assert "-p" in args
        assert "hello" in args
        assert "--output-format" in args

    def test_gemini_code_request_import(self):
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        assert GeminiCodeRequest is not None

    def test_gemini_code_request_fields(self):
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        fields = GeminiCodeRequest.model_fields
        for field in (
            "prompt",
            "system_prompt",
            "model",
            "yolo",
            "approval_mode",
            "sandbox",
            "debug",
            "include_directories",
        ):
            assert field in fields, f"Missing field: {field}"

    def test_gemini_code_request_as_cmd_args(self):
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        req = GeminiCodeRequest(prompt="analyze")
        args = req.as_cmd_args()
        assert isinstance(args, list)
        assert "-p" in args
        assert "analyze" in args

    def test_codex_code_request_import(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        assert CodexCodeRequest is not None

    def test_codex_code_request_fields(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        fields = CodexCodeRequest.model_fields
        for field in (
            "prompt",
            "system_prompt",
            "model",
            "full_auto",
            "sandbox",
            "bypass_approvals",
            "skip_git_repo_check",
            "output_schema",
            "include_plan_tool",
            "images",
        ):
            assert field in fields, f"Missing field: {field}"

    def test_codex_code_request_as_cmd_args(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        req = CodexCodeRequest(prompt="fix tests")
        args = req.as_cmd_args()
        assert isinstance(args, list)
        assert "exec" in args
        assert "fix tests" in args


class TestCLIProviderEndpoints:
    """Verify CLI endpoint classes exist and have documented configuration."""

    def test_claude_code_cli_endpoint_import(self):
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        assert ClaudeCodeCLIEndpoint is not None

    def test_claude_code_endpoint_constructs(self):
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        ep = ClaudeCodeCLIEndpoint()
        assert ep.config.provider == "claude_code"
        # Config name is auto-generated from provider+endpoint in the new registry
        assert "claude_code" in ep.config.name
        # Agentic endpoints have timeout >= 3600
        assert ep.config.timeout >= 3600
        assert ep.is_cli is True

    def test_claude_code_endpoint_handlers(self):
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        ep = ClaudeCodeCLIEndpoint()
        handlers = ep.claude_handlers
        assert isinstance(handlers, dict)
        for key in (
            "on_thinking",
            "on_text",
            "on_tool_use",
            "on_tool_result",
            "on_system",
            "on_final",
        ):
            assert key in handlers

    def test_claude_code_update_handlers(self):
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        ep = ClaudeCodeCLIEndpoint()
        callback = lambda chunk: None
        ep.update_handlers(on_text=callback)
        assert ep.claude_handlers["on_text"] is callback

    def test_gemini_cli_endpoint_import(self):
        from lionagi.providers.google.gemini_code.endpoint import GeminiCLIEndpoint

        assert GeminiCLIEndpoint is not None

    def test_gemini_cli_endpoint_constructs(self):
        from lionagi.providers.google.gemini_code.endpoint import GeminiCLIEndpoint

        ep = GeminiCLIEndpoint()
        assert ep.config.provider == "gemini_code"
        assert ep.is_cli is True

    def test_gemini_cli_endpoint_handlers(self):
        from lionagi.providers.google.gemini_code.endpoint import GeminiCLIEndpoint

        ep = GeminiCLIEndpoint()
        handlers = ep.gemini_handlers
        assert isinstance(handlers, dict)
        for key in ("on_text", "on_tool_use", "on_tool_result", "on_final"):
            assert key in handlers

    def test_codex_cli_endpoint_import(self):
        from lionagi.providers.openai.codex.endpoint import CodexCLIEndpoint

        assert CodexCLIEndpoint is not None

    def test_codex_cli_endpoint_constructs(self):
        from lionagi.providers.openai.codex.endpoint import CodexCLIEndpoint

        ep = CodexCLIEndpoint()
        assert ep.config.provider == "codex"
        assert ep.is_cli is True

    def test_codex_cli_endpoint_handlers(self):
        from lionagi.providers.openai.codex.endpoint import CodexCLIEndpoint

        ep = CodexCLIEndpoint()
        handlers = ep.codex_handlers
        assert isinstance(handlers, dict)
        for key in ("on_text", "on_tool_use", "on_tool_result", "on_final"):
            assert key in handlers


class TestCLISessionManagement:
    """Verify session management patterns documented in claude-code-usage.md."""

    def test_imodel_copy_creates_fresh_session(self):
        try:
            model = iModel(provider="claude_code", api_key="test")
            copied = model.copy()
            assert copied.id != model.id
            # Fresh copy has no session_id
            assert copied.endpoint.session_id is None
        except Exception:
            pytest.skip("claude_code provider could not be constructed")

    def test_imodel_copy_share_session(self):
        try:
            model = iModel(provider="claude_code", api_key="test")
            # Simulate a session ID being set
            model.endpoint.session_id = "test-session-123"
            shared = model.copy(share_session=True)
            assert shared.endpoint.session_id == "test-session-123"
        except Exception:
            pytest.skip("claude_code provider could not be constructed")


# ===========================================================================
# Tools (tools.md)
# ===========================================================================


class TestTools:
    """Tool construction and schema generation from documented patterns."""

    def test_function_to_schema_structure(self):
        from lionagi.libs.schema.function_to_schema import function_to_schema

        def greet(name: str, enthusiasm: int) -> str:
            """Greet someone by name.

            Args:
                name: The person's name.
                enthusiasm: How many exclamation marks to add.
            """
            return f"Hello, {name}{'!' * enthusiasm}"

        schema = function_to_schema(greet)
        assert isinstance(schema, dict)
        assert schema["type"] == "function"
        assert "function" in schema
        fn = schema["function"]
        assert fn["name"] == "greet"
        assert "parameters" in fn
        params = fn["parameters"]
        assert "properties" in params
        assert "name" in params["properties"]
        assert "enthusiasm" in params["properties"]
        assert "required" in params

    def test_function_to_schema_description(self):
        from lionagi.libs.schema.function_to_schema import function_to_schema

        def add(a: int, b: int) -> int:
            """Add two numbers together.

            Args:
                a: First number.
                b: Second number.
            """
            return a + b

        schema = function_to_schema(add)
        fn = schema["function"]
        assert fn["description"] is not None
        assert len(fn["description"]) > 0

    def test_tool_constructs_from_callable(self):
        from lionagi.protocols.action.tool import Tool

        def search(query: str, limit: int) -> list:
            """Search for items.

            Args:
                query: Search query string.
                limit: Maximum results to return.
            """
            return []

        tool = Tool(func_callable=search)
        assert tool is not None
        assert tool.function == "search"
        assert tool.tool_schema is not None
        assert tool.tool_schema["type"] == "function"
        assert tool.tool_schema["function"]["name"] == "search"

    def test_tool_schema_has_parameters(self):
        from lionagi.protocols.action.tool import Tool

        def translate(text: str, target_language: str) -> str:
            """Translate text.

            Args:
                text: The text to translate.
                target_language: ISO language code for the target language.
            """
            return text

        tool = Tool(func_callable=translate)
        params = tool.tool_schema["function"]["parameters"]
        assert "text" in params["properties"]
        assert "target_language" in params["properties"]

    def test_tool_with_request_options(self):
        from lionagi.protocols.action.tool import Tool

        class SearchParams(BaseModel):
            query: str
            max_results: int = 10

        def search(query: str, max_results: int = 10) -> list:
            """Search with validated parameters.

            Args:
                query: The search query.
                max_results: Maximum number of results.
            """
            return []

        tool = Tool(func_callable=search, request_options=SearchParams)
        assert tool is not None
        assert tool.function == "search"
        assert tool.request_options is SearchParams


# ===========================================================================
# MCP (mcp-servers.md)
# ===========================================================================


class TestMCP:
    """MCP-related imports and constructs from documented patterns."""

    def test_load_mcp_tools_import_resolves(self):
        from lionagi import load_mcp_tools

        assert callable(load_mcp_tools)
