# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING, Literal, Union

from pydantic import BaseModel, JsonValue

from lionagi.ln import AlcallParams
from lionagi.ln.types import Spec
from lionagi.models import FieldModel
from lionagi.protocols.generic import Progression
from lionagi.protocols.messages import Instruction, SenderRecipient

from .._defaults import STANDARD_REMOVED_KWARGS, make_parse_param
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


def _specs_from_fields(field_models: list) -> dict | None:
    if not field_models:
        return None
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
    return fields_dict or None


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

    reject_removed_kwargs(kwargs, STANDARD_REMOVED_KWARGS, where="operate")

    chat_model = chat_model or branch.chat_model
    parse_model = parse_model or chat_model

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

    is_cli = bool(getattr(chat_model, "is_cli", False))
    use_run_param = is_cli or stream_persist or persist_dir is not None or snapshot_dir is not None

    param_cls = RunParam if use_run_param else ChatParam
    param_kw = dict(
        guidance=instruct.guidance,
        context=instruct.context,
        sender=sender or branch.user or "user",
        recipient=recipient or branch.id,
        response_format=response_format,
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
    if response_format and not skip_validation:
        parse_param = make_parse_param(
            response_format, parse_model, handle_validation=handle_validation
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
        # Branch.operate() always sets operative=None so the single-construction block in operate() owns it.
        "operative": None,
        "middle": middle,
        "field_models": field_models,
        "reason": instruct.reason,
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
    """Execute one branch turn via Middle, optionally parse structured output and invoke tool actions."""
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

    # get_tool_schema returns {"tools": [...]}, but Instruction.tool_schemas expects a flat list — unwrap.
    if tools := (action_param.tools or True) if action_param else None:
        tool_schemas = branch.acts.get_tool_schema(tools=tools)
        if isinstance(tool_schemas, dict):
            tool_schemas = tool_schemas.get("tools", [])
        _cctx = _cctx.with_updates(tool_schemas=tool_schemas)

    model_class = None
    if chat_param.response_format is not None:
        if isinstance(chat_param.response_format, type) and issubclass(
            chat_param.response_format, BaseModel
        ):
            model_class = chat_param.response_format
        elif isinstance(chat_param.response_format, BaseModel):
            model_class = type(chat_param.response_format)

    fields_dict = _specs_from_fields(field_models)

    if not operative and (model_class or action_param or fields_dict or reason):
        from .step import Step

        operative = Step.request_operative(
            base_type=model_class,
            reason=reason,
            actions=bool(action_param),
            fields=fields_dict,
        )
        operative = Step.respond_operative(operative)

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

    # Inspect ANY BaseModel result, not just model_class: actions=True with no caller response_format
    # materializes an operative type while model_class stays None, so gating on model_class silently skips tools.
    if isinstance(result, BaseModel):
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

    action_response_models = [r for r in action_response_models if r is not None]

    if not action_response_models:
        return result

    if branch._context_providers:
        await branch._context_providers.gather_writeback(branch, action_response_models)

    if operative is not None and isinstance(result, BaseModel):
        operative.response_model = result
        operative.update_response_model(data={"action_responses": action_response_models})
        return operative.response_model

    if isinstance(result, dict):
        result["action_responses"] = action_response_models
    return result
