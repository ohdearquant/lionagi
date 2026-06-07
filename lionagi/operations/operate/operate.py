# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING, Literal, Union

from pydantic import BaseModel, JsonValue

from lionagi.ln import AlcallParams
from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.ln.types import Spec
from lionagi.models import FieldModel
from lionagi.protocols.generic import Progression
from lionagi.protocols.messages import Instruction, SenderRecipient

from ..fields import Instruct
from ..types import (
    ActionParam,
    ChatParam,
    HandleValidation,
    Middle,
    ParseParam,
    RunParam,
)

if TYPE_CHECKING:
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch, ToolRef

    from .operative import Operative


def prepare_operate_kw(
    branch: "Branch",
    *,
    instruct: Instruct = None,
    instruction: Instruction | JsonValue = None,
    guidance: JsonValue = None,
    context: JsonValue = None,
    sender: SenderRecipient = None,
    recipient: SenderRecipient = None,
    progression: Progression = None,
    chat_model: "iModel" = None,
    invoke_actions: bool = True,
    tool_schemas: list[dict] = None,
    images: list = None,
    image_detail: Literal["low", "high", "auto"] = None,
    parse_model: "iModel" = None,
    skip_validation: bool = False,
    handle_validation: HandleValidation = "return_value",
    tools: "ToolRef" = None,
    operative: "Operative" = None,
    response_format: type[BaseModel] = None,
    actions: bool = False,
    reason: bool = False,
    call_params: AlcallParams = None,
    action_strategy: Literal["sequential", "concurrent"] = "concurrent",
    verbose_action: bool = False,
    field_models: list[FieldModel | Spec] = None,
    include_token_usage_to_model: bool = False,
    clear_messages: bool = False,
    stream_persist: bool = False,
    persist_dir: str | None = None,
    snapshot_dir: str | None = None,
    middle: Middle | None = None,
    **kwargs,
) -> dict:
    from .._guards import reject_removed_kwargs

    reject_removed_kwargs(
        kwargs,
        {
            "request_model": "response_format=",
            "operative_model": "response_format=",
            "imodel": "chat_model=",
        },
        where="operate",
    )

    chat_model = chat_model or branch.chat_model
    parse_model = parse_model or chat_model

    # Convert dict-based instructions
    if isinstance(instruct, dict):
        instruct = Instruct(**instruct)

    instruct = instruct or Instruct(
        instruction=instruction,
        guidance=guidance,
        context=context,
    )

    if reason:
        instruct.reason = True
    if actions:
        instruct.actions = True
        if action_strategy:
            instruct.action_strategy = action_strategy

    # Convert field_models to Spec if needed
    fields_dict = None
    if field_models:
        fields_dict = {}
        for fm in field_models:
            # Convert FieldModel to Spec
            if isinstance(fm, FieldModel):
                spec = fm.to_spec()
            elif isinstance(fm, Spec):
                spec = fm
            else:
                raise TypeError(f"Expected FieldModel or Spec, got {type(fm)}")

            if spec.name:
                fields_dict[spec.name] = spec

    # Build Operative if needed
    operative = None
    if instruct.reason or instruct.actions or response_format or fields_dict:
        from .step import Step

        operative = Step.request_operative(
            base_type=response_format,
            reason=instruct.reason,
            actions=instruct.actions or actions,
            fields=fields_dict,
        )

        # Create response model
        operative = Step.respond_operative(operative)

    final_response_format = operative.response_type if operative else response_format

    # Choose ChatParam vs RunParam. RunParam is required when the middle
    # streams via run() (CLI endpoints, explicit stream_persist, or when
    # caller passes a middle that needs persist_dir). Defaulting to
    # RunParam for CLI endpoints keeps the call sites free of path plumbing.
    is_cli = bool(getattr(chat_model, "is_cli", False))
    use_run_param = is_cli or stream_persist or persist_dir is not None or snapshot_dir is not None

    param_cls = RunParam if use_run_param else ChatParam
    param_kw = dict(
        guidance=instruct.guidance,
        context=instruct.context,
        sender=sender or branch.user or "user",
        recipient=recipient or branch.id,
        response_format=final_response_format,
        progression=progression,
        tool_schemas=tool_schemas,
        images=images,
        image_detail=image_detail,
        plain_content=None,
        include_token_usage_to_model=include_token_usage_to_model,
        imodel=chat_model,
        imodel_kw=kwargs,
    )

    if use_run_param:
        param_kw["stream_persist"] = stream_persist
        if persist_dir is not None:
            param_kw["persist_dir"] = persist_dir
        if snapshot_dir is not None:
            param_kw["snapshot_dir"] = snapshot_dir
    chat_param = param_cls(**param_kw)

    parse_param = None
    if final_response_format and not skip_validation:
        from ..parse.parse import get_default_call

        parse_param = ParseParam(
            response_format=final_response_format,
            fuzzy_match_params=FuzzyMatchKeysParams(),
            handle_validation=handle_validation,
            alcall_params=get_default_call(),
            imodel=parse_model,
            imodel_kw={},
        )

    action_param = None
    if invoke_actions and (instruct.actions or actions):
        from ..act.act import _get_default_call_params

        action_param = ActionParam(
            action_call_params=call_params or _get_default_call_params(),
            tools=tools,
            strategy=action_strategy or instruct.action_strategy or "concurrent",
            suppress_errors=True,
            verbose_action=verbose_action,
        )

    return {
        "instruction": instruct.instruction,
        "chat_param": chat_param,
        "parse_param": parse_param,
        "action_param": action_param,
        "handle_validation": handle_validation,
        "invoke_actions": invoke_actions,
        "skip_validation": skip_validation,
        "clear_messages": clear_messages,
        "operative": operative,
        "middle": middle,
    }


async def operate(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    action_param: ActionParam | None = None,
    parse_param: ParseParam | None = None,
    handle_validation: HandleValidation = "return_value",
    invoke_actions: bool = True,
    skip_validation: bool = False,
    clear_messages: bool = False,
    reason: bool = False,
    field_models: list[FieldModel | Spec] | None = None,
    operative: Union["Operative", None] = None,
    middle: Middle | None = None,
) -> BaseModel | dict | str | None:
    """Execute operation with optional action handling.

    Args:
        branch: Branch instance
        instruction: Instruction or JSON value
        chat_param: Chat parameters
        action_param: Action parameters
        parse_param: Parse parameters
        handle_validation: Validation handling strategy
        invoke_actions: Whether to invoke actions
        skip_validation: Whether to skip validation
        clear_messages: Whether to clear messages
        reason: Whether to include reasoning
        field_models: List of FieldModel or Spec objects
        operative: Operative instance

    Returns:
        Result of operation
    """
    _cctx = chat_param
    _pctx = (
        parse_param.with_updates(handle_validation="return_value")
        if parse_param
        else ParseParam(
            response_format=chat_param.response_format,
            imodel=branch.parse_model,
            handle_validation="return_value",
        )
    )

    # Update tool schemas. get_tool_schema returns {"tools": [schema, ...]};
    # the Instruction renders tool_schemas as a flat list ("Tools:" section), so
    # unwrap here — feeding the dict through nests it as `- tools:` under each
    # entry, which the model reads as having no usable tools.
    if tools := (action_param.tools or True) if action_param else None:
        tool_schemas = branch.acts.get_tool_schema(tools=tools)
        if isinstance(tool_schemas, dict):
            tool_schemas = tool_schemas.get("tools", [])
        _cctx = _cctx.with_updates(tool_schemas=tool_schemas)

    # Extract model class
    model_class = None
    if chat_param.response_format is not None:
        if isinstance(chat_param.response_format, type) and issubclass(
            chat_param.response_format, BaseModel
        ):
            model_class = chat_param.response_format
        elif isinstance(chat_param.response_format, BaseModel):
            model_class = type(chat_param.response_format)

    # Convert field_models to fields dict
    fields_dict = None
    if field_models:
        fields_dict = {}
        for fm in field_models:
            if isinstance(fm, FieldModel):
                spec = fm.to_spec()
            elif isinstance(fm, Spec):
                spec = fm
            else:
                raise TypeError(f"Expected FieldModel or Spec, got {type(fm)}")

            if spec.name:
                fields_dict[spec.name] = spec

    # Create operative if needed
    if not operative and (model_class or action_param or fields_dict):
        from .step import Step

        operative = Step.request_operative(
            base_type=model_class,
            reason=reason,
            actions=bool(action_param),
            fields=fields_dict,
        )
        operative = Step.respond_operative(operative)

        # Update contexts
        response_fmt = operative.response_type or model_class
        if response_fmt:
            _cctx = _cctx.with_updates(response_format=response_fmt)
            _pctx = _pctx.with_updates(response_format=response_fmt)

    if middle is None:
        if isinstance(_cctx, RunParam) or getattr(branch.chat_model, "is_cli", False):
            from ..run.run import run_and_collect

            middle = run_and_collect
        else:
            from ..communicate.communicate import communicate

            middle = communicate

    result = await middle(
        branch,
        instruction,
        _cctx,
        _pctx,
        clear_messages,
        skip_validation=skip_validation,
    )

    if skip_validation:
        return result

    if model_class and not isinstance(result, model_class):
        match handle_validation:
            case "return_value":
                return result
            case "return_none":
                return None
            case "raise":
                expected_name = getattr(model_class, "__name__", repr(model_class))
                received_snippet = repr(result)[:200]
                raise ValueError(
                    f"Failed to parse LLM response into '{expected_name}'. "
                    f"Received (truncated): {received_snippet}. "
                    f"Hint: verify the model supports structured JSON output "
                    f"(e.g. response_format / function-calling) for this provider."
                )

    if not invoke_actions:
        return result

    # Handle actions. Middle may return a BaseModel (structured), a dict
    # (fuzzy-parsed), or a raw str (CLI text path with no response_format).
    # Only dicts and BaseModels can carry action_requests — raw text can't.
    if model_class:
        requests = getattr(result, "action_requests", None)
    elif isinstance(result, dict):
        requests = result.get("action_requests")
    else:
        requests = None

    action_response_models = None
    if action_param and requests is not None:
        from ..act.act import act

        action_response_models = await act(branch, requests, action_param)

    if not action_response_models:
        return result

    # Filter None values
    action_response_models = [r for r in action_response_models if r is not None]

    if not action_response_models:
        return result

    if not model_class:
        # Dict response: merge action_responses in. Raw-text results stay
        # untouched (text has no structured slot for action_responses).
        if isinstance(result, dict):
            result["action_responses"] = action_response_models
        return result

    # If we have model_class, we must have operative (created at line 268)
    # First set the response_model to the existing result
    operative.response_model = result
    # Then update it with action_responses
    operative.update_response_model(data={"action_responses": action_response_models})
    return operative.response_model
