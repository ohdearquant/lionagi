"""
the base directive
"""

import asyncio
import re
import contextlib
from typing import Any
from abc import ABC

from lionagi.libs.ln_parse import ParseUtil, StringMatch

from lionagi.core.generic.abc import ActionError

from lionagi.core.message import Instruction
from lionagi.core.message.util import _parse_action_request
from lionagi.core.validator.validator import Validator


from lionagi.core.message.action_request import ActionRequest
from lionagi.core.message.action_response import ActionResponse
from ..util import process_tools
from .template.action import ActionTemplate

from typing import Any

from lionagi.libs import convert, AsyncUtil
from lionagi.core.session.branch import Branch


class DirectiveMixin(ABC):

    async def _base_chat(
        self,
        instruction=None,
        *,
        system=None,
        context=None,
        sender=None,
        recipient=None,
        requested_fields=None,
        form=None,
        tools=False,
        invoke_tool=True,
        return_form=True,
        strict=False,
        rulebook=None,
        imodel=None,
        use_annotation=True,
        branch=None,
        clear_messages=False,
        return_branch=False,
        **kwargs,
    ):
        branch = branch or self.branch
        if clear_messages:
            branch.clear()

        config = self._create_chat_config(
            system=system,
            instruction=instruction,
            context=context,
            sender=sender,
            recipient=recipient,
            requested_fields=requested_fields,
            form=form,
            tools=tools,
            **kwargs,
        )

        payload, completion = await self._call_chatcompletion(
            imodel=imodel, branch=branch, **config
        )

        out_ = await self._output(
            payload=payload,
            completion=completion,
            sender=sender,
            invoke_tool=invoke_tool,
            requested_fields=requested_fields,
            form=form,
            return_form=return_form,
            strict=strict,
            rulebook=rulebook,
            use_annotation=use_annotation,
        )

        return out_, branch if return_branch else out_

    def _create_chat_config(
        self,
        system=None,
        instruction=None,
        context=None,
        sender=None,
        recipient=None,
        requested_fields=None,
        form=None,
        tools=False,
        **kwargs,  # additional config for the model
    ) -> Any:

        if system:
            self.branch.add_message(system=system)

        if not form:
            self.branch.add_message(
                instruction=instruction,
                context=context,
                sender=sender,
                recipient=recipient,
                requested_fields=requested_fields,
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

        config = {**self.imodel.config, **kwargs}
        if sender is not None:
            config["sender"] = sender

        return config

    async def _call_chatcompletion(self, imodel=None, branch=None, **kwargs):
        imodel = imodel or self.imodel
        branch = branch or self.branch
        return await imodel.call_chat_completion(branch.to_chat_messages(), **kwargs)

    async def _process_chatcompletion(
        self,
        payload,
        completion,
        sender,
        invoke_tool=True,
        branch=None,
        action_request=None,
    ):
        branch = branch or self.branch
        # process the raw chat completion response
        _msg = None
        if "choices" in completion:

            aa = payload.pop("messages", None)
            branch.update_last_instruction_meta(payload)
            msg = completion.pop("choices", None)
            if msg and isinstance(msg, list):
                msg = msg[0]

            if isinstance(msg, dict):
                _msg = msg.pop("message", None)
                completion.update(msg)

                branch.add_message(
                    assistant_response=_msg, metadata=completion, sender=sender
                )
                branch.imodel.status_tracker.num_tasks_succeeded += 1
        else:
            branch.imodel.status_tracker.num_tasks_failed += 1

        return await self._process_action_request(
            _msg=_msg,
            branch=branch,
            invoke_tool=invoke_tool,
            action_request=action_request,
        )

    async def _process_action_request(
        self, _msg=None, branch=None, invoke_tool=True, action_request=None
    ):
        # if the assistant response contains action request, we add each as a message to branch
        action_request = action_request or _parse_action_request(_msg)

        if action_request is None:
            return _msg if _msg else False

        if action_request:
            for i in action_request:
                if i.function in branch.tool_manager.registry:
                    i.recipient = branch.tool_manager.registry[
                        i.function
                    ].ln_id  # recipient is the tool
                else:
                    raise ActionError(f"Tool {i.function} not found in registry")
                branch.add_message(action_request=i, recipient=i.recipient)

        if invoke_tool:
            # invoke tools and add action response to branch
            tasks = []
            for i in action_request:
                tool = branch.tool_manager.registry[i.function]
                tasks.append(asyncio.create_task(tool.invoke(i.arguments)))

            results = await asyncio.gather(*tasks)

            for idx, item in enumerate(results):
                branch.add_message(
                    action_request=action_request[idx],
                    func_outputs=item,
                    sender=action_request[idx].recipient,
                    recipient=action_request[idx].sender,
                )

        return None

    async def _output(
        self,
        payload,
        completion,
        sender,
        invoke_tool,
        requested_fields,
        form=None,
        return_form=True,
        strict=False,
        rulebook=None,
        use_annotation=True,
        template_name=None,
    ) -> Any:
        _msg = await self._process_chatcompletion(
            payload=payload,
            completion=completion,
            sender=sender,
            invoke_tool=invoke_tool,
        )

        if _msg is None:
            return None

        response_ = self._process_model_response(_msg, requested_fields)

        if form:
            validator = None

            if rulebook is None:
                validator = self.validator
            else:
                validator = Validator(rulebook=rulebook)

            form = await validator.validate_response(
                form=form,
                response=response_,
                strict=strict,
                use_annotation=use_annotation,
            )
            if template_name:
                form.template_name = template_name

            return (
                form
                if return_form
                else {
                    i: form.work_fields[i]
                    for i in form.requested_fields
                    if form.work_fields[i] is not None
                }
            )

        return response_

    @staticmethod
    def _process_model_response(content_, requested_fields):
        out_ = ""

        if "content" in content_:
            out_ = content_["content"]

        if requested_fields:
            with contextlib.suppress(Exception):
                return StringMatch.force_validate_dict(out_, requested_fields)

        if isinstance(out_, str):
            with contextlib.suppress(Exception):
                match = re.search(r"```json\n({.*?})\n```", out_, re.DOTALL)
                if match:
                    out_ = ParseUtil.fuzzy_parse_json(match.group(1))

        return out_ or content_

    async def _chat(
        self,
        instruction=None,  # additional instruction
        context=None,  # context to perform the instruction on
        system=None,  # optionally swap system message
        sender=None,  # sender of the instruction, default "user"
        recipient=None,  # recipient of the instruction, default "branch.ln_id"
        branch=None,
        requested_fields=None,  # fields to request from the context, default None
        form=None,  # form to create instruction from, default None,
        tools=False,  # the tools to use, use True to consider all tools, no tools by default
        invoke_tool=True,  # whether to invoke the tool when function calling, default True
        return_form=True,  # whether to return the form if a form is passed in, otherwise return a dict/str
        strict=False,  # whether to strictly enforce the rule validation, default False
        rulebook=None,  # the rulebook to use for validation, default None, use default rulebook
        imodel=None,  # the optinally swappable iModel for the commands, otherwise self.branch.imodel
        clear_messages=False,
        use_annotation=True,  # whether to use annotation as rule qualifier, default True, (need rulebook if False)
        timeout: (
            float | None
        ) = None,  # timeout for the rcall, default None (no timeout)
        return_branch=False,
        **kwargs,
    ):

        a = await self._base_chat(
            context=context,
            instruction=instruction,
            system=system,
            sender=sender,
            recipient=recipient,
            requested_fields=requested_fields,
            form=form,
            tools=tools,
            invoke_tool=invoke_tool,
            return_form=return_form,
            strict=strict,
            rulebook=rulebook,
            imodel=imodel,
            use_annotation=use_annotation,
            timeout=timeout,
            branch=branch,
            clear_messages=clear_messages,
            return_branch=return_branch,
            **kwargs,
        )

        a = list(a)
        if len(a) == 2 and a[0] == a[1]:
            return a[0] if not isinstance(a[0], tuple) else a[0][0]

        return a[0], a[1]

    async def _act(
        self,
        form=None,
        template=ActionTemplate,
        branch=None,
        tools=None,
        confidence_score=None,
        instruction=None,
        context=None,
        return_branch=False,
        **kwargs,
    ):
        branch = branch or self.branch
        if not form:
            form = template(
                confidence_score=confidence_score,
                instruction=instruction,
                context=context,
            )

        if tools:
            process_tools(tools, branch)

        form, branch = await self._chat(
            form=form,
            return_branch=True,
            branch=branch,
            tools=tools,
            **kwargs,
        )

        if getattr(form, "action_required", False):
            actions = getattr(form, "actions", None)
            if actions:
                actions = [actions] if not isinstance(actions, list) else actions

                try:
                    requests = []
                    for action in actions:
                        msg = ActionRequest(
                            function=action["function"],
                            arguments=action["arguments"],
                            sender=branch.ln_id,
                            recipient=branch.tool_manager.registry[
                                action["function"]
                            ].ln_id,
                        )
                        requests.append(msg)
                        self.branch.add_message(msg)

                    if requests:
                        out = self._process_action_request(
                            branch=branch, invoke_tool=True, action_request=requests
                        )

                        if out == False:
                            raise ValueError(
                                "Error processing action request: No requests found."
                            )

                        len_actions = len(actions)
                        action_responses = branch.messages[-len_actions:]

                        if not all(
                            isinstance(i, ActionResponse) for i in action_responses
                        ):
                            raise ValueError(
                                "Error processing action request: Invalid action response."
                            )

                        action_responses = [i._to_dict() for i in action_responses]
                        form._add_field(
                            "action_response", list[dict], None, action_responses
                        )
                except Exception as e:
                    raise ValueError(f"Error processing action request: {e}")
            raise ValueError("Error processing action request: No requests found.")

        return form, branch if return_branch else form

    async def _select(
        self,
        form=None,
        choices=None,
        reason=False,
        confidence_score=None,
        instruction=None,
        template=None,
        context=None,
        branch=None,
        **kwargs,
    ):
        branch = branch or self.branch

        if not form:
            form = template(
                choices=choices,
                reason=reason,
                confidence_score=confidence_score,
                instruction=instruction,
                context=context,
            )

        return await self._chat(form=form, return_form=True, **kwargs)

    async def _predict(
        self,
        form=None,
        num_sentences=None,
        reason=False,
        confidence_score=None,
        instruction=None,
        context=None,
        branch=None,
        template=None,
        **kwargs,
    ):
        branch = branch or self.branch

        if not form:
            form = template(
                instruction=instruction,
                context=context,
                num_sentences=num_sentences,
                confidence_score=confidence_score,
                reason=reason,
            )

        return await self._chat(form=form, return_form=True, **kwargs)