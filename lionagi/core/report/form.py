"""
Copyright 2024 HaiyangLi

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""
This module extends the BaseForm class to implement the Form class, which
dynamically manages form operations based on specific assignments. It provides
functionalities for initializing fields, filling forms with data, and
validating the readiness of forms for further processing.
"""

from typing import Dict, Any
from lionagi.core.collections.abc import SYSTEM_FIELDS
from lionagi.core.report.util import get_input_output_fields
from lionagi.core.report.base import BaseForm


class Form(BaseForm):
    """
    A specialized implementation of BaseForm designed to manage form fields
    dynamically based on specified assignments. Supports initialization and
    management of input and requested fields, handles form filling operations,
    and ensures forms are properly configured for use.

    Attributes:
        input_fields (List[str]): Fields required to carry out the objective of the form.
        requested_fields (List[str]): Fields requested to be filled by the user.
    """

    def __init__(self, **kwargs):
        """
        Initializes a new instance of the Form, setting up input and requested
        fields based on the form's assignment.
        """
        super().__init__(**kwargs)
        self.input_fields, self.requested_fields = get_input_output_fields(
            self.assignment
        )

        for i in self.input_fields:
            self.append_to_input(i)

        for i in self.input_fields + self.requested_fields:
            if i not in self._all_fields:
                self._add_field(i, value=None)

    def append_to_request(self, field: str):
        if "," in field:
            field = field.split(",")
        if not isinstance(field, list):
            field = [field]
        
        for i in field:
            i = i.strip()
            if i not in self._all_fields:
                self._add_field(i)

            if i not in self.requested_fields:
                self.requested_fields.append(i)
                self.validation_kwargs[i] = self._get_field_attr(
                    i, "validation_kwargs", {}
                )

    def append_to_input(self, field: str, field_obj=None):
        if field not in self._all_fields:
            self._add_field(field, field_obj=field_obj)

        if field not in self.input_fields:
            self.input_fields.append(field)
            self.validation_kwargs[field] = self._get_field_attr(
                field, "validation_kwargs", {}
            )

    @property
    def work_fields(self) -> Dict[str, Any]:
        """
        Retrieves a dictionary of the fields relevant to the current task,
        excluding any SYSTEM_FIELDS and including only the input and requested
        fields.

        Returns:
            Dict[str, Any]: The relevant fields for the current task.
        """
        dict_ = self.to_dict()
        return {
            k: v
            for k, v in dict_.items()
            if k not in SYSTEM_FIELDS and k in self.input_fields + self.requested_fields
        }

    def fill(self, form: "Form" = None, strict: bool = True, **kwargs) -> None:
        """
        Fills the form from another form instance or provided keyword arguments.
        Raises an error if the form is already filled.

        Args:
            form (Form, optional): The form to fill from.
            strict (bool, optional): Whether to enforce strict filling. Defaults to True.
            **kwargs: Additional fields to fill.
        """
        if self.filled:
            if strict:
                raise ValueError("Form is filled, cannot be worked on again")

        all_fields = self._get_all_fields(form, **kwargs)

        for k, v in all_fields.items():
            if (
                k in self.work_fields
                and v is not None
                and getattr(self, k, None) is None
            ):
                setattr(self, k, v)

    def is_workable(self) -> bool:
        """
        Determines if the form is ready for processing. Checks if all required
        fields are filled and raises an error if the form is already filled or
        if any required field is missing.

        Returns:
            bool: True if the form is workable, otherwise raises ValueError.
        """
        if self.filled:
            raise ValueError("Form is already filled, cannot be worked on again")

        for i in self.input_fields:
            if not getattr(self, i, None):
                raise ValueError(f"Required field {i} is not provided")

        return True

    @property
    def _instruction_context(self) -> str:
        """
        Generates a detailed description of the input fields, including their
        current values and descriptions.

        Returns:
            str: A detailed description of the input fields.
        """
        a = "".join(
            f"""
        ## input: {i}:
        - description: {getattr(self._all_fields[i], "description", "N/A")}
        - value: {str(self.__getattribute__(self.input_fields[idx]))}
        """
            for idx, i in enumerate(self.input_fields)
        )
        return a.replace("        ", "")

    @property
    def _instruction_prompt(self) -> str:
        """
        Generates a brief summary of the form's context, including the task
        description and lists of provided and requested fields.

        Returns:
            str: A brief summary of the form's context.
        """
        ccc = f"""
        0. Your task is {self.task},
        1. provided: {self.input_fields}, 
        2. requested: {self.requested_fields}
        ----------
        """
        return ccc.replace("        ", "")

    @property
    def _instruction_requested_fields(self) -> Dict[str, str]:
        """
        Provides a dictionary mapping requested field names to their
        descriptions.

        Returns:
            Dict[str, str]: A dictionary of requested field descriptions.
        """
        return {
            i: getattr(self._all_fields[i], "description", "N/A")
            for i in self.requested_fields
        }
