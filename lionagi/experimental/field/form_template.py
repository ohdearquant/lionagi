from typing import Any
from pydantic import Field

from lionagi.core.generic import BaseComponent
from .util import get_input_output_fields


class FormTemplate(BaseComponent):
    
    title: str | None = Field(
        default=None,
        description="The title of the prompt template.",
        examples=["multiplication", "addition"]
    )
    
    task: str = Field(
        default_factory=str,  
        description="The task of the prompt template.", 
        examples=["add two numbers", "find multiplication product of two numbers"]
    )
    
    template_name: str = Field(
        title="template_name",
        default="default_form",
        description="The name of the prompt template.",
    )
    
    assignments: str = Field(
        title="assignments",
        default="null", 
        description="signature indicating inputs, outputs", 
        validation_alias="signature",
        examples=["a, b -> c, d"]
    )
    
    version: str | float | int | None = Field(
        title="version",
        default=None, 
        description="The version of the prompt template.",
        examples=[1, 1.0, "1.0"]
    )
    
    description: str | dict[str, Any] | None | Any = Field(
        title="description",
        default=None, 
        description="The description of the prompt template."
    )
    
    input_fields: list[str] = Field(
        title="input_fields",
        default_factory=list, 
        description="Extracted input fields from the signature.", 
        validation_alias="inputs"
    )
    
    output_fields: list[str] = Field(
        title="output_fields",
        default_factory=list, 
        description="Extracted output fields from the signature.", 
        validation_alias="outputs"
    )
    
    fix_input: bool = Field(True, description="a flag indicationg whether to fix input")
    fix_output: bool = Field(True, description="a flag indicating whether to fix output")
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.input_fields, self.output_fields = get_input_output_fields(self.assignments)
        self.process(in_=True)

    @property
    def prompt_fields(self):
        return self.input_fields + self.output_fields
    
    @property
    def inputs(self):
        return {i: getattr(self, i) for i in self.input_fields}

    @property
    def outputs(self):
        return {i: getattr(self, i) for i in self.output_fields}

    def validate(self):
        """
        out_ needs to be a dictionary of the form {field_name: FormField}
        """
        dict_ = {**self.inputs, **self.outputs}
        for k, v in dict_.items():
            try:
                v.validate()
            except Exception as e:
                raise ValueError(f"failed to validate field {k}") from e
