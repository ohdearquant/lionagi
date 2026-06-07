# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from typing_extensions import Self

from lionagi.utils import is_coro_func

from ..generic.event import Event
from .tool import Tool


class FunctionCalling(Event):
    """Handles asynchronous function execution with pre/post processing.

    This class manages function calls with optional preprocessing and
    postprocessing, handling both synchronous and asynchronous functions.
    """

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
        """Execute the function call with pre/post processing.

        Handles both synchronous and asynchronous functions, including optional
        preprocessing of arguments and postprocessing of results.

        Called by Event.invoke() which handles state transitions.
        """

        async def _preprocess(kwargs):
            if is_coro_func(self.func_tool.preprocessor):
                return await self.func_tool.preprocessor(
                    kwargs, **self.func_tool.preprocessor_kwargs
                )
            return self.func_tool.preprocessor(kwargs, **self.func_tool.preprocessor_kwargs)

        async def _post_process(arg: Any):
            if is_coro_func(self.func_tool.postprocessor):
                return await self.func_tool.postprocessor(
                    arg, **self.func_tool.postprocessor_kwargs
                )
            return self.func_tool.postprocessor(arg, **self.func_tool.postprocessor_kwargs)

        if self.func_tool.preprocessor:
            self.arguments = await _preprocess(self.arguments)

        if is_coro_func(self.func_tool.func_callable):
            response = await self.func_tool.func_callable(**self.arguments)
        else:
            response = self.func_tool.func_callable(**self.arguments)

        if self.func_tool.postprocessor:
            response = await _post_process(response)
        return response

    def to_dict(self, *args, **kw) -> dict[str, Any]:
        """Convert instance to dictionary.

        Returns:
            dict[str, Any]: Dictionary representation of the instance.
        """
        dict_ = super().to_dict(*args, **kw)
        dict_["function"] = self.function
        dict_["arguments"] = self.arguments
        return dict_


# File: lionagi/protocols/action/function_calling.py
