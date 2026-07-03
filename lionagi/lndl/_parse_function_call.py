# Copyright (c) 2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Function call parser for LNDL <lact> bodies.

Parses Python-style function calls into a dict with 'operation', optional
'service', and 'arguments'.  When a service prefix is present (e.g.
``svc.tool(...)``), ``qualified_name`` returns ``"svc.tool"`` — this is the
name that should be used for tool-registry lookup so namespaced tools work.

Also supports:
- Batch parsing: [call1(...), call2(...)]
- Reserved keyword handling: from= -> from_= (Python keywords as args)
"""

from __future__ import annotations

import ast
import re
from typing import Any

# Python reserved keywords that might be used as field names
# These get mapped to underscore versions for parsing
RESERVED_KEYWORDS = {
    "from",
    "import",
    "class",
    "def",
    "return",
    "yield",
    "async",
    "await",
}

# Regex to match keyword arguments with reserved names
# Matches: from="value" or from='value' at word boundary
_RESERVED_KWARG_PATTERN = re.compile(r"\b(" + "|".join(RESERVED_KEYWORDS) + r")\s*=", re.MULTILINE)

__all__ = (
    "parse_batch_function_calls",
    "parse_function_call",
    "qualified_name",
)


def qualified_name(parsed: dict[str, Any]) -> str:
    """Return the tool name suitable for registry lookup.

    When the parsed call has a service prefix, returns ``"service.action"``;
    otherwise returns just ``"action"``.
    """
    svc = parsed.get("service")
    act = parsed["action"]
    return f"{svc}.{act}" if svc else act


def _escape_reserved_keywords(call_str: str) -> str:
    """Escape Python reserved keywords used as argument names.

    Converts `from=` to `from_=` so ast.parse can handle it.
    The underscore version is what Pydantic expects for aliased fields.

    Args:
        call_str: Function call string that may contain reserved keywords

    Returns:
        String with reserved keywords escaped
    """
    return _RESERVED_KWARG_PATTERN.sub(r"\1_=", call_str)


def _ast_to_value(node: ast.AST) -> Any:
    """Convert AST node to Python value with recursive dict/list processing.

    Handles nested dicts, lists, tuples, and JSON-style literals (true/false/null).
    Normalizes JSON literals: true->True, false->False, null->None.

    Args:
        node: AST node to convert

    Returns:
        Python value

    Raises:
        ValueError: If node cannot be converted to a value
    """
    # Handle JSON-style boolean/null names: true, false, null
    if isinstance(node, ast.Name):
        if node.id in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[node.id]
        raise ValueError(f"Name '{node.id}' is not a valid literal")

    # Handle dict nodes: {key1: val1, key2: val2, ...}
    if isinstance(node, ast.Dict):
        return {
            _ast_to_value(k): _ast_to_value(v) for k, v in zip(node.keys, node.values, strict=False)
        }

    # Handle list nodes: [elem1, elem2, ...]
    if isinstance(node, ast.List):
        return [_ast_to_value(elem) for elem in node.elts]

    # Handle tuple nodes: (elem1, elem2, ...)
    if isinstance(node, ast.Tuple):
        return tuple(_ast_to_value(elem) for elem in node.elts)

    # Handle simple literals (str, int, float, bool, None) via ast.literal_eval
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Cannot convert AST node: {type(node).__name__}") from e


def parse_function_call(call_str: str) -> dict[str, Any]:
    """Parse a Python-style function call string.

    Handles optional service prefix (``service.action(...)``):
        - Simple: ``search("q")`` → ``{action: "search", ...}``
        - Namespaced: ``svc.tool("x")`` → ``{service: "svc", action: "tool", ...}``

    Use ``qualified_name(parsed)`` to get the registry-lookup name
    (``"svc.action"`` when service is present, else ``"action"``).

    Returns:
        Dict with ``action``, optional ``service``, and ``arguments``.

    Raises:
        ValueError: If the string is not a valid function call.
    """
    try:
        escaped_str = _escape_reserved_keywords(call_str)

        tree = ast.parse(escaped_str, mode="eval")
        call = tree.body

        if not isinstance(call, ast.Call):
            raise ValueError("Not a function call")

        service = None
        action = None

        if isinstance(call.func, ast.Name):
            action = call.func.id
        elif isinstance(call.func, ast.Attribute):
            action = call.func.attr
            node = call.func.value
            if isinstance(node, ast.Name):
                service = node.id
            elif isinstance(node, ast.Attribute):
                service = node.attr
        else:
            raise ValueError(f"Unsupported function type: {type(call.func)}")

        arguments = {}
        for i, arg in enumerate(call.args):
            arguments[f"_pos_{i}"] = _ast_to_value(arg)

        for keyword in call.keywords:
            if keyword.arg is None:
                raise ValueError("**kwargs not supported")
            arguments[keyword.arg] = _ast_to_value(keyword.value)

        result: dict[str, Any] = {
            "action": action,
            "arguments": arguments,
        }

        if service:
            result["service"] = service

        return result

    except (SyntaxError, ValueError) as e:
        raise ValueError(f"Invalid function call syntax: {e}") from e


def parse_batch_function_calls(batch_str: str) -> list[dict[str, Any]]:
    """Parse an array of function calls: ``[fn1(...), fn2(...)]``.

    Returns a list of dicts in the same shape as ``parse_function_call``.

    Raises:
        ValueError: If the string is not a valid array of function calls.
    """
    try:
        # Remove whitespace for easier parsing
        batch_str = batch_str.strip()

        # Must start with [ and end with ]
        if not (batch_str.startswith("[") and batch_str.endswith("]")):
            raise ValueError("Batch call must be enclosed in [ ]")

        # Escape reserved keywords before parsing (e.g., from= -> from_=)
        escaped_str = _escape_reserved_keywords(batch_str)

        # Parse as Python list expression
        tree = ast.parse(escaped_str, mode="eval")
        if not isinstance(tree.body, ast.List):
            raise ValueError("Not a list expression")

        results = []
        for element in tree.body.elts:
            if not isinstance(element, ast.Call):
                raise ValueError(f"List element is not a function call: {ast.dump(element)}")

            # Convert the Call node back to source code and parse it
            call_str = ast.unparse(element)
            parsed = parse_function_call(call_str)
            results.append(parsed)

        return results

    except (SyntaxError, ValueError) as e:
        raise ValueError(f"Invalid batch function call syntax: {e}") from e
