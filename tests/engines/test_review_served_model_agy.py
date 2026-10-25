# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ReviewVerdict.served_model on the agy (gemini-cli) provider.

agy's result object may carry no model. The verdict then records none rather
than the requested model, on both the stream path the engine takes and the
invoke path's result dict. The agy process is replaced at its event seam.
"""

from __future__ import annotations

import pytest

from lionagi.providers.google.gemini_code import GeminiCLIEndpoint, GeminiCodeRequest
from lionagi.service.imodel import iModel
from lionhub.orchestration.engines.review import ReviewEngine, ReviewVerdict

AGY_SEAM = "lionagi.providers.google.gemini_code.stream_gemini_cli_events"
REQUESTED = "gemini-3.8-flash"


def _agy(reported: str | None, calls: list[str]):
    async def events(request):
        calls.append(request.model)
        obj = {
            "conversation_id": "conv-1",
            "status": "SUCCESS",
            "response": "REQUEST-CHANGES: fix the cursor advance.",
            "duration_seconds": 0.1,
            "num_turns": 1,
            "usage": {},
        }
        if reported is not None:
            obj["model"] = reported
        yield obj

    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [None, "gemini-served-x"])
async def test_verdict_records_only_what_agy_reported(monkeypatch, reported):
    calls: list[str] = []
    monkeypatch.setattr(AGY_SEAM, _agy(reported, calls))
    eng = ReviewEngine(
        model=f"gemini-cli/{REQUESTED}", khive_injection=False, dimensions=("correctness",)
    )
    run = eng.new_run()
    await eng._verdict(run, "fn next(&mut self) { self.pos += 1; }", ("correctness",))
    # Without a synthesis call the verdict proves nothing about the reader.
    assert len(calls) == 1
    assert run.by_type(ReviewVerdict)[-1].served_model == reported


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [None, "gemini-served-x"])
async def test_invoke_result_names_only_what_agy_reported(monkeypatch, reported):
    monkeypatch.setattr(AGY_SEAM, _agy(reported, []))
    result = await GeminiCLIEndpoint()._call(
        {"request": GeminiCodeRequest(prompt="hi", model=REQUESTED)}, {}
    )
    assert iModel._reported_served_model(result) == reported
