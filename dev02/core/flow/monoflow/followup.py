"""
This module contains the MonoFollowup class for performing followup chats with an LLM.

The MonoFollowup class allows for conducting a series of followup chats with an LLM, with the ability to
process instructions, system messages, and invoke tools during the conversation. It extends the MonoChat class.
"""

from typing import Callable
from lionagi.core.messages.schema import Instruction
from lionagi.core.tool.tool import Tool
from lionagi.core.flow.monoflow.chat import MonoChat


class MonoFollowup(MonoChat):
    """
    A class for performing followup chats with an LLM, processing instructions and system messages,
    and optionally invoking tools.

    Attributes:
        FOLLOWUP_PROMPT (str): The default prompt for followup chats.
        OUTPUT_PROMPT (str): The default prompt for presenting the final output to the user.

    Methods:
        async followup(self, instruction, context=None, sender=None, system=None, tools=None,
                       max_followup=1, followup_prompt=None, output_prompt=None, **kwargs):
            Performs a series of followup chats with an LLM, processing instructions and system messages,
            and optionally invoking tools.

        _get_prompt(prompt=None, default=None, num_followup=None, instruction=None) -> str:
            Retrieves the appropriate prompt for the followup chat based on the provided parameters.

        _create_followup_config(self, tools, tool_choice="auto", **kwargs) -> dict:
            Creates the configuration for the followup chat based on the provided tools and parameters.

        async _followup(self, instruction, context=None, sender=None, system=None, tools=None,
                        max_followup=1, auto=False, followup_prompt=None, output_prompt=None,
                        out=True, **kwargs) -> str:
            Performs the actual followup chats with the LLM, processing instructions and system messages,
            and optionally invoking tools.
    """

    FOLLOWUP_PROMPT = """
    In the current task, you are allowed a maximum of another {num_followup} followup chats. 
    If further actions are needed, invoke tool usage. 
    If you are done, present the final result to the user without further tool usage.
    """

    OUTPUT_PROMPT = "Notice: Present the final output to the user. Original user instruction: {instruction}"

    async def followup(
        self,
        instruction: Instruction | str | dict[str, dict | str],
        context=None,
        sender=None,
        system=None,
        tools=None,
        max_followup: int = 1,
        followup_prompt=None,
        output_prompt=None,
        **kwargs,
    ):
        """
        Performs a series of followup chats with an LLM, processing instructions and system messages,
        and optionally invoking tools.

        Args:
            instruction (Instruction | str | dict[str, dict | str]): The instruction for the followup chat.
            context (Optional[Any]): Additional context for the followup chat.
            sender (Optional[str]): The sender of the followup chat message.
            system (Optional[Any]): System message to be processed during the followup chat.
            tools (Optional[Any]): Specifies tools to be invoked during the followup chat.
            max_followup (int): The maximum number of followup chats allowed (default: 1).
            followup_prompt (Optional[str]): The prompt to use for followup chats.
            output_prompt (Optional[str]): The prompt to use for presenting the final output to the user.
            **kwargs: Additional keyword arguments for the followup chat.

        Returns:
            str: The result of the followup chat.
        """
        return await self._followup(
            instruction=instruction,
            context=context,
            sender=sender,
            system=system,
            tools=tools,
            max_followup=max_followup,
            followup_prompt=followup_prompt,
            output_prompt=output_prompt,
            **kwargs,
        )

    @staticmethod
    def _get_prompt(prompt=None, default=None, num_followup=None, instruction=None):
        """
        Retrieves the appropriate prompt for the followup chat based on the provided parameters.

        Args:
            prompt (Optional[str]): The prompt to use for the followup chat.
            default (Optional[str]): The default prompt to use if no specific prompt is provided.
            num_followup (Optional[int]): The number of remaining followup chats.
            instruction (Optional[Any]): The original user instruction.

        Returns:
            str: The appropriate prompt for the followup chat.
        """
        if prompt is not None:
            return prompt

        try:
            if num_followup is not None:
                return default.format(num_followup=num_followup)
            elif instruction is not None:
                return default.format(instruction=instruction)
        except (KeyError, ValueError):
            pass

        return default

    def _create_followup_config(self, tools, tool_choice="auto", **kwargs):
        """
        Creates the configuration for the followup chat based on the provided tools and parameters.

        Args:
            tools (Optional[Any]): Specifies tools to be invoked during the followup chat.
            tool_choice (str): The choice of tools to use (default: "auto").
            **kwargs: Additional keyword arguments for the followup chat configuration.

        Returns:
            dict: The configuration for the followup chat.

        Raises:
            ValueError: If no tools are found and registered.
        """
        if tools and isinstance(tools, list) and isinstance(tools[0], (Callable, Tool)):
            self.branch.tool_manager.register_tools(tools)

        if not self.branch.tool_manager.has_tools:
            raise ValueError("No tools found. You need to register tools.")

        config = self.branch.tool_manager.parse_tool(tools=True, **kwargs)
        config["tool_parsed"] = True
        config["tool_choice"] = tool_choice
        return config

    async def _followup(
        self,
        instruction: Instruction | str | dict[str, dict | str],
        context=None,
        sender=None,
        system=None,
        tools=None,
        max_followup: int = 1,
        auto=True,
        followup_prompt=None,
        output_prompt=None,
        out=True,
        **kwargs,
    ) -> None:
        """
        Performs the actual followup chats with the LLM, processing instructions and system messages,
        and optionally invoking tools.

        Args:
            instruction (Instruction | str | dict[str, dict | str]): The instruction for the followup chat.
            context (Optional[Any]): Additional context for the followup chat.
            sender (Optional[str]): The sender of the followup chat message.
            system (Optional[Any]): System message to be processed during the followup chat.
            tools (Optional[Any]): Specifies tools to be invoked during the followup chat.
            max_followup (int): The maximum number of followup chats allowed (default: 1).
            auto (bool): Flag indicating whether to automatically determine if the chat is finished (default: False).
            followup_prompt (Optional[str]): The prompt to use for followup chats.
            output_prompt (Optional[str]): The prompt to use for presenting the final output to the user.
            out (bool): Flag indicating whether to return the output of the followup chat (default: True).
            **kwargs: Additional keyword arguments for the followup chat.

        Returns:
            Optional[str]: The result of the followup chat, if `out` is True.
        """
        config = self._create_followup_config(tools, **kwargs)

        i = 0
        _out = ""

        while i < max_followup:
            _prompt = self._get_prompt(
                prompt=followup_prompt,
                default=self.FOLLOWUP_PROMPT,
                num_followup=max_followup - i,
            )

            if i == 0:
                _prompt = {"NOTICE": _prompt, "TASK": instruction}
                _out = await self.chat(
                    _prompt, context=context, sender=sender, system=system, **config
                )
            else:
                _out = await self.chat(_prompt, sender=sender, **config)

            if not self.branch._is_invoked():
                return _out if out else None

            i += 1

        if not self.branch._is_invoked():
            return _out if out else None

        _prompt = self._get_prompt(
            prompt=output_prompt,
            default=self.OUTPUT_PROMPT,
            instruction=instruction,
        )
        _out = await self.chat(_prompt, sender=sender, **kwargs)
        return _out if out else None
