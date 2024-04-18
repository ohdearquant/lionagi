"""
TODO: needs doing

"""

from typing import Any, Callable

from pydantic import BaseModel, Field, field_serializer

from lionagi.libs import func_call, convert, AsyncUtil
from lionagi.core.generic.node import Node


class FunctionCalling(BaseModel):
    func: Any = Field(..., alias="function")
    kwargs: Any = Field({}, alias="arguments")

    @field_serializer("func")
    def serialize_func(self, func: Callable):
        return func.__name__

    @property
    def func_name(self):
        return self.func.__name__

    @classmethod
    @func_call.singledispatchmethod
    def create(cls, func_call: Any):
        raise TypeError(f"Unsupported type {type(func_call)}")

    @create.register
    def _(cls, func_call: tuple):
        if len(func_call) == 2:
            return cls(func=func_call[0], kwargs=func_call[1])
        else:
            raise ValueError(f"Invalid tuple length {len(func_call)}")

    @create.register
    def _(cls, func_call: dict):
        return cls(**func_call)

    @create.register
    def _(cls, func_call: str):
        try:
            return cls(**convert.to_dict(func_call))
        except Exception as e:
            raise ValueError(f"Invalid string {func_call}") from e

    async def invoke(self):
        if func_call.is_coroutine_func(self.func):
            tasks = [func_call.call_handler(self.func, **self.kwargs)]
            return await AsyncUtil.execute_tasks(*tasks)[0]

        else:
            return self.func(**self.kwargs)


class Tool(Node):

    func: Any
    schema_: dict | None = None
    manual: Any | None = None
    parser: Any | None = None
    pre_processor: Any | None = None
    post_processor: Any | None = None

    @property
    def name(self):
        return self.schema_["function"]["name"]

    @field_serializer("func")
    def serialize_func(self, func):
        return func.__name__

    async def invoke(self, kwargs):
        if self.pre_processor:
            kwargs = await self.pre_processor(kwargs)

        func_call_: FunctionCalling = FunctionCalling.create(tuple(self.func, kwargs))
        result = await func_call_.invoke()

        if self.post_processor:
            if func_call.is_coroutine_func(self.post_processor):
                return await self.post_processor(result)
            else:
                return self.post_processor(result)

        return result


TOOL_TYPE = bool | Tool | str | list[Tool | str | dict] | dict
