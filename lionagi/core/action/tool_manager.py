from functools import singledispatchmethod
from collections import deque
from typing import Tuple, Any, TypeVar, Callable
from lionagi.libs import func_call, convert, ParseUtil
from lionagi import logging as _logging
from .schema import Tool, TOOL_TYPE
from .util import parse_tool_response
from .function_calling import FunctionCalling

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


def func_to_tool(
    func_: Callable | list[Callable], parser=None, docstring_style="google"
):
    """
    Transforms a given function into a Tool object, equipped with a schema derived
    from its docstring. This process involves parsing the function's docstring based
    on a specified style ('google' or 'reST') to extract relevant metadata and
    parameters, which are then used to construct a comprehensive schema for the Tool.
    This schema facilitates the integration of the function with systems or
    frameworks that rely on structured metadata for automation, documentation, or
    interface generation purposes.

    The function to be transformed can be any Callable that adheres to the
    specified docstring conventions. The resulting Tool object encapsulates the
    original function, allowing it to be utilized within environments that require
    objects with structured metadata.

    Args:
        func_ (Callable): The function to be transformed into a Tool object. This
            function should have a docstring that follows the
            specified docstring style for accurate schema generation.
        parser (Optional[Any]): An optional parser object associated with the Tool.
                  This parameter is currently not utilized in the
                  transformation process but is included for future
                  compatibility and extension purposes.
        docstring_style (str): The format of the docstring to be parsed, indicating
                 the convention used in the function's docstring.
                 Supports 'google' for Google-style docstrings and
                 'reST' for reStructuredText-style docstrings. The
                 chosen style affects how the docstring is parsed and
                 how the schema is generated.

    Returns:
        Tool: An object representing the original function wrapped as a Tool, along
            with its generated schema. This Tool object can be used in systems that
            require detailed metadata about functions, facilitating tasks such as
            automatic documentation generation, user interface creation, or
            integration with other software tools.

    Examples:
        >>> def example_function_google(param1: int, param2: str) -> bool:
        ...     '''
        ...     An example function using Google style docstrings.
        ...
        ...     Args:
        ...         param1 (int): The first parameter, demonstrating an integer input_.
        ...         param2 (str): The second parameter, demonstrating a string input_.
        ...
        ...     Returns:
        ...         bool: A boolean value, illustrating the return type.
        ...     '''
        ...     return True
        ...
        >>> tool_google = func_to_tool(example_function_google, docstring_style='google')
        >>> print(isinstance(tool_google, Tool))
        True

        >>> def example_function_reST(param1: int, param2: str) -> bool:
        ...     '''
        ...     An example function using reStructuredText (reST) style docstrings.
        ...
        ...     :param param1: The first parameter, demonstrating an integer input_.
        ...     :type param1: int
        ...     :param param2: The second parameter, demonstrating a string input_.
        ...     :type param2: str
        ...     :returns: A boolean value, illustrating the return type.
        ...     :rtype: bool
        ...     '''
        ...     return True
        ...
        >>> tool_reST = func_to_tool(example_function_reST, docstring_style='reST')
        >>> print(isinstance(tool_reST, Tool))
        True

    Note:
        The transformation process relies heavily on the accuracy and completeness of
        the function's docstring. Functions with incomplete or incorrectly formatted
        docstrings may result in incomplete or inaccurate Tool schemas.
    """

    fs = []
    funcs = convert.to_list(func_, flatten=True, dropna=True)
    parsers = convert.to_list(parser, flatten=True, dropna=True)

    if parser:
        if len(funcs) != len(parsers) != 1:
            raise ValueError(
                "Length of parser must match length of func. Except if you only pass one"
            )

        for idx in range(len(funcs)):
            f_ = lambda _f: Tool(
                func=_f,
                schema_=ParseUtil._func_to_schema(_f, style=docstring_style),
                parser=parsers[idx] if len(parsers) > 1 else parsers[0],
            )

            fs.append(f_)

    else:
        fs = func_call.lcall(
            funcs,
            lambda _f: Tool(
                func=_f, schema_=ParseUtil._func_to_schema(_f, style=docstring_style)
            ),
        )

    return fs