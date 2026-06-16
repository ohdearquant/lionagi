# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import inspect
from typing import Any, Literal

from pydantic import Field, field_validator

from lionagi.libs.schema.extract_docstring import extract_docstring
from lionagi.libs.validate.common_field_validators import validate_model_to_type
from lionagi.models import SchemaModel

py_json_msp = {
    "str": "string",
    "int": "number",
    "float": "number",
    "list": "array",
    "tuple": "array",
    "bool": "boolean",
    "dict": "object",
}


class FunctionSchema(SchemaModel):
    name: str
    description: str | None = Field(
        None,
        description=(
            "A description of what the function does, used by the "
            "model to choose when and how to call the function."
        ),
    )
    parameters: dict[str, Any] | None = Field(
        None,
        description=(
            "The parameters the functions accepts, described as a JSON Schema object. "
            "See the guide (https://platform.openai.com/docs/guides/function-calling) "
            "for examples, and the JSON Schema reference for documentation about the "
            "format. Omitting parameters defines a function with an empty parameter list."
        ),
        validation_alias="request_options",
    )
    strict: bool | None = Field(
        None,
        description=(
            "Whether to enable strict schema adherence when generating the function call. "
            "If set to true, the model will follow the exact schema defined in the parameters "
            "field. Only a subset of JSON Schema is supported when strict is true."
        ),
    )

    @field_validator("parameters", mode="before")
    def _validate_parameters(cls, v):
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        try:
            model_type = validate_model_to_type(cls, v)
            return model_type.model_json_schema()
        except Exception:
            raise ValueError(f"Invalid model type: {v}") from None

    def to_dict(self):
        dict_ = super().to_dict()
        return {"type": "function", "function": dict_}


def function_to_schema(
    f_,
    style: Literal["google", "rest"] = "google",
    *,
    request_options: dict[str, Any] | None = None,
    strict: bool = None,
    func_description: str = None,
    parametert_description: dict[str, str] = None,
    return_obj: bool = False,
) -> dict:
    """Generate an OpenAI-format function schema from type hints and docstrings.

    Args:
        func (Callable): The function to generate a schema for.
        style (str): The docstring format. Can be 'google' (default) or
            'reST'.
        func_description (str, optional): A custom description for the
            function. If not provided, the description will be extracted
            from the function's docstring.
        params_description (dict, optional): A dictionary mapping
            parameter names to their descriptions. If not provided, the
            parameter descriptions will be extracted from the function's
            docstring.

    Returns:
        dict: A schema describing the function, including its name,
        description, and parameter details.

    Example:
        >>> def example_func(param1: int, param2: str) -> bool:
        ...     '''Example function.
        ...
        ...     Args:
        ...         param1 (int): The first parameter.
        ...         param2 (str): The second parameter.
        ...     '''
        ...     return True
        >>> schema = function_to_schema(example_func)
        >>> schema['function']['name']
        'example_func'
    """
    func_name = f_.__name__

    if not func_description or not parametert_description:
        func_desc, params_desc = extract_docstring(f_, style)
        func_description = func_description or func_desc
        parametert_description = parametert_description or params_desc

    sig = inspect.signature(f_)
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    if not request_options:
        for name, param in sig.parameters.items():
            param_type = "string"
            if param.annotation is not inspect.Parameter.empty:
                param_type = py_json_msp.get(param.annotation.__name__, param.annotation.__name__)
            param_description = parametert_description.get(name)

            # Assuming all parameters are required for simplicity
            parameters["required"].append(name)
            parameters["properties"][name] = {
                "type": param_type,
                "description": param_description,
            }
    else:
        parameters = request_options

    params = {
        "name": func_name,
        "description": func_description,
        "parameters": parameters,
    }
    if strict:
        params["strict"] = strict

    if return_obj:
        return FunctionSchema(**params)
    return FunctionSchema(**params).to_dict()
