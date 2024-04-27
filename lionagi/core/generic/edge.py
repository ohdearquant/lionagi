"""
Module for representing conditions and edges between nodes in a graph.

This module provides the base for creating and managing edges that connect
nodes within a graph. It includes support for conditional edges, allowing
the dynamic evaluation of connections based on custom logic.
"""

from typing import Any
from pydantic import Field, field_validator
from .abc import BaseComponent, Condition


class Edge(BaseComponent):
    """
    Represents an edge between two nodes, potentially with a condition.

    Attributes:
        head (str): The identifier of the head node of the edge.
        tail (str): The identifier of the tail node of the edge.
        condition (Condition | None): Optional condition that must be met
            for the edge to be considered active.
        label (str | None): An optional label for the edge.
        bundle (bool): A flag indicating if the edge is bundled.

    Methods:
        check_condition: Evaluates if the condition is met.
        string_condition: Retrieves the condition class source code.
    """

    head: str = Field(
        title="Head",
        description="The identifier of the head node of the edge.",
    )
    tail: str = Field(
        title="Tail",
        description="The identifier of the tail node of the edge.",
    )
    condition: Condition | None = Field(
        default=None,
        description="Optional condition that must be met for the edge "
                    "to be considered active.",
    )
    label: str | None = Field(
        default=None,
        description="An optional label for the edge.",
    )
    bundle: bool = Field(
        default=False,
        description="A flag indicating if the edge is bundled.",
    )

    @field_validator("head", "tail", mode="before")
    def _validate_head_tail(cls, value):
        """
        Validates head and tail fields to ensure valid node identifiers.

        Args:
            value (Any): The value of the field being validated.

        Returns:
            str: The validated value, ensuring it is a valid identifier.

        Raises:
            ValueError: If the validation fails.
        """

        if isinstance(value, BaseComponent):
            return value.id_
        return value

    def check_condition(self, obj: dict[str, Any]) -> bool:
        """
        Evaluates if the condition associated with the edge is met.

        Args:
            obj (dict[str, Any]): Context for condition evaluation.

        Returns:
            bool: True if the condition is met, False otherwise.

        Raises:
            ValueError: If the condition is not set.
        """
        if not self.condition:
            raise ValueError("The condition for the edge is not set.")
        return self.condition(obj)

    def string_condition(self):
        """
        Retrieves the condition class source code as a string.

        This method is useful for serialization and debugging, allowing
        the condition logic to be inspected or stored in a human-readable
        format. It employs advanced introspection techniques to locate and
        extract the exact class definition, handling edge cases like
        dynamically defined classes or classes defined interactively.

        Returns:
            str | None: The condition class source code if available.
                If the condition is None or the source code cannot be
                located, this method returns None.

        Raises:
            TypeError: If the condition class source code cannot be found
                due to the class being defined in a non-standard manner or
                in the interactive interpreter (__main__ context).
        """
        if self.condition is None:
            return

        import inspect, sys

        def new_getfile(object, _old_getfile=inspect.getfile):
            if not inspect.isclass(object):
                return _old_getfile(object)

            # Lookup by parent module (as in current inspect)
            if hasattr(object, "__module__"):
                object_ = sys.modules.get(object.__module__)
                if hasattr(object_, "__file__"):
                    return object_.__file__

            # If parent module is __main__, lookup by methods (NEW)
            for name, member in inspect.getmembers(object):
                if (
                    inspect.isfunction(member)
                    and object.__qualname__ + "." + member.__name__
                    == member.__qualname__
                ):
                    return inspect.getfile(member)
            else:
                raise TypeError("Source for {!r} not found".format(object))

        inspect.getfile = new_getfile

        import inspect
        from IPython.core.magics.code import extract_symbols

        obj = self.condition.__class__
        cell_code = "".join(inspect.linecache.getlines(new_getfile(obj)))
        class_code = extract_symbols(cell_code, obj.__name__)[0][0]
        return class_code

    def __str__(self) -> str:
        """
        Returns a simple string representation of the Edge.
        """
        return (
            f"Edge (id_={self.id_}, from={self.head}, to={self.tail}, "
            f"label={self.label})"
        )

    def __repr__(self) -> str:
        """
        Returns a detailed string representation of the Edge.
        """
        return (
            f"Edge(id_={self.id_}, from={self.head}, to={self.tail}, "
            f"label={self.label})"
        )
