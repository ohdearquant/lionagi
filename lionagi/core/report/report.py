from typing import Any, Type
from pydantic import Field

from lionagi.core.generic import BaseComponent
from lionagi.experimental.report.form import Form
from lionagi.experimental.report.util import get_input_output_fields
from ..work.schema import WorkStatus


class Report(BaseComponent):

    status: WorkStatus = WorkStatus.PENDING

    assignment: str = Field(..., examples=["input1, input2 -> output"])

    forms: dict[str, Form] = Field(
        default_factory=dict,
        description="A dictionary of forms related to the report, in {assignment: Form} format.",
    )

    form_assignments: list = Field(
        [],
        description="assignment for the report",
        examples=[["a, b -> c", "a -> e", "b -> f", "c -> g", "e, f, g -> h"]],
    )

    form_template: Type[Form] = Field(
        Form, description="The template for the forms in the report."
    )

    input_fields: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)

    def __init__(self, **kwargs):
        """
        at initialization, all relevant fields if not already provided, are set to None
        """
        super().__init__(**kwargs)
        self.input_fields, self.output_fields = get_input_output_fields(self.assignment)

        # if assignments is not provided, set it to assignment
        if self.form_assignments == []:
            self.form_assignments.append(self.assignment)

        # create forms
        new_forms = {i: self.form_template(assignment=i) for i in self.form_assignments}

        # add new forms into the report (will ignore new forms already in the
        # report with same assignment)
        for k, v in new_forms.items():
            if k not in self.forms:
                self.forms[k] = v

        # if the fields are not declared in the report, add them to report
        # with value set to None
        for k, v in self.forms.items():
            for f in list(v.work_fields.keys()):
                if f not in self.model_fields:
                    field = v.model_fields[f]
                    self._add_field(f, value=None, field=field)

        # if there are fields in the report that are not in the forms, add them to
        # the forms with values
        for k, v in self.model_fields.items():
            if getattr(self, k, None) is not None:
                for f in self.forms.values():
                    if k in f.work_fields:
                        f.fill(**{k: getattr(self, k)})

    @property
    def work_fields(self) -> dict[str, Any]:
        """
        all work fields across all forms, including intermediate output fields,
        this information is extracted from the forms
        """

        all_fields = {}
        for form in self.forms.values():
            for k, v in form.work_fields.items():
                if k not in all_fields:
                    all_fields[k] = v
        return all_fields

    def fill(self, **kwargs):
        """
        fill the information to both the report and forms
        """
        kwargs = {**self.work_fields, **kwargs}
        for k, v in kwargs.items():
            if k in self.work_fields and getattr(self, k, None) is None:
                setattr(self, k, v)

        for form in self.forms.values():
            if not form.filled:
                _kwargs = {k: v for k, v in kwargs.items() if k in form.work_fields}
                form.fill(**_kwargs)

        if self.filled:
            self.status = WorkStatus.COMPLETED
        elif self.workable:
            self.status = WorkStatus.IN_PROGRESS
        elif not self.workable:
            self.status = WorkStatus.PENDING

    @property
    def filled(self):
        return all([value is not None for _, value in self.work_fields.items()])

    @property
    def workable(self) -> bool:
        """
        check if the report is workable
        """
        try:
            self.check_workable()
            return True
        except Exception:
            return False

    def check_workable(self) -> bool:

        if self.filled:
            raise ValueError(
                "The report is already filled, and cannot be worked on again."
            )

        for i in self.input_fields:
            if not getattr(self, i, None):
                raise ValueError(f"Field '{i}' is required to work on the report.")

        # this is the required fields from report's own assignment
        fields = self.input_fields
        fields.extend(self.output_fields)

        # if the report's own assignment is not in the forms, return False
        for f in fields:
            if f not in self.work_fields:
                raise ValueError(
                    f"Field {f} is a required deliverable, not found in work field."
                )

        # get all the output fields from all the forms
        outs = []
        for form in self.forms.values():
            outs.extend(form.output_fields)

        # all output fields should be unique, not a single output field should be
        # calculated by more than one form
        if len(outs) != len(set(outs)):
            raise ValueError("There are duplicate output fields in the forms.")

        return True

    @property
    def next_forms(self) -> list[Form] | None:
        a = [i for i in self.forms.values() if i.workable]
        return a if len(a) > 0 else None
