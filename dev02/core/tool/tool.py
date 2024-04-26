from typing import Any

from pydantic import field_serializer
from lionagi.core.generic import Node


class Tool(Node):
    """
    Represents a tool, extending BaseNode with specific functionalities and configurations.

    Attributes:
        func: The main function or capability of the tool.
        schema_: An optional schema defining the structure and constraints of data the tool works with.
        manual: Optional documentation or manual for using the tool.
        parser: An optional parser associated with the tool for data processing or interpretation.
    """

    func: Any
    schema_: dict | None = None
    manual: Any | None = None
    parser: Any | None = None

    @field_serializer("func")
    def serialize_func(self, func):
        return func.__name__


TOOL_TYPE = bool | Tool | str | list[Tool | str | dict] | dict
