# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import warnings
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from lionagi.ln.concurrency._compat import BaseExceptionGroup as _BaseExceptionGroup

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
    """Manages an ordered Pile of Messages with system, instruction, and action lifecycle."""

    def __init__(
        self,
        messages: list[Message] | None = None,
        progression: Progression | None = None,
        system: System | None = None,
        on_message_added: list | None = None,
    ):
        super().__init__()
        self._on_message_added: list = on_message_added or []
        m_ = []
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
        self.system = system
        if self.system:
            self.add_message(system=self.system)

    @property
    def progression(self) -> Progression:
        return self.messages.progression

    def set_system(self, system: System) -> None:
        if not self.system:
            self.system = system
            self.messages.insert(0, self.system)
        else:
            old_system = self.system
            self.system = system
            self.messages.insert(0, self.system)
            self.messages.exclude(old_system)

    async def aclear_messages(self):
        async with self.messages:
            self.clear_messages()

    async def a_add_message(self, **kwargs):
        from lionagi.ln.concurrency import is_coro_func

        async with self.messages:
            _msg = self.create_message(**{k: v for k, v in kwargs.items() if v is not None})
            system = kwargs.get("system")
            if system:
                self.set_system(_msg)
            if _msg in self.messages:
                idx = self.messages.progression.index(_msg.id)
                self.messages.exclude(_msg.id)
                self.messages.insert(idx, _msg)
            else:
                self.messages.include(_msg)

        callbacks = list(self._on_message_added)
        errors: list[BaseException] = []
        for cb in callbacks:
            try:
                if is_coro_func(cb):
                    await cb(_msg)
                else:
                    cb(_msg)
            except BaseException as exc:  # noqa: BLE001 — collect, re-raise grouped
                errors.append(exc)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise _BaseExceptionGroup("on_message_added hooks failed", errors)

        return _msg

    @staticmethod
    def create_instruction(
        *,
        instruction: JsonValue = None,
        context: JsonValue = None,
        handle_context: Literal["extend", "replace"] = "extend",
        guidance: JsonValue = None,
        images: list = None,
        plain_content: JsonValue = None,
        image_detail: Literal["low", "high", "auto"] = None,
        response_format: BaseModel | type[BaseModel] = None,
        tool_schemas: dict = None,
        structure: type | str | None = None,
        sender: SenderRecipient = None,
        recipient: SenderRecipient = None,
    ) -> Instruction:
        raw_params = {k: v for k, v in locals().items() if k != "instruction" and v is not None}

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
                params["handle_context"] = "replace"
            instruction.update(**params)
            return instruction
        else:
            content_dict = {k: v for k, v in raw_params.items() if k not in ["sender", "recipient"]}
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
        params = {k: v for k, v in locals().items() if k != "assistant_response" and v is not None}

        if isinstance(assistant_response, AssistantResponse):
            assistant_response.update(**params)
            return assistant_response

        content_dict = {"assistant_response": assistant_response} if assistant_response else {}
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
        params = {k: v for k, v in locals().items() if k != "action_request" and v is not None}

        if isinstance(action_request, ActionRequest):
            action_request.update(**params)
            return action_request

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
        if not isinstance(action_request, ActionRequest):
            raise ValueError(
                "Error: please provide a corresponding action request for an action response."
            )
        if isinstance(action_response, ActionResponse):
            action_response.update(output=action_output, sender=sender, recipient=recipient)
            return action_response

        content_dict = {
            "function": action_request.content.function,
            "arguments": action_request.content.arguments,
            "output": action_output,
            "action_request_id": str(action_request.id),
        }
        response = ActionResponse(content=content_dict, sender=sender, recipient=recipient)
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
        params = {k: v for k, v in locals().items() if k != "system" and v is not None}

        if isinstance(system, System):
            system.update(**params)
            return system

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
        plain_content: JsonValue = None,
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
        if action_request and action_output is None and action_response is None:
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

        # action_output can be falsy (0, "", [], {}) — use `is not None`.
        elif action_response is not None or action_output is not None:
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
                plain_content=plain_content,
                image_detail=image_detail,
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
        plain_content: JsonValue = None,
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
        hook_snapshot = self._snapshot_hooks_or_reject_async()

        params = {
            k: v
            for k, v in locals().items()
            if k != "self" and k != "hook_snapshot" and v is not None
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

        self._fire_on_message_added(_msg, hook_snapshot)

        return _msg

    def _snapshot_hooks_or_reject_async(self) -> list:
        from lionagi.ln.concurrency import is_coro_func

        snapshot = list(self._on_message_added)
        for cb in snapshot:
            if is_coro_func(cb):
                raise RuntimeError(
                    f"Async on_message_added callback {cb!r} cannot fire "
                    "from the sync add_message path. Use a_add_message "
                    "from an async context instead."
                )
        return snapshot

    def _fire_on_message_added(self, msg: Message, snapshot: list) -> None:
        errors: list[BaseException] = []
        for cb in snapshot:
            try:
                cb(msg)
            except BaseException as exc:  # noqa: BLE001 — collect, re-raise grouped
                errors.append(exc)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise _BaseExceptionGroup("on_message_added hooks failed", errors)

    def clear_messages(self):
        self.messages.clear()
        if self.system:
            self.messages.insert(0, self.system)

    @property
    def last_response(self) -> AssistantResponse | None:
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
        return self.messages.filter_by_type(
            item_type=AssistantResponse,
            strict_type=True,
            as_pile=True,
        )

    @property
    def actions(self) -> Pile[ActionRequest | ActionResponse]:
        return self.messages.filter_by_type(
            item_type={ActionRequest, ActionResponse},
            strict_type=True,
            as_pile=True,
        )

    @property
    def action_requests(self) -> Pile[ActionRequest]:
        return self.messages.filter_by_type(
            item_type=ActionRequest,
            strict_type=True,
            as_pile=True,
        )

    @property
    def action_responses(self) -> Pile[ActionResponse]:
        return self.messages.filter_by_type(
            item_type=ActionResponse,
            strict_type=True,
            as_pile=True,
        )

    @property
    def instructions(self) -> Pile[Instruction]:
        return self.messages.filter_by_type(
            item_type=Instruction,
            strict_type=True,
            as_pile=True,
        )

    def remove_last_instruction_tool_schemas(self) -> None:
        if self.last_instruction:
            self.messages[self.last_instruction.id].content.tool_schemas.clear()

    def concat_recent_action_responses_to_instruction(self, instruction: Instruction) -> None:
        for i in reversed(list(self.messages.progression)):
            if isinstance(self.messages[i], ActionResponse):
                instruction.content.prompt_context.append(self.messages[i].content)
            else:
                break

    def to_chat_msgs(self, progression=None) -> list[dict]:
        if progression == []:
            return []
        try:
            return [self.messages[mid].chat_msg for mid in (progression or self.progression)]
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
    plain_content: JsonValue = None,
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
    warnings.warn(
        "lionagi.protocols.messages.create_message is deprecated; "
        "use MessageManager.create_message instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    params = {k: v for k, v in locals().items() if v is not None}
    return MessageManager.create_message(**params)


# File: lionagi/protocols/messages/manager.py
