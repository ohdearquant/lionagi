from typing import Any, Union, Dict, List, Optional, Callable
from functools import singledispatchmethod
import logging
from lionagi.libs.ln_func_call import call_handler, alcall
from lionagi.core.generic.node import Node
from .function_calling import FunctionCalling

class Tool(Node):
    """
    Represents a tool that encapsulates functionality around a given function,
    including optional preprocessing and postprocessing steps, and maintains
    additional metadata in a schema.
    """
    
    func: Any
    schema_: Optional[Dict[str, Any]] = None
    manual: Optional[Any] = None
    parser: Optional[Any] = None
    pre_processor: Optional[Callable] = None
    post_processor: Optional[Callable] = None

    @property
    def name(self) -> str:
        """
        Retrieves the function name from the schema.

        Returns:
            str: The name of the function.
        """
        return self.schema_["function"]["name"] if self.schema_ else "Unknown Function"

    @singledispatchmethod
    async def invoke(self, values: Any) -> Any:
        """
        Dynamically invokes the function based on the type of values provided.

        Args:
            values (Any): The values to be processed, which can vary in type.

        Raises:
            TypeError: If the input type is unsupported.
        """
        raise TypeError(f"Unsupported type {type(values).__name__}")

    @invoke.register
    async def _(self, kwargs: Dict[str, Any]) -> Any:
        """
        Handles dictionary input by possibly applying pre-processing, invoking the main function,
        and then applying post-processing if defined.

        Args:
            kwargs (Dict[str, Any]): Keyword arguments for the function.

        Returns:
            Any: The result of the function call, possibly post-processed.
        """
        try:
            if self.pre_processor:
                kwargs = await call_handler(self.pre_processor, kwargs)
            result = await call_handler(self.func, **kwargs)
            if self.post_processor:
                result = await call_handler(self.post_processor, result)
            return result
        except Exception as e:
            logging.error(f"Error invoking function {self.name}: {e}")
            return None

    @invoke.register
    async def _(self, function_calls: FunctionCalling) -> Any:
        """
        Invokes using a FunctionCalling instance.

        Args:
            function_calls (FunctionCalling): An encapsulation of the function and its arguments.

        Returns:
            Any: The result of invoking the function call.
        """
        return await self.invoke(function_calls.kwargs)

    @invoke.register
    async def _(self, values: List[Any]) -> List[Any]:
        """
        Processes a list of values asynchronously.

        Args:
            values (List[Any]): A list of values to process.

        Returns:
            List[Any]: A list of results from processing each value.
        """
        return await alcall(self.invoke, values)

TOOL_TYPE = Union[bool, Tool, str, List[Union[Tool, str, Dict]], Dict[str, Any]]
