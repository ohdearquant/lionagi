# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from lionagi.operations.fields import Instruct

from .utils import SelectionModel, parse_selection, parse_to_representation

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


async def select(
    branch: "Branch",
    instruct: Instruct | dict[str, Any],
    choices: list[str] | type[Enum] | dict[str, Any],
    max_num_selections: int = 1,
    branch_kwargs: dict[str, Any] | None = None,
    return_branch: bool = False,
    verbose: bool = False,
    **kwargs: Any,
) -> SelectionModel | tuple[SelectionModel, "Branch"]:
    """Legacy wrapper around select_v1; supports deprecated branch_kwargs and optional return_branch tuple."""
    if verbose:
        logger.debug("Starting selection with up to %d choices.", max_num_selections)

    # Handle branch creation for backwards compatibility
    if branch is None and branch_kwargs:
        from lionagi.session.branch import Branch

        branch = Branch(**branch_kwargs)

    result = await select_v1(
        branch=branch,
        instruct=instruct,
        choices=choices,
        max_num_selections=max_num_selections,
        verbose=verbose,
        **kwargs,
    )

    if return_branch:
        return result, branch
    return result


async def select_v1(
    branch: "Branch",
    instruct: Instruct | dict[str, Any],
    choices: list[str] | type[Enum] | dict[str, Any],
    max_num_selections: int = 1,
    verbose: bool = False,
    **operate_kwargs: Any,
) -> SelectionModel:
    """Operate-based selection: build prompt from choices, call branch.operate(SelectionModel), parse back to original values."""
    selections, contents = parse_to_representation(choices)
    prompt = SelectionModel.PROMPT.format(max_num_selections=max_num_selections, choices=selections)

    if isinstance(instruct, Instruct):
        instruct_dict = instruct.to_dict()
    else:
        instruct_dict = instruct or {}

    if instruct_dict.get("instruction", None) is not None:
        instruct_dict["instruction"] = f"{instruct_dict['instruction']}\n\n{prompt} \n\n "
    else:
        instruct_dict["instruction"] = prompt

    context = instruct_dict.get("context", None) or []
    context = [context] if not isinstance(context, list) else context
    context.extend([{k: v} for k, v in zip(selections, contents, strict=False)])
    instruct_dict["context"] = context

    response_model: SelectionModel = await branch.operate(
        response_format=SelectionModel,
        **operate_kwargs,
        **instruct_dict,
    )

    if verbose:
        logger.debug("Received selection: %s", response_model.selected)

    selected = response_model
    if isinstance(response_model, BaseModel) and hasattr(response_model, "selected"):
        selected = response_model.selected
    selected = [selected] if not isinstance(selected, list) else selected

    corrected_selections = [parse_selection(i, choices) for i in selected]

    if isinstance(response_model, BaseModel):
        response_model.selected = corrected_selections
    elif isinstance(response_model, dict):
        response_model["selected"] = corrected_selections

    return response_model
