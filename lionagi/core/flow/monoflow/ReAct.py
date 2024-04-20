"""
This module contains the MonoReAct class for performing reasoning and action tasks with an LLM.

The MonoReAct class allows for conducting a series of reasoning and action steps with an LLM, with the ability to
process instructions, system messages, and invoke tools during the conversation. It extends the MonoChat class.
"""

from typing import Callable
from lionagi.core.flow.monoflow.chat import MonoChat
from lionagi.core.tool import Tool
from lionagi.core.messages.schema import Instruction


class MonoReAct(MonoChat):
    """
    A class for performing reasoning and action tasks with an LLM, processing instructions and system messages,
    and optionally invoking tools.

    Attributes:
        REASON_PROMPT (str): The default prompt for reasoning steps.
        ACTION_PROMPT (str): The default prompt for action steps.
        OUTPUT_PROMPT (str): The default prompt for presenting the final output to the user.

    Methods:
        async ReAct(self, instruction, context=None, sender=None, system=None, tools=None,
                    num_rounds=1, reason_prompt=None, action_prompt=None, output_prompt=None, **kwargs):
            Performs a series of reasoning and action steps with an LLM, processing instructions and system messages,
            and optionally invoking tools.

        _get_prompt(prompt=None, default=None, num_steps=None, instruction=None) -> str:
            Retrieves the appropriate prompt for the reasoning or action step based on the provided parameters.

        _create_followup_config(self, tools, tool_choice="auto", **kwargs) -> dict:
            Creates the configuration for the followup steps based on the provided tools and parameters.

        async _ReAct(self, instruction, context=None, sender=None, system=None, tools=None,
                     num_rounds=1, auto=False, reason_prompt=None, action_prompt=None,
                     output_prompt=None, out=True, **kwargs):
            Performs the actual reasoning and action steps with the LLM, processing instructions and system messages,
            and optionally invoking tools.
    """

    REASON_PROMPT = """
    You have {num_steps} steps left in the current task. If available, integrate previous tool responses.
    Perform reasoning and prepare an action plan according to available tools only. Apply divide and conquer technique.
    """

    ACTION_PROMPT = """
    You have {num_steps} steps left in the current task. If further actions are needed, invoke tool usage.
    If you are done, present the final result to the user without further tool usage.
    """

    OUTPUT_PROMPT = "Notice: Present the final output to the user. Original user instruction: {instruction}"

    async def ReAct(
        self,
        instruction: Instruction | str | dict[str, dict | str],
        context=None,
        sender=None,
        system=None,
        tools=None,
        num_rounds: int = 1,
        reason_prompt=None,
        action_prompt=None,
        output_prompt=None,
        **kwargs,
    ):
        """
        Performs a series of reasoning and action steps with an LLM, processing instructions and system messages,
        and optionally invoking tools.

        Args:
            instruction (Instruction | str | dict[str, dict | str]): The instruction for the task.
            context (Optional[Any]): Additional context for the task.
            sender (Optional[str]): The sender of the task message.
            system (Optional[Any]): System message to be processed during the task.
            tools (Optional[Any]): Specifies tools to be invoked during the task.
            num_rounds (int): The number of reasoning and action rounds to perform (default: 1).
            reason_prompt (Optional[str]): The prompt to use for reasoning steps.
            action_prompt (Optional[str]): The prompt to use for action steps.
            output_prompt (Optional[str]): The prompt to use for presenting the final output to the user.
            **kwargs: Additional keyword arguments for the task.

        Returns:
            The result of the reasoning and action steps.
        """
        return await self._ReAct(
            instruction,
            context=context,
            sender=sender,
            system=system,
            tools=tools,
            num_rounds=num_rounds,
            reason_prompt=reason_prompt,
            action_prompt=action_prompt,
            output_prompt=output_prompt,
            **kwargs,
        )

    @staticmethod
    def _get_prompt(prompt=None, default=None, num_steps=None, instruction=None):
        """
        Retrieves the appropriate prompt for the reasoning or action step based on the provided parameters.

        Args:
            prompt (Optional[str]): The prompt to use for the step.
            default (Optional[str]): The default prompt to use if no specific prompt is provided.
            num_steps (Optional[int]): The number of remaining steps in the task.
            instruction (Optional[Any]): The original user instruction.

        Returns:
            str: The appropriate prompt for the reasoning or action step.
        """

        if prompt is not None:
            return prompt

        try:
            if num_steps is not None:
                return default.format(num_steps=num_steps)
            elif instruction is not None:
                return default.format(instruction=instruction)
        except (KeyError, ValueError):
            pass

        return default

    def _create_followup_config(self, tools, tool_choice="auto", **kwargs):
        """
        Creates the configuration for the followup steps based on the provided tools and parameters.

        Args:
            tools (Optional[Any]): Specifies tools to be invoked during the followup steps.
            tool_choice (str): The choice of tools to use (default: "auto").
            **kwargs: Additional keyword arguments for the followup configuration.

        Returns:
            dict: The configuration for the followup steps.

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

    async def _ReAct(
        self,
        instruction: Instruction | str | dict[str, dict | str],
        context=None,
        sender=None,
        system=None,
        tools=None,
        num_rounds: int = 1,
        auto=False,
        reason_prompt=None,
        action_prompt=None,
        output_prompt=None,
        out=True,
        **kwargs,
    ):
        """
        Performs the actual reasoning and action steps with the LLM, processing instructions and system messages,
        and optionally invoking tools.

        Args:
            instruction (Instruction | str | dict[str, dict | str]): The instruction for the task.
            context (Optional[Any]): Additional context for the task.
            sender (Optional[str]): The sender of the task message.
            system (Optional[Any]): System message to be processed during the task.
            tools (Optional[Any]): Specifies tools to be invoked during the task.
            num_rounds (int): The number of reasoning and action rounds to perform (default: 1).
            auto (bool): Flag indicating whether to automatically determine if the task is finished (default: False).
            reason_prompt (Optional[str]): The prompt to use for reasoning steps.
            action_prompt (Optional[str]): The prompt to use for action steps.
            output_prompt (Optional[str]): The prompt to use for presenting the final output to the user.
            out (bool): Flag indicating whether to return the output of the task (default: True).
            **kwargs: Additional keyword arguments for the task.

        Returns:
            The result of the reasoning and action steps.
        """
        config = self._create_followup_config(tools, **kwargs)

        i = 0
        _out = ""

        while i < num_rounds:
            _prompt = self._get_prompt(
                prompt=reason_prompt,
                default=self.REASON_PROMPT,
                num_steps=(num_rounds - i) * 2,
            )
            _instruct = {"NOTICE": _prompt}

            # reason
            if i == 0:
                _instruct["TASK"] = instruction

                await self.chat(
                    _instruct, context=context, sender=sender, system=system, **kwargs
                )

            elif i > 0:
                await self.chat(_instruct, sender=sender, **kwargs)

            # action
            _prompt = self._get_prompt(
                prompt=action_prompt,
                default=self.ACTION_PROMPT,
                num_steps=(num_rounds - i) * 2 - 1,
            )

            _out = await self.chat(_prompt, sender=sender, **config)

            if auto and not self.branch._is_invoked():
                return _out if out else None

            i += 1

        if auto:
            if not self.branch._is_invoked():
                return _out if out else None

            _prompt = self._get_prompt(
                prompt=output_prompt,
                default=self.OUTPUT_PROMPT,
                instruction=instruction,
            )
            _out = await self.chat(_prompt, sender=sender, **kwargs)
            return _out if out else None
