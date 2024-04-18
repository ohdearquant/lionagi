from enum import Enum

from lionagi.libs import nested, convert
from lionagi.core.generic import Node
from working.form import Form

_message_fields = ["node_id", "timestamp", "role", "sender", "recipient", "content"]


class BranchColumns(list[str], Enum):
    COLUMNS = _message_fields


class MessageField(str, Enum):
    NODE_ID = "node_id"
    TIMESTAMP = "timestamp"
    ROLE = "role"
    SENDER = "sender"
    RECIPIENT = "recipient"
    CONTENT = "content"
    METADATA = "metadata"
    RELATION = "relation"


class MessageRoleType(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class MessageContentKey(str, Enum):
    INSTRUCTION = "instruction"
    CONTEXT = "context"
    SYSTEM = "system_info"
    ACTION_REQUEST = "action_request"
    ACTION_RESPONSE = "action_response"
    RESPONSE = "response"


class MessageType(dict, Enum):
    SYSTEM = (
        {
            MessageField.ROLE.value: MessageRoleType.SYSTEM.value,
            MessageField.SENDER.value: MessageRoleType.SYSTEM.value,
            MessageField.RECIPIENT.value: "null",
            "content_key": MessageContentKey.SYSTEM.value,
        },
    )

    INSTRUCTION = (
        {
            MessageField.ROLE.value: MessageRoleType.USER.value,
            MessageField.SENDER.value: MessageRoleType.USER.value,
            MessageField.RECIPIENT.value: "null",
            "content_key": MessageContentKey.INSTRUCTION.value,
        },
    )

    CONTEXT = (
        {
            MessageField.ROLE.value: MessageRoleType.USER.value,
            MessageField.SENDER.value: MessageRoleType.USER.value,
            MessageField.RECIPIENT.value: "null",
            "content_key": MessageContentKey.CONTEXT.value,
        },
    )

    ACTION_REQUEST = (
        {
            MessageField.ROLE.value: MessageRoleType.ASSISTANT.value,
            MessageField.SENDER.value: MessageRoleType.ASSISTANT.value,
            MessageField.RECIPIENT.value: "null",
            "content_key": MessageContentKey.ACTION_REQUEST.value,
        },
    )

    ACTION_RESPONSE = (
        {
            MessageField.ROLE.value: MessageRoleType.ASSISTANT.value,
            MessageField.SENDER.value: MessageRoleType.ASSISTANT.value,
            MessageField.RECIPIENT.value: "null",
            "content_key": MessageContentKey.ACTION_RESPONSE.value,
        },
    )

    RESPONSE = {
        MessageField.ROLE.value: MessageRoleType.ASSISTANT.value,
        MessageField.SENDER.value: MessageRoleType.ASSISTANT.value,
        MessageField.RECIPIENT.value: "null",
        "content_key": MessageContentKey.RESPONSE.value,
    }


class BaseMessage(Node):
    """
    Represents a message in a chatbot-like system, inheriting from BaseNode.

    Attributes:
        role (str | None): The role of the entity sending the message, e.g., 'user', 'system'.
        sender (str | None): The identifier of the sender of the message.
        content (Any): The actual content of the message.
    """

    role: str | None = None
    sender: str | None = None
    recipient: str | None = None

    @property
    def msg(self) -> dict:
        """
        Constructs and returns a dictionary representation of the message.

        Returns:
            A dictionary representation of the message with 'role' and 'content' keys.
        """
        return self._to_message()

    @property
    def msg_content(self) -> str | dict:
        """
        Gets the 'content' field of the message.

        Returns:
            The 'content' part of the message.
        """
        return self.msg["content"]

    def _to_message(self):
        """
        Constructs and returns a dictionary representation of the message.

        Returns:
            dict: A dictionary representation of the message with 'role' and 'content' keys.
        """
        return {"role": self.role, "content": convert.to_str(self.content)}

    def __str__(self):
        content_preview = (
            f"{str(self.content)[:75]}..."
            if self.content and len(self.content) > 75
            else str(self.content)
        )
        return f"Message(role={self.role}, sender={self.sender}, content='{content_preview}')"


class Instruction(BaseMessage):
    """
    Represents an instruction message, a specialized subclass of Message.

    This class is specifically used for creating messages that are instructions from the user,
    including any associated context. It sets the message role to 'user'.
    """

    def __init__(
        self,
        instruction: dict | list | str,
        context=None,
        sender: str | None = None,
        output_fields=None,
        recipient=None,
    ):
        super().__init__(
            role="user",
            sender=sender or "user",
            content={"instruction": instruction},
            recipient=recipient or "assistant",
        )
        if context:
            self.content.update({"context": context})

        if output_fields:
            format_ = f"""
            MUST EXACTLY Follow the following response format. NO ADDITIONAL COMMENTS ALLOWED!
            ```json
            {output_fields}
            ```
            """
            self.content.update(
                {"response_format": format_.replace("            ", "")}
            )

    @property
    def instruct(self):
        return self.content["instruction"]

    @classmethod
    def from_form(
        cls,
        form: Form,
        sender: str | None = None,
        recipient=None,
    ):
        return cls(
            instruction=form.instruction,
            context=form.instruction_context,
            output_fields=form.instruction_output_fields,
            sender=sender,
            recipient=recipient,
        )


class System(BaseMessage):
    """
    Represents a system-related message, a specialized subclass of Message.

    Designed for messages containing system information, this class sets the message role to 'system'.
    """

    def __init__(
        self, system: dict | list | str, sender: str | None = None, recipient=None
    ):
        super().__init__(
            role="system",
            sender=sender or "system",
            content={"system_info": system},
            recipient=recipient or "assistant",
        )

    @property
    def system_info(self):
        return self.content["system_info"]


class Response(BaseMessage):
    """
    Represents a response message, a specialized subclass of Message.

    Used for various types of response messages including regular responses, action requests,
    and action responses. It sets the message role to 'assistant'.

    """

    def __init__(
        self, response: dict | list | str, sender: str | None = None, recipient=None
    ) -> None:
        content_key = ""
        try:
            response = response["message"]
            if convert.strip_lower(response["content"]) == "none":
                content_ = self._handle_action_request(response)
                sender = sender or "action_request"
                content_key = content_key or "action_request"
                recipient = recipient or "action"

            else:
                try:
                    if "tool_uses" in convert.to_dict(response["content"]):
                        content_ = convert.to_dict(response["content"])["tool_uses"]
                        content_key = content_key or "action_request"
                        sender = sender or "action_request"
                        recipient = recipient or "action"

                    elif "response" in convert.to_dict(response["content"]):
                        sender = sender or "assistant"
                        content_key = content_key or "response"
                        content_ = convert.to_dict(response["content"])["response"]
                        recipient = recipient or "user"

                    elif "action_request" in convert.to_dict(response["content"]):
                        sender = sender or "action_request"
                        content_key = content_key or "action_request"
                        content_ = convert.to_dict(response["content"])[
                            "action_request"
                        ]
                        recipient = recipient or "action"

                    else:
                        content_ = response["content"]
                        content_key = content_key or "response"
                        sender = sender or "assistant"
                        recipient = recipient or "user"
                except Exception:
                    content_ = response["content"]
                    content_key = content_key or "response"
                    sender = sender or "assistant"
                    recipient = recipient or "user"

        except Exception:
            sender = sender or "action_response"
            content_ = response
            content_key = content_key or "action_response"
            recipient = recipient or "assistant"

        super().__init__(
            role="assistant",
            sender=sender,
            content={content_key: content_},
            recipient=recipient,
        )

    @staticmethod
    def _handle_action_request(response):
        """
        Processes an action request response and extracts relevant information.

        Args:
            response (dict): The response dictionary containing tool calls and other information.

        Returns:
            list: A list of dictionaries, each representing a function call with action and arguments.

        Raises:
            ValueError: If the response does not conform to the expected format for action requests.
        """
        try:
            tool_count = 0
            func_list = []
            while tool_count < len(response["tool_calls"]):
                _path = ["tool_calls", tool_count, "type"]

                if nested.nget(response, _path) == "function":
                    _path1 = ["tool_calls", tool_count, "function", "name"]
                    _path2 = ["tool_calls", tool_count, "function", "arguments"]

                    func_content = {
                        "action": f"action_{nested.nget(response, _path1)}",
                        "arguments": nested.nget(response, _path2),
                    }
                    func_list.append(func_content)
                tool_count += 1
            return func_list
        except:
            raise ValueError(
                "Response message must be one of regular response or function calling"
            )
