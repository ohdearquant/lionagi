from typing import Any
from pydantic import Field

from lionagi.libs import convert, func_call

from lionagi.core.generic import Component
from lionagi.core.form.field_validator import validation_funcs


non_prompt_words = [
    "id_",
    "node_id",
    "meta",
    "metadata",
    "timestamp",
    "content",
    "signature",
    "task",
    "template_name",
    "version",
    "description",
    "in_validation_kwargs",
    "out_validation_kwargs",
    "fix_input",
    "fix_output",
    "input_fields",
    "output_fields",
    "choices",
    "prompt_fields",
    "prompt_fields_annotation",
    "instruction_context",
    "instruction",
    "instruction_output_fields",
    "inputs",
    "outputs",
    "process",
    "_validate_field",
    "_process_input",
    "_process_response",
    "_validate_field_choices",
    "_validate_input_choices",
    "_validate_output_choices",
]


class Form(Component):
    template_name: str = Field(
        default="default_form",
        description="The name of the prompt template.",
    )
    signature: str = Field("null", description="signature indicating inputs, outputs")
    version: str | float | int | None = Field(
        default=None, description="The version of the prompt template."
    )
    description: str | dict[str, Any] | None | Any = Field(
        default=None, description="The description of the prompt template."
    )
    task: str | dict[str, Any] | None = Field(
        default=None, description="The task associated with the prompt template."
    )
    out_validation_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="validation kwargs for output"
    )
    in_validation_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="validation kwargs for input"
    )
    fix_input: bool = Field(True, description="whether to fix input")
    fix_output: bool = Field(True, description="whether to fix output")
    input_fields: list[str] = Field(
        default_factory=list, description="Extracted input fields from the signature."
    )
    output_fields: list[str] = Field(
        default_factory=list, description="Extracted output fields from the signature."
    )
    choices: dict[str, list[str]] = Field(
        default_factory=dict, description="Choices available for each template field."
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.input_fields, self.output_fields = self._get_input_output_fields(
            self.signature
        )
        self.process(in_=True)

    @property
    def prompt_fields(self):
        return self.input_fields + self.output_fields

    @property
    def instruction_context(self):
        a = "".join(
            f"""
        ## input: {i}:
        - description: {self.model_fields[i].description}
        - value: {str(self.__getattribute__(self.input_fields[idx]))}
        """
            for idx, i in enumerate(self.input_fields)
        )
        return a.replace("        ", "")

    @property
    def instruction(self):
        ccc = f"""
        0. Your task is {self.task},
        1. provided: {self.input_fields}, 
        2. requested: {self.output_fields}
        ----------
        """
        return ccc.replace("        ", "")

    @property
    def instruction_output_fields(self):
        return {i: self.model_fields[i].description for i in self.output_fields}

    @property
    def inputs(self):
        return {i: getattr(self, i) for i in self.input_fields}

    @property
    def outputs(self):
        return {i: getattr(self, i) for i in self.output_fields}

    def process(self, in_=None, out_=None):
        if in_:
            self._process_input()
            self._validate_input_choices()
        if out_:
            self._process_response(out_)
            self._validate_output_choices()
        return self

    def _validate_field_choices(self, fields, fix_: bool = False):
        if len(self.choices) >= 1:
            for k, choices in self.choices.items():
                if k in fields and not self._validate_field(
                    k, getattr(self, k), choices, fix_
                ):
                    raise ValueError(
                        f"Invalid choice for field {k}: {getattr(self, k)} is not in {choices}"
                    )

    def _validate_input_choices(self):
        return self._validate_field_choices(self.input_fields, self.fix_input)

    def _validate_output_choices(self):
        return self._validate_field_choices(self.output_fields, self.fix_output)

    def _validate_field(self, k, v, choices=None, keys=None, fix_=False, **kwargs):

        str_ = self._prompt_fields_annotation[k]

        if keys:
            v_ = validation_funcs["dict"](v, keys=keys, fix_=fix_, **kwargs)
            setattr(self, k, v_)
            return True

        if choices:
            v_ = validation_funcs["enum"](v, choices=choices, fix_=fix_, **kwargs)
            if v_ not in choices:
                raise ValueError(f"{v} is not in chocies {choices}")
            setattr(self, k, v_)
            return True

        if "lionagi.core.form.action_form.actionrequest" in str_:
            self.__setattr__(k, validation_funcs["action"](v))
            return True

        if "bool" in str_ and "str" not in str_:
            self.__setattr__(k, validation_funcs["bool"](v, fix_=fix_, **kwargs))
            return True

        if any(i in str_ for i in ["int", "float", "number"]) and "str" not in str_:
            self.__setattr__(k, validation_funcs["number"](v, fix_=fix_, **kwargs))
            return True

        elif "str" in str_:
            self.__setattr__(k, validation_funcs["str"](v, fix_=fix_, **kwargs))
            return True

        return False

    def _process_input(self, fix_=False):
        kwargs = self.in_validation_kwargs.copy()
        for k, v in self.inputs.items():
            if k not in kwargs:
                kwargs = {k: {}}

            if self._field_has_choices(k):
                self.choices[k] = self.model_fields[k].json_schema_extra["choices"]
                if self._validate_field(
                    k, v, choices=self.choices[k], fix_=fix_, **kwargs[k]
                ):
                    continue
                else:
                    raise ValueError(f"{k} has no choices")

            elif self._validate_field(k, v, fix_=fix_, **kwargs[k]):
                continue
            else:
                raise ValueError(f"failed to validate field {k}")

    def _get_field_attr(self, k, attr) -> Any:
        if not self._field_has_attr(k, attr):
            raise ValueError(f"field {k} has no attribute {attr}")
        field = self.model_fields[k]
        a = getattr(field, attr)
        if not a:
            try:
                a = field.json_schema_extra[attr]
                return a
            except Exception:
                return None
        return a

    def _field_has_attr(self, k, attr):
        a = False

        field = self.model_fields[k]
        a = hasattr(field, attr)

        if not a:
            try:
                a = (
                    self.model_fields[k].json_schema_extra[attr] is not None
                    and attr in self.model_fields[k].json_schema_extra
                )
                return a if isinstance(a, bool) else False
            except Exception:
                return False

    def _field_has_keys(self, k):
        return self._field_has_attr(k, "keys")

    def _field_has_choices(self, k):
        return self._field_has_attr(k, "choices")

    def _process_choices(self, k, v, fix_=False, kwargs=None):
        choices = self._get_field_attr(k, "choices")

        if self._validate_field(k, v, choices=choices, fix_=fix_, **kwargs):
            self.choices[k] = choices
            return True
        else:
            raise ValueError(f"{k} has no choices")

    def _process_keys(self, k, v, fix_=False, kwargs=None):
        keys = self._get_field_attr(k, "keys")
        if self._validate_field(k, v, keys=keys, fix_=fix_, **kwargs):
            return True
        else:
            raise ValueError(f"{k} has no keys")

    def _process_response(self, out_, fix_=fix_output):
        kwargs = self.out_validation_kwargs.copy()
        for k, v in out_.items():
            if k not in kwargs:
                kwargs = {k: {}}

            if self._field_has_choices(k):
                try:
                    return self._process_choices(k, v, fix_=fix_, kwargs=kwargs[k])
                except Exception as e:
                    raise ValueError(
                        f"failed to process field {k} with value {v}"
                    ) from e

            elif self._field_has_keys(k):
                try:
                    return self._process_keys(k, v, fix_=fix_, kwargs=kwargs[k])
                except Exception as e:
                    raise ValueError(
                        f"failed to process field {k} with value {v}"
                    ) from e

            elif self._validate_field(k, v, fix_=fix_, **kwargs[k]):
                continue

            else:
                raise ValueError(f"failed to validate field {k} with value {v}")

    @staticmethod
    def _get_input_output_fields(str_: str) -> Any:
        inputs, outputs = str_.split("->")

        input_fields = [convert.strip_lower(i) for i in inputs.split(",")]
        output_fields = [convert.strip_lower(o) for o in outputs.split(",")]

        return input_fields, output_fields

    @property
    def _prompt_fields_annotation(self):
        dict_ = {i: self.model_fields[i].annotation for i in self.prompt_fields}
        for k, v in dict_.items():
            if "|" in str(v):
                v = str(v)
                v = v.split("|")
                dict_[k] = func_call.lcall(v, convert.strip_lower)
            else:
                dict_[k] = [v.__name__]

        return dict_
