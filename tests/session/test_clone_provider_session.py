# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A clone continues none of the original branch's provider sessions."""

from __future__ import annotations

import pytest

from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.session.session import Session


def _cli_model() -> iModel:
    return iModel(provider="claude_code", model="claude-3-5-sonnet-20241022", api_key="test-key")


def _api_model() -> iModel:
    return iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")


def _resume_sent(model: iModel) -> str | None:
    call = model.create_api_calling(messages=[{"role": "user", "content": "next"}])
    return call.payload["request"].resume


@pytest.mark.parametrize("how", ["clone", "split"])
def test_a_clone_of_a_branch_whose_parse_model_is_its_chat_model_keeps_one_model_of_its_own(how):
    model = _cli_model()
    original = Branch(chat_model=model)
    assert original.parse_model is original.chat_model
    model.endpoint.session_id = "original-thread"

    if how == "clone":
        clone = original.clone()
    else:
        session = Session()
        session.include_branches(original)
        clone = session.split(original.id)

    # neither of the clone's calls continues the original's thread
    assert _resume_sent(clone.chat_model) is None
    assert _resume_sent(clone.parse_model) is None
    assert clone.parse_model is clone.chat_model
    assert clone.parse_model is not model
    # and a thread the clone starts is not written onto the original's model
    clone.parse_model.endpoint.session_id = "clone-thread"
    assert model.endpoint.session_id == "original-thread"
    assert _resume_sent(model) == "original-thread"


def test_a_separate_cli_parse_model_is_copied_on_its_own():
    chat, parse = _cli_model(), _cli_model()
    parse.endpoint.session_id = "parse-thread"
    original = Branch(chat_model=chat, parse_model=parse)

    clone = original.clone()

    assert clone.parse_model is not parse and clone.parse_model is not clone.chat_model
    assert _resume_sent(clone.parse_model) is None


def test_api_models_are_still_shared_by_reference():
    shared = _api_model()
    original = Branch(chat_model=shared)
    clone = original.clone()
    assert clone.chat_model is shared and clone.parse_model is shared

    chat, parse = _cli_model(), _api_model()
    clone = Branch(chat_model=chat, parse_model=parse).clone()
    assert clone.chat_model is not chat and clone.parse_model is parse
