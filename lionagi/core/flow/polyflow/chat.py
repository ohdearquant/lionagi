"""
This module contains the PolyChat class for performing parallel chat conversations with multiple branches.

The PolyChat class allows for conducting parallel chat conversations with multiple branches, each processing
instructions and context independently. It provides methods for parallel chat execution and manages the
created branches within the session.
"""

from typing import Any

from lionagi.libs import convert, AsyncUtil
from lionagi.core.message import Instruction
from lionagi.core.session.branch import Branch
from lionagi.core.flow.baseflow import BasePolyFlow


class PolyChat(BasePolyFlow):
    """
    A class for performing parallel chat conversations with multiple branches.

    Methods:
        __init__(self, session: Any) -> None:
            Initializes the PolyChat instance.

        async parallel_chat(self, instruction: Union[Instruction, str], num_instances: int = 1,
                             context: Optional[Any] = None, sender: Optional[str] = None,
                             branch_system: Optional[Any] = None, messages: Optional[Any] = None,
                             tools: bool = False, out: bool = True, invoke: bool = True,
                             output_fields: Optional[Any] = None, persist_path: Optional[str] = None,
                             branch_config: Optional[dict] = None, explode: bool = False, **kwargs) -> Any:
            Performs parallel chat conversations with multiple branches.

        async _parallel_chat(self, instruction: Union[Instruction, str], num_instances: int = 1,
                              context: Optional[Any] = None, sender: Optional[str] = None,
                              messages: Optional[Any] = None, tools: bool = False, out: bool = True,
                              invoke: bool = True, output_fields: Optional[Any] = None,
                              persist_path: Optional[str] = None, branch_config: dict = {},
                              explode: bool = False, include_mapping: bool = True, default_key: str = "response",
                              **kwargs) -> Any:
            Internal method for performing parallel chat conversations with multiple branches.
    """

    def __init__(self, session) -> None:
        """
        Initializes the PolyChat instance.

        Args:
            session: The session object.
        """
        super().__init__(session)

    async def parallel_chat(
        self,
        instruction: Instruction | str,
        num_instances=1,
        context=None,
        sender=None,
        branch_system=None,
        messages=None,
        tools=False,
        out=True,
        invoke: bool = True,
        output_fields=None,
        persist_path=None,
        branch_config=None,
        explode=False,
        **kwargs,
    ) -> Any:
        """
        Performs parallel chat conversations with multiple branches.

        Args:
            instruction (Instruction | str): The instruction for the chat conversation.
            num_instances (int): The number of branch instances to create (default: 1).
            context (Optional[Any]): Additional context for the chat conversation.
            sender (Optional[str]): The sender of the chat message.
            branch_system (Optional[Any]): The system configuration for the branches.
            messages (Optional[Any]): Messages to initialize the branches with.
            tools (bool): Flag indicating whether to use tools in the chat conversation (default: False).
            out (bool): Flag indicating whether to return the output of the chat conversation (default: True).
            invoke (bool): Flag indicating whether to invoke tools during the chat conversation (default: True).
            output_fields (Optional[Any]): The output fields for the chat conversation.
            persist_path (Optional[str]): The path to persist the branch data.
            branch_config (Optional[dict]): Additional configuration for the branches.
            explode (bool): Flag indicating whether to explode the instruction and context combinations (default: False).
            **kwargs: Additional keyword arguments for the chat conversation.

        Returns:
            Any: The result of the parallel chat conversation.
        """
        if branch_config is None:
            branch_config = {}
        return await self._parallel_chat(
            instruction,
            num_instances=num_instances,
            context=context,
            sender=sender,
            branch_system=branch_system,
            messages=messages,
            tools=tools,
            out=out,
            invoke=invoke,
            output_fields=output_fields,
            persist_path=persist_path,
            branch_config=branch_config,
            explode=explode,
            **kwargs,
        )

    async def _parallel_chat(
        self,
        instruction: Instruction | str,
        num_instances=1,
        context=None,
        sender=None,
        messages=None,
        tools=False,
        out=True,
        invoke: bool = True,
        output_fields=None,
        persist_path=None,
        branch_config={},
        explode=False,
        include_mapping=True,
        default_key="response",
        **kwargs,
    ) -> Any:
        """
        Internal method for performing parallel chat conversations with multiple branches.

        Args:
            instruction (Instruction | str): The instruction for the chat conversation.
            num_instances (int): The number of branch instances to create (default: 1).
            context (Optional[Any]): Additional context for the chat conversation.
            sender (Optional[str]): The sender of the chat message.
            messages (Optional[Any]): Messages to initialize the branches with.
            tools (bool): Flag indicating whether to use tools in the chat conversation (default: False).
            out (bool): Flag indicating whether to return the output of the chat conversation (default: True).
            invoke (bool): Flag indicating whether to invoke tools during the chat conversation (default: True).
            output_fields (Optional[Any]): The output fields for the chat conversation.
            persist_path (Optional[str]): The path to persist the branch data.
            branch_config (dict): Additional configuration for the branches (default: {}).
            explode (bool): Flag indicating whether to explode the instruction and context combinations (default: False).
            include_mapping (bool): Flag indicating whether to include mapping information in the output (default: True).
            default_key (str): The default key for the output mapping (default: "response").
            **kwargs: Additional keyword arguments for the chat conversation.

        Returns:
            Any: The result of the parallel chat conversation.
        """

        branches = {}

        async def _inner(i, ins_, cxt_):

            branch_ = Branch(
                messages=messages,
                service=self.session.default_branch.service,
                llmconfig=self.session.default_branch.llmconfig,
                persist_path=persist_path,
                **branch_config,
            )

            branch_.branch_name = branch_.id_

            if tools:
                branch_.tool_manager = self.session.default_branch.tool_manager

            res_ = await branch_.chat(
                instruction=ins_ or instruction,
                context=cxt_ or context,
                sender=sender,
                tools=tools,
                invoke=invoke,
                out=out,
                output_fields=output_fields,
                **kwargs,
            )

            branches[branch_.id_] = branch_
            if include_mapping:
                return {
                    "instruction": ins_ or instruction,
                    "context": cxt_ or context,
                    "branch_id": branch_.id_,
                    default_key: res_,
                }

            else:
                return res_

        async def _inner_2(i, ins_=None, cxt_=None):
            """returns num_instances of branches performing for same task/context"""
            tasks = [_inner(i, ins_, cxt_) for _ in range(num_instances)]
            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        async def _inner_3(i):
            """different instructions but same context"""
            tasks = [_inner_2(i, ins_=ins_) for ins_ in convert.to_list(instruction)]
            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        async def _inner_3_b(i):
            """different context but same instruction"""
            tasks = [_inner_2(i, cxt_=cxt_) for cxt_ in convert.to_list(context)]
            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        async def _inner_4(i):
            """different instructions and different context"""

            tasks = []
            if explode:
                tasks = [
                    _inner_2(i, ins_=ins_, cxt_=cxt_)
                    for ins_ in convert.to_list(instruction)
                    for cxt_ in convert.to_list(context)
                ]
            else:
                tasks = [
                    _inner_2(i, ins_=ins_, cxt_=cxt_)
                    for ins_, cxt_ in zip(
                        convert.to_list(instruction), convert.to_list(context)
                    )
                ]

            ress = await AsyncUtil.execute_tasks(*tasks)
            return convert.to_list(ress)

        if len(convert.to_list(instruction)) == 1:
            if len(convert.to_list(context)) == 1:
                out_ = await _inner_2(0)
                self.session.branches.update(branches)
                return out_

            elif len(convert.to_list(context)) > 1:
                out_ = await _inner_3_b(0)
                self.session.branches.update(branches)
                return out_

        elif len(convert.to_list(instruction)) > 1:
            if len(convert.to_list(context)) == 1:
                out_ = await _inner_3(0)
                self.session.branches.update(branches)
                return out_

            elif len(convert.to_list(context)) > 1:
                out_ = await _inner_4(0)
                self.session.branches.update(branches)
                return out_
