from typing import Any
from pydantic import Field
from lionagi.experimental.report.form import Form
from lionagi.core.generic import Component
from lionagi.experimental.report.util import get_input_output_fields

"""
## Report Usage Pattern






requirements, 
the assignments name should be consistent within a report, 
meaning, all forms will refer to the same field value when using the same name.

the same input field can be used in multiple forms, but each unique output field 
should be filled only once.
meaning, not two forms should have the any output fields in common.

a filled field cannot be None

usage pattern:

class Report1(Report):
    a: Any = None
    b: Any = None
    c: Any = None
    d: Any = None
    e: Any = None
    f: Any = None
    
    assignments: list = [
        "a, b -> c", 
        "c -> d", 
        "b, d -> e"
    ]

this means that the report will have 3 work steps, each corresponding to an unique 
form. the context for the report is the fields that cannot be output by any work 
teps in the report. 

the deliverable for the report is the fields that are output produced by the work 
steps but not used as input for any other work steps in the same report.

under the hood:

the report first create three forms and add to its forms dictionary

form1 = Form1(assignment="a, b -> c")
form2 = Form2(assignment="c -> d")
form3 = Form3(assignment="b -> e")
form3 = Form3(assignment="d, e -> f")

The Form1, Form2, Form3 can be a single form class or multiple form classes, 
as long as they have the same fields to interact

report is created by intaking different context inputs, in this case, we 
need to provide a and b to the report

report let work scheduler know which form to fill next, by using the next_forms() method, 
this method check if the dependencies of a form are filled, if so, all such forms are 
deemed to be next to fill. 

so to begin with we need the context fields, a and b, then the report checks the 
next forms, notice, for (a, b -> c), we have a and b, so this form can be filled next
also for (b -> e), we have b, so this form can also be filled next

thus the report will first send forms of those two assignments to the scheduler. 

as work gets done, forms get filled, the completed forms will be sent back to scheduler, 
and the scheduler is in charge of filling in the fields onto the report. After each 
time a report gets new fields filled, the scheduler will check the next forms to fill, 
and the process continues until all forms are filled.

so as (a, b -> c) is done, we get c, thus c -> d is then the next form
and d, e -> f is the last form to fill
once all forms are filled and all fields transfered to reports, the report is completed.

"""


class Report(Component):

    report_name: str = Field(
        default="default_report",
    )
    description: Any = Field(default=None)
    task: Any = Field(default=None)
    forms: dict[str, Form] = Field(
        default_factory=dict,
        description="A dictionary of forms related to the report, in {form.id_: form} format.",
    )
    context: dict = Field(default_factory=dict, description="context for the report")
    deliverable: dict = Field(
        default_factory=dict, description="deliverable for the report"
    )
    intermediate: dict = Field(
        default_factory=dict, description="intermediate fields for the report"
    )
    filled: bool = Field(False, description="whether the report is completed")
    assignments: list = Field([], description="assignment for the report")

    def fill_report(self, form: Form | str):
        form = self.forms[form] if isinstance(form, str) else form
        if not form.filled or form not in self.forms.values():
            raise ValueError("The form is not filled or not in the report.")

        for i in form.input_fields:
            if i not in self._filled_fields:
                setattr(self, i, getattr(form, i))
                if i not in self.deliverable:
                    self.intermediate[i] = getattr(form, i)

        for i in form.output_fields:
            setattr(self, i, getattr(form, i))
            if i not in self.deliverable:
                self.intermediate[i] = getattr(form, i)

    @property
    def work_fields(self):
        """
        all work fields across all forms, including intermediate output fields
        """
        all_fields = []
        for form in self.forms.values():
            all_fields.extend(form.work_fields)
        return list(set(all_fields))

    @property
    def is_completed(self):
        return all([hasattr(self, i) for i in self.work_fields])

    @property
    def is_workable(self):
        context_fields, deliverable_fields = get_input_output_fields(self.assignment)
        context_fields.extend(deliverable_fields)

        # check whether all work fields are assigned in the forms for the report
        if not all([i in self.work_fields for i in context_fields]):
            raise ValueError(
                f"Not all work fields are assigned in the forms for the report."
            )

        outs = []
        for form in self.forms.values():
            outs.extend(form.output_fields)

        if len(outs) != len(set(outs)):
            raise ValueError(f"Output fields should be unique across all forms.")

        inputs = []
        for form in self.forms.values():
            inputs.extend(form.input_fields)

        inputs = [i for i in inputs if i not in outs]
        if not all([i in self.work_fields for i in inputs]):
            raise ValueError(
                f"Not all input fields are assigned in the forms for the report."
            )

        return True

    def next_forms(self):
        to_do = []
        for i in self._unfilled_forms:
            if all([j in self._filled_fields for j in i.input_fields]):
                to_do.append(i)
        return to_do[0] if len(to_do) == 1 else to_do or None

    @property
    def _filled_forms(self):
        return [form for form in self.forms.values() if form.filled]

    @property
    def _unfilled_forms(self):
        return [form for form in self.forms.values() if not form.filled]

    @property
    def _filled_fields(self):
        filled_fields = []
        for i in self.work_fields:
            if getattr(self, i, None) is not None:
                filled_fields.append(i)
        return filled_fields

    @property
    def _unfilled_fields(self):
        return [i for i in self.work_fields if i not in self._filled_fields]


# import unittest
# from enum import Enum
# from typing import Any
# from pydantic import Field
# from lionagi import logging as _logging
# from lionagi.experimental.form.form import Form
# from lionagi.core.generic import Component
# from lionagi.experimental.form.util import get_input_output_fields


# class EXAMPLES(str, Enum):
#     EXAMPLE1 = "example1"
#     EXAMPLE2 = "example2"
#     EXAMPLE3 = "example3"


# class Form1(Form):
#     a: str | EXAMPLES = Field(EXAMPLES.EXAMPLE3, choices=list(EXAMPLES))
#     b: str = "input2"
#     c: str | None = Field(None, choices=["output1", "output2"])
#     d: float | None = Field(None)
#     assignment: str = "a, b -> c, d"
#     form_name: str = "custom_form"
#     description: str = "Test Form"


# class TestReport(unittest.TestCase):
#     def setUp(self):
#         self.report = Report(assignment="a, b -> c, d")
#         self.form1 = Form1(assignment="a, b -> c, d")
#         self.report.forms[self.form1.id_] = self.form1

#     def test_initialization(self):
#         self.assertEqual(self.report.report_name, "default_report")
#         self.assertIn(self.form1.id_, self.report.forms)

#     def test_fill_report(self):
#         self.form1.process(out_={"c": "output1", "d": 1.1})
#         self.report.fill_report(self.form1)
#         self.assertTrue(self.form1.filled)
#         self.assertEqual(self.report.intermediate["c"], "output1")

#     def test_report_completeness(self):
#         self.form1.process(out_={"c": "output1", "d": 1.1})
#         self.report.fill_report(self.form1)
#         self.assertTrue(self.report.is_completed)

#     def test_report_workability(self):
#         # self.form1.process(out_={'c': 'output1', 'd': 1.1})
#         # self.report.fill_report(self.form1)
#         self.assertTrue(self.report.is_workable)

#     def test_handling_invalid_form_id(self):
#         with self.assertRaises(KeyError):
#             self.report.fill_report("nonexistent_form")

#     def test_next_forms_logic(self):
#         next_forms = self.report.next_forms()
#         self.assertEqual(next_forms, None)

#     def test_work_fields_property(self):
#         self.form1.process(out_={"c": "output1", "d": 1.1})
#         self.report.fill_report(self.form1)
#         self.assertIn("a", self.report.work_fields)
#         self.assertIn("b", self.report.work_fields)
#         self.assertIn("c", self.report.work_fields)
#         self.assertIn("d", self.report.work_fields)

#     def test_filled_forms_property(self):
#         self.form1.process(out_={"c": "output1", "d": 1.1})
#         self.report.fill_report(self.form1)
#         self.assertIn(self.form1, self.report._filled_forms)

#     def test_unfilled_forms_property(self):
#         self.assertIn(self.form1, self.report._unfilled_forms)
#         self.form1.process(out_={"c": "output1", "d": 1.1})
#         self.report.fill_report(self.form1)
#         self.assertNotIn(self.form1, self.report._unfilled_forms)

#     def test_filled_fields_property(self):
#         self.form1.process(out_={"c": "output1", "d": 1.1})
#         self.report.fill_report(self.form1)
#         self.assertIn("c", self.report._filled_fields)
#         self.assertIn("d", self.report._filled_fields)

#     def test_unfilled_fields_property(self):
#         self.assertIn("c", self.report._unfilled_fields)
#         self.assertIn("d", self.report._unfilled_fields)
#         self.form1.process(out_={"c": "output1", "d": 1.1})
#         self.report.fill_report(self.form1)
#         self.assertNotIn("c", self.report._unfilled_fields)
#         self.assertNotIn("d", self.report._unfilled_fields)


# if __name__ == "__main__":
#     unittest.main()
