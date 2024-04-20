"""
This module contains the Branch class, which represents a branch in a conversation tree.
"""

from collections import deque
from typing import Any, Union, TypeVar, Callable
from pathlib import Path

from lionagi.libs import StatusTracker, BaseService, convert, dataframe

from lionagi.core.generic import DataLogger
from lionagi.core.tool import ToolManager, func_to_tool, Tool, TOOL_TYPE
from lionagi.core.messages.schema import System
from lionagi.core.mail.schema import BaseMail

from lionagi.core.branch.util import MessageUtil
from lionagi.core.branch.base import BaseBranch
from lionagi.core.branch.flow_mixin import BranchFlowMixin

from dotenv import load_dotenv

load_dotenv()

T = TypeVar("T", bound=Tool)


class Branch(BaseBranch, BranchFlowMixin):
    """
    Represents a branch in a conversation tree.

    Attributes:
        sender (str): The sender of the branch (default: "system").
        tool_manager (ToolManager): The tool manager for the branch.
        service (BaseService): The service associated with the branch.
        llmconfig (dict): The configuration for the language model.
        status_tracker (StatusTracker): The status tracker for the branch.
        pending_ins (dict): The pending incoming mails for the branch.
        pending_outs (deque): The pending outgoing mails for the branch.

    Methods:
        __init__(self, name=None, system=None, messages=None, service=None, sender=None,
                 llmconfig=None, tools=None, datalogger=None, persist_path=None,
                 tool_manager=None, **kwargs) -> None:
            Initializes the Branch instance.

        from_csv(cls, filepath, name=None, service=None, llmconfig=None, tools=None,
                 datalogger=None, persist_path=None, tool_manager=None, read_kwargs=None,
                 **kwargs) -> Branch:
            Creates a Branch instance from a CSV file.

        from_json_string(cls, filepath, name=None, service=None, llmconfig=None, tools=None,
                         datalogger=None, persist_path=None, tool_manager=None, read_kwargs=None,
                         **kwargs) -> Branch:
            Creates a Branch instance from a JSON string file.

        messages_describe(self) -> dict[str, Any]:
            Describes the messages in the branch.

        has_tools(self) -> bool:
            Checks if the branch has any registered tools.

        merge_branch(self, branch: Branch, update=True) -> None:
            Merges another branch into the current branch.

        register_tools(self, tools: Union[Tool, list[Tool]]) -> None:
            Registers tools in the branch's tool manager.

        delete_tools(self, tools: Union[T, list[T], str, list[str]], verbose=True) -> bool:
            Deletes tools from the branch's tool manager.

        send(self, recipient: str, category: str, package: Any) -> None:
            Sends a mail to a recipient.

        receive(self, sender: str, messages=True, tools=True, service=True, llmconfig=True) -> None:
            Receives mails from a sender and updates the branch accordingly.

        receive_all(self) -> None:
            Receives all pending mails and updates the branch accordingly.

        _add_service(service, llmconfig) -> tuple[BaseService, dict]:
            Adds a service and its configuration to the branch.

        _is_invoked(self) -> bool:
            Checks if the conversation has been invoked with an action response.
    """

    def __init__(
        self,
        name: str | None = None,
        system: dict | list | System | None = None,
        messages: dataframe.ln_DataFrame | None = None,
        service: BaseService | None = None,
        sender: str | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        tools: list[Callable | Tool] | None = None,
        datalogger: None | DataLogger = None,
        persist_path: str | Path | None = None,  # instruction_sets=None,
        tool_manager: ToolManager | None = None,
        **kwargs,
    ):
        """
        Initializes the Branch instance.

        Args:
            name (str): The name of the branch (optional).
            system (dict | list | System): The system message for the branch (optional).
            messages (dataframe.ln_DataFrame): The messages in the branch (optional).
            service (BaseService): The service associated with the branch (optional).
            sender (str): The sender of the branch (optional, default: "system").
            llmconfig (dict[str, str | int | dict]): The configuration for the language model (optional).
            tools (list[Callable | Tool]): The tools to register in the branch (optional).
            datalogger (DataLogger): The data logger for the branch (optional).
            persist_path (str | Path): The path to persist the branch data (optional).
            tool_manager (ToolManager): The tool manager for the branch (optional).
            **kwargs: Additional keyword arguments.

        Raises:
            TypeError: If there is an error in registering the tools.
        """

        super().__init__(
            messages=messages,
            datalogger=datalogger,
            persist_path=persist_path,
            name=name,
            **kwargs,
        )

        self.sender = sender or "system"
        self.tool_manager = tool_manager or ToolManager()

        if tools:
            try:
                tools_ = []
                _tools = convert.to_list(tools)
                for i in _tools:
                    if isinstance(i, Tool):
                        tools_.append(i)
                    else:
                        tools_.append(func_to_tool(i))

                self.register_tools(tools_)
            except Exception as e:
                raise TypeError(f"Error in registering tools: {e}") from e

        self.service, self.llmconfig = self._add_service(service, llmconfig)
        self.status_tracker = StatusTracker()

        # add instruction sets
        # self.instruction_sets = instruction_sets

        self.pending_ins = {}
        self.pending_outs = deque()

        if system is not None:
            self.add_message(system=system)

    @classmethod
    def from_csv(
        cls,
        filepath,
        name: str | None = None,
        service: BaseService | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        tools: TOOL_TYPE | None = None,
        datalogger: None | DataLogger = None,
        persist_path: str | Path | None = None,  # instruction_sets=None,
        tool_manager: ToolManager | None = None,
        read_kwargs=None,
        **kwargs,
    ) -> "Branch":
        """
        Creates a Branch instance from a CSV file.

        Args:
            filepath: The path to the CSV file.
            name (str): The name of the branch (optional).
            service (BaseService): The service associated with the branch (optional).
            llmconfig (dict[str, str | int | dict]): The configuration for the language model (optional).
            tools (TOOL_TYPE): The tools to register in the branch (optional).
            datalogger (DataLogger): The data logger for the branch (optional).
            persist_path (str | Path): The path to persist the branch data (optional).
            tool_manager (ToolManager): The tool manager for the branch (optional).
            read_kwargs: Additional keyword arguments for reading the CSV file (optional).
            **kwargs: Additional keyword arguments.

        Returns:
            Branch: The created Branch instance.
        """
        return cls._from_csv(
            filepath=filepath,
            read_kwargs=read_kwargs,
            name=name,
            service=service,
            llmconfig=llmconfig,
            tools=tools,
            datalogger=datalogger,
            persist_path=persist_path,
            # instruction_sets=instruction_sets,
            tool_manager=tool_manager,
            **kwargs,
        )

    @classmethod
    def from_json_string(
        cls,
        filepath,
        name: str | None = None,
        service: BaseService | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        tools: TOOL_TYPE | None = None,
        datalogger: None | DataLogger = None,
        persist_path: str | Path | None = None,  # instruction_sets=None,
        tool_manager: ToolManager | None = None,
        read_kwargs=None,
        **kwargs,
    ) -> "Branch":
        """
        Creates a Branch instance from a JSON string file.

        Args:
            filepath: The path to the JSON string file.
            name (str): The name of the branch (optional).
            service (BaseService): The service associated with the branch (optional).
            llmconfig (dict[str, str | int | dict]): The configuration for the language model (optional).
            tools (TOOL_TYPE): The tools to register in the branch (optional).
            datalogger (DataLogger): The data logger for the branch (optional).
            persist_path (str | Path): The path to persist the branch data (optional).
            tool_manager (ToolManager): The tool manager for the branch (optional).
            read_kwargs: Additional keyword arguments for reading the JSON string file (optional).
            **kwargs: Additional keyword arguments.

        Returns:
            Branch: The created Branch instance.
        """
        return cls._from_json(
            filepath=filepath,
            read_kwargs=read_kwargs,
            name=name,
            service=service,
            llmconfig=llmconfig,
            tools=tools,
            datalogger=datalogger,
            persist_path=persist_path,
            # instruction_sets=instruction_sets,
            tool_manager=tool_manager,
            **kwargs,
        )

    def messages_describe(self) -> dict[str, Any]:
        """
        Describes the messages in the branch.

        Returns:
            dict[str, Any]: A dictionary describing the messages in the branch.
        """
        return dict(
            total_messages=len(self.messages),
            summary_by_role=self._info(),
            summary_by_sender=self._info(use_sender=True),
            # instruction_sets=self.instruction_sets,
            registered_tools=self.tool_manager.registry,
            messages=[msg.to_dict() for _, msg in self.messages.iterrows()],
        )

    @property
    def has_tools(self) -> bool:
        """
        Checks if the branch has any registered tools.

        Returns:
            bool: True if the branch has registered tools, False otherwise.
        """
        return self.tool_manager.registry != {}

    # todo: also update other attributes
    def merge_branch(self, branch: "Branch", update: bool = True) -> None:
        """
        Merges another branch into the current branch.

        Args:
            branch (Branch): The branch to merge.
            update (bool): Whether to update the existing attributes or add new ones (default: True).
        """
        message_copy = branch.messages.copy()
        self.messages = self.messages.merge(message_copy, how="outer")
        self.datalogger.extend(branch.datalogger.log)

        if update:
            # self.instruction_sets.update(branch.instruction_sets)
            self.tool_manager.registry.update(branch.tool_manager.registry)
        else:
            for key, value in branch.instruction_sets.items():
                if key not in self.instruction_sets:
                    self.instruction_sets[key] = value

            for key, value in branch.tool_manager.registry.items():
                if key not in self.tool_manager.registry:
                    self.tool_manager.registry[key] = value

    # ----- tool manager methods ----- #
    def register_tools(
        self, tools: Union[Tool, list[Tool | Callable], Callable]
    ) -> None:
        """
        Registers tools in the branch's tool manager.

        Args:
            tools (Union[Tool, list[Tool]]): The tool(s) to register.
        """
        if not isinstance(tools, list):
            tools = [tools]
        self.tool_manager.register_tools(tools=tools)

    def delete_tools(
        self,
        tools: Union[T, list[T], str, list[str]],
        verbose: bool = True,
    ) -> bool:
        """
        Deletes tools from the branch's tool manager.

        Args:
            tools (Union[T, list[T], str, list[str]]): The tool(s) to delete.
            verbose (bool): Whether to print success/failure messages (default: True).

        Returns:
            bool: True if the tools were successfully deleted, False otherwise.
        """
        if not isinstance(tools, list):
            tools = [tools]
        if convert.is_same_dtype(tools, str):
            for act_ in tools:
                if act_ in self.tool_manager.registry:
                    self.tool_manager.registry.pop(act_)
            if verbose:
                print("tools successfully deleted")
            return True
        elif convert.is_same_dtype(tools, Tool):
            for act_ in tools:
                if act_.schema_["function"]["name"] in self.tool_manager.registry:
                    self.tool_manager.registry.pop(act_.schema_["function"]["name"])
            if verbose:
                print("tools successfully deleted")
            return True
        if verbose:
            print("tools deletion failed")
        return False

    def send(self, recipient_id: str, category: str, package: Any) -> None:
        """
        Sends a mail to a recipient.

        Args:
            recipient (str): The recipient of the mail.
            category (str): The category of the mail.
            package (Any): The package to send in the mail.
        """
        mail = BaseMail(
            sender_id=self.id_,
            recipient_id=recipient_id,
            category=category,
            package=package,
        )
        self.pending_outs.append(mail)

    def receive(
        self,
        sender: str,
        messages: bool = True,
        tools: bool = True,
        service: bool = True,
        llmconfig: bool = True,
    ) -> None:
        """
        Receives mails from a sender and updates the branch accordingly.

        Args:
            sender (str): The sender of the mails.
            messages (bool): Whether to receive message updates (default: True).
            tools (bool): Whether to receive tool updates (default: True).
            service (bool): Whether to receive service updates (default: True).
            llmconfig (bool): Whether to receive language model configuration updates (default: True).

        Raises:
            ValueError: If there are no packages from the specified sender.
                        If the messages format is invalid.
                        If the tools format is invalid.
                        If the provider format is invalid.
                        If the llmconfig format is invalid.
        """
        skipped_requests = deque()
        if sender not in self.pending_ins:
            raise ValueError(f"No package from {sender}")
        while self.pending_ins[sender]:
            mail_ = self.pending_ins[sender].popleft()

            if mail_.category == "messages" and messages:
                if not isinstance(mail_.package, dataframe.ln_DataFrame):
                    raise ValueError("Invalid messages format")
                MessageUtil.validate_messages(mail_.package)
                self.messages = self.messages.merge(mail_.package, how="outer")

            elif mail_.category == "tools" and tools:
                if not isinstance(mail_.package, Tool):
                    raise ValueError("Invalid tools format")
                self.tool_manager.register_tools([mail_.package])

            elif mail_.category == "provider" and service:
                from lionagi.libs.ln_api import BaseService

                if not isinstance(mail_.package, BaseService):
                    raise ValueError("Invalid provider format")
                self.service = mail_.package

            elif mail_.category == "llmconfig" and llmconfig:
                if not isinstance(mail_.package, dict):
                    raise ValueError("Invalid llmconfig format")
                self.llmconfig.update(mail_.package)

            else:
                skipped_requests.append(mail_)

        self.pending_ins[sender] = skipped_requests
        if self.pending_ins[sender] == deque():
            self.pending_ins.pop(sender)

    def receive_all(self) -> None:
        """
        Receives all pending mails and updates the branch accordingly.
        """
        for key in list(self.pending_ins.keys()):
            self.receive(key)

    @staticmethod
    def _add_service(service, llmconfig):
        from lionagi.integrations.provider.oai import OpenAIService

        if service is None:
            try:
                from lionagi.integrations.provider import Services

                service = Services.OpenAI()

            except:
                raise ValueError("No available service")
        if llmconfig is None:
            if isinstance(service, OpenAIService):
                from lionagi.integrations.config import oai_schema

                llmconfig = oai_schema["chat/completions"]["config"]
            else:
                llmconfig = {}
        return service, llmconfig

    def _is_invoked(self) -> bool:
        """
        Check if the conversation has been invoked with an action response.

        Returns:
                bool: True if the conversation has been invoked, False otherwise.

        """
        content = self.messages.iloc[-1]["content"]
        try:
            if convert.to_dict(content)["action_response"].keys() >= {
                "function",
                "arguments",
                "output",
            }:
                return True
        except Exception:
            return False
