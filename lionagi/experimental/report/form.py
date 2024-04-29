from pydantic import Field

# from lionagi import logging as _logging
from lionagi.core.generic import Component
from lionagi.experimental.report.util import get_input_output_fields, system_fields


class Form(Component):

    assignment: str = Field(..., examples=["input1, input2 -> output"])

    input_fields: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)

    def __init__(self, **kwargs):
        """
        at initialization, all relevant fields if not already provided, are set to None,
        not every field is required to be filled, nor required to be declared at initialization
        """
        super().__init__(**kwargs)
        self.input_fields, self.output_fields = get_input_output_fields(self.assignment)
        for i in self.input_fields + self.output_fields:
            if i not in self.model_fields:
                self._add_field(i, value=None)

    @property
    def workable(self):
        if self.filled:
            return False

        for i in self.input_fields:
            if not getattr(self, i, None):
                return False

        return True

    @property
    def work_fields(self):
        dict_ = self.to_dict()
        return {
            k: v
            for k, v in dict_.items()
            if k not in system_fields and k in self.input_fields + self.output_fields
        }

    @property
    def filled(self):
        return all([value is not None for _, value in self.work_fields.items()])

    def fill(self, form: "Form" = None, **kwargs):
        """
        only work fields for this form can be filled
        a field can only be filled once
        """
        if self.filled:
            raise ValueError("Form is already filled")

        fields = form.work_fields if form else {}
        kwargs = {**fields, **kwargs}

        for k, v in kwargs.items():
            if k not in self.work_fields:
                raise ValueError(f"Field {k} is not a valid work field")
            setattr(self, k, v)
