from typing import Any
from lion_core.session.session import Session as CoreSession
from lion_core.action.tool_manager import ToolManager

from lionagi.os.primitives.node import Node
from lionagi.os.primitives.container.pile import Pile
from lionagi.os.primitives.container.exchange import Exchange
from lionagi.os.primitives.container.flow import Flow
from lionagi.os.primitives._funcs import prog, pile, flow
from lionagi.os.primitives.session.branch.branch import Branch
from lionagi.os.operator.imodel.imodel import iModel


FindableBranch = list[Branch | str] | Pile | Branch | str | None


class Session(CoreSession, Node):

    def __init__(
        self,
        system: Any = None,
        *,
        system_sender: Any = None,
        system_datetime: Any = None,
        name: str | None = None,
        user: str | None = None,
        imodel: iModel | None = None,
        mail_transfer: Exchange | None = None,
        branches: Pile[Branch] | None = None,
        default_branch: Branch | None = None,
        default_branch_config: (
            dict | None
        ) = None,  # if a default branch is provided, will ignore this config
        conversations: Flow | None = None,
        tool_manager: ToolManager | None = None,
        tools: Any = None,
    ):
        default_branch_config = default_branch_config or {}

        super().__init__(
            branch_type=Branch,
            session=system,
            session_system_sender=system_sender,
            session_system_datetime=system_datetime,
            session_name=name,
            session_user=user,
            session_imodel=imodel,
            mail_transfer=mail_transfer or Exchange(),
            branches=branches or pile({}, Branch, strict=False),
            default_branch=default_branch,
            conversations=conversations,
            branch_system=default_branch_config.get("system", None),
            branch_system_sender=default_branch_config.get("system_sender", None),
            branch_system_datetime=default_branch_config.get("system_datetime", None),
            branch_name=default_branch_config.get("name", None),
            branch_user=default_branch_config.get("user", None),
            branch_imodel=default_branch_config.get("imodel", None),
            branch_messages=default_branch_config.get("messages", None),
            branch_mailbox=default_branch_config.get("mailbox", None),
            branch_progress=default_branch_config.get("progress", None),
            tool_manager=tool_manager,
            tools=tools,
        )

        # switch branches pile class to use lionagi
        self.branches = pile(list(self.branches))

        pi = pile()
        for po in self.conversations:
            pi += prog(
                list(po), default_branch_name=self.default_branch.name
            )  # change the progression class

        self.conversations = flow(pi)  # change the flow class

    async def assess(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def chat(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def direct(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def learn(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def memorize(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def plan(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def query(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def rank(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def regurgitate(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def respond(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def route(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def score(self, *args, branch: FindableBranch = None, **kwargs): ...

    async def strategize(self, *args, **kwargs): ...


__all__ = ["Session"]
