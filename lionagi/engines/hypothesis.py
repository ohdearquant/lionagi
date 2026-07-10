# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Hypothesis engine — Chain shape: finding → question → evidence → hypothesis → experiment → result → conclusion → application."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import Field

from lionagi.casts.emission import Finding, Gap, Verdict

from .engine import ChainEvent, ChainRun, Engine, EngineEvent, EngineRun

logger = logging.getLogger("lionagi.engines")

__all__ = (
    "ChainEvent",
    "FindingPosted",
    "QuestionRaised",
    "EvidenceCollected",
    "HypothesisFormed",
    "ExperimentDesigned",
    "ResultRecorded",
    "ConclusionDrawn",
    "ApplicationMapped",
    "HypothesisRun",
    "HypothesisEngine",
    "trace_chains",
)


# ---------------------------------------------------------------------------
# Events — the pipeline vocabulary. Each carries refs to upstream events;
# the engine stamps ``eid``, agents fill refs from their instructions.
# ---------------------------------------------------------------------------


class FindingPosted(Finding, ChainEvent):
    """An observation entering the pipeline; extends Finding with cycle generation and an optional upstream ref."""

    gen: int = Field(default=0, description="Cycle generation — copy from your instruction.")
    parent_ref: str = Field(
        default="", description="Id of the upstream event that surfaced this, '' for seeds."
    )


class QuestionRaised(Gap, ChainEvent):
    """An implicit choice extracted from a finding; extends Gap with rejected alternatives and a decision ref."""

    gen: int = Field(default=0, description="Cycle generation — copy from your instruction.")
    parent_ref: str = Field(default="", description="Id of the event that raised this question.")
    alternatives: list[str] = Field(
        default_factory=list, description="The options rejected by the choice under test."
    )
    decision_ref: str = Field(
        default="",
        description="Decision id from the register this bears on (e.g. 'D-012'), '' if none.",
    )


class EvidenceCollected(Finding, ChainEvent):
    """One piece of evidence for a question; extends Finding with a question ref and evidence kind."""

    question_ref: str = Field(description="Id of the question this evidence addresses.")
    kind: str = Field(
        default="analysis",
        description="citation | precedent | analysis | measurement | knowledge.",
    )


class HypothesisFormed(EngineEvent, ChainEvent):
    """A falsifiable prediction formed from a question's evidence."""

    question_ref: str = Field(description="Id of the question this hypothesis answers.")
    statement: str = Field(description="The falsifiable prediction, stated concretely.")
    metric: str = Field(default="", description="What to measure to test it.")
    threshold: str = Field(default="", description="The pass/fail boundary on the metric.")
    falsifier: str = Field(
        default="", description="The observation that would falsify the hypothesis."
    )


class ExperimentDesigned(EngineEvent, ChainEvent):
    """The decisive test for a hypothesis — cheapest method that decides."""

    hypothesis_ref: str = Field(description="Id of the hypothesis under test.")
    method: str = Field(description="benchmark | analysis | proof | comparison.")
    dataset: str = Field(default="", description="Data or fixtures the experiment needs.")
    procedure: str = Field(description="Concrete steps to execute.")
    acceptance: str = Field(default="", description="The criteria that mean the hypothesis holds.")


class ResultRecorded(EngineEvent, ChainEvent):
    """The outcome of executing an experiment — numbers or a worked derivation."""

    experiment_ref: str = Field(description="Id of the experiment that produced this.")
    measurements: str = Field(description="The concrete numbers, counts, or derivation.")
    passed: bool | None = Field(
        default=None, description="Verdict vs acceptance criteria; null if inconclusive."
    )
    caveats: list[str] = Field(
        default_factory=list, description="Conditions that limit what this result shows."
    )


class ConclusionDrawn(Verdict, ChainEvent):
    """The typed conclusion on a question; extends Verdict with basis, confidence, and limitations. basis='taste' is legitimate when no stronger evidence exists."""

    question_ref: str = Field(description="Id of the question being concluded.")
    result_ref: str = Field(
        default="", description="Id of the supporting result; '' for taste/theoretical paths."
    )
    basis: str = Field(description="empirical | quantitative | theoretical | taste.")
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="How settled this conclusion is."
    )
    limitations: list[str] = Field(
        default_factory=list, description="Scopes where this conclusion does not apply."
    )


class ApplicationMapped(EngineEvent, ChainEvent):
    """A conclusion applied to the architecture — the ADR-support link."""

    conclusion_ref: str = Field(description="Id of the conclusion being applied.")
    decision_ref: str = Field(description="The decision or component this bears on (e.g. 'D-012').")
    effect: str = Field(description="supports | challenges | qualifies.")
    note: str = Field(default="", description="How the conclusion bears on the decision.")


_EVENT_PREFIX: dict[type, str] = {
    FindingPosted: "F",
    QuestionRaised: "Q",
    EvidenceCollected: "E",
    HypothesisFormed: "H",
    ExperimentDesigned: "X",
    ResultRecorded: "R",
    ConclusionDrawn: "C",
    ApplicationMapped: "A",
}

_REF_ATTRS = (
    "conclusion_ref",
    "result_ref",
    "experiment_ref",
    "hypothesis_ref",
    "question_ref",
    "parent_ref",
)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def trace_chains(events: list[ChainEvent]) -> list[list[ChainEvent]]:
    """Reconstruct evidence chains (root to terminal) from event refs; terminals are ApplicationMapped or unapplied ConclusionDrawn events."""
    index = {e.eid: e for e in events if e.eid}
    applied = {e.conclusion_ref for e in events if isinstance(e, ApplicationMapped)}
    terminals = [e for e in events if isinstance(e, ApplicationMapped)]
    terminals += [e for e in events if isinstance(e, ConclusionDrawn) and e.eid not in applied]

    chains: list[list[ChainEvent]] = []
    for terminal in terminals:
        chain = [terminal]
        visited = {terminal.eid}
        cur: ChainEvent | None = terminal
        while cur is not None:
            nxt = None
            for attr in _REF_ATTRS:
                ref = getattr(cur, attr, "")
                if ref and ref in index and ref not in visited:
                    nxt = index[ref]
                    break
            if nxt is None:
                break
            chain.append(nxt)
            visited.add(nxt.eid)
            cur = nxt
        chain.reverse()
        chains.append(chain)
    return chains


def _label(e: ChainEvent) -> str:
    text = (
        getattr(e, "what_is_unknown", "")
        or getattr(e, "statement", "")
        or getattr(e, "verdict", "")
        or getattr(e, "description", "")
        or getattr(e, "measurements", "")
        or getattr(e, "note", "")
        or getattr(e, "procedure", "")
    )
    text = " ".join(str(text).split())
    return f"{e.eid}[{type(e).__name__}] {text}"


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def _register_block(decisions: str) -> str:
    return f"\n\n# Decision register\n{decisions}" if decisions.strip() else ""


def _extract_instruction(f: FindingPosted, decisions: str, cap: int) -> str:
    return (
        f"A finding was posted (id {f.eid}, cycle {f.gen}):\n"
        f"- claim: {f.description}\n"
        f"- evidence: {f.evidence or '(none)'}\n"
        f"- source: {f.source or '(unknown)'}\n\n"
        "Extract the architectural questions hidden in it — every implicit choice "
        "where an alternative was rejected without evidence. For each, emit a "
        "question_raised with: area, what_is_unknown (the claim under test, phrased "
        "as 'why X over Y?'), alternatives (the rejected options), decision_ref "
        "(the decision id from the register it bears on, '' if none), "
        f"parent_ref='{f.eid}', gen={f.gen}. At most {cap} questions; skip choices "
        "that are pure style with no reversal cost."
        f"{_register_block(decisions)}"
    )


def _research_instruction(q: QuestionRaised) -> str:
    alts = "; ".join(q.alternatives) if q.alternatives else "(none stated)"
    return (
        f"Research question {q.eid} (cycle {q.gen}) in area '{q.area}':\n"
        f"- claim under test: {q.what_is_unknown}\n"
        f"- rejected alternatives: {alts}\n"
        f"- bears on decision: {q.decision_ref or '(unmapped)'}\n\n"
        "Gather concrete evidence from several kinds — citation (literature, docs), "
        "precedent (what shipped systems do), analysis (complexity, counting), "
        "measurement (existing numbers), knowledge (domain expertise). For each "
        "piece emit an evidence_collected with: description (the claim), kind, "
        "evidence (the concrete proof), source, confidence, "
        f"question_ref='{q.eid}'. Evidence for the alternatives counts as much as "
        "evidence for the chosen option. If a genuinely distinct sub-question "
        "emerges, emit a question_raised with parent_ref='" + q.eid + "' and "
        f"gen={q.gen + 1}."
    )


def _hypothesize_instruction(q: QuestionRaised, evidence: list[EvidenceCollected]) -> str:
    parts = [
        f"Form the testable hypothesis for question {q.eid}: {q.what_is_unknown}\n",
        f"\n# Evidence ({len(evidence)})",
    ]
    for e in evidence:
        parts.append(
            f"\n- [{e.eid} {e.kind} conf={e.confidence:.2f}] {e.description}"
            f" — {e.evidence or e.source or ''}"
        )
    parts.append(
        "\n\nIf the choice is decidable by measurement or formal argument, emit a "
        "hypothesis_formed with: statement (a falsifiable prediction), metric, "
        f"threshold, falsifier, question_ref='{q.eid}'. If it is provable from the "
        "evidence alone or is a pure preference, emit a conclusion_drawn directly "
        "with: verdict, rationale, basis ('theoretical' if provable, 'taste' if "
        f"preference), confidence, limitations, question_ref='{q.eid}', "
        "result_ref=''. Do not disguise taste as evidence — label it."
    )
    return "".join(parts)


def _design_instruction(h: HypothesisFormed) -> str:
    return (
        f"Design the decisive experiment for hypothesis {h.eid}:\n"
        f"- statement: {h.statement}\n"
        f"- metric: {h.metric or '(unspecified)'}\n"
        f"- threshold: {h.threshold or '(unspecified)'}\n"
        f"- falsifier: {h.falsifier or '(unspecified)'}\n\n"
        "Emit an experiment_designed with: method (benchmark | analysis | proof | "
        "comparison — prefer the cheapest that decides), dataset (data or fixtures "
        "needed), procedure (concrete executable steps), acceptance (what outcome "
        f"confirms the hypothesis), hypothesis_ref='{h.eid}'."
    )


def _validate_instruction(x: ExperimentDesigned, gen: int) -> str:
    return (
        f"Execute experiment {x.eid} ({x.method}):\n"
        f"- procedure: {x.procedure}\n"
        f"- dataset: {x.dataset or '(none)'}\n"
        f"- acceptance: {x.acceptance or '(judge rigorously)'}\n\n"
        "Carry it out with rigor — counting, complexity analysis, worked "
        "comparison, or proof steps; use tools if you have them. Emit a "
        "result_recorded with: measurements (the concrete numbers or derivation), "
        "passed (true/false vs acceptance, null if inconclusive), caveats, "
        f"experiment_ref='{x.eid}'. If execution surfaces a genuinely new fact, "
        f"emit a finding_posted with parent_ref='{x.eid}' and gen={gen + 1}."
    )


def _conclude_instruction(
    r: ResultRecorded,
    x: ExperimentDesigned | None,
    h: HypothesisFormed | None,
    q_eid: str,
    gen: int,
) -> str:
    caveats = "; ".join(r.caveats) if r.caveats else "(none)"
    return (
        f"Draw the conclusion from result {r.eid}:\n"
        f"- hypothesis: {h.statement if h else '(unavailable)'}\n"
        f"- method: {x.method if x else '(unavailable)'}\n"
        f"- measurements: {r.measurements}\n"
        f"- passed: {r.passed}\n"
        f"- caveats: {caveats}\n\n"
        "Emit a conclusion_drawn with: verdict (the decision the evidence supports), "
        "rationale, basis ('empirical' if measured, 'quantitative' if counted, "
        "'theoretical' if proven), confidence, limitations (scopes where it does "
        f"not apply), question_ref='{q_eid}', result_ref='{r.eid}'. If the result "
        "raises a genuine follow-up, emit a question_raised with "
        f"parent_ref='{r.eid}' and gen={gen + 1}."
    )


def _apply_instruction(c: ConclusionDrawn, decisions: str) -> str:
    lim = "; ".join(c.limitations) if c.limitations else "(none)"
    target = (
        "For each decision in the register it bears on"
        if decisions.strip()
        else "For each architectural component or decision it bears on"
    )
    return (
        f"Apply conclusion {c.eid} to the architecture:\n"
        f"- verdict: {c.verdict}\n"
        f"- rationale: {c.rationale}\n"
        f"- basis: {c.basis} (confidence {c.confidence:.2f})\n"
        f"- limitations: {lim}\n\n"
        f"{target}, emit an application_mapped with: decision_ref, effect "
        "('supports' | 'challenges' | 'qualifies'), note (exactly how it bears), "
        f"conclusion_ref='{c.eid}'. A conclusion that bears on nothing is a signal "
        "the question was not architectural — emit nothing in that case."
        f"{_register_block(decisions)}"
    )


def render_evidence(run: HypothesisRun) -> str:
    """Render the run's evidence trail (chains, conclusions, applications, pending experiments, open questions) as a markdown string."""
    events: list[ChainEvent] = [e for evs in run.store.values() for e in evs]
    chains = trace_chains(events)
    concluded = {c.question_ref for c in run.events_of(ConclusionDrawn)}
    open_qs = [q for q in run.events_of(QuestionRaised) if q.eid not in concluded]

    parts = [f"# Evidence chains ({len(chains)})"]
    for chain in chains:
        parts.append("\n- " + " -> ".join(_label(e) for e in chain))
    parts.append(f"\n\n# Conclusions ({len(run.events_of(ConclusionDrawn))})")
    for c in run.events_of(ConclusionDrawn):
        parts.append(f"\n- {c.eid} [{c.basis} conf={c.confidence:.2f}] {c.verdict} — {c.rationale}")
    parts.append(f"\n\n# Applications ({len(run.events_of(ApplicationMapped))})")
    for a in run.events_of(ApplicationMapped):
        parts.append(f"\n- {a.decision_ref} <- {a.effect} <- {a.conclusion_ref}: {a.note}")
    if run.pending:
        parts.append(f"\n\n# Pending experiments — need real infrastructure ({len(run.pending)})")
        for x in run.pending:
            parts.append(
                f"\n- {x.eid} [{x.method}] {x.procedure} | dataset: {x.dataset} "
                f"| acceptance: {x.acceptance}"
            )
    if open_qs:
        parts.append(f"\n\n# Open questions — no conclusion yet ({len(open_qs)})")
        for q in open_qs:
            parts.append(f"\n- {q.eid} {q.what_is_unknown}")
    return "".join(parts)


def _synthesis_instruction(run: HypothesisRun) -> str:
    return (
        "Write the evidence report for this hypothesis-pipeline run.\n\n"
        f"{render_evidence(run)}\n\n"
        "Organize by decision, not by pipeline stage: for each decision touched, "
        "state what is now supported, challenged, or qualified, on what basis "
        "(empirical | quantitative | theoretical | taste), and what evidence is "
        "still missing. List pending experiments as a ready-to-run queue. Be "
        "specific; do not pad."
    )


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


class HypothesisRun(ChainRun):
    """Per-run state for a HypothesisEngine run: event store, eid counters, pending experiments, and decisions text."""

    _chain_event_cls = ChainEvent
    _event_prefix_map = _EVENT_PREFIX

    def __init__(self, engine: Engine, **kwargs: Any) -> None:
        super().__init__(engine, **kwargs)
        self.pending: list[ExperimentDesigned] = []
        self.decisions: str = ""

    # -- typed overrides (narrower signatures than the Any base) ---------------

    def collect(self, event: ChainEvent) -> ChainEvent:
        return super().collect(event)  # type: ignore[return-value]

    def find(self, eid: str) -> ChainEvent | None:
        return self._index.get(eid)

    def events_of(self, event_type: type) -> list[Any]:
        return self.store.get(event_type, [])

    def export(self, dir_path: str | Path, *, report: str = "") -> dict[str, str]:
        """Write chains.json (event graph) and report.md (synthesis + evidence trail) to *dir_path*; returns paths dict."""
        d = Path(dir_path)
        d.mkdir(parents=True, exist_ok=True)
        events = [e for evs in self.store.values() for e in evs]
        chains = trace_chains(events)
        concluded = {c.question_ref for c in self.events_of(ConclusionDrawn)}
        payload = {
            "root": self.root,
            "agents_made": self.agents_made,
            "events": [{"type": type(e).__name__, **e.model_dump()} for e in events],
            "chains": [[e.eid for e in ch] for ch in chains],
            "pending_experiments": [x.model_dump() for x in self.pending],
            "open_questions": [
                q.eid for q in self.events_of(QuestionRaised) if q.eid not in concluded
            ],
            "decisions_touched": sorted(
                {a.decision_ref for a in self.events_of(ApplicationMapped) if a.decision_ref}
            ),
        }
        chains_path = d / "chains.json"
        chains_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        trail = render_evidence(self)
        md = f"{report.strip()}\n\n---\n\n{trail}\n" if report.strip() else f"{trail}\n"
        report_path = d / "report.md"
        report_path.write_text(md, encoding="utf-8")
        return {"chains": str(chains_path), "report": str(report_path)}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class HypothesisEngine(Engine):
    """Evidence-chain engine for hypothesis-driven development (stateless config). See docs/reference/engines.md for parameter details."""

    run_context_cls: type[EngineRun] = HypothesisRun

    def __init__(
        self,
        *,
        question_role: str = "analyst",
        research_role: str = "researcher",
        hypothesis_role: str = "analyst",
        design_role: str = "evaluator",
        validate_role: str = "analyst",
        conclude_role: str = "critic",
        apply_role: str = "architect",
        synthesis_role: str = "synthesizer",
        executable_methods: tuple[str, ...] = ("analysis", "comparison", "proof"),
        validate_tools: tuple[str, ...] = (),
        validate_cwd: str | None = None,
        validate_permissions: str | None = "safe",
        max_questions: int = 8,
        repair_retries: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.question_role = question_role
        self.research_role = research_role
        self.hypothesis_role = hypothesis_role
        self.design_role = design_role
        self.validate_role = validate_role
        self.conclude_role = conclude_role
        self.apply_role = apply_role
        self.synthesis_role = synthesis_role
        self.executable_methods = set(executable_methods)
        self.validate_tools = tuple(validate_tools)
        self.validate_cwd = validate_cwd
        self.validate_permissions = validate_permissions
        self.max_questions = max_questions
        self.repair_retries = repair_retries

    # -- lifecycle ------------------------------------------------------------

    async def _partial_export(  # type: ignore[override]
        self,
        run: HypothesisRun,
        findings: str | list[str],
        *,
        decisions: str = "",
        export_dir: str | Path | None = None,
    ) -> str:
        """Synthesize and export collected events after budget cancellation; returns empty string if nothing was collected."""
        events_collected = sum(len(v) for v in run.store.values())
        if events_collected == 0:
            return ""
        report = await self._synthesize(run)
        # Prepend so the report is self-describing without the events.jsonl context.
        status_header = (
            "**status: budget_exhausted** — "
            f"run terminated by deadline/budget before completion "
            f"({run.agents_made} agents, "
            f"{events_collected} events collected)\n\n"
        )
        report = status_header + (report or "")
        if export_dir is not None:
            paths = run.export(export_dir, report=report)
            run.notify("exported", **paths)
        return report

    async def _run(
        self,
        run: HypothesisRun,
        findings: str | list[str],
        *,
        decisions: str = "",
        export_dir: str | Path | None = None,
    ) -> str:
        """Push *findings* through the pipeline and return the synthesis report; writes chains.json + report.md when export_dir is set."""
        seeds = [findings] if isinstance(findings, str) else list(findings)
        seeds = [s.strip() for s in seeds if s and s.strip()]
        if not seeds:
            raise ValueError("findings is empty")
        run.decisions = decisions
        run.root = " | ".join(seeds)

        # Collector first: stamps eids before any reaction reads them.
        run.observe(ChainEvent, lambda e, _c: run.collect(e))
        run.observe(FindingPosted, lambda f, _c: self._on_finding(run, f))
        run.observe(QuestionRaised, lambda q, _c: self._on_question(run, q))
        run.observe(HypothesisFormed, lambda h, _c: self._on_hypothesis(run, h))
        run.observe(ExperimentDesigned, lambda x, _c: self._on_experiment(run, x))
        run.observe(ResultRecorded, lambda r, _c: self._on_result(run, r))
        run.observe(ConclusionDrawn, lambda c, _c: self._on_conclusion(run, c))

        for seed in seeds:
            await run.emit(FindingPosted(description=seed, source="seed", gen=0))

        await run.wait_quiescence()
        report = await self._synthesize(run)
        if export_dir is not None:
            paths = run.export(export_dir, report=report)
            run.notify("exported", **paths)
        return report

    # -- reactions ------------------------------------------------------------

    def _on_finding(self, run: HypothesisRun, f: FindingPosted) -> None:
        if f.gen > self.max_depth:
            run.notify("cycle_capped", eid=f.eid, gen=f.gen)
            return
        if run.seen(f"f:{f.description}"):
            return
        run.spawn(self._guard(run, "extract", self._extract, f))

    def _on_question(self, run: HypothesisRun, q: QuestionRaised) -> None:
        if q.gen > self.max_depth:
            run.notify("cycle_capped", eid=q.eid, gen=q.gen)
            return
        if run.seen(f"q:{q.what_is_unknown}"):
            return
        run.spawn(self._guard(run, "research", self._research, q))

    def _on_hypothesis(self, run: HypothesisRun, h: HypothesisFormed) -> None:
        if run.seen(f"h:{h.statement}"):
            return
        run.spawn(self._guard(run, "design", self._design, h))

    def _on_experiment(self, run: HypothesisRun, x: ExperimentDesigned) -> None:
        if x.method not in self.executable_methods:
            run.pending.append(x)
            run.notify("experiment_pending", eid=x.eid, method=x.method)
            return
        run.spawn(self._guard(run, "validate", self._validate, x))

    def _on_result(self, run: HypothesisRun, r: ResultRecorded) -> None:
        if run.seen(f"r:{r.experiment_ref}:{r.eid}"):
            return
        run.spawn(self._guard(run, "conclude", self._conclude, r))

    def _on_conclusion(self, run: HypothesisRun, c: ConclusionDrawn) -> None:
        run.spawn(self._guard(run, "apply", self._apply, c))

    # -- stages ---------------------------------------------------------------

    async def _guard(self, run: HypothesisRun, stage: str, fn: Any, event: Any) -> None:
        """Run a stage function, logging and notifying on failure so a stage error never kills the pipeline."""
        try:
            await fn(run, event)
        except Exception as exc:
            logger.warning("hypothesis stage %s failed: %s", stage, exc)
            run.notify("stage_error", stage=stage, error=str(exc))

    async def _extract(self, run: HypothesisRun, f: FindingPosted) -> None:
        # Seeds (gen 0) are caller-provided; agent-sourced findings (gen > 0)
        # pass the judge before spending more budget.
        if f.gen > 0 and not await self.judge(run, f.eid, _label(f)):
            return
        emits = (QuestionRaised,)
        async with run._sem:
            agent = await run.make_agent(
                self.question_role,
                name=f"extract-{f.eid}",
                model=self.model_for("extract"),
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _extract_instruction(f, run.decisions, self.max_questions),
                arrived=lambda: any(x.parent_ref == f.eid for x in run.events_of(QuestionRaised)),
                emits=emits,
                retries=self.repair_retries,
            )

    async def _research(self, run: HypothesisRun, q: QuestionRaised) -> None:
        subject = f"{_label(q)} | alternatives: {'; '.join(q.alternatives) or '(none)'}"
        if not await self.judge(run, q.eid, subject):
            return
        emits = (EvidenceCollected, QuestionRaised)
        async with run._sem:
            researcher = await run.make_agent(
                self.research_role,
                name=f"research-{q.eid}",
                model=self.model_for("research"),
                emits=emits,
            )
            await run.operate_with_repair(
                researcher,
                _research_instruction(q),
                arrived=lambda: any(
                    e.question_ref == q.eid for e in run.events_of(EvidenceCollected)
                ),
                emits=(EvidenceCollected,),
                retries=self.repair_retries,
            )
        evidence = [e for e in run.events_of(EvidenceCollected) if e.question_ref == q.eid]
        h_emits = (HypothesisFormed, ConclusionDrawn)
        async with run._sem:
            hypothesizer = await run.make_agent(
                self.hypothesis_role,
                name=f"hypothesize-{q.eid}",
                model=self.model_for("hypothesize"),
                emits=h_emits,
            )
            await run.operate_with_repair(
                hypothesizer,
                _hypothesize_instruction(q, evidence),
                arrived=lambda: (
                    any(h.question_ref == q.eid for h in run.events_of(HypothesisFormed))
                    or any(c.question_ref == q.eid for c in run.events_of(ConclusionDrawn))
                ),
                emits=h_emits,
                retries=self.repair_retries,
            )

    async def _design(self, run: HypothesisRun, h: HypothesisFormed) -> None:
        emits = (ExperimentDesigned,)
        async with run._sem:
            agent = await run.make_agent(
                self.design_role,
                name=f"design-{h.eid}",
                model=self.model_for("design"),
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _design_instruction(h),
                arrived=lambda: any(
                    x.hypothesis_ref == h.eid for x in run.events_of(ExperimentDesigned)
                ),
                emits=emits,
                retries=self.repair_retries,
            )

    async def _validate(self, run: HypothesisRun, x: ExperimentDesigned) -> None:
        h = run.find(x.hypothesis_ref)
        q = run.find(h.question_ref) if isinstance(h, HypothesisFormed) else None
        gen = q.gen if isinstance(q, QuestionRaised) else 0
        emits = (ResultRecorded, FindingPosted)
        async with run._sem:
            agent = await run.make_agent(
                self.validate_role,
                name=f"validate-{x.eid}",
                model=self.model_for("validate"),
                tools=self.validate_tools,
                permissions=self.validate_permissions if self.validate_tools else None,
                cwd=self.validate_cwd,
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _validate_instruction(x, gen),
                arrived=lambda: any(
                    r.experiment_ref == x.eid for r in run.events_of(ResultRecorded)
                ),
                emits=(ResultRecorded,),
                retries=self.repair_retries,
            )

    async def _conclude(self, run: HypothesisRun, r: ResultRecorded) -> None:
        x = run.find(r.experiment_ref)
        x = x if isinstance(x, ExperimentDesigned) else None
        h = run.find(x.hypothesis_ref) if x else None
        h = h if isinstance(h, HypothesisFormed) else None
        q = run.find(h.question_ref) if h else None
        q = q if isinstance(q, QuestionRaised) else None
        emits = (ConclusionDrawn, QuestionRaised)
        async with run._sem:
            agent = await run.make_agent(
                self.conclude_role,
                name=f"conclude-{r.eid}",
                model=self.model_for("conclude"),
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _conclude_instruction(r, x, h, q.eid if q else "", q.gen if q else 0),
                arrived=lambda: any(c.result_ref == r.eid for c in run.events_of(ConclusionDrawn)),
                emits=(ConclusionDrawn,),
                retries=self.repair_retries,
            )

    async def _apply(self, run: HypothesisRun, c: ConclusionDrawn) -> None:
        async with run._sem:
            agent = await run.make_agent(
                self.apply_role,
                name=f"apply-{c.eid}",
                model=self.model_for("apply"),
                emits=(ApplicationMapped,),
            )
            # No repair: "bears on nothing -> emit nothing" is legitimate here.
            await agent.operate(instruction=_apply_instruction(c, run.decisions))

    async def _synthesize(self, run: HypothesisRun) -> str:
        run.notify(
            "synthesizing",
            conclusions=len(run.events_of(ConclusionDrawn)),
            applications=len(run.events_of(ApplicationMapped)),
            pending=len(run.pending),
        )
        synth = await run.make_agent(
            self.synthesis_role,
            name="synthesizer",
            model=self.model_for("synthesize"),
            exempt=True,
        )
        res = await synth.operate(instruction=_synthesis_instruction(run))
        return str(res) if res is not None else ""
