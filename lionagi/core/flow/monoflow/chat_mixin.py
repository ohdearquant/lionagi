"""
This module contains mixins for configuring and invoking chatbots.
"""

from abc import ABC
from typing import Any

import re
from lionagi.libs import nested, func_call, convert, StringMatch, ParseUtil
from lionagi.core.tool.tool import TOOL_TYPE
from lionagi.core.messages.schema import Instruction


class MonoChatConfigMixin(ABC):
    """
    Mixin class for configuring chatbots.

    Methods:
        _create_chat_config(self, instruction=None, context=None, sender=None, system=None,
        output_fields=None, form=None, tools=False, **kwargs) -> Any:
        Creates a chat configuration based on the provided parameters.
    """

    def _create_chat_config(
        self,
        instruction: Instruction | str | dict[str, Any] = None,
        context: Any | None = None,
        sender: str | None = None,
        system: str | dict[str, Any] | None = None,
        output_fields=None,
        form=None,
        tools: TOOL_TYPE = False,
        **kwargs,
    ) -> Any:
        """
        Creates a chat configuration based on the provided parameters.

        Args:
            instruction (Instruction | str | dict[str, Any]): The instruction for the chatbot (optional).
            context (Any): The context for the chatbot (optional).
            sender (str): The sender of the message (optional).
            system (str | dict[str, Any]): The system message for the chatbot (optional).
            output_fields: The output fields for the chatbot (optional).
            form: The prompt template for the chatbot (optional).
            tools (TOOL_TYPE): The tools for the chatbot (default: False).
            **kwargs: Additional keyword arguments for the chat configuration.

        Returns:
            Any: The chat configuration.
        """
        if system:
            self.branch.change_first_system_message(system)

        if not form:
            self.branch.add_message(
                instruction=instruction,
                context=context,
                sender=sender,
                output_fields=output_fields,
            )
        else:
            instruct_ = Instruction.from_form(form)
            self.branch.add_message(instruction=instruct_)

        if "tool_parsed" in kwargs:
            kwargs.pop("tool_parsed")
            tool_kwarg = {"tools": tools}
            kwargs = tool_kwarg | kwargs
        elif tools and self.branch.has_tools:
            kwargs = self.branch.tool_manager.parse_tool(tools=tools, **kwargs)

        config = {**self.branch.llmconfig, **kwargs}
        if sender is not None:
            config["sender"] = sender

        return config


class MonoChatInvokeMixin(ABC):
    """
    Mixin class for invoking chatbots.

    Methods:
        async _output(self, invoke, out, output_fields, func_calls_=None, form=None,
                    return_template=True):
            Processes the output of the chatbot.

        _return_response(content_, output_fields) -> Any:
            Returns the response from the chatbot.

        async _invoke_tools(self, content_=None, func_calls_=None):
            Invokes the tools associated with the chatbot.

        _process_chatcompletion(self, payload, completion, sender):
            Processes the chat completion.

        async _call_chatcompletion(self, sender=None, with_sender=False, **kwargs):
            Calls the chat completion API.
    """

    async def _output(
        self,
        invoke,
        out,
        output_fields,
        func_calls_=None,
        form=None,
        return_template=True,
    ):
        """
        Processes the output of the chatbot.

        Args:
            invoke: Flag indicating whether to invoke the tools.
            out: Flag indicating whether to return the output.
            output_fields: The output fields for the chatbot.
            func_calls_: The function calls for invoking the tools (optional).
            form: The prompt template for the chatbot (optional).
            return_template (bool): Flag indicating whether to return the prompt template (default: True).
        """

        content_ = self.branch._last_message_content

        if invoke:
            try:
                await self._invoke_tools(content_, func_calls_=func_calls_)
            except Exception:
                pass

        response_ = self._return_response(content_, output_fields)

        if form:
            form._process_response(response_)
            return form if return_template else form.outputs

        if out:
            return response_

    @staticmethod
    def _return_response(content_, output_fields):
        """
        Returns the response from the chatbot.

        Args:
            content_: The content of the last message.
            output_fields: The output fields for the chatbot.

        Returns:
            Any: The response from the chatbot.
        """
        out_ = ""

        if len(content_.items()) == 1 and len(nested.get_flattened_keys(content_)) == 1:
            key = nested.get_flattened_keys(content_)[0]
            out_ = content_[key]

        if output_fields:
            try:
                return StringMatch.force_validate_dict(
                    out_, keys=list(output_fields.keys())
                )
            except Exception:
                pass

        if isinstance(out_, str):
            try:
                match = re.search(r"```json\n({.*?})\n```", out_, re.DOTALL)
                if match:
                    out_ = ParseUtil.fuzzy_parse_json(match.group(1))
            except Exception:
                pass

        return out_

    async def _invoke_tools(self, content_=None, func_calls_=None):
        """
        Invokes the tools associated with the chatbot.

        Args:
            content_: The content of the last message (optional).
            func_calls_: The function calls for invoking the tools (optional).

        Returns:
            list: The results of invoking the tools.
        """
        if func_calls_ is None and content_ is not None:
            tool_uses = content_
            func_calls_ = func_call.lcall(
                [convert.to_dict(i) for i in tool_uses["action_request"]],
                self.branch.tool_manager.get_function_call,
            )

        outs = await func_call.alcall(func_calls_, self.branch.tool_manager.invoke)
        outs = convert.to_list(outs, flatten=True)

        a = []
        for out_, f in zip(outs, func_calls_):
            res = {
                "function": f[0],
                "arguments": f[1],
                "output": out_,
            }
            self.branch.add_message(response=res)
            a.append(res)

        return a

    def _process_chatcompletion(self, payload, completion, sender):
        """
        Processes the chat completion.

        Args:
            payload: The payload for the chat completion.
            completion: The completed chat response.
            sender: The sender of the message.
        """
        if "choices" in completion:
            add_msg_config = {"response": completion["choices"][0]}
            if sender is not None:
                add_msg_config["sender"] = sender

            self.branch.datalogger.append(input_data=payload, output_data=completion)
            self.branch.add_message(**add_msg_config)
            self.branch.status_tracker.num_tasks_succeeded += 1
        else:
            self.branch.status_tracker.num_tasks_failed += 1

    async def _call_chatcompletion(self, sender=None, with_sender=False, **kwargs):
        """
        Calls the chat completion API.

        Args:
            sender: The sender of the message (optional).
            with_sender (bool): Flag indicating whether to include the sender in the chat messages (default: False).
            **kwargs: Additional keyword arguments for the chat completion API.
        """
        messages = (
            self.branch._chat_messages_with_sender
            if with_sender
            else self.branch._chat_messages
        )
        payload, completion = await self.branch.service.serve_chat(
            messages=messages, **kwargs
        )
        self._process_chatcompletion(payload, completion, sender)


class MonoChatMixin(MonoChatConfigMixin, MonoChatInvokeMixin, ABC):
    """
    Mixin class that combines MonoChatConfigMixin and MonoChatInvokeMixin.
    """

    pass
