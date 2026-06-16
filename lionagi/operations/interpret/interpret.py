# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING

from ..types import ChatParam, InterpretParam

if TYPE_CHECKING:
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch


def prepare_interpret_kw(
    branch: "Branch",
    text: str,
    domain: str | None = None,
    style: str | None = None,
    sample_writing: str | None = None,
    interpret_model: "iModel | None" = None,
    **kwargs,
) -> str:
    """Build InterpretParam kwargs dict for interpret()."""
    intp_param = InterpretParam(
        domain=domain or "general",
        style=style or "concise",
        sample_writing=sample_writing or "",
        imodel=interpret_model or branch.chat_model,
        imodel_kw=kwargs,
    )
    return {
        "text": text,
        "intp_param": intp_param,
    }


async def interpret(
    branch: "Branch",
    text: str,
    intp_param: InterpretParam,
) -> str:
    """Rewrite raw user text into a clearer, explicit LLM prompt using InterpretParam settings."""

    from ..chat.chat import chat

    instruction = (
        "You are given a user's raw instruction or question. Your task is to rewrite it into a clearer, "
        "more structured prompt for an LLM or system, making any implicit or missing details explicit. "
        "Return only the re-written prompt. Do not assume any details not mentioned in the input, nor "
        "give additional instruction than what is explicitly stated."
    )

    guidance = f"Domain hint: {intp_param.domain}. Desired style: {intp_param.style}."
    if intp_param.sample_writing:
        guidance += f" Sample writing: {intp_param.sample_writing}"

    chat_param = ChatParam.from_branch(
        branch,
        guidance=guidance,
        context=[f"User input: {text}"],
        imodel=intp_param.imodel,
        imodel_kw={
            **intp_param.imodel_kw,
            "temperature": intp_param.imodel_kw.get("temperature", 0.1),
        },
    )

    result = await chat(
        branch,
        instruction=instruction,
        chat_param=chat_param,
        return_ins_res_message=False,
    )

    return str(result)
