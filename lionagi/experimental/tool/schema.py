from typing import Any
from pydantic import field_serializer
from functools import singledispatchmethod
from lionagi import logging as _logging
from lionagi.libs import func_call, AsyncUtil
from lionagi.core.generic.node import Node
from .function_calling import FunctionCalling


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


    @singledispatchmethod
    async def invoke(self, values: Any) -> Any:
        raise TypeError(f"Unsupported type {type(values)}")

    @invoke.register
    async def _(self, kwargs: dict):

        out = None
        
        if self.pre_processor:
            kwargs = await func_call.unified_call(self.pre_processor, kwargs)
        try:
            out = await func_call.unified_call(self.func, **kwargs)
            
        except Exception as e:
            _logging.error(f"Error invoking function {self.func_name}: {e}")
            return None

        if self.post_processor:
            return await func_call.unified_call(self.post_processor, out)

        return out

    @invoke.register
    async def _(self, function_calls: FunctionCalling):
        return await self.invoke(function_calls.kwargs)

    @invoke.register
    async def _(self, values: list):
        return await func_call.alcall(self.invoke, values)
     
TOOL_TYPE = bool | Tool | str | list[Tool | str | dict] | dict
