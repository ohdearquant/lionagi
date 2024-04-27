from typing import Any, Callable
from pydantic import BaseModel, Field, field_serializer
from functools import singledispatchmethod
from lionagi.libs import convert


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
    @singledispatchmethod
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

    def __str__(self):
        return f"{self.func_name}({self.kwargs})"
