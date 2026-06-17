# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""End-to-end engine runs through the scripted provider — exercises the live emission and capability-extraction path."""

from __future__ import annotations

import json

import pytest
import yaml

from lionagi.engines.coding import (
    ChangeProposed,
    CodeResultRecorded,
    CodingEngine,
    TestsRan,
    WorkPlanned,
)
from lionagi.engines.coding import (
    VerifyResult as CodeVerifyResult,
)
from lionagi.engines.hypothesis import (
    ApplicationMapped,
    ConclusionDrawn,
    EvidenceCollected,
    HypothesisEngine,
    QuestionRaised,
    ResultRecorded,
)
from lionagi.engines.research import FindingEmitted, ResearchEngine
from lionagi.engines.review import IssueFound, ReviewEngine, VerifyResult
from lionagi.testing._endpoint import ENV_SCRIPT_PATH

SCRIPTED_MODEL = "scripted/scripted-test"


def _emit(payload: dict) -> str:
    return f"Done.\n```json\n{json.dumps(payload)}\n```\n"


def _write_script(tmp_path, monkeypatch, entries: list[dict]) -> None:
    """Write scripted-provider responses; each engine agent gets a fresh cursor via when: matchers."""
    path = tmp_path / "script.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "responses": entries}), encoding="utf-8")
    monkeypatch.setenv(ENV_SCRIPT_PATH, str(path))


def _when(marker: str, payload: dict | None = None, text: str = "") -> dict:
    return {
        "type": "text",
        "content": _emit(payload) if payload is not None else text,
        "when": {"prompt_contains": marker},
    }


@pytest.mark.asyncio
async def test_hypothesis_engine_full_chain_e2e(tmp_path, monkeypatch):
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _when(
                "Extract the architectural questions",
                {
                    "question_raised": {
                        "area": "graph storage",
                        "what_is_unknown": "why CSR over recursive CTE for BFS?",
                        "alternatives": ["recursive CTE in SQLite"],
                        "decision_ref": "D-012",
                        "parent_ref": "F-1",
                        "gen": 0,
                    }
                },
            ),
            _when(
                "Gather concrete evidence",
                {
                    "evidence_collected": {
                        "description": "SurrealDB traverses in-memory, not via SQL CTE",
                        "kind": "precedent",
                        "evidence": "surrealdb graph executor source",
                        "confidence": 0.8,
                        "question_ref": "Q-1",
                    }
                },
            ),
            _when(
                "Form the testable hypothesis",
                {
                    "hypothesis_formed": {
                        "question_ref": "Q-1",
                        "statement": "CSR BFS depth-2 beats CTE by >10x at 500K edges",
                        "metric": "ops per traversal",
                        "threshold": "10x",
                        "falsifier": "CTE within 2x of CSR",
                    }
                },
            ),
            _when(
                "Design the decisive experiment",
                {
                    "experiment_designed": {
                        "hypothesis_ref": "H-1",
                        "method": "analysis",
                        "dataset": "synthetic 500K-edge graph",
                        "procedure": "count page reads and row decodes per traversal",
                        "acceptance": "CSR ops < CTE ops / 10",
                    }
                },
            ),
            _when(
                "Execute experiment",
                {
                    "result_recorded": {
                        "experiment_ref": "X-1",
                        "measurements": "CSR: 1.2M ops; CTE: 38M ops (31x)",
                        "passed": True,
                        "caveats": ["cold-cache behavior untested"],
                    }
                },
            ),
            _when(
                "Draw the conclusion",
                {
                    "conclusion_drawn": {
                        "question_ref": "Q-1",
                        "result_ref": "R-1",
                        "verdict": "keep CSR for in-memory BFS",
                        "rationale": "31x fewer ops than recursive CTE",
                        "basis": "quantitative",
                        "confidence": 0.85,
                        "limitations": ["validated at <=500K edges only"],
                    }
                },
            ),
            _when(
                "Apply conclusion",
                {
                    "application_mapped": {
                        "conclusion_ref": "C-1",
                        "decision_ref": "D-012",
                        "effect": "supports",
                        "note": "quantitative op-count evidence for the CSR choice",
                    }
                },
            ),
            _when(
                "Write the evidence report",
                text="EVIDENCE REPORT: D-012 supported on quantitative basis (31x).",
            ),
        ],
    )

    eng = HypothesisEngine(model=SCRIPTED_MODEL, max_questions=1, repair_retries=0)
    run = eng.new_run()
    report = await eng._run(
        run,
        "ChatGPT R3 proposes CSR snapshot for BFS instead of recursive CTE",
        decisions="D-012: in-memory CSR graph snapshot for traversal",
        export_dir=tmp_path / "evidence",
    )

    assert "D-012 supported" in report
    # The full typed chain formed through the real emission path:
    chains = json.loads((tmp_path / "evidence" / "chains.json").read_text())["chains"]
    assert ["F-1", "Q-1", "H-1", "X-1", "R-1", "C-1", "A-1"] in chains
    assert run.events_of(QuestionRaised)[0].decision_ref == "D-012"
    assert run.events_of(EvidenceCollected)[0].kind == "precedent"
    assert run.events_of(ConclusionDrawn)[0].basis == "quantitative"
    assert run.events_of(ApplicationMapped)[0].effect == "supports"
    report_md = (tmp_path / "evidence" / "report.md").read_text()
    assert "EVIDENCE REPORT" in report_md and "Evidence chains" in report_md


@pytest.mark.asyncio
async def test_hypothesis_repair_recovers_weak_model_e2e(tmp_path, monkeypatch):
    """Repair turn re-prompts after a prose-only first response and the second response emits validly."""
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _when("Extract the architectural questions", text="Let me think about this..."),
            _when(
                "produced no valid emission",
                {
                    "question_raised": {
                        "area": "ql",
                        "what_is_unknown": "why || over + for concat?",
                        "parent_ref": "F-1",
                        "gen": 0,
                    }
                },
            ),
            _when(
                "Gather concrete evidence",
                {
                    "evidence_collected": {
                        "description": "SQL standard uses ||",
                        "kind": "citation",
                        "question_ref": "Q-1",
                    }
                },
            ),
            _when(
                "Form the testable hypothesis",
                {
                    "conclusion_drawn": {
                        "question_ref": "Q-1",
                        "result_ref": "",
                        "verdict": "keep ||",
                        "rationale": "SQL convention; + overload invites coercion bugs",
                        "basis": "taste",
                        "confidence": 0.6,
                    }
                },
            ),
            _when("Apply conclusion", text="Bears on nothing specific."),
            _when("Write the evidence report", text="REPORT: taste conclusion recorded."),
        ],
    )

    notified: list[dict] = []
    eng = HypothesisEngine(model=SCRIPTED_MODEL, max_questions=1, repair_retries=1)
    report = await eng.run(
        "Grammar uses || for concat",
        on_event=lambda e: notified.append(e),
    )
    assert "taste conclusion" in report
    assert any(e["type"] == "emission_repair" for e in notified)


@pytest.mark.asyncio
async def test_research_engine_e2e_with_depth_spawn(tmp_path, monkeypatch):
    """High-novelty finding at depth 0 spawns depth-1 node; synthesis reads via bundle-aware by_type."""
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _when(
                "depth 0/1",
                {
                    "finding_emitted": {
                        "description": "fusion parameter k dominates recall",
                        "evidence": "ablation table",
                        "novelty": 0.9,
                        "confidence": 0.8,
                        "depth": 0,
                    }
                },
            ),
            _when(
                "depth 1/1",
                {
                    "finding_emitted": {
                        "description": "k=60 is a historical default, not tuned",
                        "evidence": "original RRF paper",
                        "novelty": 0.2,
                        "confidence": 0.9,
                        "depth": 1,
                    }
                },
            ),
            _when("Synthesize the research", text="SYNTHESIS: k=60 needs a sweep."),
        ],
    )

    eng = ResearchEngine(model=SCRIPTED_MODEL, roles=("researcher",), max_depth=1)
    run = eng.new_run()
    out = await eng._run(run, "RRF fusion defaults")
    assert out == "SYNTHESIS: k=60 needs a sweep."
    findings = run.by_type(FindingEmitted)
    assert len(findings) == 2  # depth-0 + spawned depth-1, through real bundles
    assert {f.depth for f in findings} == {0, 1}


@pytest.mark.asyncio
async def test_review_engine_e2e_with_adversarial_verify(tmp_path, monkeypatch):
    """Critical issue spawns the adversarial verifier; verdict reads from store via bundle-aware by_type."""
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _when(
                "for **correctness** only",
                {
                    "issue_found": {
                        "dimension": "correctness",
                        "description": "off-by-one in cursor advance",
                        "severity": "critical",
                        "location": "store.rs:41",
                        "confidence": 0.7,
                    }
                },
            ),
            _when(
                "Adversarially verify",
                {
                    "verify_result": {
                        "issue": "off-by-one in cursor advance",
                        "holds": True,
                        "rationale": "boundary test confirms skip of last row",
                    }
                },
            ),
            _when(
                "Issue a single ReviewVerdict",
                text="REQUEST-CHANGES: fix cursor advance.",
            ),
        ],
    )

    eng = ReviewEngine(model=SCRIPTED_MODEL, dimensions=("correctness",))
    run = eng.new_run()
    out = await eng._run(run, "fn next(&mut self) { self.pos += 1; ... }")
    assert "REQUEST-CHANGES" in out
    assert len(run.by_type(IssueFound)) == 1
    assert run.by_type(VerifyResult)[0].holds is True


@pytest.mark.asyncio
async def test_review_repair_recovers_prose_reviewer_e2e(tmp_path, monkeypatch):
    """Repair re-prompts after prose-only first response; second response emits valid issue through fenced-JSON bundle path."""
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _when("for **correctness** only", text="The cursor logic looks suspicious..."),
            _when(
                "produced no valid emission",
                {
                    "issue_found": {
                        "dimension": "correctness",
                        "description": "off-by-one in cursor advance",
                        "severity": "major",
                        "location": "store.rs:41",
                        "confidence": 0.7,
                    }
                },
            ),
            _when("Issue a single ReviewVerdict", text="REQUEST-CHANGES: fix cursor advance."),
        ],
    )

    notified: list[dict] = []
    eng = ReviewEngine(model=SCRIPTED_MODEL, dimensions=("correctness",), repair_retries=1)
    out = await eng.run(
        "fn next(&mut self) { self.pos += 1; ... }",
        on_event=lambda e: notified.append(e),
    )
    assert "REQUEST-CHANGES" in out
    assert any(e["type"] == "emission_repair" for e in notified)


@pytest.mark.asyncio
async def test_hypothesis_budget_degrades_gracefully_e2e(tmp_path, monkeypatch):
    """Expansion stops at budget, but the exempt synthesizer still writes the final report."""
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _when(
                "Extract the architectural questions",
                {
                    "question_raised": {
                        "area": "a",
                        "what_is_unknown": "why X?",
                        "parent_ref": "F-1",
                        "gen": 0,
                    }
                },
            ),
            _when("Write the evidence report", text="PARTIAL REPORT: 1 open question."),
        ],
    )

    notified: list[dict] = []
    eng = HypothesisEngine(model=SCRIPTED_MODEL, max_agents=1, repair_retries=0)
    report = await eng.run("seed finding", on_event=lambda e: notified.append(e))
    assert "PARTIAL REPORT" in report
    assert any(e["type"] == "budget_exhausted" for e in notified)


@pytest.mark.asyncio
async def test_coding_engine_pass_path_e2e(tmp_path, monkeypatch):
    """plan+implement emit through fenced-JSON bundle path; real subprocess gate runs; critic approves."""
    _write_script(
        tmp_path,
        monkeypatch,
        [
            _when(
                "planning a coding task",
                {
                    "work_planned": {
                        "approach": "add a hello() that returns 'hi'",
                        "files_to_touch": ["hello.py"],
                        "test_strategy": "assert hello() == 'hi'",
                        "acceptance_criteria": ["hello() returns 'hi'"],
                    }
                },
            ),
            _when(
                "Implement the plan",
                {
                    "change_proposed": {
                        "summary": "wrote hello() returning 'hi'",
                        "files_touched": ["hello.py"],
                        "test_cmd": "pytest -q",
                        "plan_ref": "W-1",
                    }
                },
            ),
            _when(
                "Review the implemented change",
                {
                    "verify_result": {
                        "verdict": "APPROVE",
                        "rationale": "tests green and hello() returns 'hi'",
                        "meets_acceptance": True,
                        "change_ref": "P-1",
                        "tests_ref": "T-1",
                    }
                },
            ),
        ],
    )

    eng = CodingEngine(model=SCRIPTED_MODEL, repair_retries=0)
    run = eng.new_run()
    result = await eng._run(
        run,
        "add a hello function",
        # ground truth: a trivial command that exits 0, through the REAL runner
        test_cmd="true",
        workspace=str(tmp_path),
        export_dir=tmp_path / "out",
    )

    assert isinstance(result, CodeResultRecorded)
    assert result.passed is True
    # the full typed chain formed through the real emission path:
    assert run.events_of(WorkPlanned)[0].acceptance_criteria == ["hello() returns 'hi'"]
    assert run.events_of(ChangeProposed)[0].plan_ref == "W-1"
    assert run.events_of(TestsRan)[0].passed is True  # ground truth, not a claim
    assert run.events_of(CodeVerifyResult)[0].meets_acceptance is True

    # results.json carries hypothesis-ingestible seeds
    data = json.loads((tmp_path / "out" / "results.json").read_text())
    assert data["passed"] is True
    seed = data["hypothesis_seeds"][0]
    rr = ResultRecorded.model_validate(seed)  # the bus-interop bridge round-trips
    assert rr.passed is True
