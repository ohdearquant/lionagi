"""Chat processing module for the Lion framework."""

from typing import Any, Literal, TYPE_CHECKING

from lion_core.unit.process_chat import (
    process_action_request,
    create_chat_config as _create_chat_config,
)

from lionagi.os.primitives.form.form import Form
from lionagi.os.primitives.messages import ActionRequest
from lionagi.os.file.image.utils import ImageUtil
from lionagi.os.operator.imodel.imodel import iModel
from lionagi.os.operator.validator.validator import Validator
from lionagi.os.operator.processor.unit.utils import parse_model_response


if TYPE_CHECKING:
    from lionagi.os.session.branch.branch import Branch


def create_chat_config(
    branch: Branch,
    *,
    form=None,
    sender=None,
    recipient=None,
    instruction: Any = None,
    context: Any = None,
    request_fields=None,
    system: Any = None,
    action_request: ActionRequest | None = None,
    images=None,
    image_path=None,
    image_detail: Literal["low", "high", "auto"] = None,
    system_datetime: bool | str | None = None,
    metadata: Any = None,
    delete_previous_system: bool = False,
    tools: bool | None = None,
    system_metadata: Any = None,
    model_config: dict | None = None,
    **kwargs: Any,  # additional model parameters
) -> dict:

    if image_path:
        images = [images] if images and not isinstance(images, list) else images
        images.append(ImageUtil.read_image_to_base64(image_path))
        images = [i for i in images if i is not None]

    return _create_chat_config(
        branch,
        form=form,
        sender=sender,
        recipient=recipient,
        instruction=instruction,
        context=context,
        request_fields=request_fields,
        system=system,
        action_request=action_request,
        images=images,
        image_detail=image_detail,
        system_datetime=system_datetime,
        metadata=metadata,
        delete_previous_system=delete_previous_system,
        tools=tools,
        system_metadata=system_metadata,
        model_config=model_config,
        **kwargs,
    )


async def parse_chatcompletion(
    branch: Branch,
    imodel: iModel,
    payload: dict,
    completion: dict,
    sender: str,
    costs: tuple[float, float] | None = None,
) -> Any:
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
        await imodel.update_status("chat/completions", "succeeded")
    else:
        await imodel.update_status("chat/completions", "failed")

    return msg_


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
    *,
    form=None,
    sender=None,
    recipient=None,
    instruction: Any = None,
    context: Any = None,
    request_fields=None,
    system: Any = None,
    action_request: ActionRequest | None = None,
    imodel=None,
    images=None,
    image_path=None,
    image_detail: Literal["low", "high", "auto"] = None,
    system_datetime: bool | str | None = None,
    metadata: Any = None,
    delete_previous_system: bool = False,
    tools: bool | None = None,
    system_metadata: Any = None,
    model_config: dict | None = None,
    clear_messages: bool = False,
    fill_value: Any = None,
    fill_mapping: dict[str, Any] | None = None,
    validator: Validator | None = None,
    rulebook=None,
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
        form=form,
        sender=sender,
        recipient=recipient,
        instruction=instruction,
        context=context,
        request_fields=request_fields,
        system=system,
        action_request=action_request,
        images=images,
        image_path=image_path,
        image_detail=image_detail,
        system_datetime=system_datetime,
        metadata=metadata,
        delete_previous_system=delete_previous_system,
        tools=tools,
        system_metadata=system_metadata,
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
        request_fields=request_fields,
        fill_value=fill_value,
        fill_mapping=fill_mapping,
        strict=False,
    )

    await process_action_request(
        branch=branch,
        _msg=_res,
        action_request=action_request,
    )

    if form:
        form = await process_validation(
            form=form,
            validator=validator,
            response_=_res,
            rulebook=rulebook,
            strict=strict_validation,
            use_annotation=use_annotation,
        )
        return branch, form if return_branch else form.work_fields

    return branch, _res if return_branch else _res
