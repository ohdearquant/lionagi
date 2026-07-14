# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from lionagi._errors import ConfigurationError
from lionagi.ln import AlcallParams
from lionagi.protocols.action.tool_hooks import ActionGovernanceDeniedError
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.messages import ActionRequest, ActionResponse

from .._defaults import get_default_action_call as _get_default_call_params
from ..fields import ActionResponseModel
from ..types import ActionParam

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


async def _act(
    branch: "Branch",
    action_request: BaseModel | dict | ActionRequest,
    suppress_errors: bool = False,
    verbose_action: bool = False,
):
    _request = action_request
    if isinstance(action_request, ActionRequest):
        _request = {
            "function": action_request.function,
            "arguments": action_request.arguments,
        }
    elif isinstance(action_request, BaseModel) and set(
        action_request.__class__.model_fields.keys()
    ) >= {"function", "arguments"}:
        _request = {
            "function": action_request.function,
            "arguments": action_request.arguments,
        }
    if not isinstance(_request, dict) or not {"function", "arguments"} <= set(_request.keys()):
        raise ValueError(
            "action_request must be an ActionRequest, BaseModel with 'function'"
            " and 'arguments', or dict with 'function' and 'arguments'."
        )

    # ADR-0047: governance gate — denied calls surface as tool results (not exceptions) so ReAct loops can adapt.
    from lionagi.session.control import ToolInvocation

    _args = _request["arguments"] if isinstance(_request["arguments"], dict) else {}
    if not await branch.authorize(
        ToolInvocation(function=_request["function"], arguments=_args, branch_id=str(branch.id))
    ):
        denial = {"error": "denied by governance gate", "function": _request["function"]}
        if not isinstance(action_request, ActionRequest):
            action_request = ActionRequest(content=_request, sender=branch.id, recipient=branch.id)
        if action_request not in branch.messages:
            await branch.msgs.a_add_message(action_request=action_request)
        await branch.msgs.a_add_message(
            action_request=action_request,
            action_output=denial,
            sender=branch.id,
            recipient=branch.id,
        )
        return ActionResponseModel(function=_request["function"], arguments=_args, output=denial)

    _hooks = branch._hooks
    _tool_name = _request["function"]
    _call_id = str(uuid.uuid4())
    _args_summary = str(_request["arguments"])[:200]

    if _hooks is not None:
        from lionagi.hooks.bus import HookPoint

        await _hooks.emit(
            HookPoint.TOOL_PRE,
            tool_name=_tool_name,
            call_id=_call_id,
            args_summary=_args_summary,
        )

    func_call = None
    try:
        if verbose_action:
            args_ = str(_request["arguments"])
            args_ = args_[:50] + "..." if len(args_) > 50 else args_
            logger.debug("Invoking action %s with %s.", _request["function"], args_)

        func_call = await branch._action_manager.invoke(_request)
        if verbose_action:
            logger.debug("Action %s invoked, status: %s.", _request["function"], func_call.status)

        # ActionManager.invoke() is total: governance denials, schema
        # revalidation failures, and ordinary tool exceptions are captured as
        # FAILED status + execution.error rather than raised. Every captured
        # failure must take the error path; otherwise it emits TOOL_POST and is
        # persisted as if a tool legitimately returned None.
        if func_call.status == EventStatus.FAILED:
            failure = func_call.execution.error
            if not isinstance(failure, BaseException):
                failure = RuntimeError(
                    str(failure) if failure else f"Action {_tool_name!r} failed without an error"
                )
            raise failure

        if _hooks is not None:
            from lionagi.hooks.bus import HookPoint

            _result_summary = str(func_call.response)[:200]
            await _hooks.emit(
                HookPoint.TOOL_POST,
                call_id=_call_id,
                tool_name=_tool_name,
                result_summary=_result_summary,
                duration=func_call.execution.duration,
            )

    except Exception as e:
        if _hooks is not None:
            from lionagi.hooks.bus import HookPoint

            await _hooks.emit(
                HookPoint.TOOL_ERROR,
                call_id=_call_id,
                tool_name=_tool_name,
                error=e,
                duration=(
                    func_call.execution.duration
                    if func_call is not None and func_call.status == EventStatus.FAILED
                    else None
                ),
            )

        content = {
            "error": str(e),
            "function": _request.get("function"),
            "arguments": _request.get("arguments"),
            "branch": str(branch.id),
        }
        captured_failure = func_call is not None and func_call.status == EventStatus.FAILED
        if captured_failure:
            await branch.emit_and_log(func_call)
        else:
            branch._log_manager.log(content)
        if verbose_action:
            logger.error("Action %s failed, error: %s.", _request["function"], e)
        if captured_failure or suppress_errors:
            error_msg = f"Error invoking action '{_request['function']}': {e}"
            if suppress_errors:
                logging.error(error_msg)

            # Surface the failure in chat history so subsequent rounds
            # (ReAct, LNDL retries) see the error and can self-correct.
            if not isinstance(action_request, ActionRequest):
                action_request = ActionRequest(
                    content=_request,
                    sender=branch.id,
                    recipient=branch.id,
                )
            if action_request not in branch.messages:
                await branch.msgs.a_add_message(action_request=action_request)
            await branch.msgs.a_add_message(
                action_request=action_request,
                action_output={
                    "error": str(e),
                    "function": _request.get("function"),
                    "arguments": _request.get("arguments"),
                },
                sender=branch.id,
                recipient=branch.id,
            )

        if suppress_errors:
            # Ordinary tool failures historically degrade to output=None when
            # suppressed. Keep that caller contract while persisting the
            # error-bearing ActionResponse above. Governance denials remain
            # visible in the returned response so the model can adapt.
            if captured_failure and not isinstance(e, ActionGovernanceDeniedError):
                return ActionResponseModel(
                    function=_request.get("function", "unknown"),
                    arguments=_request.get("arguments", {}),
                    output=None,
                )

            return ActionResponseModel(
                function=_request.get("function", "unknown"),
                arguments=_request.get("arguments", {}),
                output={"error": str(e), "message": error_msg},
            )
        raise e

    await branch.emit_and_log(func_call)

    if not isinstance(action_request, ActionRequest):
        action_request = ActionRequest(
            content=_request,
            sender=branch.id,
            recipient=func_call.func_tool.id,
        )

    if action_request not in branch.messages:
        await branch.msgs.a_add_message(action_request=action_request)

    await branch.msgs.a_add_message(
        action_request=action_request,
        action_output=func_call.response,
    )

    return ActionResponseModel(
        function=action_request.function,
        arguments=action_request.arguments,
        output=func_call.response,
    )


def prepare_act_kw(
    branch: "Branch",
    action_request: list | ActionRequest | BaseModel | dict,
    *,
    strategy: Literal["concurrent", "sequential"] = "concurrent",
    verbose_action: bool = False,
    suppress_errors: bool = True,
    call_params: AlcallParams = None,
):
    action_param = ActionParam(
        action_call_params=call_params or _get_default_call_params(),
        tools=None,
        strategy=strategy,
        suppress_errors=suppress_errors,
        verbose_action=verbose_action,
    )
    return {
        "action_request": action_request,
        "action_param": action_param,
    }


async def act(
    branch: "Branch",
    action_request: list | ActionRequest | BaseModel | dict,
    action_param: ActionParam,
) -> list[ActionResponse]:
    """Execute action requests with ActionParam."""

    match action_param.strategy:
        case "concurrent":
            return await _concurrent_act(
                branch,
                action_request,
                action_param.action_call_params,
                suppress_errors=action_param.suppress_errors,
                verbose_action=action_param.verbose_action,
            )
        case "sequential":
            return await _sequential_act(
                branch,
                action_request,
                suppress_errors=action_param.suppress_errors,
                verbose_action=action_param.verbose_action,
            )
        case _:
            raise ConfigurationError("Invalid strategy. Choose 'concurrent' or 'sequential'.")


async def _concurrent_act(
    branch: "Branch",
    action_request: list | ActionRequest | BaseModel | dict,
    call_params: AlcallParams,
    suppress_errors: bool = True,
    verbose_action: bool = False,
) -> list:
    """Execute actions concurrently using AlcallParams."""

    async def _wrapper(req):
        return await _act(branch, req, suppress_errors, verbose_action)

    action_request_list = action_request if isinstance(action_request, list) else [action_request]

    return await call_params(action_request_list, _wrapper)


async def _sequential_act(
    branch: "Branch",
    action_request: list | ActionRequest | BaseModel | dict,
    suppress_errors: bool = True,
    verbose_action: bool = False,
) -> list:
    """Execute actions sequentially."""
    action_request = action_request if isinstance(action_request, list) else [action_request]
    results = []
    for req in action_request:
        result = await _act(branch, req, suppress_errors, verbose_action)
        results.append(result)
    return results
