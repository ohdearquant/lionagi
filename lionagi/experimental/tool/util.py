from typing import Callable, Tuple
from lionagi.libs import convert, func_call, ParseUtil
from lionagi.experimental.tool.schema import Tool

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



def parse_tool_response(response: dict) -> Tuple[str, dict]:
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