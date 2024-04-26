from abc import ABC
from typing import Any, Optional, Union, TypeVar

from lionagi.core.tool import Tool, TOOL_TYPE
from lionagi.core.messages.schema import Instruction, System
from lionagi.core.flow.monoflow import MonoChat, MonoFollowup, MonoReAct

T = TypeVar("T", bound=Tool)


class BranchFlowMixin(ABC):

    async def chat(
        self,
        instruction: Union[Instruction, str] = None,
        context: Optional[Any] = None,
        sender: Optional[str] = None,
        system: Optional[Union[System, str, dict[str, Any]]] = None,
        tools: TOOL_TYPE = False,
        out: bool = True,
        invoke: bool = True,
        output_fields=None,
        form=None,
        **kwargs,
    ) -> Any:
        flow = MonoChat(self)
        return await flow.chat(
            instruction=instruction,
            context=context,
            sender=sender,
            system=system,
            tools=tools,
            out=out,
            invoke=invoke,
            output_fields=output_fields,
            form=form,
            **kwargs,
        )

    async def ReAct(
        self,
        instruction: Instruction | str | dict[str, dict | str],
        context=None,
        sender=None,
        system=None,
        tools=None,
        auto=False,
        num_rounds: int = 1,
        reason_prompt=None,
        action_prompt=None,
        output_prompt=None,
        **kwargs,
    ):
        flow = MonoReAct(self)
        return await flow.ReAct(
            instruction=instruction,
            context=context,
            sender=sender,
            system=system,
            tools=tools,
            auto=auto,
            num_rounds=num_rounds,
            reason_prompt=reason_prompt,
            action_prompt=action_prompt,
            output_prompt=output_prompt,
            **kwargs,
        )

    async def followup(
        self,
        instruction: Instruction | str | dict[str, dict | str],
        context=None,
        sender=None,
        system=None,
        tools=None,
        max_followup: int = 1,
        auto=False,
        followup_prompt=None,
        output_prompt=None,
        out=True,
        **kwargs,
    ):
        flow = MonoFollowup(self)
        return await flow.followup(
            instruction=instruction,
            context=context,
            sender=sender,
            system=system,
            tools=tools,
            max_followup=max_followup,
            auto=auto,
            followup_prompt=followup_prompt,
            output_prompt=output_prompt,
            out=out,
            **kwargs,
        )
