import contextlib
from datetime import datetime
from typing import Any

from lionagi.libs import convert, nested, func_call, dataframe

from lionagi.core.messages.schema import (
    System,
    Instruction,
    Response,
    BaseMessage,
    BranchColumns,
)

CUSTOM_TYPE = dict[str, Any] | str | list[Any] | None


class MessageUtil:

    @staticmethod
    def create_message(
        system: System | CUSTOM_TYPE = None,
        instruction: Instruction | CUSTOM_TYPE = None,
        context: str | dict[str, Any] | None = None,
        response: Response | CUSTOM_TYPE = None,
        output_fields=None,
        **kwargs,
    ) -> BaseMessage:
        """
        Creates a message object based on the input parameters, ensuring only one message role is present.

        Args:
                system: Information for creating a System message.
                instruction: Information for creating an Instruction message.
                context: Context information for the message.
                response: Response data for creating a message.
                **kwargs: Additional keyword arguments for message creation.

        Returns:
                A message object of the appropriate type based on provided inputs.

        Raises:
                ValueError: If more than one of the role-specific parameters are provided.
        """
        if sum(func_call.lcall([system, instruction, response], bool)) != 1:
            raise ValueError("Error: Message must have one and only one role.")

        if isinstance(system, System):
            return system
        elif isinstance(instruction, Instruction):
            return instruction
        elif isinstance(response, Response):
            return response

        msg = 0
        if response:
            msg = Response(response=response, **kwargs)
        elif instruction:
            msg = Instruction(
                instruction=instruction,
                context=context,
                output_fields=output_fields,
                **kwargs,
            )
        elif system:
            msg = System(system=system, **kwargs)
        return msg

    @staticmethod
    def validate_messages(messages: dataframe.ln_DataFrame) -> bool:
        """
        Validates the format and content of a DataFrame containing messages.

        Args:
                messages: A DataFrame with message information.

        Returns:
                True if the messages DataFrame is correctly formatted, False otherwise.

        Raises:
                ValueError: If the DataFrame does not match expected schema or content requirements.
        """

        if list(messages.columns) != BranchColumns.COLUMNS.value:
            raise ValueError("Invalid messages dataframe. Unmatched columns.")
        if messages.isnull().values.any():
            raise ValueError("Invalid messages dataframe. Cannot have null.")
        if any(
            role not in ["system", "user", "assistant"]
            for role in messages["role"].unique()
        ):
            raise ValueError(
                'Invalid messages dataframe. Cannot have role other than ["system", "user", "assistant"].'
            )
        for cont in messages["content"]:
            if cont.startswith("Sender"):
                cont = cont.split(":", 1)[1]
            try:
                convert.to_dict(cont)
            except:
                raise ValueError(
                    "Invalid messages dataframe. Content expect json string."
                )
        return True

    @staticmethod
    def sign_message(
        messages: dataframe.ln_DataFrame, sender: str
    ) -> dataframe.ln_DataFrame:
        """
        Appends a sender prefix to the 'content' field of each message in a DataFrame.

        Args:
                messages: A DataFrame containing message data.
                sender: The identifier of the sender to prefix to message contents.

        Returns:
                A DataFrame with sender-prefixed message contents.

        Raises:
                ValueError: If the sender is None or the value is 'none'.
        """

        if sender is None or convert.strip_lower(sender) == "none":
            raise ValueError("sender cannot be None")
        df = convert.to_df(messages)

        for i in df.index:
            if not df.loc[i, "content"].startswith("Sender"):
                df.loc[i, "content"] = f"Sender {sender}: {df.loc[i, 'content']}"
            else:
                content = df.loc[i, "content"].split(":", 1)[1]
                df.loc[i, "content"] = f"Sender {sender}: {content}"

        return convert.to_df(df)

    @staticmethod
    def filter_messages_by(
        messages: dataframe.ln_DataFrame,
        role: str | None = None,
        sender: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        content_keywords: str | list[str] | None = None,
        case_sensitive: bool = False,
    ) -> dataframe.ln_DataFrame:
        """
        Filters messages in a DataFrame based on specified criteria.

        Args:
                messages: The DataFrame to filter.
                role: The role to filter by.
                sender: The sender to filter by.
                start_time: The minimum timestamp for messages.
                end_time: The maximum timestamp for messages.
                content_keywords: Keywords to look for in message content.
                case_sensitive: Whether the keyword search should be case-sensitive.

        Returns:
                A filtered DataFrame based on the specified criteria.
        """

        try:
            outs = messages.copy()

            if content_keywords:
                outs = MessageUtil.search_keywords(
                    outs, keywords=content_keywords, case_sensitive=case_sensitive
                )

            outs = outs[outs["role"] == role] if role else outs
            outs = outs[outs["sender"] == sender] if sender else outs
            outs = outs[outs["timestamp"] > start_time] if start_time else outs
            outs = outs[outs["timestamp"] < end_time] if end_time else outs

            return convert.to_df(outs)

        except Exception as e:
            raise ValueError(f"Error in filtering messages: {e}") from e

    @staticmethod
    def remove_message(messages: dataframe.ln_DataFrame, node_id: str) -> bool:
        """
        Removes a message from the DataFrame based on its node ID.

        Args:
                messages: The DataFrame containing messages.
                node_id: The unique identifier of the message to be removed.

        Returns:
                If any messages are removed.

        Examples:
                >>> messages = dataframe.ln_DataFrame([...])
                >>> updated_messages = MessageUtil.remove_message(messages, "node_id_123")
        """

        initial_length = len(messages)
        messages.drop(messages[messages["node_id"] == node_id].index, inplace=True)
        messages.reset_index(drop=True, inplace=True)

        return len(messages) < initial_length

    @staticmethod
    def get_message_rows(
        messages: dataframe.ln_DataFrame,
        sender: str | None = None,
        role: str | None = None,
        n: int = 1,
        sign_: bool = False,
        from_: str = "front",
    ) -> dataframe.ln_DataFrame:
        """
        Retrieves a specified number of message rows based on sender and role.

        Args:
                messages: The DataFrame containing messages.
                sender: Filter messages by the sender.
                role: Filter messages by the role.
                n: The number of messages to retrieve.
                sign_: If True, sign the message with the sender.
                from_: Specify retrieval from the 'front' or 'last' of the DataFrame.

        Returns:
                A DataFrame containing the filtered messages.
        """

        outs = ""

        if from_ == "last":
            if sender is None and role is None:
                outs = messages.iloc[-n:]
            elif sender and role:
                outs = messages[
                    (messages["sender"] == sender) & (messages["role"] == role)
                ].iloc[-n:]

            elif sender:
                outs = messages[messages["sender"] == sender].iloc[-n:]
            else:
                outs = messages[messages["role"] == role].iloc[-n:]

        elif from_ == "front":
            if sender is None and role is None:
                outs = messages.iloc[:n]
            elif sender and role:
                outs = messages[
                    (messages["sender"] == sender) & (messages["role"] == role)
                ].iloc[:n]
            elif sender:
                outs = messages[messages["sender"] == sender].iloc[:n]
            else:
                outs = messages[messages["role"] == role].iloc[:n]

        return MessageUtil.sign_message(outs, sender) if sign_ else outs

    @staticmethod
    def extend(
        df1: dataframe.ln_DataFrame, df2: dataframe.ln_DataFrame, **kwargs
    ) -> dataframe.ln_DataFrame:
        """
        Extends a DataFrame with another DataFrame's rows, ensuring no duplicate 'node_id'.

        Args:
                df1: The primary DataFrame.
                df2: The DataFrame to merge with the primary DataFrame.
                **kwargs: Additional keyword arguments for `drop_duplicates`.

        Returns:
                A DataFrame combined from df1 and df2 with duplicates removed based on 'node_id'.

        Examples:
                >>> df_main = dataframe.ln_DataFrame([...])
                >>> df_additional = dataframe.ln_DataFrame([...])
                >>> combined_df = MessageUtil.extend(df_main, df_additional, keep='first')
        """

        MessageUtil.validate_messages(df2)
        try:
            if len(df2.dropna(how="all")) > 0 and len(df1.dropna(how="all")) > 0:
                df = convert.to_df([df1, df2])
                df.drop_duplicates(
                    inplace=True, subset=["node_id"], keep="first", **kwargs
                )
                return convert.to_df(df)
        except Exception as e:
            raise ValueError(f"Error in extending messages: {e}") from e

    @staticmethod
    def to_markdown_string(messages: dataframe.ln_DataFrame) -> str:
        """
        Converts messages in a DataFrame to a Markdown-formatted string for easy reading.

        Args:
                messages: A DataFrame containing messages with columns for 'role' and 'content'.

        Returns:
                A string formatted in Markdown, where each message's content is presented
                according to its role in a readable format.
        """

        answers = []
        for _, i in messages.iterrows():
            content = convert.to_dict(i.content)

            if i.role == "assistant":
                with contextlib.suppress(Exception):
                    a = nested.nget(content, ["action_response", "func"])
                    b = nested.nget(content, ["action_response", "arguments"])
                    c = nested.nget(content, ["action_response", "output"])
                    if a is not None:
                        answers.extend(
                            (f"Function: {a}", f"Arguments: {b}", f"Output: {c}")
                        )
                    else:
                        answers.append(nested.nget(content, ["assistant_response"]))
            elif i.role == "user":
                with contextlib.suppress(Exception):
                    answers.append(nested.nget(content, ["instruction"]))
            else:
                with contextlib.suppress(Exception):
                    answers.append(nested.nget(content, ["system_info"]))
        return "\n".join(answers)
