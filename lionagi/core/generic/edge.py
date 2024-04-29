"""
Module for representing conditions and edges between nodes in a graph structure.

This module provides the base for creating and managing edges that connect nodes
within a graph. It includes support for conditional edges, allowing the dynamic
evaluation of connections based on custom logic.
"""

from typing import Any
from pydantic import Field, field_validator
from .abc import Component, BaseNode, Condition


class Edge(Component):
    """
    Represents an edge between two nodes, potentially with a condition.

    Attributes:
        head (str): The identifier of the head node of the edge.
        tail (str): The identifier of the tail node of the edge.
        condition (Optional[Condition]): An optional condition that must be met
            for the edge to be considered active.
        label (Optional[str]): An optional label for the edge.

    Methods:
        check_condition: Evaluates if the condition associated with the edge is met.
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
        description="An optional condition that must be met for the edge to be considered active.",
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
        Validates the head and tail fields to ensure they are valid node identifiers.

        Args:
            value: The value of the field being validated.
            values: A dictionary of all other values on the model.
            field: The model field being validated.

        Returns:
            The validated value, ensuring it is a valid identifier.

        Raises:
            ValueError: If the validation fails.
        """
        if isinstance(value, BaseNode):
            return value.id_
        return value

    def check_condition(self, obj: dict[str, Any]) -> bool:
        """
        Evaluates if the condition associated with the edge is met.

        Args:
            obj (dict[str, Any]): The context object used for condition evaluation.

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
        Retrieves the source code of the condition class associated with this edge as a string.

        This method is useful for serialization and debugging, allowing the condition logic to be inspected or stored
        in a human-readable format. It employs advanced introspection techniques to locate and extract the exact class
        definition, handling edge cases like dynamically defined classes or classes defined in interactive environments.

        Returns:
            str: The source code of the condition's class, if available. If the condition is None or the source code
                cannot be located, this method returns None.

        Raises:
            TypeError: If the source code of the condition's class cannot be found due to the class being defined in a
                non-standard manner or in the interactive interpreter (__main__ context).
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
        Returns a simple string representation of the Relationship.
        """

        return (
            f"Edge (id_={self.id_}, from={self.head}, to={self.tail}, "
            f"label={self.label})"
        )

    def __repr__(self) -> str:
        """
        Returns a detailed string representation of the Relationship.

        Examples:
            >>> edge = Relationship(source_node_id="node1", target_node_id="node2")
            >>> repr(edge)
            'Relationship(id_=None, from=node1, to=node2, content=None, metadata=None, label=None)'
        """
        return (
            f"Edge(id_={self.id_}, from={self.head}, to={self.tail}, "
            f"label={self.label})"
        )
