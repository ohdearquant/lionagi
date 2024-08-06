from functools import singledispatch
import re
from lion_core.setting import LN_UNDEFINED
from typing import Any, Callable, Literal
from lion_core.action.tool import Tool

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


