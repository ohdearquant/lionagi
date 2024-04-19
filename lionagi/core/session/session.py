from collections import deque
from typing import Tuple

from pathlib import Path
from lionagi.libs import BaseService, convert, dataframe

from lionagi.core.generic import DataLogger
from lionagi.core.tool import ToolManager, Tool, TOOL_TYPE
from lionagi.core.mail.mail_manager import MailManager
from lionagi.core.messages.schema import System, Instruction
from lionagi.core.branch.branch import Branch
from lionagi.core.flow.polyflow import PolyChat


class Session:
    """
    Represents a session for managing conversations and branches.

    A `Session` encapsulates the state and behavior for managing conversations and their branches.
    It provides functionality for initializing and managing conversation sessions, including setting up default
    branches, configuring language learning models, managing tools, and handling session data logging.

    Attributes:
            branches (dict[str, Branch]): A dictionary of branch instances associated with the session.
            service (BaseService]): The external service instance associated with the | Nonesession.
            mail_manager (BranchManager): The manager for handling branches within the session.
            datalogger (Optional[Any]): The datalogger instance for session data logging.
    """

    def __init__(
        self,
        system: dict | list | System | None = None,
        sender: str | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        service: BaseService | None = None,
        branches: dict[str, Branch] | None = None,
        default_branch: Branch | None = None,
        default_branch_name: str | None = None,
        tools: TOOL_TYPE | None = None,
        # instruction_sets: Optional[List[Instruction]] = None,
        tool_manager: ToolManager | None = None,
        messages: dataframe.ln_DataFrame | None = None,
        datalogger: None | DataLogger = None,
        persist_path: Path | str | None = None,
    ):
        """Initialize a new session with optional configuration for managing conversations.

        Args:
                system (Optional[Union[str, System]]): The system message.
                sender (str | None): the default sender name for default branch
                llmconfig (dict[str, Any] | None): Configuration for language learning models.
                service (BaseService]): External service  | Nonenstance.
                branches (dict[str, Branch] | None): dictionary of branch instances.
                default_branch (Branch | None): The default branch for the session.
                default_branch_name (str | None): The name of the default branch.
                tools (TOOL_TYPE | None): List of tools available for the session.
                instruction_sets (Optional[List[Instruction]]): List of instruction sets.
                tool_manager (Optional[Any]): Manager for handling tools.
                messages (Optional[List[dict[str, Any]]]): Initial list of messages.
                datalogger (Optional[Any]): Logger instance for the session.
                persist_path (str | None): Directory path for saving session data.

        Examples:
                >>> session = Session(system="you are a helpful assistant", sender="researcher")
        """
        self.branches = branches if isinstance(branches, dict) else {}
        self.service = service
        self.setup_default_branch(
            system=system,
            sender=sender,
            default_branch=default_branch,
            default_branch_name=default_branch_name,
            messages=messages,
            # instruction_sets=instruction_sets,
            tool_manager=tool_manager,
            service=service,
            llmconfig=llmconfig,
            tools=tools,
            persist_path=persist_path,
            datalogger=datalogger,
        )
        self.mail_manager = MailManager(self.branches)
        self.datalogger = self.default_branch.datalogger
        for key, branch in self.branches.items():
            branch.name = key

    # --- default branch methods ---- #

    @property
    def messages(self):
        return self.default_branch.messages

    @property
    def messages_describe(self):
        """
        Provides a descriptive summary of all messages in the branch.

        Returns:
                dict[str, Any]: A dictionary containing summaries of messages by role and sender, total message count,
                instruction sets, registered tools, and message details.

        Examples:
                >>> session.messages_describe
                {'total_messages': 100, 'by_sender': {'User123': 60, 'Bot': 40}}
        """
        return self.default_branch.messages_describe

    @property
    def has_tools(self) -> bool:
        """
        Checks if there are any tools registered in the tool manager.

        Returns:
                bool: True if there are tools registered, False otherwise.

        Examples:
                >>> session.has_tools
                True
        """
        return self.default_branch.has_tools

    @property
    def last_message(self) -> dataframe.ln_DataFrame:
        """
        Retrieves the last message from the conversation.

        Returns:
                pd.Series: The last message as a pandas Series.
        """
        return self.default_branch.last_message

    @property
    def first_system(self) -> dataframe.ln_DataFrame:
        """
        Retrieves the first system message from the conversation.

        Returns:
                pd.Series: The first system message as a pandas Series.
        """
        return self.default_branch.first_system

    @property
    def last_response(self) -> dataframe.ln_DataFrame:
        """
        Retrieves the last response message from the conversation.

        Returns:
                pd.Series: The last response message as a pandas Series.
        """
        return self.default_branch.last_response

    @property
    def last_response_content(self) -> dict:
        """
        Retrieves the content of the last response message from the conversation.

        Returns:
                dict: The content of the last response message as a dictionary
        """
        return self.default_branch.last_response_content

    @property
    def tool_request(self) -> dataframe.ln_DataFrame:
        """
        Retrieves all tool request messages from the conversation.

        Returns:
                dataframe.ln_DataFrame: A DataFrame containing all tool request messages.
        """
        return self.default_branch.tool_request

    @property
    def tool_response(self) -> dataframe.ln_DataFrame:
        """
        Retrieves all tool response messages from the conversation.

        Returns:
                dataframe.ln_DataFrame: A DataFrame containing all tool response messages.
        """
        return self.default_branch.tool_response

    @property
    def responses(self) -> dataframe.ln_DataFrame:
        """
        Retrieves all response messages from the conversation.

        Returns:
                dataframe.ln_DataFrame: A DataFrame containing all response messages.
        """
        return self.default_branch.responses

    @property
    def assistant_responses(self) -> dataframe.ln_DataFrame:
        """
        Retrieves all assistant responses from the conversation, excluding tool requests and responses.

        Returns:
                dataframe.ln_DataFrame: A DataFrame containing assistant responses excluding tool requests and responses.
        """
        return self.default_branch.assistant_responses

    @property
    def info(self) -> dict[str, int]:
        """
        Get a summary of the conversation messages categorized by role.

        Returns:
                dict[str, int]: A dictionary with keys as message roles and values as counts.
        """

        return self.default_branch.info

    @property
    def sender_info(self) -> dict[str, int]:
        """
        Provides a descriptive summary of the conversation, including total message count and summary by sender.

        Returns:
                dict[str, Any]: A dictionary containing the total number of messages and a summary categorized by sender.
        """
        return self.default_branch.sender_info

    def register_tools(self, tools):
        self.default_branch.register_tools(tools)

    @classmethod
    def from_csv(
        cls,
        filepath: Path | str,
        system: dict | list | System | None = None,
        sender: str | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        service: BaseService = None,
        default_branch_name: str = "main",
        tools: TOOL_TYPE = False,  # instruction_sets=None,
        tool_manager=None,
        **kwargs,
    ) -> "Session":
        """
        Creates a Session instance from a CSV file containing messages.

        Args:
                filepath (str): Path to the CSV file.
                name (str | None): Name of the branch, default is None.
                instruction_sets (Optional[dict[str, InstructionSet]]): Instruction sets, default is None.
                tool_manager (Optional[ToolManager]): Tool manager for the branch, default is None.
                service (BaseService]): External service for the branch, default | Noneis None.
                llmconfig (Optional[dict]): Configuration for language learning models, default is None.
                tools (TOOL_TYPE | None): Initial list of tools to register, default is None.
                **kwargs: Additional keyword arguments for pd.read_csv().

        Returns:
                Branch: A new Branch instance created from the CSV data.

        Examples:
                >>> branch = Branch.from_csv("path/to/messages.csv", name="ImportedBranch")
        """
        df = dataframe.read_csv(filepath, **kwargs)

        return cls(
            system=system,
            sender=sender,
            llmconfig=llmconfig,
            service=service,
            default_branch_name=default_branch_name,
            tools=tools,
            tool_manager=tool_manager,
            messages=df,
            **kwargs,
        )

    @classmethod
    def from_json(
        cls,
        filepath: Path | str,
        system: dict | list | System | None = None,
        sender: str | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        service: BaseService = None,
        default_branch_name: str = "main",
        tools: TOOL_TYPE = False,  # instruction_sets=None,
        tool_manager=None,
        **kwargs,
    ) -> "Session":
        """
        Creates a Branch instance from a JSON file containing messages.

        Args:
                filepath (str): Path to the JSON file.
                name (str | None): Name of the branch, default is None.
                instruction_sets (Optional[dict[str, InstructionSet]]): Instruction sets, default is None.
                tool_manager (Optional[ToolManager]): Tool manager for the branch, default is None.
                service (BaseService]): External service for the branch, default | Noneis None.
                llmconfig (Optional[dict]): Configuration for language learning models, default is None.
                **kwargs: Additional keyword arguments for pd.read_json().

        Returns:
                Branch: A new Branch instance created from the JSON data.

        Examples:
                >>> branch = Branch.from_json_string("path/to/messages.json", name="JSONBranch")
        """
        df = dataframe.read_json(filepath, **kwargs)
        return cls(
            system=system,
            sender=sender,
            llmconfig=llmconfig,
            service=service,
            default_branch_name=default_branch_name,
            tools=tools,  # instruction_sets=instruction_sets,
            tool_manager=tool_manager,
            messages=df,
            **kwargs,
        )

    def to_csv_file(
        self,
        filename: str = "messages.csv",
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        **kwargs,
    ):
        """
        Saves the branch's messages to a CSV file.

        Args:
                filename (str): The name of the output CSV file, default is 'messages.csv'.
                dir_exist_ok (bool): If True, does not raise an error if the directory already exists, default is True.
                timestamp (bool): If True, appends a timestamp to the filename, default is True.
                time_prefix (bool): If True, adds a timestamp prefix to the filename, default is False.
                verbose (bool): If True, prints a message upon successful save, default is True.
                clear (bool): If True, clears the messages after saving, default is True.
                **kwargs: Additional keyword arguments for DataFrame.to_csv().

        Examples:
                >>> branch.to_csv_file("exported_messages.csv")
                >>> branch.to_csv_file("timed_export.csv", timestamp=True, time_prefix=True)
        """
        for name, branch in self.branches.items():
            f_name = f"{name}_{filename}"
            branch.to_csv_file(
                filename=f_name,
                dir_exist_ok=dir_exist_ok,
                timestamp=timestamp,
                time_prefix=time_prefix,
                verbose=verbose,
                clear=clear,
                **kwargs,
            )

    def to_json_file(
        self,
        filename: str = "messages.json",
        dir_exist_ok: bool = False,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        **kwargs,
    ):
        """
        Saves the branch's messages to a JSON file.

        Args:
                filename (str): The name of the output JSON file, default is 'messages.json'.
                dir_exist_ok (bool): If True, does not raise an error if the directory already exists, default is True.
                timestamp (bool): If True, appends a timestamp to the filename, default is True.
                time_prefix (bool): If True, adds a timestamp prefix to the filename, default is False.
                verbose (bool): If True, prints a message upon successful save, default is True.
                clear (bool): If True, clears the messages after saving, default is True.
                **kwargs: Additional keyword arguments for DataFrame.to_json().

        Examples:
                >>> branch.to_json_file("exported_messages.json")
                >>> branch.to_json_file("timed_export.json", timestamp=True, time_prefix=True)
        """

        for name, branch in self.branches.items():
            f_name = f"{name}_{filename}"
            branch.to_json_file(
                filename=f_name,
                dir_exist_ok=dir_exist_ok,
                timestamp=timestamp,
                time_prefix=time_prefix,
                verbose=verbose,
                clear=clear,
                **kwargs,
            )

    def log_to_csv(
        self,
        filename: str = "log.csv",
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        **kwargs,
    ):
        """
        Saves the branch's log data to a CSV file.

        This method is designed to export log data, potentially including operations and intertools,
        to a CSV file for analysis or record-keeping.

        Args:
                filename (str): The name of the output CSV file. Defaults to 'log.csv'.
                dir_exist_ok (bool): If True, will not raise an error if the directory already exists. Defaults to True.
                timestamp (bool): If True, appends a timestamp to the filename for uniqueness. Defaults to True.
                time_prefix (bool): If True, adds a timestamp prefix to the filename. Defaults to False.
                verbose (bool): If True, prints a success message upon completion. Defaults to True.
                clear (bool): If True, clears the log after saving. Defaults to True.
                **kwargs: Additional keyword arguments for `DataFrame.to_csv()`.

        Examples:
                >>> branch.log_to_csv("branch_log.csv")
                >>> branch.log_to_csv("detailed_branch_log.csv", timestamp=True, verbose=True)
        """
        for name, branch in self.branches.items():
            f_name = f"{name}_{filename}"
            branch.log_to_csv(
                filename=f_name,
                dir_exist_ok=dir_exist_ok,
                timestamp=timestamp,
                time_prefix=time_prefix,
                verbose=verbose,
                clear=clear,
                **kwargs,
            )

    def log_to_json(
        self,
        filename: str = "log.json",
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        **kwargs,
    ):
        """
        Saves the branch's log data to a JSON file.

        Useful for exporting log data in JSON format, allowing for easy integration with web applications
        and services that consume JSON.

        Args:
                filename (str): The name of the output JSON file. Defaults to 'log.json'.
                dir_exist_ok (bool): If directory existence should not raise an error. Defaults to True.
                timestamp (bool): If True, appends a timestamp to the filename. Defaults to True.
                time_prefix (bool): If True, adds a timestamp prefix to the filename. Defaults to False.
                verbose (bool): If True, prints a success message upon completion. Defaults to True.
                clear (bool): If True, clears the log after saving. Defaults to True.
                **kwargs: Additional keyword arguments for `DataFrame.to_json()`.

        Examples:
                >>> branch.log_to_json("branch_log.json")
                >>> branch.log_to_json("detailed_branch_log.json", verbose=True, timestamp=True)
        """
        for name, branch in self.branches.items():
            f_name = f"{name}_{filename}"
            branch.log_to_json(
                filename=f_name,
                dir_exist_ok=dir_exist_ok,
                timestamp=timestamp,
                time_prefix=time_prefix,
                verbose=verbose,
                clear=clear,
                **kwargs,
            )

    @property
    def all_messages(self) -> dataframe.ln_DataFrame:
        """
        return all messages across branches
        """
        dfs = deque()
        for _, v in self.branches.items():
            dfs.append(convert.to_df(v.messages))
        return convert.to_df(convert.to_list(dfs, flatten=True, dropna=True))

    # ----- chatflow ----#
    async def call_chatcompletion(
        self,
        branch: Branch | str | None = None,
        sender: str | None = None,
        with_sender=False,
        **kwargs,
    ):
        """
        Asynchronously calls the chat completion service with the current message queue.

        This method prepares the messages for chat completion, sends the request to the configured service, and handles the response. The method supports additional keyword arguments that are passed directly to the service.

        Args:
                sender (str | None): The name of the sender to be included in the chat completion request. Defaults to None.
                with_sender (bool): If True, includes the sender's name in the messages. Defaults to False.
                **kwargs: Arbitrary keyword arguments passed directly to the chat completion service.

        Examples:
                >>> await branch.call_chatcompletion()
        """
        branch = self.get_branch(branch)
        await branch.call_chatcompletion(
            sender=sender,
            with_sender=with_sender,
            **kwargs,
        )

    async def chat(
        self,
        instruction: dict | list | Instruction | str=None,
        branch: Branch | str | None = None,
        context: dict | list | str = None,
        sender: str | None = None,
        form=None,
        system: dict | list | System | None = None,
        tools: TOOL_TYPE = False,
        out: bool = True,
        invoke: bool = True,
        output_fields=None,
        **kwargs,
    ) -> str | None:
        """
        a chat conversation with LLM, processing instructions and system messages, optionally invoking tools.

        Args:
                branch: The Branch instance to perform chat operations.
                instruction (dict | list | Instruction | str): The instruction for the chat.
                context (Optional[Any]): Additional context for the chat.
                sender (str | None): The sender of the chat message.
                system (Optional[Union[System, str, dict[str, Any]]]): System message to be processed.
                tools (Union[bool, Tool, List[Tool], str, List[str]]): Specifies tools to be invoked.
                out (bool): If True, outputs the chat response.
                invoke (bool): If True, invokes tools as part of the chat.
                **kwargs: Arbitrary keyword arguments for chat completion.

        Examples:
                >>> await ChatFlow.chat(branch, "Ask about user preferences")
        """

        branch = self.get_branch(branch)
        return await branch.chat(
            instruction=instruction,
            context=context,
            sender=sender,
            system=system,
            tools=tools,
            form=form,
            out=out,
            invoke=invoke,
            output_fields=output_fields,
            **kwargs,
        )

    async def ReAct(
        self,
        instruction: dict | list | Instruction | str,
        branch: Branch | str | None = None,
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
        """
        Performs a reason-tool cycle with optional tool invocation over multiple rounds.

        Args:
                branch: The Branch instance to perform ReAct operations.
                instruction (dict | list | Instruction | str): Initial instruction for the cycle.
                context: Context relevant to the instruction.
                sender (str | None): Identifier for the message sender.
                system: Initial system message or configuration.
                tools: Tools to be registered or used during the cycle.
                num_rounds (int): Number of reason-tool cycles to perform.
                **kwargs: Additional keyword arguments for customization.

        Examples:
                >>> await ChatFlow.ReAct(branch, "Analyze user feedback", num_rounds=2)
        """
        branch = self.get_branch(branch)

        return await branch.ReAct(
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
        instruction: dict | list | Instruction | str,
        branch=None,
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
        """
        Automatically performs follow-up tools based on chat intertools and tool invocations.

        Args:
                branch: The Branch instance to perform follow-up operations.
                instruction (dict | list | Instruction | str): The initial instruction for follow-up.
                context: Context relevant to the instruction.
                sender (str | None): Identifier for the message sender.
                system: Initial system message or configuration.
                tools: Specifies tools to be considered for follow-up tools.
                max_followup (int): Maximum number of follow-up chats allowed.
                out (bool): If True, outputs the result of the follow-up tool.
                **kwargs: Additional keyword arguments for follow-up customization.

        Examples:
                >>> await ChatFlow.auto_followup(branch, "Finalize report", max_followup=2)
        """
        branch = self.get_branch(branch)
        return await branch.followup(
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
        include_mapping=False,
        **kwargs,
    ):
        """
        parallel chat
        """

        if branch_config is None:
            branch_config = {}
        flow = PolyChat(self)

        return await flow.parallel_chat(
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
            include_mapping=include_mapping,
            **kwargs,
        )

    # ---- branch manipulation ---- #
    def new_branch(
        self,
        branch_name: str,
        system: dict | list | System | None = None,
        sender: str | None = None,
        messages: dataframe.ln_DataFrame | None = None,
        tool_manager=None,
        service=None,
        llmconfig=None,
        tools: TOOL_TYPE = False,
    ) -> None:
        """Create a new branch with the specified configurations.

        Args:
                branch_name (str | None): Name of the new branch.
                system (Optional[Union[System, str]]): System or context identifier for the new branch.
                sender (str | None): Default sender identifier for the new branch.
                messages (Optional[dataframe.ln_DataFrame]): Initial set of messages for the new branch.
                instruction_sets (Optional[Any]): Instruction sets for the new branch.
                tool_manager (Optional[Any]): Tool manager for handling tools in the new branch.
                service (BaseService]): External service instance for the ne | None branch.
                llmconfig (dict[str, Any] | None): Configuration for language learning models in the new branch.
                tools (TOOL_TYPE | None): List of tools available for the new branch.

        Raises:
                ValueError: If the branch name already exists.

        Examples:
                >>> session.new_branch("new_branch_name")
        """
        if branch_name in self.branches.keys():
            raise ValueError(
                f"Invalid new branch name {branch_name}. Branch already existed."
            )
        if isinstance(tools, Tool):
            tools = [tools]
        new_branch = Branch(
            name=branch_name,
            messages=messages,
            tool_manager=tool_manager,
            service=service,
            llmconfig=llmconfig,
            tools=tools,
        )
        if system:
            new_branch.add_message(system=system, sender=sender)

        self.branches[branch_name] = new_branch
        self.mail_manager.sources[branch_name] = new_branch
        self.mail_manager.mails[branch_name] = {}

    def get_branch(
        self, branch: Branch | str | None = None, get_name: bool = False
    ) -> Branch | Tuple[Branch, str]:
        """
        Retrieve a branch by name or instance.

        Args:
                branch (Optional[Branch | str]): The branch name or instance to retrieve.
                get_name (bool): If True, returns a tuple of the branch instance and its name.

        Returns:
                Union[Branch, Tuple[Branch, str]]: The branch instance or a tuple of the branch instance and its name.

        Raises:
                ValueError: If the branch name does not exist or the branch input is invalid.

        Examples:
                >>> branch_instance = session.get_branch("existing_branch_name")
                >>> branch_instance, branch_name = session.get_branch("existing_branch_name", get_name=True)
        """
        if isinstance(branch, str):
            if branch not in self.branches.keys():
                raise ValueError(f"Invalid branch name {branch}. Not exist.")
            return (
                (self.branches[branch], branch) if get_name else self.branches[branch]
            )
        elif isinstance(branch, Branch) and branch in self.branches.values():
            return (branch, branch.name) if get_name else branch
        elif branch is None:
            if get_name:
                return self.default_branch, self.default_branch_name
            return self.default_branch

        else:
            raise ValueError(f"Invalid branch input {branch}.")

    def change_default_branch(self, branch: str | Branch) -> None:
        """Change the default branch of the session.

        Args:
                branch (str | Branch): The branch name or instance to set as the new default.

        Examples:
                >>> session.change_default_branch("new_default_branch")
        """
        branch_, name_ = self.get_branch(branch, get_name=True)
        self.default_branch = branch_
        self.default_branch_name = name_

    def delete_branch(self, branch: Branch | str, verbose: bool = True) -> bool:
        """Delete a branch from the session.

        Args:
                branch (Branch | str): The branch name or instance to delete.
                verbose (bool): If True, prints a message upon deletion.

        Returns:
                bool: True if the branch was successfully deleted.

        Raises:
                ValueError: If attempting to delete the current default branch.

        Examples:
                >>> session.delete_branch("branch_to_delete")
        """
        _, branch_name = self.get_branch(branch, get_name=True)

        if branch_name == self.default_branch_name:
            raise ValueError(
                f"{branch_name} is the current default branch, please switch to another branch before delete it."
            )
        self.branches.pop(branch_name)
        # self.mail_manager.sources.pop(branch_name)
        self.mail_manager.mails.pop(branch_name)
        if verbose:
            print(f"Branch {branch_name} is deleted.")
        return True

    def merge_branch(
        self,
        from_: str | Branch,
        to_branch: str | Branch,
        update: bool = True,
        del_: bool = False,
    ) -> None:
        """Merge messages and settings from one branch to another.

        Args:
                from_ (str | Branch): The source branch name or instance.
                to_branch (str | Branch): The target branch name or instance where the merge will happen.
                update (bool): If True, updates the target branch with the source branch's settings.
                del_ (bool): If True, deletes the source branch after merging.

        Examples:
                >>> session.merge_branch("source_branch", "target_branch", del_=True)
        """
        from_ = self.get_branch(branch=from_)
        to_branch, to_name = self.get_branch(branch=to_branch, get_name=True)
        to_branch.merge_branch(from_, update=update)

        if del_:
            if from_ == self.default_branch:
                self.default_branch_name = to_name
                self.default_branch = to_branch
            self.delete_branch(from_, verbose=False)

    def take_branch(self, branch):
        self.branches[branch.branch_name] = branch
        self.mail_manager.sources[branch.id_] = branch
        self.mail_manager.mails[branch.id_] = {}

    def collect(self, from_: str | Branch | list[str | Branch] | None = None):
        """
        Collects requests from specified branches or from all branches if none are specified.

        This method is intended to aggregate data or requests from one or more branches for processing or analysis.

        Args:
                from_ (Optional[Union[str, Branch, List[str | Branch]]]): The branch(es) from which to collect requests.
                        Can be a single branch name, a single branch instance, a list of branch names, a list of branch instances, or None.
                        If None, requests are collected from all branches.

        Examples:
                >>> session.collect("branch_name")
                >>> session.collect([branch_instance_1, "branch_name_2"])
                >>> session.collect()  # Collects from all branches
        """
        if from_ is None:
            for branch in self.branches.values():
                self.mail_manager.collect(branch.id_)
        else:
            if not isinstance(from_, list):
                from_ = [from_]
            for branch in from_:
                if isinstance(branch, str):
                    branch = self.branches[branch]
                    self.mail_manager.collect(branch.id_)
                elif isinstance(branch, Branch):
                    self.mail_manager.collect(branch.id_)

    def send(self, to_: str | Branch | list[str | Branch] | None = None):
        """
        Sends requests or data to specified branches or to all branches if none are specified.

        This method facilitates the distribution of data or requests to one or more branches, potentially for further tool or processing.

        Args:
                to_ (Optional[Union[str, Branch, List[str | Branch]]]): The target branch(es) to which to send requests.
                        Can be a single branch name, a single branch instance, a list of branch names, a list of branch instances, or None.
                        If None, requests are sent to all branches.

        Examples:
                >>> session.send("target_branch")
                >>> session.send([branch_instance_1, "target_branch_2"])
                >>> session.send()  # Sends to all branches
        """
        if to_ is None:
            for branch in self.branches.values():
                self.mail_manager.send(branch.id_)
        else:
            if not isinstance(to_, list):
                to_ = [to_]
            for branch in to_:
                if isinstance(branch, str):
                    branch = self.branches[branch]
                    self.mail_manager.send(branch.id_)
                if isinstance(branch, Branch):
                    self.mail_manager.send(branch.id_)

    def collect_send_all(self, receive_all=False):
        """
        Collects and sends requests across all branches, optionally invoking a receive operation on each branch.

        This method is a convenience function for performing a full cycle of collect and send operations across all branches,
        useful in scenarios where data or requests need to be aggregated and then distributed uniformly.

        Args:
                receive_all (bool): If True, triggers a `receive_all` method on each branch after sending requests,
                        which can be used to process or acknowledge the received data.

        Examples:
                >>> session.collect_send_all()
                >>> session.collect_send_all(receive_all=True)
        """
        self.collect()
        self.send()
        if receive_all:
            for branch in self.branches.values():
                branch.receive_all()

    def setup_default_branch(self, **kwargs):
        self._setup_default_branch(**kwargs)
        self._verify_default_branch()

    def _verify_default_branch(self):
        if self.branches:
            if self.default_branch_name not in self.branches.keys():
                raise ValueError("default branch name is not in imported branches")
            if self.default_branch is not self.branches[self.default_branch_name]:
                raise ValueError(
                    f"default branch does not match Branch object under {self.default_branch_name}"
                )

        if not self.branches:
            self.branches[self.default_branch_name] = self.default_branch

    def _setup_default_branch(
        self,
        system,
        sender,
        default_branch,
        default_branch_name,
        messages,  # instruction_sets,
        tool_manager,
        service,
        llmconfig,
        tools,
        persist_path,
        datalogger,
    ):

        branch = default_branch or Branch(
            name=default_branch_name,
            service=service,
            llmconfig=llmconfig,
            tools=tools,
            tool_manager=tool_manager,
            # instruction_sets=instruction_sets,
            messages=messages,
            persist_path=persist_path,
            datalogger=datalogger,
        )

        self.default_branch = branch
        self.default_branch_name = default_branch_name or "main"
        if system:
            self.default_branch.add_message(system=system, sender=sender)

        self.llmconfig = self.default_branch.llmconfig
