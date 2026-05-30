# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Capability opt-in: grant_capabilities sets the runtime grant AND injects an
idempotent instruction block telling the model what it may emit. Strict
response_format behavior is orthogonal and untouched.
"""

from __future__ import annotations

from pydantic import BaseModel

from lionagi.ln.types import Operable, Spec
from lionagi.operations.run.run import _emit_message_signal
from lionagi.protocols.messages import AssistantResponse
from lionagi.protocols.messages.assistant_response import AssistantResponseContent
from lionagi.session.capabilities import CAP_BEGIN, CAP_END, render_capabilities_prompt
from lionagi.session.session import Session


class Finding(BaseModel):
    claim: str
    confidence: float = 0.5


class Question(BaseModel):
    text: str


def _grant() -> Operable:
    return Operable(
        (Spec(Finding, name="finding"), Spec(Question, name="question")),
        name="AgentCapabilities",
    )


def _assistant(text: str) -> AssistantResponse:
    return AssistantResponse(
        content=AssistantResponseContent(assistant_response=text),
        sender=None,
        recipient="user",
    )


# -- renderer ---------------------------------------------------------------


def test_render_lists_names_and_schema():
    prompt = render_capabilities_prompt(_grant())
    assert "finding" in prompt and "question" in prompt
    assert "rejected" in prompt  # the keys ⊆ grant rule, in prose
    assert "claim" in prompt  # nested schema is shown


# -- grant / revoke ---------------------------------------------------------


def test_grant_sets_runtime_and_prompt():
    s = Session()
    branch = s.default_branch
    branch.grant_capabilities(_grant())

    assert branch.capabilities is not None
    sys_text = branch.msgs.system.content.system_message
    assert CAP_BEGIN in sys_text and CAP_END in sys_text
    assert "finding" in sys_text


def test_grant_prompt_false_sets_runtime_only():
    s = Session()
    branch = s.default_branch
    before = branch.msgs.system.content.system_message if branch.msgs.system else None
    branch.grant_capabilities(_grant(), prompt=False)

    assert branch.capabilities is not None
    after = branch.msgs.system.content.system_message if branch.msgs.system else None
    assert after == before  # no prompt mutation


def test_regrant_replaces_block_no_duplicate():
    s = Session()
    branch = s.default_branch
    branch.grant_capabilities(_grant())
    branch.grant_capabilities(_grant())  # again

    sys_text = branch.msgs.system.content.system_message
    assert sys_text.count(CAP_BEGIN) == 1  # not stacked


def test_grant_preserves_base_system():
    s = Session()
    branch = s.default_branch
    branch.msgs.set_system(branch.msgs.create_system(system="You are a researcher."))
    branch.grant_capabilities(_grant())

    sys_text = branch.msgs.system.content.system_message
    assert "You are a researcher." in sys_text
    assert CAP_BEGIN in sys_text


def test_revoke_clears_and_strips():
    s = Session()
    branch = s.default_branch
    branch.msgs.set_system(branch.msgs.create_system(system="Base prompt."))
    branch.grant_capabilities(_grant())
    branch.revoke_capabilities()

    assert branch.capabilities is None
    sys_text = branch.msgs.system.content.system_message
    assert CAP_BEGIN not in sys_text
    assert "Base prompt." in sys_text


# -- end-to-end: grant → emit → observe -------------------------------------


async def test_grant_then_emit_observed():
    s = Session()
    seen = []
    s.observe(Finding, lambda f, _: seen.append(f.claim))

    branch = s.default_branch
    branch.grant_capabilities(_grant())

    await _emit_message_signal(branch, _assistant('{"finding": {"claim": "wired"}}'))
    assert seen == ["wired"]
