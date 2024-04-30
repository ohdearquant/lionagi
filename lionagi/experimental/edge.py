from pydantic import Field, field_validator
from .abc import Component, Condition


class Edge(Component):
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

    def __contains__(self, item):
        return item in (self.head, self.tail)

    def __iter__(self):
        iter(self.head, self.tail)
        
    @field_validator("head", "tail", mode="before")
    def _validate_head_tail(cls, value):
        if isinstance(value, Component):
            return value.id_
        return value

    def __str__(self) -> str:
        return (
            f"Edge (id_={self.id_}, from={self.head}, to={self.tail}, "
            f"label={self.label})"
        )

    def __repr__(self) -> str:
        return (
            f"Edge(id_={self.id_}, from={self.head}, to={self.tail}, "
            f"label={self.label})"
        )
