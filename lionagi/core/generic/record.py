from abc import ABC, abstractmethod
from pydantic import Field
from .component import BaseComponent


class Record(BaseComponent, ABC):

    assignment: str | None = Field(None, examples=["input1, input2 -> output"])
    input_fields: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)

    @abstractmethod
    def fill(self, *args, **kwargs):
        pass

    @abstractmethod
    def check_workable(self):
        pass
