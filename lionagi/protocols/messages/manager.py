# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from .._concepts import Manager
from ..generic.pile import Pile
from ..generic.progression import Progression
from .action_request import ActionRequest
from .action_response import ActionResponse
from .assistant_response import AssistantResponse
from .instruction import Instruction
from .message import Message, SenderRecipient
from .system import System

DEFAULT_SYSTEM = "You are a helpful AI assistant. Let's think step by step."


class MessageManager(Manager):
    """
    A manager maintaining an ordered list of `Message` items.
    Capable of setting or replacing a system message, adding instructions,
    assistant responses, or actions, and retrieving them conveniently.
    """

    def __init__(
        self,
        messages: list[Message] | None = None,
        progression: Progression | None = None,
        system: System | None = None,
    ):
        super().__init__()
        m_ = []
        # Attempt to parse 'messages' as a list or from a dictionary
        if isinstance(messages, list):
            for i in messages:
                if isinstance(i, dict):
                    i = Message.from_dict(i)
                if isinstance(i, Message):
                    m_.append(i)
        if isinstance(messages, dict):
            self.messages = Pile.from_dict(messages)
        else:
            self.messages: Pile[Message] = Pile(
                collections=m_,
                item_type={Message},
                strict_type=False,
                progression=progression,
            )
        if system and not isinstance(system, System):
            raise ValueError("System message must be a System instance.")
        self.system = system  # system must be the first message
        if self.system:
            self.add_message(system=self.system)

    @property
    def progression(self) -> Progression:
        return self.messages.progression

    def set_system(self, system: System) -> None:
        """
        Replace or set the system message. If one existed, remove it.
        """
        if not self.system:
            self.system = system
            self.messages.insert(0, self.system)
        else:
            old_system = self.system
            self.system = system
            self.messages.insert(0, self.system)
            self.messages.exclude(old_system)

    async def aclear_messages(self):
        """Async clear all messages except system."""
        async with self.messages:
            self.clear_messages()

    async def a_add_message(self, **kwargs):
        """Add a message asynchronously with a manager-level lock."""
        async with self.messages:
            return self.add_message(**kwargs)

    @staticmethod
    def create_instruction(
        *,
        instruction: JsonValue = None,
        context: JsonValue = None,
        handle_context: Literal["extend", "replace"] = "extend",
        guidance: JsonValue = None,
        images: list = None,
        request_fields: JsonValue = None,
        plain_content: JsonValue = None,
        image_detail: Literal["low", "high", "auto"] = None,
        request_model: BaseModel | type[BaseModel] = None,
        response_format: BaseModel | type[BaseModel] = None,
        tool_schemas: dict = None,
        sender: SenderRecipient = None,
        recipient: SenderRecipient = None,
    ) -> Instruction:
        """
        Construct or update an Instruction message with advanced parameters.

        If `instruction` is an existing Instruction, it is updated in place.
        Otherwise, a new instance is created.
        """
        raw_params = {
            k: v for k, v in locals().items() if k != "instruction" and v is not None
        }

        handle_ctx = raw_params.get("handle_context", "extend")

        if isinstance(instruction, Instruction):
            params = {k: v for k, v in raw_params.items() if k != "handle_context"}
            ctx_value = params.pop("context", None)
            if ctx_value is not None:
                if isinstance(ctx_value, list):
                    ctx_list = list(ctx_value)
                else:
                    ctx_list = [ctx_value]
                if handle_ctx == "extend":
                    merged = list(instruction.content.prompt_context)
                    merged.extend(ctx_list)
                    params["context"] = merged
                else:
                    params["context"] = list(ctx_list)
                # Always replace in from_dict since we've already done the merge logic
                params["handle_context"] = "replace"
            instruction.update(**params)
            return instruction
        else:
            # Build content dict for Instruction
            content_dict = {
                k: v for k, v in raw_params.items() if k not in ["sender", "recipient"]
            }
            content_dict["handle_context"] = handle_ctx
            if instruction is not None:
                content_dict["instruction"] = instruction
            return Instruction(
                content=content_dict,
                sender=raw_params.get("sender"),
                recipient=raw_params.get("recipient"),
            )

    @staticmethod
    def create_assistant_response(
        *,
        sender: Any = None,
        recipient: Any = None,
        assistant_response: AssistantResponse | Any = None,
    ) -> AssistantResponse:
        """
        Build or update an `AssistantResponse`. If `assistant_response` is an
        existing instance, it's updated. Otherwise, a new one is created.
        """
        params = {
            k: v
            for k, v in locals().items()
            if k != "assistant_response" and v is not None
        }

        if isinstance(assistant_response, AssistantResponse):
            assistant_response.update(**params)
            return assistant_response

        # Create new AssistantResponse
        content_dict = (
            {"assistant_response": assistant_response} if assistant_response else {}
        )
        return AssistantResponse(
            content=content_dict,
            sender=params.get("sender"),
            recipient=params.get("recipient"),
        )

    @staticmethod
    def create_action_request(
        *,
        sender: SenderRecipient = None,
        recipient: SenderRecipient = None,
        function: str = None,
        arguments: dict[str, Any] = None,
        action_request: ActionRequest | None = None,
    ) -> ActionRequest:
        """
        Build or update an ActionRequest.

        Args:
            sender: Sender role or ID.
            recipient: Recipient role or ID.
            function: Function name for the request.
            arguments: Arguments for the function.
            action_request: Possibly existing ActionRequest to update.

        Returns:
            ActionRequest: The new or updated request object.
        """
        params = {
            k: v for k, v in locals().items() if k != "action_request" and v is not None
        }

        if isinstance(action_request, ActionRequest):
            action_request.update(**params)
            return action_request

        # Create new ActionRequest
        content_dict = {}
        if function:
            content_dict["function"] = function
        if arguments:
            content_dict["arguments"] = arguments
        return ActionRequest(
            content=content_dict,
            sender=params.get("sender"),
            recipient=params.get("recipient"),
        )

    @staticmethod
    def create_action_response(
        *,
        action_request: ActionRequest,
        action_output: Any = None,
        action_response: ActionResponse | Any = None,
        sender: SenderRecipient = None,
        recipient: SenderRecipient = None,
    ) -> ActionResponse:
        """
        Create or update an ActionResponse, referencing a prior ActionRequest.

        Args:
            action_request (ActionRequest):
                The request being answered.
            action_output (Any):
                The result of the invoked function.
            action_response (ActionResponse|Any):
                Possibly existing ActionResponse to update.
            sender:
                Sender ID or role.
            recipient:
                Recipient ID or role.

        Returns:
            ActionResponse: The newly created or updated response object.
        """
        if not isinstance(action_request, ActionRequest):
            raise ValueError(
                "Error: please provide a corresponding action request for an action response."
            )
        if isinstance(action_response, ActionResponse):
            action_response.update(
                output=action_output, sender=sender, recipient=recipient
            )
            return action_response

        # Create new ActionResponse
        content_dict = {
            "function": action_request.content.function,
            "arguments": action_request.content.arguments,
            "output": action_output,
            "action_request_id": str(action_request.id),
        }
        response = ActionResponse(
            content=content_dict, sender=sender, recipient=recipient
        )

        # Update the request to reference this response
        action_request.content.action_response_id = str(response.id)

        return response

    @staticmethod
    def create_system(
        *,
        system: Any = None,
        system_datetime: bool | str = None,
        sender: Any = None,
        recipient: Any = None,
    ) -> System:
        """
        Create or update a `System` message. If `system` is an instance, update.
        Otherwise, create a new System message.
        """
        params = {k: v for k, v in locals().items() if k != "system" and v is not None}

        if isinstance(system, System):
            system.update(**params)
            return system

        # Create new System message
        content_dict = {}
        if system:
            content_dict["system_message"] = system
        if system_datetime is not None:
            content_dict["system_datetime"] = system_datetime

        return System(
            content=content_dict if content_dict else None,
            sender=params.get("sender"),
            recipient=params.get("recipient"),
        )

    @staticmethod
    def create_message(
        # common
        sender: SenderRecipient = None,
        recipient: SenderRecipient = None,
        metadata: dict[str, Any] = None,
        # instruction
        instruction: JsonValue = None,
        context: JsonValue = None,
        handle_context: Literal["extend", "replace"] = "extend",
        guidance: JsonValue = None,
        request_fields: JsonValue = None,
        plain_content: JsonValue = None,
        request_model: BaseModel | type[BaseModel] = None,
        response_format: BaseModel | type[BaseModel] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        tool_schemas: dict = None,
        # system
        system: Any = None,
        system_datetime: bool | str = None,
        # assistant_response
        assistant_response: AssistantResponse | Any = None,
        # actions
        action_function: str = None,
        action_arguments: dict[str, Any] = None,
        action_output: Any = None,
        action_request: ActionRequest | None = None,
        action_response: ActionResponse | Any = None,
    ):
        message_types = [instruction, assistant_response, system]
        if action_request and not action_output:
            message_types.append(action_request)

        if sum(bool(x) for x in message_types) > 1:
            raise ValueError("Only one message type can be added at a time.")

        _msg = None
        if system:
            _msg = MessageManager.create_system(
                system=system,
                system_datetime=system_datetime,
                sender=sender,
                recipient=recipient,
            )

        elif action_output:
            _msg = MessageManager.create_action_response(
                action_request=action_request,
                action_output=action_output,
                action_response=action_response,
                sender=sender,
                recipient=recipient,
            )
        elif action_request or (action_function and action_arguments is not None):
            _msg = MessageManager.create_action_request(
                sender=sender,
                recipient=recipient,
                function=action_function,
                arguments=action_arguments,
                action_request=action_request,
            )

        elif assistant_response:
            _msg = MessageManager.create_assistant_response(
                sender=sender,
                recipient=recipient,
                assistant_response=assistant_response,
            )

        else:
            _msg = MessageManager.create_instruction(
                instruction=instruction,
                context=context,
                handle_context=handle_context,
                guidance=guidance,
                images=images,
                request_fields=request_fields,
                plain_content=plain_content,
                image_detail=image_detail,
                request_model=request_model,
                response_format=response_format,
                tool_schemas=tool_schemas,
                sender=sender,
                recipient=recipient,
            )

        if metadata:
            _msg.metadata.setdefault("extra", {})
            _msg.metadata["extra"].update(metadata)
        return _msg

    def add_message(
        self,
        *,
        # common
        sender: SenderRecipient = None,
        recipient: SenderRecipient = None,
        metadata: dict[str, Any] = None,
        # instruction
        instruction: JsonValue = None,
        context: JsonValue = None,
        handle_context: Literal["extend", "replace"] = "extend",
        guidance: JsonValue = None,
        request_fields: JsonValue = None,
        plain_content: JsonValue = None,
        request_model: BaseModel | type[BaseModel] = None,
        response_format: BaseModel | type[BaseModel] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        tool_schemas: dict = None,
        # system
        system: Any = None,
        system_datetime: bool | str = None,
        # assistant_response
        assistant_response: AssistantResponse | Any = None,
        # actions
        action_function: str = None,
        action_arguments: dict[str, Any] = None,
        action_output: Any = None,
        action_request: ActionRequest | None = None,
        action_response: ActionResponse | Any = None,
    ) -> Message:
        """
        The central method to add a new message of various types:
        - System
        - Instruction
        - AssistantResponse
        - ActionRequest / ActionResponse
        """
        params = {
            k: v
            for k, v in locals().items()
            if k != "self" and v is not None
        }
        _msg = self.create_message(**params)
        if system:
            self.set_system(_msg)
        if _msg in self.messages:
            idx = self.messages.progression.index(_msg.id)
            self.messages.exclude(_msg.id)
            self.messages.insert(idx, _msg)
        else:
            self.messages.include(_msg)

        return _msg

    def clear_messages(self):
        """Remove all messages except the system message if it exists."""
        self.messages.clear()
        if self.system:
            self.messages.insert(0, self.system)

    @property
    def last_response(self) -> AssistantResponse | None:
        """Retrieve the most recent `AssistantResponse`."""
        res = self.messages.filter_by_type(
            item_type=AssistantResponse,
            strict_type=True,
            as_pile=False,
            reverse=True,
            num_items=1,
        )
        if len(res) == 1:
            return res[0]
        return None

    @property
    def last_instruction(self) -> Instruction | None:
        """Retrieve the most recent `Instruction`."""
        res = self.messages.filter_by_type(
            item_type=Instruction,
            strict_type=True,
            as_pile=False,
            reverse=True,
            num_items=1,
        )
        if len(res) == 1:
            return res[0]
        return None

    @property
    def assistant_responses(self) -> Pile[AssistantResponse]:
        """All `AssistantResponse` messages in the manager."""
        return self.messages.filter_by_type(
            item_type=AssistantResponse,
            strict_type=True,
            as_pile=True,
        )

    @property
    def actions(self) -> Pile[ActionRequest | ActionResponse]:
        """All action messages in the manager."""
        return self.messages.filter_by_type(
            item_type={ActionRequest, ActionResponse},
            strict_type=True,
            as_pile=True,
        )

    @property
    def action_requests(self) -> Pile[ActionRequest]:
        """All `ActionRequest` messages in the manager."""
        return self.messages.filter_by_type(
            item_type=ActionRequest,
            strict_type=True,
            as_pile=True,
        )

    @property
    def action_responses(self) -> Pile[ActionResponse]:
        """All `ActionResponse` messages in the manager."""
        return self.messages.filter_by_type(
            item_type=ActionResponse,
            strict_type=True,
            as_pile=True,
        )

    @property
    def instructions(self) -> Pile[Instruction]:
        """All `Instruction` messages in the manager."""
        return self.messages.filter_by_type(
            item_type=Instruction,
            strict_type=True,
            as_pile=True,
        )

    def remove_last_instruction_tool_schemas(self) -> None:
        """
        Convenience method to strip 'tool_schemas' from the most recent Instruction.
        """
        if self.last_instruction:
            self.messages[self.last_instruction.id].content.tool_schemas.clear()

    def concat_recent_action_responses_to_instruction(
        self, instruction: Instruction
    ) -> None:
        """
        Example method to merge the content of recent ActionResponses
        into an instruction's context.
        """
        for i in reversed(list(self.messages.progression)):
            if isinstance(self.messages[i], ActionResponse):
                instruction.content.prompt_context.append(self.messages[i].content)
            else:
                break

    def to_chat_msgs(self, progression=None) -> list[dict]:
        """
        Convert a subset (or all) of messages into a chat representation array.

        Args:
            progression (Optional[Sequence]): A subset of message IDs or the full progression.

        Returns:
            list[dict]: Each item is a dict with 'role' and 'content'.
        """
        if progression == []:
            return []
        try:
            return [
                self.messages[mid].chat_msg for mid in (progression or self.progression)
            ]
        except Exception as e:
            raise ValueError(
                "One or more messages in the requested progression are invalid."
            ) from e

    def __bool__(self):
        return bool(self.messages)

    def __contains__(self, message: Message) -> bool:
        return message in self.messages


def create_message(
    sender: SenderRecipient = None,
    recipient: SenderRecipient = None,
    metadata: dict[str, Any] = None,
    # instruction
    instruction: JsonValue = None,
    context: JsonValue = None,
    handle_context: Literal["extend", "replace"] = "extend",
    guidance: JsonValue = None,
    request_fields: JsonValue = None,
    plain_content: JsonValue = None,
    request_model: BaseModel | type[BaseModel] = None,
    response_format: BaseModel | type[BaseModel] = None,
    images: list = None,
    image_detail: Literal["low", "high", "auto"] = None,
    tool_schemas: dict = None,
    # system
    system: Any = None,
    system_datetime: bool | str = None,
    # assistant_response
    assistant_response: AssistantResponse | Any = None,
    # actions
    action_function: str = None,
    action_arguments: dict[str, Any] = None,
    action_output: Any = None,
    action_request: ActionRequest | None = None,
    action_response: ActionResponse | Any = None,
) -> System | ActionResponse | ActionRequest | AssistantResponse | Instruction:
    params = {k: v for k, v in locals().items() if v is not None}
    return MessageManager.create_message(**params)


# File: lionagi/protocols/messages/manager.py
