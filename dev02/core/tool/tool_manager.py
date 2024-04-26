import asyncio

from typing import Tuple, Any, TypeVar, Callable
from lionagi.libs import func_call, convert, ParseUtil

from lionagi.core.tool.tool import Tool, TOOL_TYPE

T = TypeVar("T", bound=Tool)


class ToolManager:
    """
    A manager class for handling the registration and invocation of tools that are subclasses of Tool.

    This class maintains a registry of tool instances, allowing for dynamic invocation based on
    tool name and provided arguments. It supports both synchronous and asynchronous tool function
    calls.

    Attributes:
        registry (dict[str, Tool]): A dictionary to hold registered tools, keyed by their names.
    """

    registry: dict = {}

    def name_existed(self, name: str) -> bool:
        """
        Checks if a tool name already exists in the registry.

        Args:
            name (str): The name of the tool to check.

        Returns:
            bool: True if the name exists, False otherwise.
        """
        return name in self.registry

    @property
    def has_tools(self):
        return self.registry != {}

    def _register_tool(self, tool: Tool | Callable) -> None:
        """
        Registers a tool in the registry. Raises a TypeError if the object is not an instance of Tool.

        Args:
            tool (Tool): The tool instance to register.

        Raises:
            TypeError: If the provided object is not an instance of Tool.
        """
        if isinstance(tool, Callable):
            tool = func_to_tool(tool)[0]
        if not isinstance(tool, Tool):
            raise TypeError("Please register a Tool object.")
        name = tool.schema_["function"]["name"]
        self.registry.update({name: tool})

    async def invoke(self, func_calls: Tuple[str, dict[str, Any]]) -> Any:
        """
        Invokes a registered tool's function with the given arguments. Supports both coroutine and regular functions.

        Args:
            func_call (Tuple[str, Dict[str, Any]]): A tuple containing the function name and a dictionary of keyword arguments.

        Returns:
            Any: The result of the function call.

        Raises:
            ValueError: If the function name is not registered or if there's an error during function invocation.
        """
        name, kwargs = func_calls
        if not self.name_existed(name):
            raise ValueError(f"Function {name} is not registered.")
        tool = self.registry[name]
        func = tool.func
        parser = tool.parser
        try:
            if func_call.is_coroutine_func(func):
                tasks = [func_call.call_handler(func, **kwargs)]
                out = await asyncio.gather(*tasks)
                return parser(out[0]) if parser else out[0]
            else:
                out = func(**kwargs)
                return parser(out) if parser else out
        except Exception as e:
            raise ValueError(
                f"Error when invoking function {name} with arguments {kwargs} with error message {e}"
            ) from e

    @staticmethod
    def get_function_call(response: dict) -> Tuple[str, dict]:
        """
        Extracts a function call and arguments from a response dictionary.

        Args:
            response (dict): The response dictionary containing the function call information.

        Returns:
            Tuple[str, dict]: A tuple containing the function name and a dictionary of arguments.

        Raises:
            ValueError: If the response does not contain valid function call information.
        """
        try:
            func = response["action"][7:]
            args = convert.to_dict(response["arguments"])
            return func, args
        except Exception:
            try:
                func = response["recipient_name"].split(".")[-1]
                args = response["parameters"]
                return func, args
            except:
                raise ValueError("response is not a valid function call")

    def register_tools(self, tools: list[Tool]) -> None:
        """
        Registers multiple tools in the registry.

        Args:
            tools (list[Tool]): A list of tool instances to register.
        """
        func_call.lcall(tools, self._register_tool)

    def to_tool_schema_list(self) -> list[dict[str, Any]]:
        """
        Generates a list of schemas for all registered tools.

        Returns:
            list[dict[str, Any]]: A list of tool schemas.

        """
        return [tool.schema_ for tool in self.registry.values()]

    def parse_tool(self, tools: TOOL_TYPE, **kwargs) -> dict:
        """
        Parses tool information and generates a dictionary for tool invocation.

        Args:
            tools: Tool information which can be a single Tool instance, a list of Tool instances, a tool name, or a list of tool names.
            **kwargs: Additional keyword arguments.

        Returns:
            dict: A dictionary containing tool schema information and any additional keyword arguments.

        Raises:
            ValueError: If a tool name is provided that is not registered.
        """

        def tool_check(tool):
            if isinstance(tool, dict):
                return tool
            elif isinstance(tool, Tool):
                return tool.schema_
            elif isinstance(tool, str):
                if self.name_existed(tool):
                    tool: Tool = self.registry[tool]
                    return tool.schema_
                else:
                    raise ValueError(f"Function {tool} is not registered.")

        if isinstance(tools, bool):
            tool_kwarg = {"tools": self.to_tool_schema_list()}
            kwargs = tool_kwarg | kwargs

        else:
            if not isinstance(tools, list):
                tools = [tools]
            tool_kwarg = {"tools": func_call.lcall(tools, tool_check)}
            kwargs = tool_kwarg | kwargs

        return kwargs


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
