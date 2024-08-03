import asyncio

import contextlib

from lion_core.abc import BaseExecutor, Action
from lion_core.libs import fuzzy_parse_json, to_str
from lion_core.graph.edge import Edge
from lion_core.communication import Mail

from lionagi.os.primitives import System, Instruction, pile
from lionagi.os.session.branch.branch import Branch


class BranchEngine(Branch, BaseExecutor):

    def __init__(
        self,
        context=None,
        verbose=True,
        system=None,
        user=None,
        messages=None,
        progress=None,
        tool_manager=None,
        tools=None,
        imodel=None,
        **kwargs,
    ):
        super().__init__(
            system=system,
            user=user,
            messages=messages,
            progress=progress,
            tool_manager=tool_manager,
            tools=tools,
            imodel=imodel,
            **kwargs,
        )
        self.context = context
        self.verbose = verbose

    async def forward(self) -> None:
        """
        Forwards the execution by processing all pending incoming mails in each branch. Depending on the category of the mail,
        it processes starts, nodes, node lists, conditions, or ends, accordingly executing different functions.
        """
        for key in list(self.mailbox.pending_ins.keys()):
            while self.mailbox.pending_ins.get(key, pile()).size() > 0:
                mail_id = self.mailbox.pending_ins[key].popleft()
                mail = self.mailbox.pile.pop(mail_id)
                if mail.category == "start":
                    self._process_start(mail)
                elif mail.category == "node":
                    await self._process_node(mail)
                elif mail.category == "node_list":
                    self._process_node_list(mail)
                elif mail.category == "condition":
                    await self._process_condition(mail)
                elif mail.category == "end":
                    self._process_end(mail)
            if (
                key in self.mailbox.pending_ins
                and self.mailbox.pending_ins.get(key, pile()).size() == 0
            ):
                self.mailbox.pending_ins.pop(key)

    async def execute(self, refresh_time=1) -> None:
        """
        Executes the forward process repeatedly at specified time intervals until execution is instructed to stop.

        Args:
            refresh_time (int): The interval, in seconds, at which the forward method is called repeatedly.
        """
        while not self.execute_stop:
            await self.forward()
            await asyncio.sleep(refresh_time)

    async def _process_node(self, mail: Mail):
        """
        Processes a single node based on the node type specified in the mail's package. It handles different types of nodes such as System,
        Instruction, ActionNode, and generic nodes through separate processes.

        Args:
            mail (Mail): The mail containing the node to be processed along with associated details.

        Raises:
            ValueError: If an invalid mail is encountered or the process encounters errors.
        """
        node = mail.package.package
        if isinstance(node, System):
            self._system_process(node, verbose=self.verbose)
            self.send(
                recipient=mail.sender,
                category="node_id",
                package=node.ln_id,
                request_source=self.ln_id,
            )

        elif isinstance(node, Instruction):
            await self._instruction_process(node, verbose=self.verbose)
            self.send(
                recipient=mail.sender,
                category="node_id",
                package=node.ln_id,
                request_source=self.ln_id,
            )

        elif isinstance(node, Action):
            await self._action_process(node, verbose=self.verbose)
            self.send(
                recipient=mail.sender,
                category="node_id",
                package=node.instruction.ln_id,
                request_source=self.ln_id,
            )
        else:
            try:
                await self._agent_process(node, verbose=self.verbose)
                self.send(
                    recipient=mail.sender,
                    category="node_id",
                    package=node.ln_id,
                    request_source=self.ln_id,
                )
            except Exception as e:
                raise ValueError(f"Invalid mail to process. Mail:{mail}, Error: {e}")

    def _process_node_list(self, mail: Mail):
        """
        Processes a list of nodes provided in the mail, but currently only sends an end signal as multiple path selection is not supported.

        Args:
            mail (BaseMail): The mail containing a list of nodes to be processed.

        Raises:
            ValueError: When trying to process multiple paths which is currently unsupported.
        """
        self.send(mail.sender, category="end", package="end", request_source=self.ln_id)
        self.execute_stop = True
        raise ValueError("Multiple path selection is not supported in BranchExecutor")

    async def _process_condition(self, mail: Mail):
        """
        Processes a condition associated with an edge based on the mail's package, setting up the result of the condition check.

        Args:
            mail (BaseMail): The mail containing the condition to be processed.
        """
        edge: Edge = mail.package.package
        check_result = await edge.check_condition(self)
        back_mail = {
            "from": self.ln_id,
            "edge_id": edge.ln_id,
            "check_result": check_result,
        }
        self.send(
            recipient=mail.sender,
            category="condition",
            package=back_mail,
            request_source=self.ln_id,
        )

    def _system_process(self, system: System, verbose=True, context_verbose=False):
        """
        Processes a system node, possibly displaying its content and context if verbose is enabled.

        Args:
            system (System): The system node to process.
            verbose (bool): Flag to enable verbose output.
            context_verbose (bool): Flag to enable verbose output specifically for context.
        """
        from IPython.display import Markdown, display

        if verbose:
            print(f"------------------Welcome: {system.sender}--------------------")
            with contextlib.suppress(Exception):
                system.content = fuzzy_parse_json(system.content)
            display(Markdown(f"system: {to_str(system.system_info)}"))
            if self.context and context_verbose:
                display(Markdown(f"context: {to_str(self.context)}"))

        self.add_message(system=system)

    async def _instruction_process(
        self, instruction: Instruction, verbose=True, **kwargs
    ):
        """
        Processes an instruction node, possibly displaying its content if verbose is enabled, and handling any additional keyword arguments.

        Args:
            instruction (Instruction): The instruction node to process.
            verbose (bool): Flag to enable verbose output.
            **kwargs: Additional keyword arguments that might affect how instructions are processed.
        """
        from IPython.display import Markdown, display

        if verbose:
            with contextlib.suppress(Exception):
                instruction.content = fuzzy_parse_json(instruction.content)
            display(Markdown(f"{instruction.sender}: {to_str(instruction.instruct)}"))

        if self.context:
            result = await self.chat(
                instruction=instruction.instruct, context=self.context, **kwargs
            )
            self.context = None
        else:
            result = await self.chat(instruction=instruction.instruct, **kwargs)
            # instruction._add_context(context=self.context)
            # self.context_log.append(self.context)
            # self.context = None

        with contextlib.suppress(Exception):
            result = fuzzy_parse_json(result)
            if "assistant_response" in result.keys():
                result = result["assistant_response"]
        if verbose:
            display(Markdown(f"assistant {self.ln_id}: {to_str(result)}"))
            print("-----------------------------------------------------")

        self.execution_responses.append(result)

    async def _action_process(self, action: Action, verbose=True):
        """
        Processes an action node, executing the defined action along with any tools specified within the node.

        Args:
            action (ActionNode): The action node to process.
            verbose (bool): Flag to enable verbose output of the action results.
        """
        from IPython.display import Markdown, display

        # try:
        #     func = getattr(self, action.action)
        # except:
        #     raise ValueError(f"{action.action} is not a valid action")

        if verbose:
            display(
                Markdown(
                    f"{action.instruction.sender}: {to_str(action.instruction.instruct)}"
                )
            )

        # if action.tools:
        #     self.register_tools(action.tools)
        # if self.context:
        # result = await self.direct(
        #     action.directive,
        #     instruction=action.instruction.instruct,
        #     context=self.context,
        #     tools=action.tools,
        #     **action.directive_kwargs,
        # )
        result = await action.invoke(branch=self, context=self.context)
        self.context = None
        # else:
        #     result = await self.direct(
        #         action.directive,
        #         instruction=action.instruction.content,
        #         tools=action.tools,
        #         **action.directive_kwargs
        #     )

        if verbose:
            if action.directive == "chat":
                display(Markdown(f"assistant {self.ln_id}: {to_str(result)}"))
            else:
                display(Markdown(f"assistant {self.ln_id}:\n"))
                for k, v in result.work_fields.items():
                    display(Markdown(f"{k}: \n{v}\n"))
            print("-----------------------------------------------------")

        self.execution_responses.append(result)

    async def _agent_process(self, agent, verbose=True):
        """
        Processes an agent.

        Args:
            agent: The agent to process.
            verbose (bool): A flag indicating whether to provide verbose output (default: True).
        """
        context = [msg["content"] for msg in self.to_chat_messages()]
        if verbose:
            print("*****************************************************")
        result = await agent.execute(context)

        if verbose:
            print("*****************************************************")

        self.context = result
        self.execution_responses.append(result)

    def _process_start(self, mail):
        """
        Processes a start mail.

        Args:
            mail (BaseMail): The start mail to process.
        """
        start_mail_content = mail.package.package
        self.context = start_mail_content["context"]
        self.send(
            recipient=start_mail_content["structure_id"],
            category="start",
            package="start",
            request_source=self.ln_id,
        )

    def _process_end(self, mail: Mail):
        """
        Processes an end mail.

        Args:
            mail (BaseMail): The end mail to process.
        """
        self.execute_stop = True
        self.send(
            recipient=mail.sender,
            category="end",
            package="end",
            request_source=self.ln_id,
        )
