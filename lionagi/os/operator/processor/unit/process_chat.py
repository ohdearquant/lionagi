"""Chat processing module for the Lion framework."""

from __future__ import annotations

import asyncio
from typing import Any, Literal, TYPE_CHECKING

from lion_core.action.function_calling import FunctionCalling
from lion_core.exceptions import ItemNotFoundError
from lion_core.session.utils import parse_action_request

from lionagi.os.primitives.form.form import Form
from lionagi.os.primitives.messages import Instruction, ActionRequest, System
from lionagi.os.file.image.utils import ImageUtil
from lionagi.os.operator.imodel.imodel import iModel
from lionagi.os.operator.validator.validator import Validator
from lionagi.os.operator.processor.unit.utils import parse_model_response

if TYPE_CHECKING:
    from lionagi.os.session.branch.branch import Branch


def create_chat_config(
    branch: Branch,
    form: Form,
    *,
    system: dict | str | System | None = None,
    system_metadata: Any = None,
    system_datetime: bool | str | None = None,
    delete_previous_system: bool = False,
    instruction: dict | str | Instruction | None = None,
    context: dict | str | None = None,
    action_request: ActionRequest | None = None,
    image: str | list[str] | None = None,
    image_path: str | None = None,
    sender: Any = None,
    recipient: Any = None,
    requested_fields: dict[str, str] | None = None,
    metadata: Any = None,
    tools: bool | None = None,
    model_config: dict | None = None,
    **kwargs: Any,
) -> dict:
    """
    Create chat configuration for the branch.

    Args:
        branch: The branch to configure.
        form: The form associated with the chat.
        system: System message configuration.
        system_metadata: Additional system metadata.
        system_datetime: Datetime for the system message.
        delete_previous_system: Whether to delete the previous system message.
        instruction: Instruction for the chat.
        context: Additional context for the chat.
        action_request: Action request for the chat.
        image: Image data for the chat.
        image_path: Path to an image file.
        sender: Sender of the message.
        recipient: Recipient of the message.
        requested_fields: Fields requested in the response.
        metadata: Additional metadata for the instruction.
        tools: Whether to include tools in the configuration.
        model_config: Additional model configuration.
        **kwargs: Additional keyword arguments for the model.

    Returns:
        A dictionary containing the chat configuration.
    """
    if system:
        branch.add_message(
            system=system,
            system_datetime=system_datetime,
            metadata=system_metadata,
            delete_previous_system=delete_previous_system,
        )

    if image_path:
        image = [image] if image and not isinstance(image, list) else image
        image.append(ImageUtil.read_image_to_base64(image_path))
        image = [i for i in image if i is not None]

    if not form:
        if not recipient:
            recipient = branch.ln_id

        branch.add_message(
            instruction=instruction,
            context=context,
            sender=sender,
            recipient=recipient,
            requested_fields=requested_fields,
            image=image,
            metadata=metadata,
            action_request=action_request,
        )
    else:
        branch.add_message(
            instruction=Instruction.from_form(form),
            context=context,
            sender=sender,
            recipient=recipient,
            images=image,
            metadata=metadata,
            action_request=action_request,
        )

    if "tool_parsed" in kwargs:
        kwargs.pop("tool_parsed")
        tool_kwarg = {"tools": tools}
        kwargs = tool_kwarg | kwargs
    elif tools and branch.has_tools:
        kwargs = branch.tool_manager.get_tool_schema(tools=tools, **kwargs)

    config = {**model_config, **kwargs}
    if sender is not None:
        config["sender"] = sender

    return config


def parse_chatcompletion(
    branch: Branch,
    imodel: iModel,
    payload: dict,
    completion: dict,
    sender: str,
    costs: tuple[float, float] | None = None,
) -> Any:
    """
    Parse chat completion and update branch.

    Args:
        branch: The branch to update.
        imodel: The iModel instance.
        payload: The payload sent to the model.
        completion: The completion response from the model.
        sender: The sender of the message.
        costs: A tuple of (prompt_cost, completion_cost) per token.

    Returns:
        The parsed message or None if parsing fails.
    """
    msg_ = None
    imodel = imodel or branch.imodel

    if "choices" in completion:
        payload.pop("messages", None)
        branch.update_last_instruction_meta(payload)
        _choices = completion.pop("choices", None)

        def process_completion_choice(choice, price=None):
            if isinstance(choice, dict):
                msg = choice.pop("message", None)
                _completion = completion.copy()
                _completion.update(choice)
                branch.add_message(
                    assistant_response=msg,
                    metadata=_completion,
                    sender=sender,
                )
            a = branch.messages[-1].metadata.get(["extra", "usage", "prompt_tokens"], 0)
            b = branch.messages[-1].metadata.get(
                ["extra", "usage", "completion_tokens"], 0
            )
            m = completion.get("model", None)
            if m:
                ttl = (a * price[0] + b * price[1]) / 1_000_000
                branch.messages[-1].metadata.insert(["extra", "usage", "expense"], ttl)
            return msg

        if _choices and not isinstance(_choices, list):
            _choices = [_choices]

        if _choices and isinstance(_choices, list):
            for _choice in _choices:
                msg_ = process_completion_choice(_choice, price=costs)

        # the imodel.endpoints still needs doing
        imodel.endpoints["chat/completions"].status_tracker.num_tasks_succeeded += 1
    else:
        imodel.endpoints["chat/completions"].status_tracker.num_tasks_failed += 1

    return msg_


async def process_action_request(
    branch: Branch,
    _msg: dict | None = None,
    invoke_tool: bool = True,
    action_request: list[ActionRequest] | dict | str | None = None,
) -> Any:
    """
    Process action requests.

    Args:
        branch: The branch to process action requests for.
        msg: The message containing action requests.
        invoke_tool: Whether to invoke the tools associated with the action requests.
        action_request: Explicit action requests to process.

    Returns:
        The processed message or None if no action requests were processed.

    Raises:
        ItemNotFoundError: If a requested tool is not found in the registry.
    """
    action_request: list[ActionRequest] = action_request or parse_action_request(_msg)
    if not action_request:
        return _msg if _msg else False

    for i in action_request:
        if i.content.get(["action_request", "function"], None) in branch.tool_manager:
            i.recipient = branch.tool_manager.registry[
                i.content["action_request", "function"]
            ].ln_id

        else:
            raise ItemNotFoundError(
                f"Tool {i.content.get(["action_request", "function"], "N/A")} not "
                "found in registry"
            )
        branch.add_message(action_request=i, recipient=i.recipient)

    if invoke_tool:
        tasks = []
        for i in action_request:
            func_ = i.content.get["action_request", "function"]
            args_ = i.content.get["action_request", "arguments"]

            tool = branch.tool_manager.registry[func_]
            func_call = FunctionCalling(tool, args_)
            tasks.append(asyncio.create_task(func_call.invoke()))

        results = await asyncio.gather(*tasks)

        for idx, item in enumerate(results):
            if item is not None:
                branch.add_message(
                    action_request=action_request[idx],
                    func_outputs=item,
                    sender=action_request[idx].recipient,
                    recipient=action_request[idx].sender,
                )
    return None


async def process_validation(
    form: Form,
    validator: Validator,
    response_: dict | str,
    rulebook: Any = None,
    strict: bool = False,
    use_annotation: bool = True,
    template_name: str | None = None,
) -> Form:
    """
    Process form validation.

    Args:
        form: The form to validate.
        validator: The validator to use.
        response_: The response to validate.
        rulebook: Optional rulebook for validation.
        strict: Whether to use strict validation.
        use_annotation: Whether to use annotation for validation.
        template_name: Optional template name to set on the form.

    Returns:
        The validated form.
    """
    validator = Validator(rulebook=rulebook) if rulebook else validator
    form = await validator.validate_response(
        form=form,
        response=response_,
        strict=strict,
        use_annotation=use_annotation,
    )
    if template_name:
        form.template_name = template_name

    return form


async def process_chat(
    branch: Branch,
    form: Form,
    *,
    clear_messages: bool = False,
    system: System | Any = None,
    system_metadata: dict | None = None,
    system_datetime: bool | str | None = None,
    delete_previous_system: bool = False,
    instruction: Instruction | None = None,
    context: dict | str | None = None,
    action_request: ActionRequest | None = None,
    image: str | list[str] | None = None,
    image_path: str | None = None,
    sender: Any = None,
    recipient: Any = None,
    requested_fields: dict[str, str] | None = None,
    metadata: Any = None,
    tools: bool = False,
    invoke_tool: bool = True,
    model_config: dict | None = None,
    imodel: iModel | None = None,
    handle_unmatched: Literal["ignore", "raise", "remove", "force"] = "force",
    fill_value: Any = None,
    fill_mapping: dict[str, Any] | None = None,
    validator: Validator | None = None,
    rulebook: Any = None,
    strict_validation: bool = False,
    use_annotation: bool = True,
    return_branch: bool = False,
    **kwargs: Any,
) -> tuple[Branch, Any] | Any:
    """
    Process chat interaction.

    Args:
        branch: The branch to process the chat for.
        form: The form associated with the chat.
        clear_messages: Whether to clear existing messages.
        system: System message configuration.
        system_metadata: Additional system metadata.
        system_datetime: Datetime for the system message.
        delete_previous_system: Whether to delete the previous system message.
        instruction: Instruction for the chat.
        context: Additional context for the chat.
        action_request: Action request for the chat.
        image: Image data for the chat.
        image_path: Path to an image file.
        sender: Sender of the message.
        recipient: Recipient of the message.
        requested_fields: Fields requested in the response.
        metadata: Additional metadata for the instruction.
        tools: Whether to include tools in the configuration.
        invoke_tool: Whether to invoke tools for action requests.
        model_config: Additional model configuration.
        imodel: The iModel to use for chat completion.
        handle_unmatched: Strategy for handling unmatched fields.
        fill_value: Value to use for filling unmatched fields.
        fill_mapping: Mapping for filling unmatched fields.
        validator: The validator to use for form validation.
        rulebook: Optional rulebook for validation.
        strict_validation: Whether to use strict validation.
        use_annotation: Whether to use annotation for validation.
        return_branch: Whether to return the branch along with the result.
        **kwargs: Additional keyword arguments for the model.

    Returns:
        The processed result, optionally including the branch.
    """
    if clear_messages:
        branch.clear()

    config = create_chat_config(
        branch,
        form,
        system=system,
        system_metadata=system_metadata,  # system metadata
        system_datetime=system_datetime,
        delete_previous_system=delete_previous_system,
        instruction=instruction,
        context=context,
        action_request=action_request,
        image=image,
        image_path=image_path,
        sender=sender,
        recipient=recipient,
        requested_fields=requested_fields,
        metadata=metadata,  # extra metadata for the instruction
        tools=tools,
        model_config=model_config,
        **kwargs,
    )

    imodel = imodel or branch.imodel
    payload, completion = await imodel.chat(branch.to_chat_messages(), **config)
    costs = imodel.endpoints.get(["chat/completions", "model", "costs"], (0, 0))

    _msg = parse_chatcompletion(
        branch=branch,
        imodel=imodel,
        payload=payload,
        completion=completion,
        sender=sender,
        costs=costs,
    )

    if _msg is None:
        return None

    _res = parse_model_response(
        content_=_msg,
        requested_fields=requested_fields,
        handle_unmatched=handle_unmatched,
        fill_value=fill_value,
        fill_mapping=fill_mapping,
        strict=False,
    )

    await process_action_request(
        branch=branch,
        _msg=_res,
        invoke_tool=invoke_tool,
        action_request=action_request,
    )

    validator = validator or branch.validator
    if form:
        form = await process_validation(
            form,
            validator,
            _res,
            rulebook=rulebook,
            strict=strict_validation,
            use_annotation=use_annotation,
            template_name=None,
        )
        return branch, form if return_branch else form.work_fields

    return branch, _res if return_branch else _res
