from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, JsonValue, field_validator

if TYPE_CHECKING:
    from lionagi.models.field_model import FieldModel

from lionagi.ln import extract_json, to_dict, to_list
from lionagi.ln.types import Unset, not_sentinel
from lionagi.models import HashableModel

logger = logging.getLogger(__name__)

_DEFAULT_FIELDS = {}


class Instruct(HashableModel):
    """Model for defining instruction parameters and execution requirements."""

    instruction: str | None = Field(
        None,
        description=(
            "A clear, actionable task definition. Specify:\n"
            "1) The primary goal or objective\n"
            "2) Key success criteria or constraints\n"
            "\n"
            "Guidelines:\n"
            "- Start with a direct action verb (e.g., 'Analyze', 'Generate', 'Create')\n"
            "- Include scope, boundaries, or constraints\n"
            "- Provide success criteria if relevant\n"
            "- For complex tasks, break them into logical steps"
        ),
    )

    guidance: JsonValue | None = Field(
        None,
        description=(
            "Strategic direction and constraints for executing the task. "
            "Include:\n"
            "1) Preferred methods or frameworks\n"
            "2) Quality benchmarks (e.g., speed, clarity)\n"
            "3) Resource or environmental constraints\n"
            "4) Relevant compliance or standards\n"
            "Use None if no special guidance."
        ),
    )

    context: JsonValue | None = Field(
        None,
        description=(
            "Background information and current-state data needed for the task. "
            "Should be:\n"
            "1) Directly relevant\n"
            "2) Sufficient to perform the task\n"
            "3) Free of extraneous detail\n"
            "Include environment, prior outcomes, system states, or dependencies. "
            "Use None if no additional context is needed."
        ),
    )

    reason: bool | None = Field(
        None,
        description=(
            "Include a thoughtful explanation of decisions, trade-offs, "
            "and insights. Encourage deeper introspection on why certain "
            "choices were made, potential alternatives, and how confidence "
            "was shaped. If not needed, set to None."
        ),
    )
    actions: bool | None = Field(
        None,
        description=(
            "Controls execution mode. "
            "True: Execute specified actions. "
            "False: Analysis/recommendations only. "
            "None: Contextual execution."
        ),
    )

    action_strategy: Literal["sequential", "concurrent"] | None = Field(
        None,
        description="Action strategy to use for executing actions. Default "
        "is 'concurrent'. Only provide for if actions are enabled.",
    )

    @field_validator("instruction", "guidance", "context", mode="before")
    def _validate_instruction(cls, v):
        from lionagi.libs.validate.common_field_validators import (
            validate_nullable_jsonvalue_field,
        )

        return validate_nullable_jsonvalue_field(cls, v)

    @field_validator("reason", "actions", mode="before")
    def _validate_reason(cls, v):
        from lionagi.libs.validate.common_field_validators import validate_boolean_field

        return validate_boolean_field(cls, v)

    @field_validator("action_strategy", mode="before")
    def _validate_action_strategy(cls, v):
        if v not in ["batch", "sequential", "concurrent"]:
            return "concurrent"
        return v

    @classmethod
    def handle(
        cls,
        instruct: Instruct | dict | None = None,
        instruction: str | None = None,
        guidance: JsonValue | None = None,
        context: JsonValue | None = None,
        reason: bool | None = None,
    ):
        if instruct is None:
            instruct = {}
        elif isinstance(instruct, Instruct):
            instruct = instruct.to_dict()

        overrides = {
            "instruction": instruction,
            "guidance": guidance,
            "context": context,
            "reason": reason,
        }
        instruct.update(
            {
                k: v
                for k, v in overrides.items()
                if not_sentinel(v, none_as_sentinel=True, empty_as_sentinel=True)
            }
        )
        return cls.from_dict(instruct)


class Reason(HashableModel):
    title: str | None = None
    content: str | None = None
    confidence_score: float | None = Field(
        None,
        title="Confidence Score",
        description=(
            "Numeric confidence score (0.0 to 1.0, up to three decimals) indicating "
            "how well you've met user expectations. Use this guide:\n"
            "  • 1.0: Highly confident\n"
            "  • 0.8-1.0: Reasonably sure\n"
            "  • 0.5-0.8: Re-check, refine or backtrack\n"
            "  • 0.0-0.5: Off track, stop"
        ),
    )

    @field_validator("confidence_score", mode="before")
    def _validate_confidence(cls, v):
        if v is None:
            return None
        try:
            from lionagi.libs.validate.to_num import to_num

            return to_num(
                v,
                upper_bound=1,
                lower_bound=0,
                num_type=float,
                precision=3,
            )
        except Exception:
            return -1


class ActionRequestModel(HashableModel):
    """
    Captures a single action request, typically from a user or system message.
    Includes the name of the function and the arguments to be passed.
    """

    function: str | None = Field(
        None,
        title="Function",
        description=(
            "Name of the function to call from the provided `tool_schemas`. "
            "If no `tool_schemas` exist, set to None or leave blank. "
            "Never invent new function names outside what's given."
        ),
        examples=["multiply", "create_user"],
    )
    arguments: dict[str, Any] | None = Field(
        None,
        title="Arguments",
        description=(
            "Dictionary of arguments for the chosen function. "
            "Use only argument names/types defined in `tool_schemas`. "
            "Never introduce extra argument names."
        ),
    )

    @field_validator("arguments", mode="before")
    def validate_arguments(cls, value: Any) -> dict[str, Any]:
        """
        Coerce arguments into a dictionary if possible, recursively.

        Raises:
            ValueError if the data can't be coerced.
        """
        return to_dict(
            value,
            fuzzy_parse=True,
            recursive=True,
            recursive_python_only=False,
        )

    @field_validator("function", mode="before")
    def validate_function(cls, value: str) -> str:
        """
        Ensure the function name is a valid non-empty string (if provided).
        """
        from lionagi.libs.validate.common_field_validators import (
            validate_nullable_string_field,
        )

        return validate_nullable_string_field(cls, value, "function", False)

    @classmethod
    def create(cls, content: str):
        """
        Attempt to parse a string (usually from a conversation or JSON) into
        one or more ActionRequestModel instances.

        If no valid structure is found, returns an empty list.
        """

        def parse_action_request(content: str | dict) -> list[dict]:
            json_blocks = []

            if isinstance(content, BaseModel):
                json_blocks = [content.model_dump()]

            elif isinstance(content, str):
                json_blocks = extract_json(content, fuzzy_parse=True)
                if not json_blocks:
                    pattern2 = r"```python\s*(.*?)\s*```"
                    _d = re.findall(pattern2, content, re.DOTALL)
                    json_blocks = [extract_json(match, fuzzy_parse=True) for match in _d]
                    json_blocks = to_list(json_blocks, dropna=True)

                logger.debug("Parsed action request JSON blocks: %s", json_blocks)

            elif content and isinstance(content, dict):
                json_blocks = [content]

            if json_blocks and not isinstance(json_blocks, list):
                json_blocks = [json_blocks]

            out = []

            for i in json_blocks:
                j = {}
                if isinstance(i, dict):
                    if "function" in i and isinstance(i["function"], dict):
                        if "name" in i["function"]:
                            i["function"] = i["function"]["name"]
                    for k, v in i.items():
                        k = k.replace("action_", "").replace("recipient_", "").replace("s", "")
                        if k in ["name", "function", "recipient"]:
                            j["function"] = v
                        elif k in ["parameter", "argument", "arg", "param"]:
                            j["arguments"] = to_dict(
                                v,
                                str_type="json",
                                fuzzy_parse=True,
                                suppress=True,
                            )
                    if j and all(key in j for key in ["function", "arguments"]) and j["arguments"]:
                        out.append(j)

            return out

        try:
            ctx = parse_action_request(content)
            if ctx:
                return [cls.model_validate(i) for i in ctx]
            return []
        except Exception:
            return []


class ActionResponseModel(HashableModel):
    """
    Encapsulates a function's output after being called. Typically
    references the original function name, arguments, and the result.
    """

    function: str = Field(default_factory=str, title="Function")
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: Any = None


def get_default_field(
    kind: Literal[
        "action_requests",
        "action_responses",
        "action_required",
        "instruct",
        "reason",
    ],
    default: Any = Unset,
    nullable: bool = True,
    listable: bool = None,
):
    global _DEFAULT_FIELDS
    key = (kind, str(default), nullable, listable)
    if key not in _DEFAULT_FIELDS:
        _DEFAULT_FIELDS[key] = _get_default_fields(
            kind, default=default, nullable=nullable, listable=listable
        )
    return _DEFAULT_FIELDS[key]


def _get_default_fields(
    kind: Literal[
        "action_requests",
        "action_responses",
        "action_required",
        "instruct",
        "reason",
    ],
    default: Any = Unset,
    nullable: bool = True,
    listable: bool = None,
):
    from lionagi.models.field_model import FieldModel

    fm = None

    match kind:
        case "instruct":
            fm = FieldModel(Instruct, name="instruct_model")

        case "action_required":
            from lionagi.libs.validate.common_field_validators import (
                validate_boolean_field,
            )

            fm = FieldModel(
                bool,
                name="action_required",
                validator=lambda cls, v: validate_boolean_field(cls, v, False),
                description=(
                    "Whether this step strictly requires performing actions. "
                    "If true, the requests in `action_requests` must be fulfilled, "
                    "assuming `tool_schemas` are available. "
                    "If false or no `tool_schemas` exist, actions are optional."
                ),
            )

        case "action_requests":
            fm = FieldModel(
                ActionRequestModel,
                name="action_requests",
                listable=True,
                description=(
                    "List of actions to be executed when `action_required` is true. "
                    "Each action must align with the available `tool_schemas`. "
                    "Leave empty if no actions are needed."
                ),
            )

        case "action_responses":
            fm = FieldModel(ActionResponseModel, name="action_responses", listable=True)

        case "reason":
            fm = FieldModel(Reason, name="reason")

        case _:
            raise ValueError(f"Unknown default field kind: {kind}")

    if listable is not None:
        if listable and not fm.is_listable:
            fm = fm.as_listable()
        else:
            fm = fm.with_metadata("listable", False)

    if nullable:
        fm = fm.as_nullable()
        default = None

    if fm.is_listable and default is Unset:
        default = list

    if default is not Unset:
        fm = fm.with_default(default)

    if fm.is_listable:
        fm = fm.with_validator(lambda cls, x: to_list(x, dropna=True, flatten=True, unique=True))

    return fm


# Lazily-generated field constants
_FIELD_CONSTANTS = {
    "ACTION_REQUESTS_FIELD": ("action_requests", {}),
    "ACTION_RESPONSES_FIELD": ("action_responses", {}),
    "ACTION_REQUIRED_FIELD": ("action_required", {}),
    "INSTRUCT_FIELD": ("instruct", {}),
    "LIST_INSTRUCT_FIELD_MODEL": ("instruct", {"listable": True}),
    "REASON_FIELD": ("reason", {}),
}


def __getattr__(name: str):
    if name in _FIELD_CONSTANTS:
        field_name, kwargs = _FIELD_CONSTANTS[name]
        return get_default_field(field_name, **kwargs)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


# Stub assignments so IDEs and static-analysis tools (ruff F822, mypy) can
# resolve these names even though they are generated at runtime by __getattr__.
if TYPE_CHECKING:
    ACTION_REQUESTS_FIELD: FieldModel
    ACTION_RESPONSES_FIELD: FieldModel
    ACTION_REQUIRED_FIELD: FieldModel
    INSTRUCT_FIELD: FieldModel
    LIST_INSTRUCT_FIELD_MODEL: FieldModel
    REASON_FIELD: FieldModel


__all__ = (
    "ACTION_REQUESTS_FIELD",
    "ACTION_RESPONSES_FIELD",
    "ACTION_REQUIRED_FIELD",
    "INSTRUCT_FIELD",
    "LIST_INSTRUCT_FIELD_MODEL",
    "REASON_FIELD",
    "ActionRequestModel",
    "ActionResponseModel",
    "Instruct",
    "Reason",
    "get_default_field",
)
