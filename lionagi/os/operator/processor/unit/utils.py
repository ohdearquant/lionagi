from functools import singledispatch
import re
from lion_core.setting import LN_UNDEFINED
from typing import Any, Callable, Literal
from lion_core.action.tool import Tool
from lion_core.libs import (
    validate_mapping,
    fuzzy_parse_json,
    extract_json_block,
    md_to_json,
    to_dict,
)


retry_kwargs = {
    "retries": 0,  # kwargs for rcall, number of retries if failed
    "delay": 0,  # number of seconds to delay before retrying
    "backoff_factor": 1,  # exponential backoff factor, default 1 (no backoff)
    "default": LN_UNDEFINED,  # default value to return if all retries failed
    "timeout": None,  # timeout for the rcall, default None (no timeout)
    "timing": False,  # if timing will return a tuple (output, duration)
}

ai_fields = [
    "id",
    "object",
    "created",
    "model",
    "choices",
    "usage",
    "system_fingerprint",
]

choices_fields = ["index", "message", "logprobs", "finish_reason"]

usage_fields = ["prompt_tokens", "completion_tokens", "total_tokens"]


@singledispatch
def check_tool(tool_obj: Any, branch):
    raise NotImplementedError(f"Tool processing not implemented for {tool_obj}")


@check_tool.register(Tool)
def _(tool_obj: Tool, branch):
    if tool_obj.function_name not in branch.tool_manager:
        branch.register_tools(tool_obj)


@check_tool.register(Callable)
def _(tool_obj: Callable, branch):
    if tool_obj.__name__ not in branch.tool_manager:
        branch.register_tools(tool_obj)


@check_tool.register(bool)
def _(tool_obj: bool, branch):
    return


@check_tool.register(list)
def _(tool_obj: list, branch):
    [check_tool(i, branch) for i in tool_obj]


# parse the response directly from the AI model into dictionary format if possible
def parse_model_response(
    content_: dict | str,
    requested_fields: dict,
    handle_unmatched: Literal["ignore", "raise", "remove", "force"] = "force",
    fill_value: Any = None,
    fill_mapping: dict[str, Any] | None = None,
    strict: bool = False,
) -> dict | str:

    out_ = content_.get("content", "")

    if isinstance(out_, str):

        # we will start with three different json parsers
        a_ = to_dict(out_, str_type="json", parser=md_to_json, surpress=True)
        if a_ is None:
            a_ = to_dict(out_, str_type="json", parser=fuzzy_parse_json, surpress=True)
        if a_ is None:
            a_ = to_dict(
                out_, str_type="json", parser=extract_json_block, surpress=True
            )

        # if still failed, we try with xml parser
        if a_ is None:
            try:
                a_ = to_dict(out_, str_type="xml")
            except ValueError:
                a_ = None

        # if still failed, we try with using regex to extract json block
        if a_ is None:
            match = re.search(r"```json\n({.*?})\n```", out_, re.DOTALL)
            if match:
                a_ = match.group(1)
                a_ = fuzzy_parse_json(a_, surpress=True)

        # if still failed, we try with using regex to extract xml block
        if a_ is None:
            match = re.search(r"```xml\n({.*?})\n```", out_, re.DOTALL)
            if match:
                a_ = match.group(1)
                try:
                    a_ = to_dict(out_, str_type="xml")
                except ValueError:
                    a_ = None

        # we try replacing single quotes with double quotes
        if a_ is None:
            a_ = fuzzy_parse_json(out_.replace("'", '"'), surpress=True)

        # we give up here if still not succesfully parsed into a dictionary
        if a_:
            out_ = a_

    # we will forcefully correct the format of the dictionary
    # with all missing fields filled with fill_value or fill_mapping
    if isinstance(out_, dict) and requested_fields:
        return validate_mapping(
            a_,
            requested_fields,
            score_func=None,
            fuzzy_match=True,
            handle_unmatched=handle_unmatched,
            fill_value=fill_value,
            fill_mapping=fill_mapping,
            strict=strict,
        )

    return out_
