# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import inspect
from collections.abc import Callable
from typing import Literal


def extract_docstring(
    func: Callable, style: Literal["google", "rest"] = "google"
) -> tuple[str | None, dict[str, str]]:
    """
    Extract function description and parameter descriptions from docstring.

    Args:
        func: The function from which to extract docstring details.
        style: The style of docstring to parse ('google' or 'rest').

    Returns:
        A tuple containing the function description and a dictionary with
        parameter names as keys and their descriptions as values.

    Raises:
        ValueError: If an unsupported style is provided.

    Examples:
        >>> def example_function(param1: int, param2: str):
        ...     '''Example function.
        ...
        ...     Args:
        ...         param1 (int): The first parameter.
        ...         param2 (str): The second parameter.
        ...     '''
        ...     pass
        >>> description, params = extract_docstring_details(example_function)
        >>> description
        'Example function.'
        >>> params == {'param1': 'The first parameter.',
        ...            'param2': 'The second parameter.'}
        True
    """
    style = str(style).strip().lower()

    if style == "google":
        func_description, params_description = _extract_docstring_details_google(func)
    elif style == "rest":
        func_description, params_description = _extract_docstring_details_rest(func)
    else:
        raise ValueError(f'{style} is not supported. Please choose either "google" or "reST".')
    return func_description, params_description


def _extract_docstring_details_google(
    func: Callable,
) -> tuple[str | None, dict[str, str]]:
    """
    Extract details from Google-style docstring.

    Args:
        func: The function from which to extract docstring details.

    Returns:
        A tuple containing the function description and a dictionary with
        parameter names as keys and their descriptions as values.

    Examples:
        >>> def example_function(param1: int, param2: str):
        ...     '''Example function.
        ...
        ...     Args:
        ...         param1 (int): The first parameter.
        ...         param2 (str): The second parameter.
        ...     '''
        ...     pass
        >>> description, params = _extract_docstring_details_google(
        ...     example_function)
        >>> description
        'Example function.'
        >>> params == {'param1': 'The first parameter.',
        ...            'param2': 'The second parameter.'}
        True
    """
    docstring = inspect.getdoc(func)
    if not docstring:
        return None, {}
    lines = docstring.split("\n")
    func_description = lines[0].strip()

    lines_len = len(lines)

    params_description = {}
    param_start_pos = next(
        (
            i + 1
            for i in range(1, lines_len)
            if (
                (a := str(lines[i]).strip().lower()).startswith("args")
                or a.startswith("parameters")
                or a.startswith("params")
                or a.startswith("arguments")
            )
        ),
        0,
    )
    current_param = None
    for i in range(param_start_pos, lines_len):
        if lines[i] == "":
            continue
        elif lines[i].startswith(" "):
            param_desc = lines[i].split(":", 1)
            if len(param_desc) == 1:
                params_description[current_param] += f" {param_desc[0].strip()}"
                continue
            param, desc = param_desc
            param = param.split("(")[0].strip()
            params_description[param] = desc.strip()
            current_param = param
        else:
            break
    return func_description, params_description


def _extract_docstring_details_rest(
    func: Callable,
) -> tuple[str | None, dict[str, str]]:
    """
    Extract details from reStructuredText-style docstring.

    Args:
        func: The function from which to extract docstring details.

    Returns:
        A tuple containing the function description and a dictionary with
        parameter names as keys and their descriptions as values.

    Examples:
        >>> def example_function(param1: int, param2: str):
        ...     '''Example function.
        ...
        ...     :param param1: The first parameter.
        ...     :type param1: int
        ...     :param param2: The second parameter.
        ...     :type param2: str
        ...     '''
        ...     pass
        >>> description, params = _extract_docstring_details_rest(
        ...     example_function)
        >>> description
        'Example function.'
        >>> params == {'param1': 'The first parameter.',
        ...            'param2': 'The second parameter.'}
        True
    """
    docstring = inspect.getdoc(func)
    if not docstring:
        return None, {}
    lines = docstring.split("\n")
    func_description = lines[0].strip()

    params_description = {}
    current_param = None
    for line in lines[1:]:
        line = line.strip()
        if line.startswith(":param"):
            param_desc = line.split(":", 2)
            _, param, desc = param_desc
            param = param.split()[-1].strip()
            params_description[param] = desc.strip()
            current_param = param
        elif line.startswith(" "):
            params_description[current_param] += f" {line}"

    return func_description, params_description
