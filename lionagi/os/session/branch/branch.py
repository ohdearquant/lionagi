from __future__ import annotations

from typing import Any

import pandas as pd

from lion_core.action.tool_manager import ToolManager
from lion_core.communication import RoledMessage
from lion_core.session.branch import Branch as CoreBranch

from lionagi.os.primitives import (
    Node,
    pile,
    Pile,
    Progression,
    prog,
    Exchange,
)

from lionagi.os.operator.imodel.imodel import iModel
from lionagi.app.Pandas.convert import to_df

from lionagi.os.primitives import Form
from .branch_converter import BranchConverterRegistry


class Branch(CoreBranch, Node):

    def __init__(
        self,
        system: Any = None,
        system_sender: str | None = None,
        system_datetime: Any = None,
        name: str | None = None,
        user: str | None = None,
        imodel: iModel | None = None,
        messages: Pile | None = None,
        tool_manager: ToolManager | None = None,
        mailbox: Exchange | None = None,
        progress: Progression | None = None,
        tools: Any = None,
    ):
        super().__init__(
            system=system,
            system_sender=system_sender,
            system_datetime=system_datetime,
            name=name,
            user=user,
            imodel=imodel or iModel(),
            messages=messages,
            tool_manager=tool_manager,
            mailbox=mailbox or Exchange(),
            progress=progress,
            tools=tools,
        )

        # switch core pile/progression class to use lionagi primitives
        self.messages = pile(list(self.messages), RoledMessage, strict=False)
        self.progress = prog(list(self.progress))

    def to_df(self) -> pd.DataFrame:
        fields = [
            "ln_id",
            "message_type",
            "timestamp",
            "role",
            "content",
            "metadata",
            "sender",
            "recipient",
        ]
        dicts_ = []
        for i in self.progress:
            _d = {j: getattr(self.messages[i], j, None) for j in fields}
            _d["message_type"] = self.messages[i].class_name()
            dicts_.append(_d)

        return to_df(dicts_)

    async def chat(
        self,
        instruction: Any = None,
        context: Any = None,
        system: Any = None,
        sender: Any = None,
        recipient: Any = None,
        form=None,
        tools=None,
        image: str | list[str] | None = None,
        return_branch=False,
        **kwargs,
    ):
        from lionagi.os.operation.chat.chat import chat

        return await chat(
            instruction=instruction,
            context=context,
            system=system,
            sender=sender,
            recipient=recipient,
            branch=self,
            form=form,
            image=image,
            tools=tools,
            return_branch=return_branch,
            **kwargs,
        )

    async def plan(
        self,
        instruction=None,
        context=None,
        system=None,
        sender=None,
        recipient=None,
        form=None,
        image: str | list[str] | None = None,
        allow_extension=False,
        num_step=3,
        verbose=True,
        reflect: Any = None,
        **kwargs,
    ):
        from lionagi.os.operation.plan.plan import plan

        return await plan(
            instruction=instruction,
            context=context,
            system=system,
            sender=sender,
            recipient=recipient,
            branch=self,
            form=form,
            image=image,
            allow_extension=allow_extension,
            num_step=num_step,
            verbose=verbose,
            reflect=reflect,
            **kwargs,
        )

    async def score(
        self,
        instruction=None,
        context=None,
        system=None,
        sender=None,
        recipient=None,
        form=None,
        confidence=None,
        num_digits=None,
        score_range=None,
        image: str | list[str] | None = None,
        verbose=True,
        reason=False,
        reflect=None,
        **kwargs,
    ):
        from lionagi.os.operation.score.score import score

        return await score(
            instruction=instruction,
            context=context,
            system=system,
            sender=sender,
            recipient=recipient,
            branch=self,
            form=form,
            confidence=confidence,
            reason=reason,
            score_range=score_range,
            num_digits=num_digits,
            image=image,
            verbose=verbose,
            reflect=reflect,
            **kwargs,
        )

    async def react(
        self,
        instruction=None,
        context=None,
        system=None,
        sender=None,
        recipient=None,
        form=None,
        tools=None,
        image: str | list[str] | None = None,
        verbose=True,
        reflect: Any = None,
        **kwargs,
    ):
        from lionagi.os.operation.react.react import react

        return await react(
            instruction=instruction,
            context=context,
            system=system,
            sender=sender,
            recipient=recipient,
            branch=self,
            form=form,
            tools=tools,
            image=image,
            verbose=verbose,
            reflect=reflect,
            **kwargs,
        )

    # async def learn(self, *args, **kwargs): ...

    # async def memorize(self, *args, **kwargs): ...

    # async def query(self, *args, **kwargs): ...

    # async def rank(self, *args, **kwargs): ...

    # async def regurgitate(self, *args, **kwargs): ...

    # async def respond(self, *args, **kwargs): ...

    # async def route(self, *args, **kwargs): ...

    # async def score(self, *args, **kwargs): ...

    # async def strategize(self, *args, **kwargs): ...

    # async def assess(self, *args, **kwargs): ...

    # __slots__ = [
    #     "ln_id",
    #     "timestamp",
    #     "from_dict",
    #     "to_dict",
    #     "class_name",
    #     "metadata",
    #     "content",
    #     "extra_fields",
    #     "all_fields",
    #     "add_field",
    #     "update_field",
    #     "system",
    #     "user",
    #     "imodel",
    #     "name",
    #     "messages",
    #     "tool_manager",
    #     "mailbox",
    #     "progress",
    #     "set_system",
    #     "add_message",
    #     "clear_messages",
    #     "send",
    #     "receive",
    #     "receive_all",
    #     "convert_from",
    #     "convert_to",
    #     "last_response",
    #     "assistant_responses",
    #     "update_last_instruction_meta",
    #     "has_tools",
    #     "register_tools",
    #     "delete_tools",
    #     "to_chat_messages",
    #     "to_df",
    #     "assess",
    #     "chat",
    #     "direct",
    #     "learn",
    #     "memorize",
    #     "plan",
    #     "query",
    #     "rank",
    #     "regurgitate",
    #     "respond",
    #     "route",
    #     "score",
    #     "strategize",
    # ]


Branch._converter_registry = BranchConverterRegistry

__all__ = ["Branch"]


# File path: lionagi/os/space/branch.py
