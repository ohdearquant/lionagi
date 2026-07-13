# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from lionagi.service.connections.mcp_wrapper import MCPSecurityConfig

from lionagi.protocols._concepts import Manager
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.messages.action_request import ActionRequest
from lionagi.utils import to_list

from .function_calling import FunctionCalling
from .tool import FuncTool, FuncToolRef, Tool, ToolRef
from .tool_hooks import (
    ToolPostHook,
    ToolPreHook,
    run_tool_post_hooks,
    run_tool_pre_hooks,
)

logger = logging.getLogger(__name__)


class ActionManager(Manager):
    """Registers function-based tools and invokes them from ActionRequests."""

    def __init__(self, *args: FuncTool, **kwargs) -> None:
        super().__init__()
        self.registry: dict[str, Tool] = {}
        self._tool_pre_hooks: list[ToolPreHook] = []
        self._tool_post_hooks: list[ToolPostHook] = []

        tools = []
        if args:
            tools.extend(to_list(args, dropna=True, flatten=True))
        if kwargs:
            tools.extend(to_list(kwargs.values(), dropna=True, flatten=True))

        self.register_tools(tools, update=True)

    def add_tool_pre_hook(self, hook: ToolPreHook) -> None:
        """Register a tool-pre hook, outermost, ahead of the spec-level chain.

        Hooks run in registration order at ``invoke()``, before the tool's
        own ``preprocessor`` (the spec-level security/user chain) ever sees
        the arguments -- see ``tool_hooks.py`` for the decision contract.
        """
        self._tool_pre_hooks.append(hook)

    def add_tool_post_hook(self, hook: ToolPostHook) -> None:
        """Register a tool-post hook, outermost, after the spec-level chain.

        Advisory only -- see ``tool_hooks.py``.
        """
        self._tool_post_hooks.append(hook)

    def __contains__(self, tool: FuncToolRef) -> bool:
        if isinstance(tool, Tool):
            return tool.function in self.registry
        elif isinstance(tool, str):
            return tool in self.registry
        elif callable(tool):
            return tool.__name__ in self.registry
        return False

    def register_tool(self, tool: FuncTool, update: bool = False) -> None:
        if not update and tool in self:
            name = None
            if isinstance(tool, Tool):
                name = tool.function
            elif callable(tool):
                name = tool.__name__
            elif isinstance(tool, dict):
                name = list(tool.keys())[0] if tool else None
            raise ValueError(f"Tool {name} is already registered.")

        if callable(tool):
            tool = Tool(func_callable=tool)
        elif isinstance(tool, dict):
            if len(tool) == 1:
                (raw_tool_name,) = tool.keys()
                if isinstance(raw_tool_name, str):
                    from lionagi.service.connections.mcp_wrapper import (
                        validate_mcp_tool_admission,
                    )

                    validate_mcp_tool_admission(raw_tool_name, None, None)
            tool = Tool(mcp_config=tool)
        elif not isinstance(tool, Tool):
            raise TypeError(
                "Must provide a `Tool` object, a callable function, or an MCP config dict."
            )
        elif tool.mcp_config is not None:
            self._validate_prebuilt_mcp_tool_admission(tool)

        self.registry[tool.function] = tool

    def _validate_prebuilt_mcp_tool_admission(self, tool: Tool) -> None:
        from lionagi.service.connections.mcp_wrapper import (
            is_synthetic_mcp_wrapper_schema,
            validate_mcp_tool_admission,
        )

        mcp_tool_name, mcp_server_config = next(iter(tool.mcp_config.items()))
        actual_name = mcp_server_config.get("_original_tool_name")
        if not isinstance(actual_name, str) or not actual_name:
            actual_name = mcp_tool_name

        input_schema = None
        description = None
        advertised_name = None
        if isinstance(tool.tool_schema, dict):
            function = tool.tool_schema.get("function")
            if isinstance(function, dict):
                advertised_name = function.get("name")
                input_schema = function.get("parameters")
                description = function.get("description")

        # A generic `**kwargs` wrapper schema carries no remote-server info;
        # treat it as absent so identities fail closed, not laundered through it.
        if is_synthetic_mcp_wrapper_schema(
            mcp_tool_name, advertised_name, input_schema, description
        ):
            input_schema = None
            description = None

        validate_mcp_tool_admission(actual_name, input_schema, description)
        if isinstance(advertised_name, str) and advertised_name != actual_name:
            validate_mcp_tool_admission(advertised_name, input_schema, description)

    def register_tools(self, tools: list[FuncTool] | FuncTool, update: bool = False) -> None:
        tools_list = tools if isinstance(tools, list) else [tools]
        for t in tools_list:
            self.register_tool(t, update=update)

    def match_tool(self, action_request: ActionRequest | BaseModel | dict) -> FunctionCalling:
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
            tool = self._resolve_plugin_tool(func)
        if not isinstance(tool, Tool):
            raise ValueError(f"Function {func} is not registered.")

        return FunctionCalling(func_tool=tool, arguments=args)

    def _resolve_plugin_tool(self, name: str) -> Tool | None:
        """ADR-0088 D3 consumer: on a registry miss, ask the plugin registry whether a
        trusted, enabled, version-compatible plugin declares a tool named *name*.

        Deferred import — `lionagi.plugins` must stay out of the import graph
        until an actual miss occurs (see tests/test_import_laziness.py).
        Resolution and trust are re-checked fresh on every call (never cached
        onto ``self.registry``), so a plugin disabled or edited mid-session
        stops being reachable through this path immediately. Returns ``None``
        when no plugin declares *name* — the caller's existing "not
        registered" error applies unchanged. Raises ``PluginToolCollisionError``
        unmodified when two enabled plugins declare the same tool name
        (ADR-0088 D6): that is a hard error, not a miss.
        """
        from lionagi.libs.schema.function_to_schema import function_to_schema
        from lionagi.plugins.registry import PluginRegistry

        resolved = PluginRegistry.resolve_tool_target(name)
        if resolved is None:
            return None

        callable_ = PluginRegistry.activate_target(resolved.plugin_name, resolved.target)
        # The manifest's declared tool `name` (what the caller/model asked
        # for) is independent of the underlying callable's own `__name__` —
        # the schema advertised for this Tool must reflect the requested
        # name, not whatever the plugin author called the Python function.
        schema = function_to_schema(callable_)
        schema["function"]["name"] = name
        return Tool(func_callable=callable_, tool_schema=schema)

    async def invoke(
        self,
        func_call: BaseModel | ActionRequest,
    ) -> FunctionCalling:
        """Match, run tool-pre hooks, invoke, then run tool-post hooks.

        Every tool routed through this method -- plain function tools,
        ``Tool`` objects, and MCP-discovered tools alike -- passes through
        the same tool-pre/tool-post hook layer. A construction directly on
        ``FunctionCalling`` (bypassing this manager) skips this layer
        entirely; that is a documented, tested limit, not an oversight.

        Tool-pre hooks run before the tool's own ``preprocessor`` chain (the
        spec-level security/user hooks, which keep running last of the
        pre-stage validators) and may rewrite the arguments; a denial raises
        directly out of this call, before the tool is ever invoked. The
        rewritten arguments are revalidated against the tool's declared
        request model inside ``FunctionCalling._invoke()``, after the
        spec-level chain has also had a chance to mutate them and before the
        callable executes.

        Tool-post hooks run after invocation completes (success or failure)
        and are advisory only -- they observe the final arguments, the
        result (``None`` on failure), and the error (``None`` on success),
        and cannot change either. A hook that returns a ``ToolPostDecision``
        with a non-empty ``reason`` has that reason collected into a list of
        notes; when any notes are collected they are attached to the
        returned event at ``function_calling.metadata["tool_post_hook_notes"]``
        and logged, whether the tool itself succeeded or failed (a tool
        exception is captured onto ``function_calling.status`` without
        re-raising, so the event -- and its notes -- always reach the
        caller). The finally block runs the post hooks unconditionally,
        including on the rarer path where invocation raises a
        ``BaseException`` that propagates past this call; the notes are
        recorded on ``function_calling`` before that exception continues
        outward, but nothing here changes the exception itself or the fact
        that this method never returns a value on that path.
        """
        function_calling = self.match_tool(func_call)
        tool_name = function_calling.function

        if self._tool_pre_hooks:
            function_calling.arguments = await run_tool_pre_hooks(
                self._tool_pre_hooks, tool_name, function_calling.arguments
            )

        error: BaseException | None = None
        try:
            await function_calling.invoke()
            if function_calling.status == EventStatus.FAILED:
                error = function_calling.execution.error
        except BaseException as exc:
            error = exc
            raise
        finally:
            if self._tool_post_hooks:
                notes = await run_tool_post_hooks(
                    self._tool_post_hooks,
                    tool_name,
                    function_calling.arguments,
                    function_calling.response,
                    error,
                )
                if notes:
                    function_calling.metadata["tool_post_hook_notes"] = notes
                    logger.info("tool post hook notes for %r: %s", tool_name, notes)

        return function_calling

    @property
    def schema_list(self) -> list[dict[str, Any]]:
        return [tool.tool_schema for tool in self.registry.values()]

    def get_tool_schema(
        self,
        tools: ToolRef = False,
        auto_register: bool = True,
        update: bool = False,
    ) -> dict:
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
        if isinstance(tool, dict):
            return tool
        if callable(tool):
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
        security: "MCPSecurityConfig | None" = None,
    ) -> list[str]:
        registered_tools = []

        if security is not None:
            from lionagi.service.connections.mcp_wrapper import MCPConnectionPool

            MCPConnectionPool.remember_security(server_config, security)

        server_name = None
        if isinstance(server_config, dict) and "server" in server_config:
            server_name = server_config["server"]

        if request_options:
            for k in list(request_options.keys()):
                if not k.startswith(f"{server_name}_"):
                    request_options[f"{server_name}_{k}"] = request_options.pop(k)

        if tool_names:
            from lionagi.service.connections.mcp_wrapper import (
                validate_mcp_tool_admission,
            )

            # Validate the whole list before registering any tool: a denial
            # anywhere must leave the registry unchanged, not partially populated.
            for tool_name in tool_names:
                validate_mcp_tool_admission(tool_name, None, None)

            for tool_name in tool_names:
                logger.warning(
                    f"MCP tool {tool_name!r} registered via the metadata-free "
                    "tool_names= shortcut with no descriptor (schema/description) "
                    "evidence; the generic-executor admission rule could not "
                    "inspect its shape and admitted it by name alone."
                )

                config_with_metadata = dict(server_config)
                config_with_metadata["_original_tool_name"] = tool_name

                mcp_config = {tool_name: config_with_metadata}

                tool_request_options = None
                if request_options and tool_name in request_options:
                    tool_request_options = request_options[tool_name]

                tool = Tool(mcp_config=mcp_config, request_options=tool_request_options)
                self.register_tool(tool, update=update)
                registered_tools.append(tool_name)
        else:
            from lionagi.service.connections.mcp_wrapper import (
                MCPConnectionPool,
                validate_mcp_tool_admission,
            )

            client = await MCPConnectionPool.get_client(server_config, security=security)
            tools = await client.list_tools()

            # Validate every descriptor before mutating the registry: a
            # denial anywhere must leave the registry unchanged.
            for tool in tools:
                validate_mcp_tool_admission(
                    tool.name,
                    getattr(tool, "inputSchema", None),
                    getattr(tool, "description", None),
                )

            for tool in tools:
                tool_name = tool.name
                input_schema = getattr(tool, "inputSchema", None)
                description = getattr(tool, "description", None)

                config_with_metadata = dict(server_config)
                config_with_metadata["_original_tool_name"] = tool_name

                mcp_config = {tool_name: config_with_metadata}

                tool_request_options = None
                if request_options and tool_name in request_options:
                    tool_request_options = request_options[tool_name]

                tool_schema = None
                try:
                    if isinstance(input_schema, dict):
                        tool_schema = {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "description": description,
                                "parameters": input_schema,
                            },
                        }
                except Exception as schema_error:
                    logging.warning(f"Could not extract schema for {tool_name}: {schema_error}")
                    tool_schema = None

                try:
                    tool_obj = Tool(
                        mcp_config=mcp_config,
                        request_options=tool_request_options,
                        tool_schema=tool_schema,
                    )
                    self.register_tool(tool_obj, update=update)
                    registered_tools.append(tool_name)
                except PermissionError:
                    raise
                except Exception as e:
                    logging.warning(f"Failed to register tool {tool_name}: {e}")

        return registered_tools

    async def load_mcp_config(
        self,
        config_path: str,
        server_names: list[str] | None = None,
        update: bool = False,
        mcp_security: "MCPSecurityConfig | None" = None,
    ) -> dict[str, list[str]]:
        from lionagi.service.connections.mcp_wrapper import (
            MCPConnectionPool,
            MCPSecurityConfig,
        )

        # Explicit config load trusts declared transports by default.
        if mcp_security is None:
            mcp_security = MCPSecurityConfig(allow_commands=True, allow_urls=True)

        loaded_names = MCPConnectionPool.load_config(config_path)

        if server_names is None:
            # Default to servers in THIS config file — the pool accumulates
            # configs globally, so enumerating it would re-register unrelated servers.
            server_names = loaded_names
        all_tools = {}
        for server_name in server_names:
            try:
                tools = await self.register_mcp_server(
                    {"server": server_name}, update=update, security=mcp_security
                )
                all_tools[server_name] = tools
                logger.info("Registered %d tools from server '%s'", len(tools), server_name)
            except PermissionError as exc:
                logger.error("MCP server %r registration denied: %s", server_name, exc)
                raise
            except Exception as e:
                logger.warning("Failed to register server '%s': %s", server_name, e)
                all_tools[server_name] = []

        return all_tools


async def load_mcp_tools(
    config_path: str | None = None,
    server_names: list[str] | None = None,
    request_options_map: dict[str, dict[str, type]] | None = None,
    update: bool = False,
    mcp_security: "MCPSecurityConfig | None" = None,
) -> list[Tool]:
    from lionagi.service.connections.mcp_wrapper import (
        MCPConnectionPool,
        MCPSecurityConfig,
    )

    manager = ActionManager()

    if mcp_security is None:
        mcp_security = MCPSecurityConfig(allow_commands=True, allow_urls=True)

    if config_path:
        MCPConnectionPool.load_config(config_path)

    if server_names is None and config_path:
        server_names = list(MCPConnectionPool._configs.keys())

    if server_names is None:
        raise ValueError("Either provide server_names or config_path to discover servers")

    for server_name in server_names:
        try:
            request_options = None
            if request_options_map and server_name in request_options_map:
                request_options = request_options_map[server_name]

            tools_registered = await manager.register_mcp_server(
                {"server": server_name},
                request_options=request_options,
                update=update,
                security=mcp_security,
            )
            logger.info("Loaded %d tools from %s", len(tools_registered), server_name)
        except PermissionError as exc:
            logger.error("MCP server %r registration denied: %s", server_name, exc)
            raise
        except Exception as e:
            logger.warning("Failed to load server '%s': %s", server_name, e)

    return list(manager.registry.values())


__all__ = ["ActionManager", "load_mcp_tools"]
