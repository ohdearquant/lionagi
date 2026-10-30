# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Prepare a session gate's view without changing tool admission."""

import inspect
from typing import Any


def _structural_copy(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """Copy built-in containers without invoking object copy hooks."""
    if type(value) not in (dict, list, tuple):
        return value
    if memo is None:
        memo = {}
    key = id(value)
    if key in memo:
        return memo[key]
    if type(value) is dict:
        result: Any = {}
        memo[key] = result
        for item_key, item_value in value.items():
            result[item_key] = _structural_copy(item_value, memo)
        return result
    if type(value) is list:
        result = []
        memo[key] = result
        result.extend(_structural_copy(item, memo) for item in value)
        return result
    result = tuple(_structural_copy(item, memo) for item in value)
    # A tuple may have been reached again through a list during recursion.
    return memo.setdefault(key, result)


def gate_arguments(manager: Any, function: Any, arguments: dict) -> tuple[dict, dict]:
    """Return invocation arguments and an isolated, default-bound gate view.

    Unknown tools are not resolved here: plugin activation belongs to the
    action manager, after the session gate. Failed inspection or binding leaves
    the gate with the supplied arguments, as before.
    """
    # Isolation covers JSON-shaped values. Other objects are shared, and a
    # later hook can still mutate the callable's own mutable default object.
    invocation = _structural_copy(arguments)

    view = invocation
    try:
        tool = manager.registry.get(function)
        if tool is not None:
            signature = inspect.signature(tool.func_callable)
            bound = signature.bind(**invocation)
            bound.apply_defaults()
            view = {}
            for name, value in bound.arguments.items():
                kind = signature.parameters[name].kind
                if kind is inspect.Parameter.VAR_KEYWORD:
                    view.update(value)
                elif kind is not inspect.Parameter.VAR_POSITIONAL:
                    view[name] = value
    except Exception:
        view = invocation

    return invocation, _structural_copy(view)
