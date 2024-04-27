from lionagi.libs import func_call, AsyncUtil

from lionagi.integrations.bridge.pydantic_.pydantic_bridge import Field
from lionagi.core.form.action_form import ActionForm
from lionagi.core.branch.branch import Branch
from lionagi.core.direct.utils import _process_tools


class ReactTemplate(ActionForm):
    template_name: str = "default_react"
    sentence: str | list | dict | None = Field(
        default_factory=str,
        description="the given sentence(s) to reason and take actions on",
    )

    def __init__(
        self,
        sentence=None,
        instruction=None,
        confidence_score=False,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.sentence = sentence
        self.task = f"Think step by step. Perform reasoning and prepare actions with given tools only.Instruction: {instruction}. Absolutely DO NOT MAKE UP FUNCTIONS !!!"

        if confidence_score:
            self.output_fields.append("confidence_score")


async def _react(
    sentence=None,
    *,
    instruction=None,
    branch=None,
    confidence_score=False,
    retries=2,
    delay=0.5,
    backoff_factor=2,
    default_value=None,
    timeout=None,
    branch_name=None,
    system=None,
    messages=None,
    service=None,
    sender=None,
    llmconfig=None,
    tools=None,
    datalogger=None,
    persist_path=None,
    tool_manager=None,
    return_branch=False,
    **kwargs,
):

    if "temperature" not in kwargs:
        kwargs["temperature"] = 0.1

    instruction = instruction or ""

    if branch and tools:
        _process_tools(tools, branch)

    branch = branch or Branch(
        name=branch_name,
        system=system,
        messages=messages,
        service=service,
        sender=sender,
        llmconfig=llmconfig,
        tools=tools,
        datalogger=datalogger,
        persist_path=persist_path,
        tool_manager=tool_manager,
    )

    _template = ReactTemplate(
        sentence=sentence,
        instruction=instruction,
        confidence_score=confidence_score,
    )

    await func_call.rcall(
        branch.chat,
        form=_template,
        retries=retries,
        delay=delay,
        backoff_factor=backoff_factor,
        default=default_value,
        timeout=timeout,
        **kwargs,
    )

    if _template.action_needed:
        actions = _template.actions
        tasks = [branch.tool_manager.invoke(i.values()) for i in actions]
        results = await AsyncUtil.execute_tasks(*tasks)

        a = []
        for idx, item in enumerate(actions):
            res = {
                "function": item["function"],
                "arguments": item["arguments"],
                "output": results[idx],
            }
            branch.add_message(response=res)
            a.append(res)

        _template.__setattr__("action_response", a)

    return (_template, branch) if return_branch else _template


async def react(
    sentence=None,
    *,
    instruction=None,
    num_instances=1,
    branch=None,
    confidence_score=False,
    retries=2,
    delay=0.5,
    backoff_factor=2,
    default_value=None,
    timeout=None,
    branch_name=None,
    system=None,
    messages=None,
    service=None,
    sender=None,
    llmconfig=None,
    tools=None,
    datalogger=None,
    persist_path=None,
    tool_manager=None,
    return_branch=False,
    **kwargs,
):

    async def _inner(i=0):
        return await _react(
            sentence=sentence,
            instruction=instruction,
            num_instances=num_instances,
            branch=branch,
            confidence_score=confidence_score,
            retries=retries,
            delay=delay,
            backoff_factor=backoff_factor,
            default_value=default_value,
            timeout=timeout,
            branch_name=branch_name,
            system=system,
            messages=messages,
            service=service,
            sender=sender,
            llmconfig=llmconfig,
            tools=tools,
            datalogger=datalogger,
            persist_path=persist_path,
            tool_manager=tool_manager,
            return_branch=return_branch,
            **kwargs,
        )

    if num_instances == 1:
        return await _inner()

    elif num_instances > 1:
        return await func_call.alcall(range(num_instances), _inner)
