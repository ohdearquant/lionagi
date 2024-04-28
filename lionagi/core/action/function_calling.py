from typing import Any, Callable
from pydantic import BaseModel, Field, field_serializer
from functools import singledispatchmethod
from lionagi.libs import convert


class FunctionCalling(BaseModel):
    """
    A model that encapsulates information about a function call, including the
    function itself and any keyword arguments. It provides methods to serialize
    function calls, instantiate from various types, and convert to string.
    """
    func: Callable = Field(..., alias="function")
    kwargs: dict = Field(default_factory=dict, alias="arguments")

    @field_serializer("func")
    def serialize_func(self, func: Callable):
        return func.__name__
    
    @property
    def func_name(self) -> str:
        """
        Returns the name of the function.

        Returns:
            str: The name of the function.
        """
        return self.func.__name__

    @classmethod
    @singledispatchmethod
    def create(cls, func_call: Any):
        """
        Create an instance based on the type of func_call. Raises TypeError if the
        type is unsupported.

        Args:
            func_call (Any): The function call information.

        Raises:
            TypeError: If the type of func_call is unsupported.
        """
        raise TypeError(f"Unsupported type {type(func_call).__name__}")

    @create.register
    def _(cls, func_call: tuple):
        """
        Create an instance from a tuple containing the function and its kwargs.

        Args:
            func_call (tuple): A tuple (function, kwargs).

        Returns:
            FunctionCalling: An instantiated object.

        Raises:
            ValueError: If the tuple does not contain exactly two elements.
        """
        if len(func_call) == 2:
            return cls(func=func_call[0], kwargs=func_call[1])
        else:
            raise ValueError(f"Invalid tuple length {len(func_call)}")

    @create.register
    def _(cls, func_call: dict):
        """
        Create an instance from a dictionary.

        Args:
            func_call (dict): A dictionary with keys 'func' and 'kwargs'.

        Returns:
            FunctionCalling: An instantiated object.
        """
        return cls(**func_call)

    @create.register
    def _(cls, func_call: str):
        """
        Create an instance from a string by converting it to a dictionary.

        Args:
            func_call (str): A string that represents function calling info.

        Returns:
            FunctionCalling: An instantiated object.

        Raises:
            ValueError: If the string cannot be converted to a dictionary.
        """
        try:
            return cls(**convert.to_dict(func_call))
        except Exception as e:
            raise ValueError(f"Invalid string {func_call}") from e

    def __str__(self) -> str:
        """
        String representation of the function call.

        Returns:
            str: Function call in the format 'func_name(kwargs)'.
        """
        return f"{self.func_name}({self.kwargs})"
