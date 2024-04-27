from lionagi.core.generic import BaseComponent
from .util import get_input_output_fields, system_fields


class Form(BaseComponent):

    def __init__(self, **kwargs):
        """
        at initialization, all relevant fields if not already provided, are set to None,
        not every field is required to be filled, nor required to be declared at initialization
        """
        super().__init__(**kwargs)
        if not self.assignment:
            self.input_fields, self.output_fields = [], []
        else:
            self.input_fields, self.output_fields = get_input_output_fields(
                self.assignment
            )
        for i in self.input_fields + self.output_fields:
            if i not in self.model_fields:
                self._add_field(i, value=None)

    def check_workable(self):
        if self.filled:
            raise ValueError(f"Form {self.id_} is already filled")

        if (
            len(
                non_fields := [
                    i for i in self.input_fields if getattr(self, i, None) is None
                ]
            )
            > 0
        ):
            raise ValueError(f"Form {self.id_} is missing input fields: {non_fields}")

        return True

    def fill(self, form: "Form" = None, **kwargs):
        """
        only work fields for this form can be filled
        a field can only be filled once
        """
        if self.filled:
            raise ValueError(f"Form {self.id_} is already filled")

        fields = form.work_fields if form else {}
        kwargs = {**fields, **kwargs}

        for k, v in kwargs.items():
            if k not in self.work_fields:
                raise ValueError(
                    f"Form {self.id_}: Field {k} is not a valid work field"
                )
            setattr(self, k, v)

    @property
    def workable(self):
        try:
            self.check_workable()
            return True
        except Exception:
            return False

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
