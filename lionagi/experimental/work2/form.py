from typing import Any
from pydantic import Field
from lionagi.core.generic import Component
from lionagi.libs import validation_funcs, convert
from lionagi.experimental.report.util import get_input_output_fields


class Form(Component):

    form_name: str = Field(
        default="default_form",
    )
    description: Any = Field(default=None)
    assignment: str = Field(..., examples=["input1, input2 -> output"])
    instruction: Any = Field(
        default=None,
    )
    input_fields: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    examples: Any = Field(
        default=None,
    )
    fix_input: bool = Field(False, description="whether to fix input")
    fix_output: bool = Field(True, description="whether to fix output")
    filled: bool = Field(False, description="whether the form is completed")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.input_fields, self.output_fields = get_input_output_fields(self.assignment)

        for field in self.input_fields:
            if not hasattr(self, field):
                setattr(self, field, None)  # zero initialization
        self.process()

    @property
    def work_fields(self):
        return self.input_fields + self.output_fields

    @property
    def is_completed(self):
        return all(getattr(self, i, None) for i in self.work_fields)

    def process(self, in_=True, out_=None):
        if not in_ and not out_:
            raise ValueError("at least one of in_ and out_ must be True")
        if in_:
            self._process_input()
        if out_:
            self._process_output(out_)

    def _process_input(self):
        for k in self.input_fields:
            try:
                valid_ = self._validate_field(
                    k,
                    getattr(self, k, None),
                    choices=self._get_field_attr(k, "choices", None),
                    keys=self._get_field_attr(k, "keys", None),
                    fix_=self.fix_input,
                    **self._get_field_attr(k, "validation_kwargs", {}),
                )
                if not valid_:
                    raise ValueError(f"failed to validate field {k}")
            except Exception as e:
                raise ValueError(f"failed to validate field {k}") from e

    def _process_output(self, out_: dict = None):
        for k, v in out_.items():
            try:
                valid_ = self._validate_field(
                    k,
                    v,
                    choices=self._get_field_attr(k, "choices", None),
                    keys=self._get_field_attr(k, "keys", None),
                    fix_=self.fix_output,
                    **self._get_field_attr(k, "validation_kwargs", {}),
                )
                if not valid_:
                    raise ValueError(f"failed to validate field {k}")
            except Exception as e:
                raise ValueError(f"failed to validate field {k}") from e

        if self.is_completed:
            self.filled = True

    def _validate_field(self, k, v, choices=None, keys=None, fix_=False, **kwargs):
        annotation = self.field_annotations[k]

        if choices:
            if choices and not isinstance(choices, list):
                try:
                    choices = [i.value for i in choices]
                except Exception as e:
                    raise ValueError(f"failed to get choices for field {k}") from e
            v_ = validation_funcs["enum"](v, choices=choices, fix_=fix_, **kwargs)
            if v_ not in choices:
                raise ValueError(f"{v} is not in chocies {choices}")
            setattr(self, k, v_)
            return True

        if any("actionrequest" in i for i in annotation):
            self.__setattr__(k, validation_funcs["action"](v))
            return True

        if "bool" in annotation and "str" not in annotation:
            self.__setattr__(k, validation_funcs["bool"](v, fix_=fix_, **kwargs))
            return True

        if (
            any([i in annotation for i in ["int", "float", "number"]])
            and "str" not in annotation
        ):
            if "float" in annotation:
                kwargs["num_type"] = float
                if "precision" not in kwargs:
                    kwargs["precision"] = 10

            self.__setattr__(k, validation_funcs["number"](v, fix_=fix_, **kwargs))
            return True

        if "dict" in annotation:
            if "str" not in annotation or keys:
                v_ = validation_funcs["dict"](v, keys=keys, fix_=fix_, **kwargs)
                setattr(self, k, v_)
                return True

        if "str" in annotation:
            self.__setattr__(k, validation_funcs["str"](v, fix_=fix_, **kwargs))
            return True

        return False


# from enum import Enum
# from pydantic import Field

# class EXAMPLES(str, Enum):
#     EXAMPLE1 = "example1"
#     EXAMPLE2 = "example2"
#     EXAMPLE3 = "example3"

# class Form1(Form):
#     a: str | EXAMPLES = Field("example3", choices=EXAMPLES)
#     b: str = "input2"
#     c: str|None = Field(None, choices=["output1", "output2"])
#     d: float | None = Field(None, json_schema_extra={"validation_kwargs":{"num_type": float, "precision": 2}})
#     assignment: str='a, b -> c, d'
#     form_name: str='custom_form'
#     description: str='Test Form'

# form = Form1()
# form.process(out_ = {"c": "output1", "d": "1.1"})


"""
usage pattern:

from pydantic import Field

class JokeForm(Form):

    # 1. use the key kwarg to specify the key in the dict,
    # which will be used validate the field value
    
    # 2. use fix flag to indicate whether to fix the value 
    # if it is not valid, default to True

    material: dict = Field(
        ..., 
        description="materials to read", 
        keys=["title", "author", "year"],       # use fix flag for dict key can be dangerous
        fix=True                                # the validator will force the value to match
    )                                           # the provided keys, which might pollute the data


    # 3. use choices to specify the available options
    topic: str = Field(
        ..., 
        description="topic to write joke on", 
        choices=["animal", "food", "people"],
    )
    
    # you can also use enum or subclass of Enum to explicitly 
    # declare that the field requires
    # the value to be one of the provided choices
    
    from enum import Enum
    
    class TOPIC(str, Enum):
        ANIMAL = "animal"
        FOOD = "food"
        PEOPLE = "people"
    
    
    topic: TOPIC = Field(
        default = TOPIC.ANIMAL, 
        description="topic to write joke on", 
        choices=TOPIC,
        fix_input=True,                             # fix_input and fix_output are used to indicate
    )                                               # whether to fix invalid values, default to True
    
    # 4. using optional fields
    # you can add None in type annotation to indicate that the field is optional
    # otherwise there will be a validation error if there is no default
    # field value, and value is also not provided at initialization

    joke: str | None = Field(
        default = None, 
        description = "joke to write"
    )
    
    # 5. using validation_kwargs
    # you can use validation_kwargs to specify the validation parameters
    # there are built-in validators for numbers

    rating: float | None = Field(
        default=None, 
        description="rating, a numerical value", 
        validation_kwargs={
            "upper_bound": 10,                      # this will ensure the value for this field 
            "lower_bound": 0,                       # is a number between upper_bound and lower_bound
            "num_type": "float",                    
            "precision": 2}                         
        )
    
    
    # 6. using assignment
    # in a form, you do not need to specify the input output for each field, 
    # instead, you can use assignment to specify the input and output of the form
    
    # an assignment is a string that describes the input and output of the form
    # it is used to generate a composable set of fields specific to different context
    # from the same custom Form class you specified, for example, two fields "a" and "b",  
    # in one form, you can specify the assignment as "a -> b", and in another form,
    # you can specify the assignment as "b -> a", and the system will generate two different
    # instruction for the worker to perform. 
    
    assignment: str = Field(
        default=..., 
        examples=["input1, input2 -> output"]
    )
    
    # for example, "work -> review, rating" and "review, work -> rating" have completely different
    # meanings, and the system will generate two different forms for the worker to complete
    # the former means, a review task of a work performance with corresponding rating for the work
    # the latter means, a rating task on the review quality for the work performance, 
    # such as, whether it is fair for the rating
    

    # 7. general guidance
    # in principle, a field should only be filled once, thus making form a single use object
    # the form has a filled flag to indicate whether the form is completed
    
"""

# import unittest
# from pydantic import Field


# class Form1(Form):
#     input1: str = Field("input1", json_schema_extra={"choices": ["option1", "option2"]})
#     input2: str = "input2"
#     output1: str = "output1"


# class TestForm(unittest.TestCase):

#     def test_default_initialization(self):
#         class Form1(Form):
#             input1: str = "input1"
#             input2: str = "input2"
#             output1: str = "output1"
#             assignment: str = "input1, input2 -> output1"

#         form = Form1()
#         self.assertEqual(form.form_name, "default_form")
#         self.assertIsNone(form.description)
#         self.assertEqual(form.input_fields, ["input1", "input2"])
#         self.assertEqual(form.output_fields, ["output1"])
#         self.assertFalse(form.filled)
#         self.assertFalse(form.fix_input)
#         self.assertTrue(form.fix_output)

#     def test_custom_initialization(self):
#         class Form1(Form):
#             a: str = "input1"
#             b: str = "input2"
#             c: str = "output1"
#             assignment: str = "a, b -> c"
#             form_name: str = "custom_form"
#             description: str = "Test Form"

#         form = Form1()
#         self.assertEqual(form.form_name, "custom_form")
#         self.assertEqual(form.description, "Test Form")
#         self.assertEqual(form.input_fields, ["a", "b"])
#         self.assertEqual(form.output_fields, ["c"])

#     def test_process_inputs_outputs(self):
#         class Form1(Form):
#             input1: str = Field(
#                 "option1", json_schema_extra={"choices": ["option1", "option2"]}
#             )
#             input2: str = "input2"
#             output1: str = "output1"
#             assignment: str = "input1, input2 -> output1"

#         form = Form1()
#         setattr(form, "input1", "option1")
#         # Test processing valid input
#         form.process(in_=True)  # Should process without error

#         setattr(form, "input1", "option3")
#         # Test processing invalid input
#         with self.assertRaises(ValueError):
#             form.process(in_=True)  # Should raise error

#     def test_check_complete(self):
#         class Form1(Form):
#             input1: str = "input1"
#             input2: str = "input2"
#             output1: str = "output1"

#         form = Form1(assignment="input1, input2 -> output1")
#         setattr(form, "input1", "value1")
#         setattr(form, "input2", "value2")
#         setattr(form, "output1", "result1")
#         self.assertTrue(form.is_completed)

#     def test_input_output_fields_parsing(self):
#         class Form1(Form):
#             x: str = "input1"
#             y: str = "input2"
#             z: str = "output1"

#         form = Form1(assignment="x, y -> z")
#         self.assertEqual(form.input_fields, ["x", "y"])
#         self.assertEqual(form.output_fields, ["z"])

#     def test_validation_failure(self):
#         class Form1(Form):
#             input1: str = Field(
#                 None, json_schema_extra={"choices": ["option1", "option2"]}
#             )
#             input2: str = "input2"
#             output1: str = "output1"
#             assignment: str = "input1, input2 -> output1"

#         # Test handling invalid input choice
#         with self.assertRaises(ValueError):
#             form = Form1()

#     def test_output_assignment(self):
#         class Form1(Form):
#             input1: str = "value1"
#             output1: str = Field("output1", choices=["result1", "result2"])
#             assignment: str = "input1 -> output1"

#         form = Form1()
#         # Test handling invalid output choice
#         try:
#             form.process(out_={"output1": "result3"})
#         except ValueError:
#             pass

#         form.process(out_={"output1": "result3"})  # Should process without error


# if __name__ == "__main__":
#     unittest.main()
