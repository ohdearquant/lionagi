from abc import ABC
from typing import Any

from lionagi.libs.sys_util import SysUtil, PATH_TYPE

import lionagi.libs.ln_convert as convert
import lionagi.libs.ln_dataframe as dataframe

from lionagi.core.schema.base_node import BaseRelatableNode
from lionagi.core.schema.data_logger import DataLogger
from lionagi.core.messages.schema import (
    BranchColumns,
    System,
    Instruction,
    BaseMessage,
)
from lionagi.core.branch.base.util import MessageUtil
from lionagi.core.branch.base.branch_io_mixin import BranchIOMixin


class BaseBranch(BaseRelatableNode, BranchIOMixin, ABC):
    """
    Base class for managing branches of conversation, incorporating messages
    and logging functionality. This class provides a structured way to store and
    manipulate conversation data, including adding messages, exporting data, and
    filtering messages based on various criteria.

    Attributes:
        messages (dataframe.ln_DataFrame): Holds the messages in the branch.
        datalogger (DataLogger): Logs data related to the branch's operation.
        persist_path (PATH_TYPE): Filesystem path for data persistence.
    """

    _columns: list[str] = BranchColumns.COLUMNS.value

    def __init__(
        self,
        messages: dataframe.ln_DataFrame | None = None,
        datalogger: DataLogger | None = None,
        persist_path: PATH_TYPE | None = None,
        **kwargs,
    ) -> None:
        """Initializes a new instance of the BaseBranch class.

        Sets up the branch with an optional initial set of messages, a data logger for operation logging,
        and a path for data persistence. If messages are not provided, an empty dataframe is initialized
        with predefined columns for message management.

        Args:
            messages (dataframe.ln_DataFrame | None): Initial set of messages for the branch. Defaults to None.
            datalogger (DataLogger | None): Data logger instance for the branch. Defaults to None.
            persist_path (PATH_TYPE | None): Filesystem path for data persistence. Defaults to None.
            **kwargs: Additional keyword arguments.

        Raises:
            ValueError: If the provided messages dataframe is not in the expected format.
        """
        super().__init__(**kwargs)
        if isinstance(messages, dataframe.ln_DataFrame):
            if MessageUtil.validate_messages(messages):
                self.messages = messages
            else:
                raise ValueError("Invalid messages format")
        else:
            self.messages = dataframe.ln_DataFrame(columns=self._columns)

        self.datalogger = (
            datalogger if datalogger else DataLogger(persist_path=persist_path)
        )

    def add_message(
        self,
        system: dict | list | System | None = None,
        instruction: dict | list | Instruction | None = None,
        context: str | dict[str, Any] | None = None,
        response: dict | list | BaseMessage | None = None,
        output_fields: dict | None = None,
        **kwargs,
    ) -> None:
        """
        Adds a message to the branch, enriching the conversation flow.

        Args:
            system (dict | list | System | None): System-related information for the message.
            instruction (dict | list | Instruction | None): Instruction details for the message.
            context (str | dict[str, Any] | None): Contextual information for the message.
            response (dict | list | BaseMessage | None): Response data for the message.
            output_fields (Optional): Specifies fields to be included in the output.
            **kwargs: Additional keyword arguments for custom message creation.

        Raises:
            ValueError: If the messages format is invalid.

        Examples:
            >>> branch.add_message(instruction={'command': 'start'}, context="User action")
        """
        _msg = MessageUtil.create_message(
            system=system,
            instruction=instruction,
            context=context,
            response=response,
            output_fields=output_fields,
            **kwargs,
        )

        _msg.content = _msg.msg_content
        self.messages.loc[len(self.messages)] = _msg.to_pd_series()

    @property
    def chat_messages(self) -> list[dict[str, Any]]:
        """
        Retrieves all chat messages without sender information.

        Returns:
            A list of dictionaries representing chat messages.
        """

        return self._to_chatcompletion_message()

    @property
    def chat_messages_with_sender(self) -> list[dict[str, Any]]:
        """
        Retrieves all chat messages, including sender information.

        Returns:
            A list of dictionaries representing chat messages, each prefixed with its sender.
        """

        return self._to_chatcompletion_message(with_sender=True)

    @property
    def last_message(self) -> dataframe.ln_DataFrame:
        """
        Retrieves the last message from the branch as a pandas Series.

        Returns:
            A pandas Series representing the last message in the branch.
        """

        return MessageUtil.get_message_rows(self.messages, n=1, from_="last")

    @property
    def last_message_content(self) -> dict[str, Any]:
        """
        Extracts the content of the last message in the branch.

        Returns:
            A dictionary representing the content of the last message.
        """

        return convert.to_dict(self.messages.content.iloc[-1])

    @property
    def first_system(self) -> dataframe.ln_DataFrame:
        """
        Retrieves the first message marked with the 'system' role.

        Returns:
            A pandas Series representing the first 'system' message in the branch.
        """

        return MessageUtil.get_message_rows(
            self.messages, role="system", n=1, from_="front"
        )

    @property
    def last_response(self) -> dataframe.ln_DataFrame:
        """
        Retrieves the last message marked with the 'assistant' role.

        Returns:
            A pandas Series representing the last 'assistant' (response) message in the branch.
        """

        return MessageUtil.get_message_rows(
            self.messages, role="assistant", n=1, from_="last"
        )

    @property
    def last_response_content(self) -> dict[str, Any]:
        """
        Extracts the content of the last 'assistant' (response) message.

        Returns:
            A dictionary representing the content of the last 'assistant' message.
        """

        return convert.to_dict(self.last_response.content.iloc[-1])

    @property
    def action_request(self) -> dataframe.ln_DataFrame:
        """
        Filters and retrieves all messages sent by 'action_request'.

        Returns:
            A pandas DataFrame containing all 'action_request' messages.
        """

        return convert.to_df(self.messages[self.messages.sender == "action_request"])

    @property
    def action_response(self) -> dataframe.ln_DataFrame:
        """
        Filters and retrieves all messages sent by 'action_response'.

        Returns:
            A pandas DataFrame containing all 'action_response' messages.
        """

        return convert.to_df(self.messages[self.messages.sender == "action_response"])

    @property
    def responses(self) -> dataframe.ln_DataFrame:
        """
        Retrieves all messages marked with the 'assistant' role.

        Returns:
            A pandas DataFrame containing all messages with an 'assistant' role.
        """

        return convert.to_df(self.messages[self.messages.role == "assistant"])

    @property
    def assistant_responses(self) -> dataframe.ln_DataFrame:
        """
        Filters 'assistant' role messages excluding 'action_request' and 'action_response'.

        Returns:
            A pandas DataFrame of 'assistant' messages excluding action requests/responses.
        """

        a_responses = self.responses[self.responses.sender != "action_response"]
        a_responses = a_responses[a_responses.sender != "action_request"]
        return convert.to_df(a_responses)

    @property
    def info(self) -> dict[str, Any]:
        """
        Summarizes branch information, including message counts by role.

        Returns:
            A dictionary containing counts of messages categorized by their role.
        """

        return self._info()

    @property
    def sender_info(self) -> dict[str, int]:
        """
        Provides a summary of message counts categorized by sender.

        Returns:
            A dictionary with senders as keys and counts of their messages as values.
        """

        return self._info(use_sender=True)

    @property
    def describe(self) -> dict[str, Any]:
        """
        Provides a detailed description of the branch, including a summary of messages.

        Returns:
            A dictionary with a summary of total messages, a breakdown by role, and
            a preview of the first five messages.
        """

        return {
            "total_messages": len(self.messages),
            "summary_by_role": self._info(),
            "summary_by_sender": self._info(use_sender=True),
            "registered_tools": self.tool_manager.registry,
            "messages": [msg.to_dict() for _, msg in self.messages.iterrows()][
                : len(self.messages) - 1 if len(self.messages) < 5 else 5
            ],
        }

    def remove_message(self, node_id: str) -> None:
        """
        Removes a message from the branch based on its node ID.

        Args:
            node_id: The unique identifier of the message to be removed.
        """
        MessageUtil.remove_message(self.messages, node_id)

    def update_message(self, node_id: str, column: str, value: Any) -> bool:
        """
        Updates a specific column of a message identified by node_id with a new value.

        Args:
            value: The new value to update the message with.
            node_id: The unique identifier of the message to update.
            column: The column of the message to update.
        """

        index = self.messages[self.messages["node_id"] == node_id].index[0]

        return dataframe.update_row(
            self.messages, row=index, column=column, value=value
        )

    def change_first_system_message(
        self, system: str | dict[str, Any] | System, sender: str | None = None
    ) -> None:
        """
        Updates the first system message with new content and/or sender.

        Args:
            system: The new system message content or a System object.
            sender: The identifier of the sender for the system message.
        """

        if len(self.messages[self.messages["role"] == "system"]) == 0:
            raise ValueError("There is no system message in the messages.")

        if not isinstance(system, (str, dict, System)):
            raise ValueError("Input cannot be converted into a system message.")

        if isinstance(system, (str, dict)):
            system = System(system, sender=sender)

        if isinstance(system, System):
            system.timestamp = SysUtil.get_timestamp()
            sys_index = self.messages[self.messages.role == "system"].index
            self.messages.loc[sys_index[0]] = system.to_pd_series()

    def rollback(self, steps: int) -> None:
        """
        Removes the last 'n' messages from the branch.

        Args:
            steps: The number of messages to remove from the end.
        """

        self.messages = dataframe.remove_last_n_rows(self.messages, steps)

    def clear_messages(self) -> None:
        """
        Clears all messages from the branch.
        """
        self.messages = dataframe.ln_DataFrame(columns=self._columns)

    def replace_keyword(
        self,
        keyword: str,
        replacement: str,
        column: str = "content",
        case_sensitive: bool = False,
    ) -> None:

        dataframe.replace_keyword(
            self.messages,
            keyword,
            replacement,
            column=column,
            case_sensitive=case_sensitive,
        )

    def search_keywords(
        self,
        keywords: str | list[str],
        case_sensitive: bool = False,
        reset_index: bool = False,
        dropna: bool = False,
    ) -> dataframe.ln_DataFrame:
        return dataframe.search_keywords(
            self.messages,
            keywords,
            case_sensitive=case_sensitive,
            reset_index=reset_index,
            dropna=dropna,
        )

    def extend(self, messages: dataframe.ln_DataFrame, **kwargs) -> None:

        self.messages = MessageUtil.extend(self.messages, messages, **kwargs)

    def filter_by(
        self,
        role: str | None = None,
        sender: str | None = None,
        start_time=None,
        end_time=None,
        content_keywords: str | list[str] | None = None,
        case_sensitive: bool = False,
    ) -> dataframe.ln_DataFrame:

        return MessageUtil.filter_messages_by(
            self.messages,
            role=role,
            sender=sender,
            start_time=start_time,
            end_time=end_time,
            content_keywords=content_keywords,
            case_sensitive=case_sensitive,
        )

    # noinspection PyTestUnpassedFixture
    def _info(self, use_sender: bool = False) -> dict[str, int]:
        """
        Helper method to generate summaries of messages either by role or sender.

        Args:
            use_sender: If True, summary is categorized by sender. Otherwise, by role.

        Returns:
            A dictionary summarizing the count of messages either by role or sender.
        """

        messages = self.messages["sender"] if use_sender else self.messages["role"]
        result = messages.value_counts().to_dict()
        result["total"] = len(self.messages)
        return result

    def _to_chatcompletion_message(
        self, with_sender: bool = False
    ) -> list[dict[str, Any]]:
        """
        Converts messages to a list of dictionaries formatted for chat completion,
        optionally including sender information.

        Args:
            with_sender: Flag to include sender information in the output.

        Returns:
            A list of message dictionaries, each with 'role' and 'content' keys,
            and optionally prefixed by 'Sender' if with_sender is True.
        """

        message = []

        for _, row in self.messages.iterrows():
            content_ = row["content"]
            if content_.startswith("Sender"):
                content_ = content_.split(":", 1)[1]

            out = {"role": row["role"], "content": content_}
            if with_sender:
                out["content"] = f"Sender {row['sender']}: {content_}"

            message.append(out)
        return message