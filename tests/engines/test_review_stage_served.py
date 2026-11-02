# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ReviewVerdict.served_models: one row per leg the review run made, with what each leg's provider reported.

The verdict's own ``served_model`` names the synthesis call only. Each review,
fallback, verify and clean-audit leg is a separate model call, so a verdict that
names one model while its legs ran on others misstates who produced the review.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import yaml

from lionagi.testing._endpoint import ENV_SCRIPT_PATH
from lionhub.orchestration.engines.review import (
    IssueFound,
    ReviewEngine,
    ReviewVerdict,
    _clean_ref,
    _verify_ref,
)

SCRIPTED = "scripted/scripted-test"
HERMETIC = {"khive_injection": False}
ARTIFACT = "fn next(&mut self) { self.pos += 1; ... }"

ISSUE = {
    "issue_found": {
        "dimension": "correctness",
        "description": "off-by-one in cursor advance",
        "severity": "critical",
        "location": "store.rs:41",
        "confidence": 0.7,
    }
}
VERIFY = {
    "verify_result": {
        "issue": "off-by-one in cursor advance",
        "holds": True,
        "rationale": "boundary test confirms skip of last row",
    }
}
CLEAN = {
    "dimension_clean": {"dimension": "correctness", "rationale": "cursor logic checked, no defect"}
}


def _fenced(payload: dict) -> str:
    return f"Done.\n```json\n{json.dumps(payload)}\n```\n"


def _write_script(tmp_path, monkeypatch, entries: list[dict]) -> None:
    path = tmp_path / "script.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "responses": entries}), encoding="utf-8")
    monkeypatch.setenv(ENV_SCRIPT_PATH, str(path))


def _named(marker: str, served: str, text: str) -> dict:
    """A streamed reply whose provider names *served* as the model that answered."""
    return {
        "type": "stream",
        "chunks": [
            {"type": "system", "metadata": {"model": served}},
            {"type": "text", "content": text},
            {"type": "result", "metadata": {"done": True}},
        ],
        "when": {"prompt_contains": marker},
    }


def _unnamed(marker: str, text: str) -> dict:
    """A reply whose provider names no model."""
    return {"type": "text", "content": text, "when": {"prompt_contains": marker}}


def _rows(verdict: ReviewVerdict) -> list[dict[str, Any]]:
    return [row.model_dump() for row in verdict.served_models]


@pytest.mark.asyncio
async def test_each_leg_gets_a_row_in_completion_order(tmp_path, monkeypatch):
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _named("for **correctness** only", "served-review", _fenced(ISSUE)),
            _named("Adversarially verify", "served-verify", _fenced(VERIFY)),
            _named(
                "Issue a single ProposedVerdict", "served-verdict", "REQUEST-CHANGES: fix cursor."
            ),
        ],
    )
    eng = ReviewEngine(
        **HERMETIC,
        dimensions=("correctness",),
        models={
            "review": "scripted/review-req",
            "verify": "scripted/verify-req",
            "verdict": "scripted/verdict-req",
        },
    )
    run = eng.new_run()
    await eng._run(run, ARTIFACT)

    verdict = run.by_type(ReviewVerdict)[-1]
    issue = run.by_type(IssueFound)[0]
    assert _rows(verdict) == [
        {
            "stage": "review",
            "dimension": "correctness",
            "ref": None,
            "agent": "review-correctness",
            "requested": "scripted/review-req",
            "served": "served-review",
        },
        {
            "stage": "verify",
            "dimension": "correctness",
            "ref": _verify_ref(issue),
            "agent": "verify-correctness",
            "requested": "scripted/verify-req",
            "served": "served-verify",
        },
        {
            "stage": "verdict",
            "dimension": None,
            "ref": None,
            "agent": "verdict",
            "requested": "scripted/verdict-req",
            "served": "served-verdict",
        },
    ]
    assert verdict.served_model == "served-verdict"


@pytest.mark.asyncio
async def test_the_clean_audit_gets_its_own_row(tmp_path, monkeypatch):
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _named("for **correctness** only", "served-review", _fenced(CLEAN)),
            _named("Adversarially", "served-audit", _fenced(VERIFY)),
            _named("Issue a single ProposedVerdict", "served-verdict", "APPROVE: clean."),
        ],
    )
    eng = ReviewEngine(**HERMETIC, model=SCRIPTED, dimensions=("correctness",))
    run = eng.new_run()
    await eng._run(run, ARTIFACT)

    rows = _rows(run.by_type(ReviewVerdict)[-1])
    assert [(r["stage"], r["agent"], r["served"]) for r in rows] == [
        ("review", "review-correctness", "served-review"),
        ("verify_clean", "verify-clean", "served-audit"),
        ("verdict", "verdict", "served-verdict"),
    ]
    audit = rows[1]
    assert audit["ref"] == _clean_ref(("correctness",))
    assert audit["dimension"] is None
    assert audit["requested"] == SCRIPTED


@pytest.mark.asyncio
async def test_a_leg_whose_provider_names_no_model_records_none_not_the_requested_one(
    tmp_path, monkeypatch
):
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _unnamed("for **correctness** only", _fenced(ISSUE)),
            _named("Adversarially verify", "served-verify", _fenced(VERIFY)),
            _named("Issue a single ProposedVerdict", "served-verdict", "REQUEST-CHANGES: fix."),
        ],
    )
    eng = ReviewEngine(**HERMETIC, model=SCRIPTED, dimensions=("correctness",))
    run = eng.new_run()
    await eng._run(run, ARTIFACT)

    review, verify, verdict = _rows(run.by_type(ReviewVerdict)[-1])
    assert review["requested"] == SCRIPTED
    assert review["served"] is None
    assert verify["served"] == "served-verify"
    assert verdict["served"] == "served-verdict"


class _Leg:
    """A stand-in leg: ``operate`` runs the next scripted step; ``chat_model`` carries the provider's report."""

    def __init__(self, name: str, steps: list, state: dict, served: str | None = None) -> None:
        self.name = name
        self.chat_model = SimpleNamespace(
            is_cli=False, provider_metadata={} if served is None else {"served_model": served}
        )
        self.msgs = SimpleNamespace(last_response=None)
        self._steps = list(steps)
        self._state = state

    async def operate(self, *, instruction: str) -> str:
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step:
            self._state["arrived"] = True
        return "reply"


def _engine_with(legs: dict[str, _Leg], state: dict, **kwargs: Any):
    """A one-dimension ReviewEngine whose run builds the stand-in leg for each agent name."""
    engine = ReviewEngine(dimensions=("correctness",), verify_clean=False, **kwargs)
    engine._reported_dimensions = lambda run: {"correctness"} if state["arrived"] else set()
    run = engine.new_run()
    built: list[dict] = []

    async def make_agent(role, *, name=None, model=None, **_):
        built.append({"name": name, "model": model})
        return legs[name]

    run.make_agent = make_agent
    return engine, run, built


@pytest.mark.asyncio
async def test_a_review_leg_that_fails_still_has_a_row():
    state = {"arrived": False}
    legs = {"review-correctness": _Leg("review-correctness", [anyio.BrokenResourceError()], state)}
    engine, run, _ = _engine_with(legs, state, model="base-model")

    await engine._review_dimension_isolated(run, ARTIFACT, "correctness")

    assert run._dimension_failures == {"correctness": "BrokenResourceError"}
    assert [r.model_dump() for r in run.stage_served] == [
        {
            "stage": "review",
            "dimension": "correctness",
            "ref": None,
            "agent": "review-correctness",
            "requested": "base-model",
            "served": None,
        }
    ]


@pytest.mark.asyncio
async def test_a_cancelled_review_leg_has_a_row_and_the_cancellation_propagates():
    state = {"arrived": False}
    legs = {
        "review-correctness": _Leg(
            "review-correctness", [asyncio.CancelledError()], state, served="served-before-cancel"
        )
    }
    engine, run, _ = _engine_with(legs, state, model="base-model")

    with pytest.raises(asyncio.CancelledError):
        await engine._review_dimension_isolated(run, ARTIFACT, "correctness")

    assert [(r.stage, r.served) for r in run.stage_served] == [("review", "served-before-cancel")]


@pytest.mark.asyncio
async def test_a_review_fallback_leg_gets_its_own_row():
    state = {"arrived": False}
    legs = {
        "review-correctness": _Leg(
            "review-correctness", [False, False], state, served="served-primary"
        ),
        "review-correctness-fallback": _Leg(
            "review-correctness-fallback", [True], state, served="served-fallback"
        ),
    }
    engine, run, built = _engine_with(
        legs,
        state,
        model="base-model",
        models={"review": "haiku-model", "review_fallback": "sonnet-model"},
    )

    await engine._review_dimension(run, ARTIFACT, "correctness")

    assert [b["name"] for b in built] == ["review-correctness", "review-correctness-fallback"]
    assert [r.model_dump() for r in run.stage_served] == [
        {
            "stage": "review",
            "dimension": "correctness",
            "ref": None,
            "agent": "review-correctness",
            "requested": "haiku-model",
            "served": "served-primary",
        },
        {
            "stage": "review_fallback",
            "dimension": "correctness",
            "ref": None,
            "agent": "review-correctness-fallback",
            "requested": "sonnet-model",
            "served": "served-fallback",
        },
    ]


@pytest.mark.asyncio
async def test_the_first_leg_is_read_before_the_fallback_leg_is_built():
    """A leg that finished earlier is not read again after later legs have run."""
    state = {"arrived": False}
    primary = _Leg("review-correctness", [False, False], state, served="served-primary")
    fallback = _Leg("review-correctness-fallback", [True], state, served="served-fallback")
    engine, run, _ = _engine_with(
        {"review-correctness": primary, "review-correctness-fallback": fallback},
        state,
        model="base-model",
        models={"review_fallback": "sonnet-model"},
    )
    seen_at_build: list[list[str]] = []
    inner = run.make_agent

    async def make_agent(role, *, name=None, **kwargs):
        seen_at_build.append([r.agent for r in run.stage_served])
        return await inner(role, name=name, **kwargs)

    run.make_agent = make_agent

    await engine._review_dimension(run, ARTIFACT, "correctness")

    assert seen_at_build == [[], ["review-correctness"]]


@pytest.mark.asyncio
async def test_rows_are_per_run():
    engine = ReviewEngine(**HERMETIC, model=SCRIPTED, dimensions=("correctness",))
    first, second = engine.new_run(), engine.new_run()
    assert first.stage_served is not second.stage_served
    assert ReviewVerdict(verdict="APPROVE", rationale="r").served_models is not (
        ReviewVerdict(verdict="APPROVE", rationale="r").served_models
    )


@pytest.mark.asyncio
async def test_the_shared_session_refusal_carries_the_rows_collected_so_far():
    from lionhub.orchestration.engines.review import StageServed

    engine = ReviewEngine(**HERMETIC, model=SCRIPTED, dimensions=("correctness",))
    run = engine.new_run()
    run.shares_session = True
    run.stage_served.append(
        StageServed(
            stage="review",
            dimension="correctness",
            agent="review-correctness",
            requested=SCRIPTED,
            served="served-review",
        )
    )

    await engine._verdict(run, ARTIFACT, ("correctness",))

    verdict = run.by_type(ReviewVerdict)[-1]
    assert verdict.verdict == "REQUEST-CHANGES"
    assert [(r.stage, r.served) for r in verdict.served_models] == [("review", "served-review")]
    assert verdict.served_models is not run.stage_served


def test_served_models_round_trip_and_old_payloads_load_empty():
    from lionhub.orchestration.engines.review import StageServed, __all__

    assert "StageServed" in __all__
    row = StageServed(
        stage="verify",
        dimension="security",
        ref="V-1a2b3c4d",
        agent="verify-security",
        requested="m-req",
        served=None,
    )
    verdict = ReviewVerdict(verdict="APPROVE", rationale="ok", served_models=[row])

    for reloaded in (
        ReviewVerdict.model_validate_json(verdict.model_dump_json()),
        ReviewVerdict.model_validate(verdict.model_dump(mode="json")),
    ):
        assert reloaded.served_models == [row]
        assert reloaded.served_models[0].served is None

    old = ReviewVerdict.model_validate(
        {"verdict": "APPROVE", "rationale": "ok", "served_model": "m"}
    )
    assert old.served_models == []
    assert old.served_model == "m"
