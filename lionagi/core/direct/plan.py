# plan.py

from lionagi.libs import func_call, ParseUtil
from lionagi.integrations.bridge.pydantic_.pydantic_bridge import Field
from lionagi.core.form.scored_form import ScoredForm
from lionagi.core.branch.branch import Branch


class PlanTemplate(ScoredForm):
    template_name: str = "default_plan"
    sentence: str | list | dict = Field(
        default_factory=str,
        description="the given sentence(s) or context to generate a plan for",
    )
    plan: dict | str = Field(
        default_factory=dict,
        description="the generated step by step plan, return as a dictionary following {step_n: {plan: ..., reason: ...}} format",
    )
    signature: str = "sentence -> plan"

    def __init__(
        self,
        sentence=None,
        instruction=None,
        confidence_score=False,
        reason=False,
        num_step=3,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.sentence = sentence
        self.task = f"Generate a {num_step}_step plan based on the given context. Instruction: {instruction}."

        if reason:
            self.output_fields.append("reason")

        if confidence_score:
            self.output_fields.append("confidence_score")


async def _plan(
    sentence,
    *,
    instruction=None,
    branch=None,
    confidence_score=False,
    reason=False,
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

    _template = PlanTemplate(
        sentence=sentence,
        instruction=instruction,
        confidence_score=confidence_score,
        reason=reason,
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

    _template.plan = ParseUtil.fuzzy_parse_json(_template.plan)

    return (_template, branch) if return_branch else _template


async def plan(
    sentence,
    *,
    instruction=None,
    num_instances=1,
    branch=None,
    confidence_score=False,
    reason=False,
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
        return await _plan(
            sentence=sentence,
            instruction=instruction,
            branch=branch,
            confidence_score=confidence_score,
            reason=reason,
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
