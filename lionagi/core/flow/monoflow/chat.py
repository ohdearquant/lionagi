"""
This module contains the MonoChat class for performing chat conversations with an LLM.

The MonoChat class allows for processing instructions and system messages, and optionally invoking tools
during the chat conversation. It extends the BaseMonoFlow and MonoChatMixin classes.
"""

from typing import Any

from lionagi.core.flow.baseflow import BaseMonoFlow
from lionagi.core.flow.monoflow.chat_mixin import MonoChatMixin


class MonoChat(BaseMonoFlow, MonoChatMixin):
    """
    A class for performing a chat conversation with an LLM, processing instructions and system messages,
    and optionally invoking tools.

    Attributes:
        branch: The Branch instance to perform chat operations.

    Methods:
        __init__(self, branch) -> None:
            Initializes the MonoChat instance.

        async chat(self, instruction=None, context=None, sender=None, system=None, tools=False,
                   out=True, invoke=True, output_fields=None, form=None, **kwargs) -> Any:
            Performs a chat conversation with an LLM, processing instructions and system messages,
            and optionally invoking tools.
    """

    def __init__(self, branch) -> None:
        """
        Initializes the MonoChat instance.

        Args:
            branch: The Branch instance to perform chat operations.
        """
        super().__init__(branch)

    async def chat(
        self,
        instruction=None,
        context=None,
        sender=None,
        system=None,
        tools=False,
        out: bool = True,
        invoke: bool = True,
        output_fields=None,
        form=None,
        **kwargs,
    ) -> Any:
        """
        Performs a chat conversation with an LLM, processing instructions and system messages,
        and optionally invoking tools.

        Args:
            instruction (Union[Instruction, str]): The instruction for the chat.
            context (Optional[Any]): Additional context for the chat.
            sender (Optional[str]): The sender of the chat message.
            system (Optional[Union[System, str, Dict[str, Any]]]): System message to be processed.
            tools (Union[bool, Tool, List[Tool], str, List[str]]): Specifies tools to be invoked.
            out (bool): If True, outputs the chat response.
            invoke (bool): If True, invokes tools as part of the chat.
            output_fields (Optional[Any]): The output fields for the chat.
            form (Optional[Any]): The prompt template for the chat.
            **kwargs: Arbitrary keyword arguments for chat completion.

        Returns:
            Any: The result of the chat conversation.

        Examples:
            >>> await MonoChat.chat(branch, "Ask about user preferences")
        """

        config = self._create_chat_config(
            instruction=instruction,
            context=context,
            sender=sender,
            system=system,
            form=form,
            tools=tools,
            output_fields=output_fields,
            **kwargs,
        )

        await self._call_chatcompletion(**config)

        return await self._output(
            invoke=invoke,
            out=out,
            output_fields=output_fields,
            form=form,
        )
