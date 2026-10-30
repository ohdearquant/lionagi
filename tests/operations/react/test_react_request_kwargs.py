# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""The first ReAct request carries only model inputs."""

from __future__ import annotations

import pytest

from lionagi import Branch
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True])
async def test_react_first_request_has_no_internal_settings(monkeypatch, verbose):
    endpoint = Endpoint(
        EndpointConfig(
            name="generic",
            provider="generic",
            endpoint="chat",
            auth_type="none",
            kwargs={"model": "sample-model"},
        )
    )
    assert endpoint.request_options is None
    branch = Branch(chat_model=iModel(endpoint=endpoint))
    captured = []

    class Captured(BaseException):
        pass

    async def capture(self, **request):
        request.pop("include_token_usage_to_model", None)
        payload, _ = self.endpoint.create_payload(request)
        captured.append(payload)
        raise Captured()

    monkeypatch.setattr(iModel, "invoke", capture)
    with pytest.raises(Captured):
        await branch.ReAct(instruction="hello", max_extensions=0, verbose=verbose)

    assert len(captured) == 1
    assert set(captured[0]) == {"model", "messages"}
    assert captured[0]["messages"]
