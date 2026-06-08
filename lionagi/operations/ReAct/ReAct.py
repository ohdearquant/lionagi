# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: N999  -- module intentionally named ReAct (acronym); rename breaks public API

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel

from lionagi.libs.schema.as_readable import as_readable
from lionagi.libs.validate.common_field_validators import validate_model_to_type
from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.models.field_model import FieldModel
from lionagi.service.imodel import iModel

from ..fields import Instruct
from ..types import ActionParam, ChatParam, HandleValidation, InterpretParam, ParseParam
from .utils import Analysis, ReActAnalysis

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

B = TypeVar("B", bound=type[BaseModel])


async def ReAct(  # noqa: N802  # public name is the ReAct acronym
    branch: "Branch",
    instruct: Instruct | dict[str, Any],
    interpret: bool = False,
    interpret_domain: str | None = None,
    interpret_style: str | None = None,
    interpret_sample: str | None = None,
    interpret_model: iModel | None = None,
    interpret_kwargs: dict | None = None,
    tools: Any = None,
    tool_schemas: Any = None,
    response_format: type[BaseModel] | BaseModel = None,
    intermediate_response_options: list[BaseModel] | BaseModel = None,
    intermediate_listable: bool = False,
    reasoning_effort: Literal["low", "medium", "high"] = None,
    extension_allowed: bool = True,
    max_extensions: int | None = 3,
    response_kwargs: dict | None = None,
    display_as: Literal["json", "yaml"] = "yaml",
    return_analysis: bool = False,
    analysis_model: iModel | None = None,
    verbose_analysis: bool = False,
    verbose_length: int = None,
    include_token_usage_to_model: bool = True,
    continue_after_failed_response: bool = False,
    **kwargs,
):
    """ReAct reasoning loop with legacy API - wrapper around ReAct_v1."""

    # Handle legacy verbose parameter
    if "verbose" in kwargs:
        verbose_analysis = kwargs.pop("verbose")

    # Convert Instruct to dict if needed
    instruct_dict = instruct.to_dict() if isinstance(instruct, Instruct) else dict(instruct)

    # Build InterpretParam if interpretation requested
    intp_param = None
    if interpret:
        intp_param = InterpretParam(
            domain=interpret_domain or "general",
            style=interpret_style or "concise",
            sample_writing=interpret_sample or "",
            imodel=interpret_model or analysis_model or branch.chat_model,
            imodel_kw=interpret_kwargs or {},
        )

    chat_param = ChatParam.from_branch(
        branch,
        guidance=instruct_dict.get("guidance"),
        context=instruct_dict.get("context"),
        tool_schemas=tool_schemas or [],
        include_token_usage_to_model=include_token_usage_to_model,
        imodel=analysis_model or branch.chat_model,
        imodel_kw=kwargs,
    )

    # Build ActionParam
    action_param = None
    if tools is not None or tool_schemas is not None:
        from ..act.act import _get_default_call_params

        action_param = ActionParam(
            action_call_params=_get_default_call_params(),
            tools=tools or True,
            strategy="concurrent",
            suppress_errors=True,
            verbose_action=False,
        )

    # Build ParseParam
    from ..parse.parse import get_default_call

    parse_param = ParseParam(
        response_format=ReActAnalysis,  # Initial format
        fuzzy_match_params=FuzzyMatchKeysParams(),
        handle_validation="return_value",
        alcall_params=get_default_call(),
        imodel=analysis_model or branch.chat_model,
        imodel_kw={},
    )

    # Response context for final answer
    resp_ctx = response_kwargs or {}
    if response_format:
        resp_ctx["response_format"] = response_format

    return await ReAct_v1(
        branch,
        instruction=instruct_dict.get("instruction", str(instruct)),
        chat_param=chat_param,
        action_param=action_param,
        parse_param=parse_param,
        intp_param=intp_param,
        resp_ctx=resp_ctx,
        reasoning_effort=reasoning_effort,
        reason=True,  # ReAct always uses reasoning
        field_models=None,
        handle_validation="return_value",
        invoke_actions=True,  # ReAct always invokes actions
        clear_messages=False,
        intermediate_response_options=intermediate_response_options,
        intermediate_listable=intermediate_listable,
        intermediate_nullable=False,
        max_extensions=max_extensions,
        extension_allowed=extension_allowed,
        verbose_analysis=verbose_analysis,
        display_as=display_as,
        verbose_length=verbose_length,
        continue_after_failed_response=continue_after_failed_response,
        return_analysis=return_analysis,
        between_rounds=kwargs.get("between_rounds"),
    )


async def ReAct_v1(  # noqa: N802  # public name preserves the ReAct acronym
    branch: "Branch",
    instruction: str,
    chat_param: ChatParam,
    action_param: ActionParam | None = None,
    parse_param: ParseParam | None = None,
    intp_param: InterpretParam | None = None,
    resp_ctx: dict | None = None,
    reasoning_effort: Literal["low", "medium", "high"] | None = None,
    reason: bool = False,
    field_models: list[FieldModel] | None = None,
    handle_validation: HandleValidation = "raise",
    invoke_actions: bool = True,
    clear_messages=False,
    intermediate_response_options: B | list[B] = None,
    intermediate_listable: bool = False,
    intermediate_nullable: bool = False,
    max_extensions: int | None = 0,
    extension_allowed: bool = True,
    verbose_analysis: bool = False,
    display_as: Literal["yaml", "json"] = "yaml",
    verbose_length: int = None,
    continue_after_failed_response: bool = False,
    return_analysis: bool = False,
    between_rounds: Callable[["Branch", int], Awaitable[str | None]] | None = None,
):
    """
    Context-based ReAct implementation - collects all outputs from ReActStream.

    Args:
        return_analysis: If True, returns list of all intermediate analyses.
                        If False, returns only the final result.
    """
    outs = []

    if verbose_analysis:
        async for i in ReActStream(
            branch=branch,
            instruction=instruction,
            chat_param=chat_param,
            action_param=action_param,
            parse_param=parse_param,
            intp_param=intp_param,
            resp_ctx=resp_ctx,
            reasoning_effort=reasoning_effort,
            reason=reason,
            field_models=field_models,
            handle_validation=handle_validation,
            invoke_actions=invoke_actions,
            clear_messages=clear_messages,
            intermediate_response_options=intermediate_response_options,
            intermediate_listable=intermediate_listable,
            intermediate_nullable=intermediate_nullable,
            max_extensions=max_extensions,
            extension_allowed=extension_allowed,
            verbose_analysis=verbose_analysis,
            display_as=display_as,
            verbose_length=verbose_length,
            continue_after_failed_response=continue_after_failed_response,
            between_rounds=between_rounds,
        ):
            analysis, str_ = i
            # str_ is already formatted markdown - just print it
            as_readable(
                str_,
                md=True,
                display_str=True,
            )
            outs.append(analysis)
    else:
        async for i in ReActStream(
            branch=branch,
            instruction=instruction,
            chat_param=chat_param,
            action_param=action_param,
            parse_param=parse_param,
            intp_param=intp_param,
            resp_ctx=resp_ctx,
            reasoning_effort=reasoning_effort,
            reason=reason,
            field_models=field_models,
            handle_validation=handle_validation,
            invoke_actions=invoke_actions,
            clear_messages=clear_messages,
            intermediate_response_options=intermediate_response_options,
            intermediate_listable=intermediate_listable,
            intermediate_nullable=intermediate_nullable,
            max_extensions=max_extensions,
            extension_allowed=extension_allowed,
            display_as=display_as,
            verbose_length=verbose_length,
            continue_after_failed_response=continue_after_failed_response,
            between_rounds=between_rounds,
        ):
            outs.append(i)

    if return_analysis:
        return outs

    # Extract answer from the final Analysis object
    final_result = outs[-1]
    if hasattr(final_result, "answer"):
        return final_result.answer
    return final_result


def handle_field_models(
    field_models: list[FieldModel] | None,
    intermediate_response_options: B | list[B] = None,
    intermediate_listable: bool = False,
    intermediate_nullable: bool = False,
):
    """Build field models including intermediate response options."""
    fms = [] if not field_models else field_models

    if intermediate_response_options:

        def create_intermediate_response_field_model():
            from lionagi.models import OperableModel

            _iro = intermediate_response_options
            iro = [_iro] if not isinstance(_iro, list) else _iro
            opm = OperableModel()

            for i in iro:
                type_ = validate_model_to_type(None, i)
                opm.add_field(
                    str(type_.__name__).lower(),
                    annotation=type_ | None,
                    # Remove lambda validator to avoid Pydantic serialization errors
                )

            m_ = opm.new_model(name="IntermediateResponseOptions")
            irfm = FieldModel(
                name="intermediate_response_options",
                base_type=m_,
                description="Intermediate deliverable outputs. fill as needed ",
                # Remove lambda validator to avoid Pydantic serialization errors
            )

            if intermediate_listable:
                irfm = irfm.as_listable()

            if intermediate_nullable:
                irfm = irfm.as_nullable()

            return irfm

        fms = [fms] if not isinstance(fms, list) else fms
        fms += [create_intermediate_response_field_model()]

    return fms


async def ReActStream(  # noqa: N802  # public name preserves the ReAct acronym
    branch: "Branch",
    instruction: str,
    chat_param: ChatParam,
    action_param: ActionParam | None = None,
    parse_param: ParseParam | None = None,
    intp_param: InterpretParam | None = None,
    resp_ctx: dict | None = None,
    reasoning_effort: Literal["low", "medium", "high"] | None = None,
    reason: bool = False,
    field_models: list[FieldModel] | None = None,
    handle_validation: HandleValidation = "raise",
    invoke_actions: bool = True,
    clear_messages=False,
    intermediate_response_options: B | list[B] = None,
    intermediate_listable: bool = False,
    intermediate_nullable: bool = False,
    max_extensions: int | None = 0,
    extension_allowed: bool = True,
    verbose_analysis: bool = False,
    display_as: Literal["yaml", "json"] = "yaml",
    verbose_length: int = None,
    continue_after_failed_response: bool = False,
    between_rounds: Callable[["Branch", int], Awaitable[str | None]] | None = None,
) -> AsyncGenerator:
    """Core ReAct streaming implementation with context-based architecture."""

    # Validate and clamp max_extensions
    if max_extensions and max_extensions > 100:
        logger.warning("max_extensions should not exceed 100; defaulting to 100.")
        max_extensions = 100

    def verbose_yield(title, s_):
        if verbose_analysis:
            str_ = title + "\n"
            str_ += as_readable(
                s_,
                md=True,
                format_curly=True if display_as == "yaml" else False,
                max_chars=verbose_length,
            )
            return s_, str_
        else:
            return s_

    # Step 1: Interpret instruction if requested
    intp = None
    if intp_param:
        from ..interpret.interpret import interpret

        intp = await interpret(branch, instruction, intp_param)

    # Step 2: Handle field models
    fms = handle_field_models(
        field_models,
        intermediate_response_options,
        intermediate_listable,
        intermediate_nullable,
    )

    # Step 3: Initial ReAct analysis
    from ..operate.operate import operate

    # Build context for initial analysis
    initial_chat_param = chat_param.with_updates(response_format=ReActAnalysis)

    initial_parse_param = (
        parse_param.with_updates(response_format=ReActAnalysis) if parse_param else None
    )

    # Add proper extension prompt for initial analysis
    initial_instruction = instruction
    if intp is not None:
        initial_instruction += f"\n\nInterpreted instruction: {intp}"

    if extension_allowed and max_extensions:
        initial_instruction += "\n\n" + ReActAnalysis.FIRST_EXT_PROMPT.format(
            extensions=max_extensions
        )

    analysis = await operate(
        branch,
        instruction=initial_instruction,
        chat_param=initial_chat_param,
        action_param=action_param,
        parse_param=initial_parse_param,
        handle_validation=handle_validation,
        invoke_actions=invoke_actions,
        skip_validation=False,
        clear_messages=clear_messages,
        reason=reason,
        field_models=fms,
    )

    out = verbose_yield("\n### ReAct Round No.1 Analysis:\n", analysis)
    yield out

    # Step 4: Extension loop
    extensions = max_extensions or 0
    round_count = 1

    def _need_extension(analysis):
        if hasattr(analysis, "extension_needed"):
            return analysis.extension_needed
        if isinstance(analysis, dict):
            return analysis.get("extension_needed", False)
        return False

    def _extension_allowed(exts):
        return extension_allowed and exts > 0

    def prepare_analysis_kwargs(exts):
        new_instruction = None
        if exts == max_extensions:
            new_instruction = ReActAnalysis.FIRST_EXT_PROMPT.format(extensions=exts)
        else:
            new_instruction = ReActAnalysis.CONTINUE_EXT_PROMPT.format(extensions=exts)

        # Use with_updates to create new context instances
        updates = {"response_format": ReActAnalysis}

        if reasoning_effort:
            guide = {
                "low": "Quick concise reasoning.\n",
                "medium": "Reasonably balanced reasoning.\n",
                "high": "Thorough, try as hard as you can in reasoning.\n",
            }.get(reasoning_effort, "")

            updates["guidance"] = (guide or "") + (chat_param.guidance or "")
            updates["imodel_kw"] = {
                **(chat_param.imodel_kw or {}),
                "reasoning_effort": reasoning_effort,
            }

        _cctx = chat_param.with_updates(**updates)

        # Import default call params if needed
        from ..act.act import _get_default_call_params

        _actx = (
            action_param.with_updates(strategy=getattr(analysis, "action_strategy", "concurrent"))
            if action_param
            else ActionParam(
                action_call_params=_get_default_call_params(),
                tools=True,
                strategy=getattr(analysis, "action_strategy", "concurrent"),
                suppress_errors=True,
                verbose_action=False,
            )
        )

        return {
            "instruction": new_instruction,
            "chat_param": _cctx,
            "action_param": _actx,
            "reason": reason,
            "field_models": fms,
        }

    while _extension_allowed(extensions) and _need_extension(analysis):
        if between_rounds:
            injected = await between_rounds(branch, round_count)
            if injected:
                from ..operate.operate import operate as _op

                _inj_chat = chat_param.with_updates(response_format=ReActAnalysis)
                analysis = await _op(
                    branch,
                    instruction=injected,
                    chat_param=_inj_chat,
                    action_param=action_param,
                    parse_param=(
                        parse_param.with_updates(response_format=ReActAnalysis)
                        if parse_param
                        else None
                    ),
                    handle_validation=handle_validation,
                    invoke_actions=invoke_actions,
                    skip_validation=False,
                    clear_messages=False,
                    reason=reason,
                    field_models=fms,
                )
                round_count += 1
                out = verbose_yield(f"\n### ReAct Round No.{round_count} (injected):\n", analysis)
                yield out
                if extensions:
                    extensions -= 1
                continue

        kwargs = prepare_analysis_kwargs(extensions)

        # Build parse context for extension
        ext_parse_param = (
            parse_param.with_updates(response_format=kwargs["chat_param"].response_format)
            if parse_param
            else None
        )

        analysis = await operate(
            branch,
            instruction=kwargs["instruction"],
            chat_param=kwargs["chat_param"],
            action_param=kwargs.get("action_param"),
            parse_param=ext_parse_param,
            handle_validation=handle_validation,
            invoke_actions=invoke_actions,
            skip_validation=False,
            clear_messages=False,  # Keep messages to maintain context
            reason=kwargs.get("reason", True),
            field_models=kwargs.get("field_models"),
        )
        round_count += 1

        if isinstance(analysis, dict) and all(i is None for i in analysis.values()):
            if not continue_after_failed_response:
                raise ValueError(
                    "All values in the response are None. "
                    "This might be due to a failed response. "
                    "Set `continue_after_failed_response=True` to ignore this error."
                )

        out = verbose_yield(f"\n### ReAct Round No.{round_count} Analysis:\n", analysis)
        yield out

        if extensions:
            extensions -= 1

    # Step 5: Final answer
    answer_prompt = ReActAnalysis.ANSWER_PROMPT.format(instruction=instruction)

    final_response_format = resp_ctx.get("response_format") if resp_ctx else None
    if not final_response_format:
        final_response_format = Analysis

    # Build contexts for final answer
    resp_ctx_updates = {"response_format": final_response_format}
    if resp_ctx:
        # Merge resp_ctx into updates (filter allowed keys)
        for k, v in resp_ctx.items():
            if k in chat_param.allowed() and k != "response_format":
                resp_ctx_updates[k] = v

    final_chat_param = chat_param.with_updates(**resp_ctx_updates)

    final_parse_param = (
        parse_param.with_updates(response_format=final_response_format) if parse_param else None
    )

    # Build operate kwargs, honoring response_kwargs
    operate_kwargs = {
        "branch": branch,
        "instruction": answer_prompt,
        "chat_param": final_chat_param,
        "action_param": None,  # No actions in final answer
        "parse_param": final_parse_param,
        "invoke_actions": False,
        "clear_messages": False,
        "reason": False,  # No reasoning wrapper in final answer
        "field_models": None,
        # Defaults that can be overridden by resp_ctx
        "handle_validation": handle_validation,
        "skip_validation": False,
    }

    # Honor response_kwargs for final answer generation
    if resp_ctx:
        # Extract operate specific parameters from resp_ctx
        operate_params = {
            "skip_validation",
            "handle_validation",
            "clear_messages",
            "reason",
            "field_models",
        }
        for param in operate_params:
            if param in resp_ctx:
                operate_kwargs[param] = resp_ctx[param]

    try:
        out = await operate(**operate_kwargs)

        if isinstance(out, dict) and all(i is None for i in out.values()):
            if not continue_after_failed_response:
                raise ValueError(
                    "All values in the response are None. "
                    "This might be due to a failed response. "
                    "Set `continue_after_failed_response=True` to ignore this error."
                )
    except Exception:
        out = branch.msgs.last_response.response

    # Don't extract .answer - return the full Analysis object
    _o = verbose_yield("\n### ReAct Final Answer:\n", out)
    yield _o
