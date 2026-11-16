# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cover Studio workflow engine inputs and outputs."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize(
    "context,expected",
    [
        (None, ""),
        ({}, ""),
        ({"left_result": "left", "right_result": "right"}, "left"),
        ({"right_result": "right", "left_result": "left"}, "right"),
        (
            {"zero_result": 0, "false_result": False, "empty_result": "", "question": "prompt"},
            "0\nFalse\n\nprompt",
        ),
        ({"empty_result": [], "nil": None, "text": "fallback"}, "fallback"),
        (
            {"left_result": {"z": 1, "a": "café"}, "right_result": "unused"},
            '{"z": 1, "a": "caf\\u00e9"}',
        ),
        (
            {"input": {"nested": 1}, "other": [False, 0]},
            '{"input": {"nested": 1}, "other": [false, 0]}',
        ),
        ({"work_result": True}, "true"),
    ],
    ids=[
        "absent-context",
        "empty-context",
        "left-first",
        "right-first",
        "false-results-fall-back-to-primitives",
        "non-primitives-omitted-from-primitive-fallback",
        "structured-result-preserves-order-and-ascii-encoding",
        "structured-context-fallback",
        "truthy-boolean-result",
    ],
)
async def test_legacy_context_reaches_engine_unchanged_as_derived_input(
    monkeypatch, context, expected
):
    import lionhub.orchestration.engine_definitions as engine_mod
    from lionagi.studio.services.workflow_compile import make_engine_operation

    received = []

    class RecordingEngine:
        async def run(self, spec_input, *, session, on_branch_created):
            received.append((spec_input, session, on_branch_created))
            return "ok"

    monkeypatch.setattr(engine_mod, "import_engine_class", lambda *_args: RecordingEngine)
    session = object()

    def branch_created(_branch):
        pass

    operation = make_engine_operation(session, on_branch_created=branch_created)
    before = deepcopy(context)

    result = await operation(context=context, engine_kind="research")

    assert received == [(expected, session, branch_created)]
    assert context == before
    assert result["result"] == "ok"


async def test_workflow_engine_nodes_return_distinct_span_ids(monkeypatch) -> None:
    import lionhub.orchestration.engine_definitions as engine_mod
    from lionagi.studio.services.workflow_compile import make_engine_operation
    from lionhub.orchestration.engines.engine import EngineResult

    counter = 0

    class FakeEngine:
        _total_agent_failure = False

        def __init__(self, **_kwargs):
            nonlocal counter
            counter += 1
            self.span_id = f"engine-span-{counter}"

        async def run(self, _spec, **_kwargs):
            return EngineResult(
                "ok",
                events_by_type=lambda _kind: [],
                skipped=[],
                degraded=False,
                run=SimpleNamespace(run_id=self.span_id),
            )

    monkeypatch.setattr(engine_mod, "import_engine_class", lambda *_a: FakeEngine)
    operation = make_engine_operation(MagicMock())

    first = await operation(context={"input": "one"}, engine_kind="research")
    second = await operation(context={"input": "two"}, engine_kind="research")

    assert first["engine_span_id"] == "engine-span-1"
    assert second["engine_span_id"] == "engine-span-2"
    assert first["engine_span_id"] != second["engine_span_id"]
