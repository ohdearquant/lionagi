from functools import singledispatchmethod
from collections import deque
from typing import Tuple, Any, TypeVar, Callable
from lionagi.libs import func_call, convert, ParseUtil
from lionagi import logging as _logging
from .tool import Tool, TOOL_TYPE
from .util import parse_tool_response
from .function_calling import FunctionCalling

from ..tool.tool_manager import func_to_tool

T = TypeVar("T", bound=Tool)


class ToolManager:

    def __init__(
        self,
        tool_registry: dict = {},
        function_calling_tasks: dict[str : deque[FunctionCalling]] = {},
    ):
        self.registry = tool_registry
        self.function_calling_tasks = function_calling_tasks

    @singledispatchmethod
    def register_tools(self, tools: Any):
        raise TypeError(f"Unsupported type {type(tools)}")

    @register_tools.register(Tool)
    def _(self, tools):
        name = tools.schema_["function"]["name"]
        if self._has_name(name):
            err_msg = f"Function {name} is already registered."
            _logging.error(err_msg)
            raise ValueError(err_msg)
        else:
            self.registry[name] = tools
            self.function_calling_tasks[name] = deque()
            return True

    @register_tools.register(Callable)
    def _(self, tools):
        tool = func_to_tool(tools)[0]
        return self.register_tools(tool)

    @register_tools.register(list)
    def _(self, tools):
        return func_call.lcall(tools, self.register_tools)

    @singledispatchmethod
    def register_function_calling(self, func_params: Any):
        raise TypeError(f"Unsupported type {type(func_params)}")

    @register_function_calling.register(tuple)
    def _(self, func_params):
        func = self.registry[func_params[0]].func
        kwargs = func_params[1]
        _function_calling = FunctionCalling(func=func, kwargs=kwargs)
        self.function_calling_tasks[func.__name__].append(_function_calling)
        return True

    @register_function_calling.register(dict)
    def _(self, response):
        tuple_ = parse_tool_response(response)
        return self.register_function_calling(tuple_)

    @register_function_calling.register(list)
    def _(self, func_params):
        return func_call.lcall(func_params, self.register_function_calling)

    async def invoke(self, func_params: Tuple[str, dict[str, Any]]) -> Any:
        name, kwargs = func_params
        if not self._has_name(name):
            raise ValueError(f"Function {name} is not registered.")
        tool = self.registry[name]
        func = tool.func
        parser = tool.parser
        try:
            out = await func_call.call_handler(func, **kwargs)
            return parser(out) if parser else out

        except Exception as e:
            raise ValueError(
                f"Error when invoking function {name} with arguments {kwargs} with error message {e}"
            ) from e

    @property
    def _schema_list(self) -> list[dict[str, Any]]:
        return [tool.schema_ for tool in self.registry.values()]

    def get_tool_schema(self, tools: TOOL_TYPE, **kwargs):
        if isinstance(tools, bool):
            tool_kwarg = {"tools": self._schema_list}
            return tool_kwarg | kwargs

        else:
            if not isinstance(tools, list):
                tools = [tools]
            tool_kwarg = {"tools": self._get_tool_schema(tools)}
            return tool_kwarg | kwargs

    def _has_name(self, name: str) -> bool:
        return name in self.registry

    @singledispatchmethod
    def _get_tool_schema(self, tool: Any) -> dict:
        raise TypeError(f"Unsupported type {type(tool)}")

    @_get_tool_schema.register(dict)
    def _(self, tool):
        """
        assuming that the tool is a schema
        """
        return tool

    @_get_tool_schema.register(Tool)
    def _(self, tool):
        if self._has_name(tool.name):
            return self.registry[tool.name].schema_
        else:
            err_msg = f"Function {tool.name} is not registered."
            _logging.error(err_msg)
            raise ValueError(err_msg)

    @_get_tool_schema.register(str)
    def _(self, tool):
        """
        assuming that the tool is a name
        """
        if self._has_name(tool):
            return self.registry[tool].schema_
        else:
            err_msg = f"Function {tool} is not registered."
            _logging.error(err_msg)
            raise ValueError(err_msg)

    @_get_tool_schema.register(list)
    def _(self, tools):
        return func_call.lcall(tools, self._get_tool_schema)