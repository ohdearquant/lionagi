from lionagi.core.generic import BaseComponent
from pydantic import Field
from typing import Type, Any, Optional
from lionagi.core.form.field_validator import validation_funcs
from lionagi.libs import convert, func_call


class FormField(BaseComponent):
    form_id: Optional[str] = Field(None, description="The id of the form associated.")

    field_name: str | None = Field(None, description="The name of the field, required.")

    annotation: str | None = Field(
        None, description="The type annotation of the field, optional."
    )
    
    description: str = Field(default="", description="The description of the field")

    instruction: str = Field(
        default="", description="The optional instruction for handling this field."
    )

    content: Any = Field(
        None, description="The content of the field, optional."
    )

    keys: list | dict | None = Field(
        None, description="The dict keys of the field, optional."
    )

    choices: list | dict | None = Field(
        None, description="The choices of the field, optional."
    )

    validation_kwargs: dict | None = Field(
        None, description="The validation kwargs of the field, optional."
    )

    fix_validation: bool = Field(
        default=True, 
        description="Flag indicating whether to attempt fixing the value if it's invalid (default: True)."
    )
    
    examples: Any = Field(
        None, description="Examples of the field, optional."
    )

    @property
    def content_dict(self):
        return {self.field_name: self.content}

    @property
    def has_keys(self):
        return self.keys is not None

    @property
    def has_choices(self):
        return self.choices is not None

    def validate(self):
        content = self.content
        if self.has_keys:
            self.content = validation_funcs["dict"](
                content,
                keys=self.keys,
                fix_=self.fix_validation,
                **self.validation_kwargs,
            )
            return True

        if self.has_choices:
            content = validation_funcs["enum"](
                content,
                choices=self.choices,
                fix_=self.fix_validation,
                **self.validation_kwargs,
            )
            if content not in self.choices:
                raise ValueError(f"{content} is not in chocies {self.choices}")
            self.content = content
            return True

        str_ = str(self.annotation)

        if "lionagi.core.form.action_form.actionrequest" in str_:
            self.content = validation_funcs["action"](self.content)
            return True

        if "bool" in str_ and "str" not in str_:
            self.content = validation_funcs["bool"](
                self.content, fix_=self.fix_validation, **self.validation_kwargs
            )

        if any(i in str_ for i in ["int", "float", "number"]) and "str" not in str_:
            self.content = validation_funcs["number"](
                self.content, fix_=self.fix_validation, **self.validation_kwargs
            )
            return True

        if "str" in str_:
            self.content = validation_funcs["str"](
                self.content, fix_=self.fix_validation, **self.validation_kwargs
            )
            return True

        return False


import unittest
from pydantic import ValidationError


class TestFormField(unittest.TestCase):

    def test_initialization_with_valid_data(self):
        form_field = FormField(
            field_name="username",
            field_type=str,
            description="User's username",
            instruction="Must be unique",
            form_id="form123",
        )
        self.assertEqual(form_field.field_name, "username")
        self.assertEqual(form_field.field_type, str)
        self.assertEqual(form_field.description, "User's username")
        self.assertEqual(form_field.instruction, "Must be unique")
        self.assertEqual(form_field.form_id, "form123")

    def test_initialization_with_defaults(self):
        form_field = FormField(field_name="email", field_type=str)
        self.assertEqual(form_field.description, "")
        self.assertEqual(form_field.instruction, "")
        self.assertIsNone(form_field.form_id)

    def test_validation_errors(self):
        # This should correctly raise a ValidationError because 'field_name' is required and missing
        with self.assertRaises(ValidationError):
            FormField(field_type=str)

    def test_type_validation_property(self):
        form_field = FormField(field_name="age", field_type=int, content=25)
        self.assertTrue(form_field.is_validate_type)

        form_field.content = "twenty-five"
        self.assertFalse(form_field.is_validate_type)

    def test_content_dict_property(self):
        form_field = FormField(
            field_name="password", field_type=str, content="pass1234"
        )
        expected_dict = {"password": "pass1234"}
        self.assertEqual(form_field.content_dict, expected_dict)


if __name__ == "__main__":
    unittest.main()
