# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from lionagi.ln import AlcallParams
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

    # ADR-0076: governance gate — denied calls surface as tool results (not exceptions) so ReAct loops can adapt.
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

    try:
        if verbose_action:
            args_ = str(_request["arguments"])
            args_ = args_[:50] + "..." if len(args_) > 50 else args_
            logger.debug("Invoking action %s with %s.", _request["function"], args_)

        func_call = await branch._action_manager.invoke(_request)
        if verbose_action:
            logger.debug("Action %s invoked, status: %s.", _request["function"], func_call.status)

    except Exception as e:
        content = {
            "error": str(e),
            "function": _request.get("function"),
            "arguments": _request.get("arguments"),
            "branch": str(branch.id),
        }
        branch._log_manager.log(content)
        if verbose_action:
            logger.error("Action %s failed, error: %s.", _request["function"], e)
        if suppress_errors:
            error_msg = f"Error invoking action '{_request['function']}': {e}"
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
            raise ValueError("Invalid strategy. Choose 'concurrent' or 'sequential'.")


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
