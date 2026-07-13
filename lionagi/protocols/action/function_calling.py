# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from typing_extensions import Self

from lionagi.ln.concurrency import is_coro_func

from ..generic.event import Event
from .tool import Tool
from .tool_hooks import ActionGovernanceDeniedError


class RevalidationDeniedError(ActionGovernanceDeniedError):
    """Raised when rewritten tool arguments fail schema revalidation after a
    hook or preprocessor rewrite. A governance denial, not an ordinary tool
    exception -- distinct from a tool body raising ``PermissionError`` for
    its own business reasons.
    """


class FunctionCalling(Event):
    """Executes a Tool's callable asynchronously with optional pre/postprocessing."""

    func_tool: Tool = Field(
        ...,
        description="Tool instance containing the function to be called",
        exclude=True,
    )

    arguments: dict[str, Any] | BaseModel = Field(
        ..., description="Dictionary of arguments to pass to the function"
    )

    @field_validator("arguments", mode="before")
    def _validate_argument(cls, value):
        if isinstance(value, BaseModel):
            return value.model_dump(exclude_unset=True)
        return value

    @model_validator(mode="after")
    def _validate_strict_tool(self) -> Self:
        if self.func_tool.request_options:
            args: BaseModel = self.func_tool.request_options(**self.arguments)
            self.arguments = args.model_dump(exclude_unset=True)

        if self.func_tool.strict_func_call is True:
            if not set(self.arguments.keys()) == self.func_tool.required_fields:
                raise ValueError("arguments must match the function schema")

        else:
            if not self.func_tool.minimum_acceptable_fields.issubset(set(self.arguments.keys())):
                raise ValueError("arguments must match the function schema")
        return self

    @property
    def function(self):
        return self.func_tool.function

    async def _invoke(self) -> Any:
        """Execute the tool callable with pre/postprocessors; called by Event.invoke()."""

        if self.func_tool.preprocessor:
            if is_coro_func(self.func_tool.preprocessor):
                self.arguments = await self.func_tool.preprocessor(
                    self.arguments, **self.func_tool.preprocessor_kwargs
                )
            else:
                self.arguments = self.func_tool.preprocessor(
                    self.arguments, **self.func_tool.preprocessor_kwargs
                )

        # Re-validate after any pre-stage rewrite (hook layer or preprocessor
        # above) so a rewrite can never bypass the tool's declared schema.
        if self.func_tool.request_options:
            try:
                validated = self.func_tool.request_options(**self.arguments)
            except Exception as e:
                raise RevalidationDeniedError(
                    f"rewritten arguments failed validation for {self.func_tool.function!r}: {e}"
                ) from e
            self.arguments = validated.model_dump(exclude_unset=True)

        if is_coro_func(self.func_tool.func_callable):
            response = await self.func_tool.func_callable(**self.arguments)
        else:
            response = self.func_tool.func_callable(**self.arguments)

        if self.func_tool.postprocessor:
            if is_coro_func(self.func_tool.postprocessor):
                response = await self.func_tool.postprocessor(
                    response, **self.func_tool.postprocessor_kwargs
                )
            else:
                response = self.func_tool.postprocessor(
                    response, **self.func_tool.postprocessor_kwargs
                )
        return response

    def to_dict(self, *args, **kw) -> dict[str, Any]:
        """Serialize to dict, adding function name and arguments."""
        dict_ = super().to_dict(*args, **kw)
        dict_["function"] = self.function
        dict_["arguments"] = self.arguments
        return dict_
