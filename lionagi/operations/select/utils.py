# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import inspect
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, Field, JsonValue

from lionagi.ln import is_same_dtype
from lionagi.ln.fuzzy import string_similarity


class SelectionModel(BaseModel):
    """Model representing the selection output."""

    PROMPT: ClassVar[str] = (
        "Please select up to {max_num_selections} items from the following list {choices}. Provide the selection(s) into appropriate field in format required, and no comments from you"
    )

    selected: list[Any] = Field(default_factory=list)


def parse_to_representation(
    choices: Enum | dict | list | tuple | set,
) -> tuple[list[str], JsonValue]:
    """
    should use
    1. iterator of string | BaseModel
    2. dict[str, JsonValue | BaseModel]
    3. Enum[str, JsonValue | BaseModel]
    """

    if isinstance(choices, tuple | set | list):
        choices = list(choices)
        if is_same_dtype(choices, str):
            return choices, choices

    if isinstance(choices, list):
        if is_same_dtype(choices, BaseModel):
            choices = {i.__class__.__name__: i for i in choices}
        if all(inspect.isclass(i) and issubclass(i, BaseModel) for i in choices):
            choices = {i.__name__: i for i in choices}
    if isinstance(choices, type) and issubclass(choices, Enum):
        keys = [i.name for i in choices]
        contents = [get_choice_representation(i) for i in choices]
        return keys, contents

    if isinstance(choices, dict):
        keys = list(choices.keys())
        contents = list(choices.values())
        contents = [get_choice_representation(v) for k, v in choices.items()]
        return keys, contents

    if isinstance(choices, tuple | set | list):
        choices = list(choices)
        if is_same_dtype(choices, str):
            return choices, choices

    raise TypeError(
        f"Unsupported choices type: {type(choices).__name__!r}. "
        "Expected list/tuple/set of str or BaseModel, dict, or Enum subclass."
    )


def get_choice_representation(choice: Any) -> str:
    if isinstance(choice, str):
        return choice

    if isinstance(choice, BaseModel):
        from lionagi.ln import json_dumps

        schema = choice.model_json_schema()
        return f"{choice.__class__.__name__}:\n{json_dumps(schema, pretty=True)}"

    if isinstance(choice, Enum):
        return get_choice_representation(choice.value)

    return str(choice)


def parse_selection(selection_str: str, choices: Any):
    select_from = []

    if isinstance(choices, dict):
        select_from = list(choices.keys())

    if inspect.isclass(choices) and issubclass(choices, Enum):
        select_from = [choice.name for choice in choices]

    if isinstance(choices, list | tuple | set):
        if is_same_dtype(choices, BaseModel):
            select_from = [i.__class__.__name__ for i in choices]
        if is_same_dtype(choices, str):
            select_from = list(choices)
        if all(inspect.isclass(i) and issubclass(i, BaseModel) for i in choices):
            select_from = [i.__name__ for i in choices]

    if not select_from:
        raise ValueError("The values provided for choice is not valid")

    selected = string_similarity(selection_str, select_from, return_most_similar=True)

    if isinstance(choices, dict) and selected in choices:
        return choices[selected]

    if inspect.isclass(choices) and issubclass(choices, Enum):
        for i in choices:
            if i.name == selected:
                return i

    if isinstance(choices, list) and is_same_dtype(choices, str):
        if selected in choices:
            return selected

    return selected
