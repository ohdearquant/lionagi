# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Static return-type contract for ``Branch.chat`` overloads."""

from typing_extensions import assert_type

from lionagi.protocols.types import AssistantResponse, Instruction
from lionagi.session.branch import Branch


async def assert_chat_return_types(branch: Branch, flag: bool) -> None:
    assert_type(await branch.chat(), str)
    assert_type(await branch.chat(return_ins_res_message=False), str)
    assert_type(
        await branch.chat(return_ins_res_message=True),
        tuple[Instruction, AssistantResponse],
    )
    assert_type(
        await branch.chat(return_ins_res_message=flag),
        str | tuple[Instruction, AssistantResponse],
    )
