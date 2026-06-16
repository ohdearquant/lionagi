# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from lionagi.utils import is_same_dtype

from .._concepts import Condition, Relational
from ..generic.element import ID, Element

__all__ = (
    "EdgeCondition",
    "Edge",
)


class EdgeCondition(BaseModel, Condition):
    """Pydantic-validated condition for edge traversal evaluation."""

    source: Any = Field(
        default=None,
        title="Source",
        description="The source for condition evaluation",
    )

    model_config = ConfigDict(
        extra="allow",
        arbitrary_types_allowed=True,
    )


class Edge(Element):
    """Directed graph edge from head to tail with optional condition and label in properties."""

    head: UUID
    tail: UUID
    properties: dict[str, Any] = Field(
        default_factory=dict,
        title="Properties",
        description="Custom properties associated with this edge.",
    )

    def __init__(
        self,
        head: ID[Relational].Ref,
        tail: ID[Relational].Ref,
        condition: Condition | None = None,
        label: list[str] | None = None,
        **kwargs,
    ):
        """Link head to tail with optional condition and labels; extra kwargs go into properties."""
        head = ID.get_id(head)
        tail = ID.get_id(tail)
        if condition:
            if not isinstance(condition, Condition):
                raise ValueError(
                    "condition must be a Condition subclass "
                    "(e.g. EdgeCondition or a custom async Condition)."
                )
            kwargs["condition"] = condition
        if label:
            if isinstance(label, str):
                kwargs["label"] = [label]
            elif isinstance(label, list) and is_same_dtype(label, str):
                kwargs["label"] = label
            else:
                raise ValueError("Label must be a string or a list of strings.")

        super().__init__(head=head, tail=tail, properties=kwargs)

    @field_serializer("head", "tail")
    def _serialize_id(self, value: UUID) -> str:
        return str(value)

    @field_validator("head", "tail", mode="before")
    def _validate_id(cls, value: str) -> UUID:
        return ID.get_id(value)

    @property
    def label(self) -> list[str] | None:
        return self.properties.get("label", None)

    @property
    def condition(self) -> Condition | None:
        return self.properties.get("condition", None)

    @condition.setter
    def condition(self, value: Condition | None) -> None:
        if value is not None and not isinstance(value, Condition):
            raise ValueError(
                "condition must be a Condition subclass "
                "(e.g. EdgeCondition or a custom async Condition)."
            )
        self.properties["condition"] = value

    @label.setter
    def label(self, value: list[str] | None) -> None:
        if not value:
            self.properties["label"] = []
            return
        if isinstance(value, str):
            self.properties["label"] = [value]
            return
        if isinstance(value, list) and is_same_dtype(value, str):
            self.properties["label"] = value
            return
        raise ValueError("Label must be a string or a list of strings.")

    async def check_condition(self, *args, **kwargs) -> bool:
        """Return True if condition passes or no condition is set."""
        if self.condition is not None:
            return await self.condition.apply(*args, **kwargs)
        return True

    def update_property(self, key: str, value: Any) -> None:
        """Update or add a custom property in `self.properties`."""
        self.properties[key] = value

    def update_condition_source(self, source: Any) -> None:
        """Update the `.source` attribute in the assigned EdgeCondition, if any."""
        cond: EdgeCondition | None = self.properties.get("condition", None)
        if cond:
            cond.source = source


# File: lionagi/protocols/graph/edge.py
