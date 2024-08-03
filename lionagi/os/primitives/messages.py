from typing import Any, Literal, Callable

from pydantic import Field

from lion_core.communication import (
    Instruction as CoreInstruction,
    System as CoreSystem,
    ActionRequest as CoreActionRequest,
    ActionResponse as CoreActionResponse,
    AssistantResponse as CoreAssistantResponse,
    StartMail as CoreStartMail,
)
from lionagi.os.primitives.container.exchange import Exchange
from lionagi.os.primitives.node import Node
from lionagi.os.primitives.utils import core_to_lionagi_container


class Instruction(CoreInstruction, Node):

    def __init__(
        self,
        instruction: Any,
        context: Any = None,
        images: list = None,
        sender: Any = None,
        recipient: Any = None,
        requested_fields: dict = None,
        image_detail: Literal["low", "high", "auto"] = None,
        protected_init_params: dict | None = None,
    ):

        super().__init__(
            instruction=instruction,
            context=context,
            images=images,
            sender=sender,
            recipient=recipient,
            requested_fields=requested_fields,
            image_detail=image_detail,
            protected_init_params=protected_init_params,
        )
        self.metadata = core_to_lionagi_container(self.metadata)
        self.content = core_to_lionagi_container(self.content)


class System(CoreSystem, Node):

    def __init__(
        self,
        system: Any = None,
        sender: str | None = None,
        recipient: str | None = None,
        system_datetime: bool | str = None,
        protected_init_params: dict | None = None,
    ):

        super().__init__(
            system=system,
            sender=sender,
            recipient=recipient,
            system_datetime=system_datetime,
            protected_init_params=protected_init_params,
        )
        self.metadata = core_to_lionagi_container(self.metadata)
        self.content = core_to_lionagi_container(self.content)


class ActionRequest(CoreActionRequest, Node):

    def __init__(
        self,
        func: str | Callable,
        arguments: dict,
        sender: Any,
        recipient: Any,
        protected_init_params: dict | None = None,
    ):
        super().__init__(
            func=func,
            arguments=arguments,
            sender=sender,
            recipient=recipient,
            protected_init_params=protected_init_params,
        )
        self.metadata = core_to_lionagi_container(self.metadata)
        self.content = core_to_lionagi_container(self.content)


class ActionResponse(CoreActionResponse, Node):

    def __init__(
        self,
        action_request: ActionRequest,
        sender: Any,
        func_output: Any,
        protected_init_params: dict | None = None,
    ):

        super().__init__(
            action_request=action_request,
            sender=sender,
            func_output=func_output,
            protected_init_params=protected_init_params,
        )

        self.metadata = core_to_lionagi_container(self.metadata)
        self.content = core_to_lionagi_container(self.content)


class AssistantResponse(CoreAssistantResponse, Node):

    def __init__(
        self,
        assistant_response: dict,
        sender: Any,
        recipient: Any,
        protected_init_params: dict | None = None,
    ):
        super().__init__(
            assistant_response=assistant_response,
            sender=sender,
            recipient=recipient,
            protected_init_params=protected_init_params,
        )

        self.metadata = core_to_lionagi_container(self.metadata)
        self.content = core_to_lionagi_container(self.content)


class StartMail(CoreStartMail):
    """a start mail node that triggers the initiation of a process."""

    mailbox: Exchange = Field(
        default_factory=Exchange, description="The pending start mail"
    )
