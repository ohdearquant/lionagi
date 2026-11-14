# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""A resumed API agent sends the request a fresh one would send, and it prices."""

from __future__ import annotations

import json

from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.service.accounts._pricing import request_ttls
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

MODEL = "claude-sonnet-5-5"


def _agent_model() -> iModel:
    """The chat model an Anthropic API agent is built with: effort and prompt caching."""
    model = iModel(provider="anthropic", model=MODEL, output_config={"effort": "high"})
    model.endpoint.config.prompt_caching = True
    return model


async def _branch_with_history(model: iModel) -> Branch:
    branch = Branch(system="You are a careful agent.", chat_model=model)
    branch.msgs.add_message(instruction="Read the file.")
    branch.msgs.add_message(assistant_response="I will read it.")
    request = await branch.msgs.a_add_message(
        action_function="reader", action_arguments={"path": "a.py"}
    )
    await branch.msgs.a_add_message(action_request=request, action_output="print('a')")
    return branch


def _routed_payload(branch: Branch, monkeypatch) -> dict:
    """The final request the next turn sends when account routing is on."""
    model = branch.chat_model
    monkeypatch.setattr(model.endpoint, "_account_routing_enabled", lambda: True)
    _, kw = _prepare_run_kwargs(
        branch,
        "Now summarize it.",
        ChatParam(imodel=model, imodel_kw={}),
        imodel=model,
    )
    payload, _ = model.endpoint.create_payload(kw)
    # The router names the tier for a request that names none.
    if payload.get("service_tier") is None:
        payload = {**payload, "service_tier": "standard_only"}
    return payload


async def test_resumed_agent_request_matches_a_fresh_one_and_prices(monkeypatch):
    fresh = await _branch_with_history(_agent_model())
    restored = Branch.from_dict(json.loads(json.dumps(fresh.to_dict(mode="json"))))

    fresh_payload = _routed_payload(fresh, monkeypatch)
    restored_payload = _routed_payload(restored, monkeypatch)

    assert restored_payload == fresh_payload
    assert request_ttls(restored_payload) == request_ttls(fresh_payload)
