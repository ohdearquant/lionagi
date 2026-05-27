# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


import inspect
from collections.abc import Callable
from typing import Any, Literal, TypeAlias

from pydantic import Field, model_validator
from typing_extensions import Self

from lionagi.protocols.generic.element import Element

__all__ = (
    "Tool",
    "FuncTool",
    "FuncToolRef",
    "ToolRef",
)


class Tool(Element):
    """
    Wraps a callable function with optional:
      - Preprocessing of arguments,
      - Postprocessing of results,
      - Strict or partial argument matching.

    `tool_schema` is auto-generated from the function signature if not provided.
    """

    func_callable: Callable[..., Any] = Field(
        ...,  # ... indicates required field
        description="The callable function to be wrapped by the tool",
        exclude=True,
    )

    mcp_config: dict[str, dict[str, Any]] | None = None
    """{tool_name: mcp_config dict}"""

    tool_schema: dict[str, Any] | None = Field(
        default=None,
        description="Schema describing the function's parameters and structure",
    )

    request_options: type | None = Field(
        default=None,
        description="Optional Pydantic model for validating the function's input",
    )

    preprocessor: Callable[[Any], Any] | None = Field(
        default=None,
        description="Optional function for preprocessing inputs before execution",
        exclude=True,
    )

    preprocessor_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments passed to the preprocessor function",
        exclude=True,
    )

    postprocessor: Callable[[Any], Any] | None = Field(
        default=None,
        description="Optional function for postprocessing outputs after execution",
        exclude=True,
    )

    postprocessor_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments passed to the postprocessor function",
        exclude=True,
    )

    strict_func_call: bool = Field(
        default=False,
        description="Whether to enforce strict validation of function parameters",
    )

    governance_meta: dict[str, Any] | None = Field(
        default=None,
        description="Optional governance metadata from `@governed_tool`.",
    )

    @model_validator(mode="before")
    def _validate_callable_config(cls, data):
        mcp_config = data.get("mcp_config")
        func_callable = data.get("func_callable")
        # Accept `func` as a shorthand alias for `func_callable`
        if func_callable is None and "func" in data:
            func_callable = data.pop("func")
            data["func_callable"] = func_callable

        if mcp_config is not None:
            if func_callable is not None:
                raise ValueError("`mcp_config` and `func_callable` cannot both be set.")
            if not isinstance(mcp_config, dict):
                raise ValueError("`mcp_config` must be a dictionary.")
            if len(mcp_config) != 1:
                raise ValueError("`mcp_config` must contain exactly one entry.")
            tool_name, config = next(iter(mcp_config.items()))

            from lionagi.service.connections.mcp_wrapper import create_mcp_tool

            func_callable = create_mcp_tool(config, tool_name)
        else:
            from lionagi.libs.validate.common_field_validators import validate_callable

            validate_callable(cls, func_callable, undefind_able=False, check_name=True)

        data["func_callable"] = func_callable
        return data

    @model_validator(mode="after")
    def _validate_tool_schema(self) -> Self:
        if self.tool_schema is None:
            from lionagi.libs.schema.function_to_schema import function_to_schema

            self.tool_schema = function_to_schema(
                self.func_callable, request_options=self.request_options
            )

        return self

    @property
    def function(self) -> str:
        """Return the function name from the auto-generated schema."""
        return self.tool_schema["function"]["name"]

    @property
    def required_fields(self) -> set[str]:
        """Return the set of required parameter names from the schema."""
        return set(self.tool_schema["function"]["parameters"]["required"])

    @property
    def minimum_acceptable_fields(self) -> set[str]:
        """
        Return the set of parameters that have no default values,
        ignoring `*args` or `**kwargs`.
        """
        try:
            a = {
                k
                for k, v in inspect.signature(self.func_callable).parameters.items()
                if v.default == inspect.Parameter.empty
            }
            for i in ("kw", "kwargs", "args"):
                if i in a:
                    a.remove(i)
            return a
        except Exception:
            return set()

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        """This is not implemented, as Tools are not typically created from arbitrary dicts."""
        raise NotImplementedError("`Tool.from_dict` is not supported.")

    def to_dict(self, mode: Literal["python", "json", "db"] = "python", **kw) -> dict[str, Any]:
        """
        Serialize the Tool to a dict, including the `function` name.

        Args:
            mode: Serialization mode — "python" (default), "json", or "db".
            **kw: Additional keyword arguments forwarded to Element.to_dict().

        Returns:
            dict[str, Any]: The dictionary form (excluding callables).
        """
        dict_ = super().to_dict(mode=mode, **kw)
        dict_["function"] = self.function
        return dict_


FuncTool: TypeAlias = Tool | Callable[..., Any] | dict
"""Represents either a `Tool` instance, a raw callable function or mcp config."""

FuncToolRef: TypeAlias = FuncTool | str
"""
A reference to a function-based tool, by either the actual object,
the raw callable, or the function name as a string.
"""

ToolRef: TypeAlias = FuncToolRef | list[FuncToolRef] | bool
"""
Used for specifying one or more tool references, or a boolean
indicating 'all' or 'none'.
"""

# File: lionagi/protocols/action/tool.py
