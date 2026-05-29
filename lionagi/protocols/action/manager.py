# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import Any

from pydantic import BaseModel

from lionagi.governance.context import OperationContext, get_operation_context
from lionagi.governance.evidence import EvidenceChain, LogTier
from lionagi.governance.gates import (
    GateExecutor,
    GateResult,
    GateVerdict,
    GovernanceViolationError,
)
from lionagi.governance.targets import GateRegistration
from lionagi.protocols._concepts import Manager
from lionagi.protocols.messages.action_request import ActionRequest
from lionagi.utils import to_list

from .function_calling import FunctionCalling
from .tool import FuncTool, FuncToolRef, Tool, ToolRef

logger = logging.getLogger(__name__)


class ActionManager(Manager):
    """
    A manager that registers function-based tools and invokes them
    when triggered by an ActionRequest. Tools can be registered
    individually or in bulk, and each tool must have a unique name.
    """

    def __init__(self, *args: FuncTool, **kwargs) -> None:
        """
        Create an ActionManager, optionally registering initial tools.

        Args:
            *args (FuncTool):
                A variable number of tools or callables.
            **kwargs:
                Additional named arguments that are also considered tools.
        """
        super().__init__()
        self.registry: dict[str, Tool] = {}
        self._governance_artifacts: Any | None = None
        self._evidence_chain: EvidenceChain | None = None

        tools = []
        if args:
            tools.extend(to_list(args, dropna=True, flatten=True))
        if kwargs:
            tools.extend(to_list(kwargs.values(), dropna=True, flatten=True))

        self.register_tools(tools, update=True)

    @property
    def evidence_chain(self) -> EvidenceChain | None:
        return self._evidence_chain

    @evidence_chain.setter
    def evidence_chain(self, value: EvidenceChain | None) -> None:
        self._evidence_chain = value

    @property
    def governance_artifacts(self) -> Any | None:
        return self._governance_artifacts

    @governance_artifacts.setter
    def governance_artifacts(self, value: Any | None) -> None:
        self._governance_artifacts = value

    def _governance_gate_registrations(self) -> list[GateRegistration]:
        if self._governance_artifacts is None:
            return []

        regs = getattr(self._governance_artifacts, "gates", None)
        if regs is not None:
            return list(regs)

        if isinstance(self._governance_artifacts, list):
            return list(self._governance_artifacts)

        return []

    def _governance_gate_for_tool(self, tool_name: str) -> list[GateRegistration]:
        return [
            reg for reg in self._governance_gate_registrations() if reg.target_tool == tool_name
        ]

    def _tool_governed(self, tool_name: str) -> bool:
        return bool(self._governance_gate_for_tool(tool_name))

    def _emit_governance_evidence(
        self,
        tool_call: FunctionCalling,
        pre_gate: GateResult,
        post_gate: GateResult | None = None,
    ) -> None:
        chain = self._evidence_chain
        if chain is None:
            return

        ctx = get_operation_context()
        content: dict[str, Any] = {
            "type": "governance.tool_call",
            "tool": {
                "name": tool_call.function,
                "arguments": tool_call.arguments,
            },
            "result": tool_call.execution.to_dict(),
            "gates": {
                "pre": pre_gate.to_dict(),
                "post": post_gate.to_dict() if post_gate is not None else None,
            },
        }

        if ctx is not None:
            content["trace"] = {
                "trace_id": ctx.trace_id,
                "span_id": ctx.span_id,
                "actor_id": ctx.actor_id,
                "actor_role": ctx.actor_role,
                "policy_ref": pre_gate.policy_ref or ctx.policy_pin.charter_id,
            }

        node = chain.append(content, tier=LogTier.IMMUTABLE)
        pre_gate.evidence_ref = node.node_hash
        if post_gate is not None:
            post_gate.evidence_ref = node.node_hash

    def __contains__(self, tool: FuncToolRef) -> bool:
        """
        Check if a tool is registered, by either:
        - The Tool object itself,
        - A string name for the function,
        - Or the callable's __name__.

        Returns:
            bool: True if found, else False.
        """
        if isinstance(tool, Tool):
            return tool.function in self.registry
        elif isinstance(tool, str):
            return tool in self.registry
        elif callable(tool):
            return tool.__name__ in self.registry
        return False

    def register_tool(self, tool: FuncTool, update: bool = False) -> None:
        """
        Register a single tool/callable in the manager.

        Args:
            tool (FuncTool):
                A `Tool` object, a raw callable function, or an MCP config dict.
                - Tool: Registered directly
                - Callable: Wrapped as Tool(func_callable=...)
                - Dict: Treated as MCP config, Tool(mcp_config=...)
            update (bool):
                If True, allow replacing an existing tool with the same name.

        Raises:
            ValueError: If tool already registered and update=False.
            TypeError: If `tool` is not a Tool, callable, or dict.
        """
        # Check if tool already exists
        if not update and tool in self:
            name = None
            if isinstance(tool, Tool):
                name = tool.function
            elif callable(tool):
                name = tool.__name__
            elif isinstance(tool, dict):
                # For MCP config, extract the tool name (first key)
                name = list(tool.keys())[0] if tool else None
            raise ValueError(f"Tool {name} is already registered.")

        # Convert to Tool object based on type
        if callable(tool):
            tool = Tool(func_callable=tool)
        elif isinstance(tool, dict):
            # Dict is treated as MCP config
            tool = Tool(mcp_config=tool)
        elif not isinstance(tool, Tool):
            raise TypeError(
                "Must provide a `Tool` object, a callable function, or an MCP config dict."
            )
        self.registry[tool.function] = tool

    def register_tools(self, tools: list[FuncTool] | FuncTool, update: bool = False) -> None:
        """
        Register multiple tools at once.

        Args:
            tools (list[FuncTool] | FuncTool):
                A single or list of tools/callables.
            update (bool):
                If True, allow updating existing tools.

        Raises:
            ValueError: If a duplicate tool is found and update=False.
            TypeError: If any item is not a Tool or callable.
        """
        tools_list = tools if isinstance(tools, list) else [tools]
        for t in tools_list:
            self.register_tool(t, update=update)

    def match_tool(self, action_request: ActionRequest | BaseModel | dict) -> FunctionCalling:
        """
        Convert an ActionRequest (or dict with "function"/"arguments")
        into a `FunctionCalling` instance by finding the matching tool.

        Raises:
            TypeError: If `action_request` is an unsupported type.
            ValueError: If no matching tool is found in the registry.

        Returns:
            FunctionCalling: The event object that can be invoked.
        """
        if not isinstance(action_request, ActionRequest | BaseModel | dict):
            raise TypeError(f"Unsupported type {type(action_request)}")

        func, args = None, None
        if isinstance(action_request, dict):
            func = action_request["function"]
            args = action_request["arguments"]
        else:
            func = action_request.function
            args = action_request.arguments

        tool = self.registry.get(func, None)
        if not isinstance(tool, Tool):
            raise ValueError(f"Function {func} is not registered.")

        return FunctionCalling(func_tool=tool, arguments=args)

    async def invoke(
        self,
        func_call: BaseModel | ActionRequest,
    ) -> FunctionCalling:
        """
        High-level API to parse and run a function call.

        Steps:
          1) Convert `func_call` to FunctionCalling via `match_tool`.
          2) `invoke()` the resulting object.
          3) Return the `FunctionCalling`, which includes `execution`.

        Args:
            func_call: The action request model or ActionRequest object.

        Returns:
            `FunctionCalling` event after it completes execution.
        """
        function_calling = self.match_tool(func_call)
        if self._tool_governed(function_calling.function):
            ctx = get_operation_context()
            result = await self.execute_governed(
                function_calling.function, function_calling.arguments, ctx
            )
            # Copy result back onto the FunctionCalling so callers see a
            # fully-populated event (response set, status COMPLETED), not the
            # original PENDING shell that execute_governed never touched.
            from lionagi.protocols.generic.event import EventStatus

            function_calling.execution.response = result
            function_calling.status = EventStatus.COMPLETED
            return function_calling

        try:
            await function_calling.invoke()
        except Exception:  # noqa: S110
            pass
        return function_calling

    async def execute(self, tool_name: str, arguments: dict) -> Any:
        """Execute a registered tool by name with no governance overhead."""
        tool = self.registry.get(tool_name)
        if not isinstance(tool, Tool):
            raise ValueError(f"Function {tool_name} is not registered.")
        function_calling = FunctionCalling(func_tool=tool, arguments=arguments)
        await function_calling.invoke()
        return function_calling.response

    async def execute_governed(
        self,
        tool_name: str,
        arguments: dict,
        ctx: OperationContext | None = None,
    ) -> Any:
        """Execute a tool with pre/post governance gate checks and evidence recording.

        Falls back to execute() when ctx is None or no gate registrations apply
        to tool_name. Hard DENY raises GovernanceViolationError after recording
        a denial EvidenceNode. Advisory gates record evidence and let execution
        proceed.
        """
        # (a) No governance context — ungoverned fallback
        if ctx is None:
            return await self.execute(tool_name, arguments)

        # (b) Gate registrations for this tool; fall back if none
        registrations = self._governance_gate_for_tool(tool_name)
        if not registrations:
            return await self.execute(tool_name, arguments)

        chain = self._evidence_chain

        # (c) Pre-gate evaluation
        executor = GateExecutor(registrations)
        gate_result = executor.evaluate(tool_name, ctx)

        # (d) DENY — record denial node, raise
        if gate_result.verdict == GateVerdict.DENY:
            if chain is not None:
                node = chain.append(
                    {
                        "event": "gate_deny",
                        "tool_name": tool_name,
                        "gate_id": gate_result.gate_id,
                        "justification": gate_result.justification,
                        "policy_ref": gate_result.policy_ref or ctx.policy_pin.charter_hash,
                        "actor_id": ctx.actor_id,
                        "actor_role": ctx.actor_role,
                        "trace_id": ctx.trace_id,
                    },
                    tier=LogTier.IMMUTABLE,
                )
                gate_result.evidence_ref = node.node_hash
                ctx.embed_evidence(chain)
            raise GovernanceViolationError(gate_result)

        # (e) ADVISORY — record advisory node, continue
        if gate_result.verdict == GateVerdict.ADVISORY:
            if chain is not None:
                node = chain.append(
                    {
                        "event": "gate_advisory",
                        "tool_name": tool_name,
                        "gate_id": gate_result.gate_id,
                        "justification": gate_result.justification,
                        "actor_id": ctx.actor_id,
                        "trace_id": ctx.trace_id,
                    },
                    tier=LogTier.IMMUTABLE,
                )
                gate_result.evidence_ref = node.node_hash

        # (f) Execute the tool
        result = await self.execute(tool_name, arguments)

        # (g) Post-execution evidence sidecar
        if chain is not None:
            sensitive = {"secret", "token", "password", "key"}
            sanitized_args = {
                k: v for k, v in arguments.items() if not any(s in k.lower() for s in sensitive)
            }
            node = chain.append(
                {
                    "event": "tool_execution",
                    "tool_name": tool_name,
                    "arguments": sanitized_args,
                    "result_summary": str(result)[:500],
                    "gate_verdict": gate_result.to_dict(),
                    "actor_id": ctx.actor_id,
                    "actor_role": ctx.actor_role,
                    "trace_id": ctx.trace_id,
                    "span_id": ctx.span_id,
                },
                tier=LogTier.IMMUTABLE,
            )
            ctx.embed_evidence(chain)

        return result

    @property
    def schema_list(self) -> list[dict[str, Any]]:
        """Return the list of JSON schemas for all registered tools."""
        return [tool.tool_schema for tool in self.registry.values()]

    def get_tool_schema(
        self,
        tools: ToolRef = False,
        auto_register: bool = True,
        update: bool = False,
    ) -> dict:
        """
        Retrieve schemas for a subset of tools or for all.

        Args:
            tools (ToolRef):
                - If True, return schema for all tools.
                - If False, return an empty dict.
                - If specific tool(s), returns only those schemas.
            auto_register (bool):
                If a tool (callable) is not yet in the registry, register if True.
            update (bool):
                If True, allow updating existing tools.

        Returns:
            dict: e.g., {"tools": [list of schemas]}

        Raises:
            ValueError: If requested tool is not found and auto_register=False.
            TypeError: If tool specification is invalid.
        """
        if isinstance(tools, list | tuple) and len(tools) == 1:
            tools = tools[0]
        if isinstance(tools, bool):
            if tools is True:
                return {"tools": self.schema_list}
            return []
        else:
            schemas = self._get_tool_schema(tools, auto_register=auto_register, update=update)
            return {"tools": schemas}

    def _get_tool_schema(
        self,
        tool: Any,
        auto_register: bool = True,
        update: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """
        Internal helper to handle retrieval or registration of a single or
        multiple tools, returning their schema(s).
        """
        if isinstance(tool, dict):
            return tool  # Already a schema
        if callable(tool):
            # Possibly unregistered function
            name = tool.__name__
            if name not in self.registry:
                if auto_register:
                    self.register_tool(tool, update=update)
                else:
                    raise ValueError(f"Tool {name} is not registered.")
            return self.registry[name].tool_schema

        elif isinstance(tool, Tool) or isinstance(tool, str):
            name = tool.function if isinstance(tool, Tool) else tool
            if name in self.registry:
                return self.registry[name].tool_schema
            raise ValueError(f"Tool {name} is not registered.")
        elif isinstance(tool, list):
            return [self._get_tool_schema(t, auto_register=auto_register) for t in tool]
        raise TypeError(f"Unsupported type {type(tool)}")

    async def register_mcp_server(
        self,
        server_config: dict[str, Any],
        tool_names: list[str] | None = None,
        request_options: dict[str, type] | None = None,
        update: bool = False,
    ) -> list[str]:
        """
        Register tools from an MCP server with automatic discovery.

        Args:
            server_config: MCP server configuration (command, args, etc.)
                          Can be {"server": "name"} to reference loaded config
                          or full config dict with command/args
            tool_names: Optional list of specific tool names to register.
                       If None, will discover and register all available tools.
            request_options: Optional dict mapping tool names to Pydantic model classes
                            for request validation. E.g., {"exa_search": ExaSearchRequest}
            update: If True, allow updating existing tools.

        Returns:
            List of registered tool names

        Example:
            # Auto-discover with Pydantic validation
            from lionagi.providers.exa.search.models import ExaSearchRequest
            tools = await manager.register_mcp_server(
                {"server": "search"},
                request_options={"exa_search": ExaSearchRequest}
            )

            # Register specific tools only
            tools = await manager.register_mcp_server(
                {"command": "python", "args": ["-m", "server"]},
                tool_names=["search", "fetch"]
            )
        """
        registered_tools = []

        # Extract server name for qualified naming
        server_name = None
        if isinstance(server_config, dict) and "server" in server_config:
            server_name = server_config["server"]

        if request_options:
            for k in list(request_options.keys()):
                if not k.startswith(f"{server_name}_"):
                    request_options[f"{server_name}_{k}"] = request_options.pop(k)

        if tool_names:
            # Register specific tools with qualified names
            for tool_name in tool_names:
                # Use qualified name to avoid collisions
                # Store original tool name in config for MCP calls
                config_with_metadata = dict(server_config)
                config_with_metadata["_original_tool_name"] = tool_name

                mcp_config = {tool_name: config_with_metadata}

                # Get request_options for this tool if provided
                tool_request_options = None
                if request_options and tool_name in request_options:
                    tool_request_options = request_options[tool_name]

                # Create tool with request_options for Pydantic validation
                tool = Tool(mcp_config=mcp_config, request_options=tool_request_options)
                self.register_tool(tool, update=update)
                registered_tools.append(tool_name)
        else:
            # Auto-discover tools from the server
            from lionagi.service.connections.mcp_wrapper import MCPConnectionPool

            # Get client and discover tools
            client = await MCPConnectionPool.get_client(server_config)
            tools = await client.list_tools()

            # Register each discovered tool with qualified name
            for tool in tools:
                # Store original tool name in config for MCP calls
                config_with_metadata = dict(server_config)
                config_with_metadata["_original_tool_name"] = tool.name

                mcp_config = {tool.name: config_with_metadata}

                # Get request_options for this tool if provided
                tool_request_options = None
                if request_options and tool.name in request_options:
                    tool_request_options = request_options[tool.name]

                # Extract schema from FastMCP tool and convert to lionagi format
                tool_schema = None
                try:
                    if (
                        hasattr(tool, "inputSchema")
                        and tool.inputSchema is not None
                        and isinstance(tool.inputSchema, dict)
                    ):
                        tool_schema = {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": (
                                    tool.description if hasattr(tool, "description") else None
                                ),
                                "parameters": tool.inputSchema,
                            },
                        }
                except Exception as schema_error:
                    # If schema extraction fails, let Tool auto-generate from function signature
                    logging.warning(f"Could not extract schema for {tool.name}: {schema_error}")
                    tool_schema = None

                try:
                    # Create tool with auto-populated schema from MCP discovery
                    tool_obj = Tool(
                        mcp_config=mcp_config,
                        request_options=tool_request_options,
                        tool_schema=tool_schema,
                    )
                    self.register_tool(tool_obj, update=update)
                    registered_tools.append(tool.name)
                except Exception as e:
                    logging.warning(f"Failed to register tool {tool.name}: {e}")

        return registered_tools

    async def load_mcp_config(
        self,
        config_path: str,
        server_names: list[str] | None = None,
        update: bool = False,
    ) -> dict[str, list[str]]:
        """
        Load MCP configurations from a .mcp.json file with auto-discovery.

        Args:
            config_path: Path to .mcp.json configuration file
            server_names: Optional list of server names to load.
                         If None, loads all servers.
            update: If True, allow updating existing tools.

        Returns:
            Dict mapping server names to lists of registered tool names

        Example:
            # Load all servers and auto-discover their tools
            tools = await manager.load_mcp_config("/path/to/.mcp.json")

            # Load specific servers only
            tools = await manager.load_mcp_config(
                "/path/to/.mcp.json",
                server_names=["search", "memory"]
            )
        """
        from lionagi.service.connections.mcp_wrapper import MCPConnectionPool

        # Load the config file into the connection pool
        MCPConnectionPool.load_config(config_path)

        # Get server list to process
        if server_names is None:
            # Get all server names from loaded config
            # The config has already been validated by load_config
            server_names = list(MCPConnectionPool._configs.keys())

        # Register tools from each server
        all_tools = {}
        for server_name in server_names:
            try:
                # Register using server reference
                tools = await self.register_mcp_server({"server": server_name}, update=update)
                all_tools[server_name] = tools
                logger.info("Registered %d tools from server '%s'", len(tools), server_name)
            except Exception as e:
                logger.warning("Failed to register server '%s': %s", server_name, e)
                all_tools[server_name] = []

        return all_tools


async def load_mcp_tools(
    config_path: str | None = None,
    server_names: list[str] | None = None,
    request_options_map: dict[str, dict[str, type]] | None = None,
    update: bool = False,
) -> list[Tool]:
    """
    Standalone helper function to load MCP tools from servers.
    Creates an ActionManager internally and returns tools ready for use.

    Args:
        config_path: Path to .mcp.json file. If None, assumes config already loaded.
        server_names: Optional list of server names to load.
                     If None, loads all servers from config.
        request_options_map: Optional dict mapping server names to tool request options.
                             E.g., {"search": {"exa_search": ExaSearchRequest}}
        update: If True, allow updating existing tools.

    Returns:
        List of Tool objects ready to use with Branch

    Example:
        # Simple one-liner to get MCP tools
        from lionagi.protocols.action.manager import load_mcp_tools
        from lionagi.providers.exa.search.models import ExaSearchRequest
        from lionagi.providers.perplexity.chat.models import PerplexityChatRequest

        # Load with Pydantic validation
        tools = await load_mcp_tools(
            "/path/to/.mcp.json",
            ["search"],
            request_options_map={
                "search": {
                    "exa_search": ExaSearchRequest,
                    "perplexity_search": PerplexityChatRequest
                }
            }
        )
        branch = Branch(tools=tools)
    """
    from lionagi.service.connections.mcp_wrapper import MCPConnectionPool

    # Create a temporary ActionManager for tool management
    manager = ActionManager()

    # Load config if provided
    if config_path:
        MCPConnectionPool.load_config(config_path)

    # If no server names specified, discover from config
    if server_names is None and config_path:
        # Get all server names from loaded config
        server_names = list(MCPConnectionPool._configs.keys())

    if server_names is None:
        raise ValueError("Either provide server_names or config_path to discover servers")

    # Register all servers
    for server_name in server_names:
        try:
            # Get request_options for this server if provided
            request_options = None
            if request_options_map and server_name in request_options_map:
                request_options = request_options_map[server_name]

            tools_registered = await manager.register_mcp_server(
                {"server": server_name},
                request_options=request_options,
                update=update,
            )
            logger.info("Loaded %d tools from %s", len(tools_registered), server_name)
        except Exception as e:
            logger.warning("Failed to load server '%s': %s", server_name, e)

    # Return all registered tools as a list
    return list(manager.registry.values())


__all__ = ["ActionManager", "load_mcp_tools"]

# File: lionagi/protocols/action/manager.py
