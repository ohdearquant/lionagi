retry_kwargs = {
    "retries": 3,  # kwargs for rcall, number of retries if failed
    "delay": 0,  # number of seconds to delay before retrying
    "backoff_factor": 1,  # exponential backoff factor, default 1 (no backoff)
    "default": None,  # default value to return if all retries failed
    "timeout": None,  # timeout for the rcall, default None (no timeout)
    "timing": False,  # if timing will return a tuple (output, duration)
}

oai_fields = [
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

from typing import Callable
from lionagi.core.action.tool import Tool
from lionagi.core.action.tool_manager import func_to_tool


def process_tools(tool_obj, branch):
    if isinstance(tool_obj, Callable):
        _process_tool(tool_obj, branch)
    else:
        tool_obj = [tool_obj] if not isinstance(tool_obj, list) else tool_obj
        for i in tool_obj:
            _process_tool(i, branch)


def _process_tool(tool_obj, branch):
    if (
        isinstance(tool_obj, Tool)
        and tool_obj.schema_["function"]["name"] not in branch.tool_manager.registry
    ):
        branch.register_tools(tool_obj)
    if isinstance(tool_obj, Callable):
        tool = func_to_tool(tool_obj)[0]
        if tool.schema_["function"]["name"] not in branch.tool_manager.registry:
            branch.register_tools(tool)


async def _direct(
    directive,
    form=None,
    template=None,
    reason=False,
    confidence_score=None,
    instruction=None,
    context=None,
    template_kwargs={},
    **kwargs,
):
    if not form:
        form = template(
            instruction=instruction,
            context=context,
            confidence_score=confidence_score,
            reason=reason,
            **template_kwargs,
        )

    return await directive.direct(form=form, return_form=True, **kwargs)
