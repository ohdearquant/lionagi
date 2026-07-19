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

from .engine import ChainEvent, ChainRun, Engine, EngineEvent, EngineRun, role_profile_route

logger = logging.getLogger("lionagi.engines")

__all__ = (
    "ChainEvent",
    "FindingPosted",
    "DedupChecked",
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


# --- Events — the pipeline vocabulary (engine stamps eid; agents fill refs
# from their instructions). Each event carries refs to upstream events. ---


class FindingPosted(Finding, ChainEvent):
    """An observation entering the pipeline; extends Finding with cycle generation and an optional upstream ref."""

    gen: int = Field(default=0, description="Cycle generation — copy from your instruction.")
    parent_ref: str = Field(
        default="", description="Id of the upstream event that surfaced this, '' for seeds."
    )


class DedupChecked(EngineEvent, ChainEvent):
    """Novelty verdict on a finding against the target repo's issue tracker; runs before extraction when a dedup repo is configured."""

    finding_ref: str = Field(description="Id of the finding this verdict covers.")
    verdict: str = Field(
        description=(
            "new (no issue covers the mechanism) | duplicate (an issue covers this "
            "exact mechanism; closed means already fixed) | extends (a related issue "
            "exists but not this exact mechanism)."
        )
    )
    issue_number: int | None = Field(
        default=None, description="Most specific matching issue number; null for new."
    )
    issue_title: str = Field(default="", description="Title of the matched issue, '' for new.")
    issue_state: str = Field(default="", description="open | closed | '' for new.")
    source_confirmed: bool | None = Field(
        default=None,
        description="Whether the finding's cite was confirmed in source; null if not checked.",
    )
    rationale: str = Field(
        default="",
        description="One line citing the matched issue text or the decisive absence.",
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
    DedupChecked: "D",
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


# --- Audit trail ---


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


# --- Instructions ---


def _register_block(decisions: str) -> str:
    return f"\n\n# Decision register\n{decisions}" if decisions.strip() else ""


def _extract_instruction(f: FindingPosted, decisions: str, cap: int) -> str:
    return (
        f"A finding was posted (id {f.eid}, cycle {f.gen}):\n"
        f"- claim: {f.description}\n"
        f"- evidence: {f.evidence or '(none)'}\n"
        f"- source: {f.source or '(unknown)'}\n\n"
        "Extract the architectural questions hidden in it — every implicit choice "
        "where an alternative was rejected without evidence. If the finding is "
        "already adjudicated (mechanism established, remedy decided), extract the "
        "questions that VALIDATE the adjudication instead: does the chosen remedy "
        "close the failure under adversarial conditions, is it better than the "
        "rejected remedies, does the same mechanism recur elsewhere. For each, "
        "emit a question_raised with: area, what_is_unknown (the claim under test, "
        "phrased as 'why X over Y?'), alternatives (the rejected options), "
        "decision_ref (the decision id from the register it bears on, '' if none), "
        f"parent_ref='{f.eid}', gen={f.gen}. At most {cap} questions; skip choices "
        "that are pure style with no reversal cost. Only if genuinely nothing is "
        "open and nothing needs validation, emit no questions and say why."
        f"{_register_block(decisions)}"
    )


def _dedup_instruction(
    f: FindingPosted,
    repo: str,
    seed_issues: tuple[int, ...],
    has_checkout: bool,
) -> str:
    seeds = ""
    if seed_issues:
        seeds = (
            "3. Known related issues to verify precisely (do NOT limit the search "
            f"to them): {', '.join(f'#{n}' for n in seed_issues)}.\n"
        )
    confirm = (
        "4. Source-confirm the finding's cite by reading the cited symbol in the "
        "checkout at your working directory; set source_confirmed true/false.\n"
        if has_checkout
        else "4. No checkout is available; set source_confirmed to null.\n"
    )
    return (
        f"A finding is entering a hypothesis pipeline (id {f.eid}, cycle {f.gen}):\n"
        f"- claim: {f.description}\n"
        f"- evidence: {f.evidence or '(none)'}\n"
        f"- source: {f.source or '(unknown)'}\n\n"
        f"Determine whether the GitHub repo '{repo}' already tracks this finding's "
        "MECHANISM. Match on the defect mechanism (what breaks, where, why), never "
        "on title keywords or line numbers — line numbers drift, and same-file is "
        "not same-mechanism.\n\n"
        "Method (read-only):\n"
        "1. Query filed issues, OPEN and CLOSED — a closed issue means already "
        f"fixed, still a duplicate: `gh issue list --repo {repo} --state all "
        "--limit 400 --json number,title,state`, plus "
        f'`gh search issues --repo {repo} "<concept and symbol terms>"`.\n'
        f"2. Read candidate bodies: `gh issue view <N> --repo {repo} --json "
        "title,body,state`; judge whether the issue covers THIS mechanism, not "
        "merely the same file or area. Place the finding against the MOST "
        "SPECIFIC covering issue.\n"
        f"{seeds}{confirm}\n"
        f"Emit a dedup_checked with: finding_ref='{f.eid}', verdict ('new' | "
        "'duplicate' | 'extends'), issue_number / issue_title / issue_state for "
        "the most specific match (null / '' for new; for 'extends' state in the "
        "rationale what the issue covers vs the gap this finding adds), "
        "source_confirmed, rationale (one line citing the matched issue text or "
        "the decisive absence).\n\n"
        "HARD constraints: read-only — never file, edit, comment on, or close any "
        "issue or PR; no builds, compiles, or tests; every issue number you cite "
        "must come from a query result you actually saw — never fabricate one."
    )


def _judge_bar(gen: int) -> str:
    """Escalating admission bar appended to judge subjects for recursive cycles."""
    if gen >= 2:
        return (
            "\n\nEscalated bar (cycle 2+): admit only if leaving this unanswered "
            "plausibly threatens correctness."
        )
    if gen == 1:
        return (
            "\n\nEscalated bar (cycle 1): admit only if the answer could change a "
            "register decision or an already-drawn conclusion."
        )
    return ""


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
    dedups = run.events_of(DedupChecked)
    if dedups:
        parts.append(f"\n\n# Novelty verdicts ({len(dedups)})")
        for d in dedups:
            issue = f" #{d.issue_number} ({d.issue_state})" if d.issue_number else ""
            parts.append(f"\n- {d.finding_ref}: {d.verdict}{issue} — {d.rationale}")
        queue = filing_queue(run)
        if queue:
            parts.append(f"\n\n# Filing queue — certified findings, not yet filed ({len(queue)})")
            parts.append("\nFiling is the owner's call; this engine never files.")
            for item in queue:
                ext = f" (extends #{item['issue_number']})" if item.get("issue_number") else ""
                parts.append(f"\n- {item['finding_ref']} [{item['verdict']}{ext}] {item['claim']}")
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


def filing_queue(run: HypothesisRun) -> list[dict[str, Any]]:
    """Findings certified 'new' or 'extends' by the dedup stage, with their conclusions — ready for the owner to file. The engine never files."""
    conclusions = run.events_of(ConclusionDrawn)
    questions = {q.eid: q for q in run.events_of(QuestionRaised)}
    out: list[dict[str, Any]] = []
    for d in run.events_of(DedupChecked):
        if d.verdict not in ("new", "extends"):
            continue
        f = run.find(d.finding_ref)
        if not isinstance(f, FindingPosted):
            continue
        related = [
            c.eid
            for c in conclusions
            if (q := questions.get(c.question_ref)) is not None and q.parent_ref == f.eid
        ]
        out.append(
            {
                "finding_ref": f.eid,
                "claim": f.description,
                "verdict": d.verdict,
                "issue_number": d.issue_number,
                "source_confirmed": d.source_confirmed,
                "rationale": d.rationale,
                "conclusions": related,
            }
        )
    return out


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


# --- Run context ---


class HypothesisRun(ChainRun):
    """Per-run state for a HypothesisEngine run: event store, eid counters, pending experiments, and decisions text."""

    _chain_event_cls = ChainEvent
    _event_prefix_map = _EVENT_PREFIX

    def __init__(self, engine: Engine, **kwargs: Any) -> None:
        super().__init__(engine, **kwargs)
        self.pending: list[ExperimentDesigned] = []
        self.decisions: str = ""
        # Findings that passed the dedup gate (and its judge, for gen > 0), so
        # extraction neither re-judges nor waits on a second novelty check.
        self.dedup_cleared: set[str] = set()

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
            "filing_queue": filing_queue(self),
        }
        chains_path = d / "chains.json"
        chains_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        trail = render_evidence(self)
        md = f"{report.strip()}\n\n---\n\n{trail}\n" if report.strip() else f"{trail}\n"
        report_path = d / "report.md"
        report_path.write_text(md, encoding="utf-8")
        return {"chains": str(chains_path), "report": str(report_path)}


# --- Engine ---


class HypothesisEngine(Engine):
    """Evidence-chain engine for hypothesis-driven development (stateless config). See docs/reference/engines.md for parameter details."""

    run_context_cls: type[EngineRun] = HypothesisRun

    #: Per-stage model fallbacks, consulted after the caller's ``models`` /
    #: ``model``. CLI-backed providers so a bare run needs no API key; heavier
    #: reasoning tiers on decomposition, validation, and verdict stages; a
    #: lighter tier where the work is retrieval- or mapping-shaped.
    STAGE_MODEL_DEFAULTS: dict[str, str] = {
        "dedup": "codex/gpt-5.6-luna",
        "extract": "codex/gpt-5.6-terra",
        "research": "codex/gpt-5.6-terra",
        "hypothesize": "codex/gpt-5.6-terra",
        "design": "codex/gpt-5.6-luna",
        "validate": "codex/gpt-5.6-terra",
        "conclude": "codex/gpt-5.6-terra",
        "apply": "codex/gpt-5.6-luna",
        "synthesize": "claude_code/sonnet",
    }
    #: Per-stage reasoning-effort fallbacks, same precedence as models.
    #: Highest on conclude (the verdict gate), high on the stages that shape
    #: the run (extract, hypothesize, validate), medium on volume/mapping legs.
    STAGE_EFFORT_DEFAULTS: dict[str, str] = {
        "dedup": "medium",
        "extract": "high",
        "research": "medium",
        "hypothesize": "high",
        "design": "medium",
        "validate": "high",
        "conclude": "xhigh",
        "apply": "medium",
    }
    #: Judge defaults ON for this engine (pass ``judge_model=None`` to disable):
    #: recursive cycles need a cheap quality gate or they expand on noise.
    DEFAULT_JUDGE_MODEL: str = "claude_code/haiku"

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
        dedup_role: str = "investigator",
        dedup_repo: str | None = None,
        dedup_cwd: str | None = None,
        dedup_seed_issues: tuple[int, ...] = (),
        dedup_tools: tuple[str, ...] = ("bash",),
        executable_methods: tuple[str, ...] = ("analysis", "comparison", "proof"),
        validate_tools: tuple[str, ...] = (),
        validate_cwd: str | None = None,
        validate_permissions: str | None = "safe",
        max_questions: int = 8,
        repair_retries: int = 1,
        **kwargs: Any,
    ) -> None:
        # Recursion default: two cycles. Cycle 1 catches what execution
        # surfaces; deeper cycles trade quadratic agent spend for tail findings
        # and are opt-in via max_depth.
        kwargs.setdefault("max_depth", 2)
        kwargs.setdefault("judge_model", self.DEFAULT_JUDGE_MODEL)
        super().__init__(**kwargs)
        self.question_role = question_role
        self.research_role = research_role
        self.hypothesis_role = hypothesis_role
        self.design_role = design_role
        self.validate_role = validate_role
        self.conclude_role = conclude_role
        self.apply_role = apply_role
        self.synthesis_role = synthesis_role
        self.dedup_role = dedup_role
        self.dedup_repo = dedup_repo
        self.dedup_cwd = dedup_cwd
        self.dedup_seed_issues = tuple(dedup_seed_issues)
        self.dedup_tools = tuple(dedup_tools)
        self.executable_methods = set(executable_methods)
        self.validate_tools = tuple(validate_tools)
        self.validate_cwd = validate_cwd
        self.validate_permissions = validate_permissions
        self.max_questions = max_questions
        self.repair_retries = repair_retries

    def _stage_role(self, stage: str) -> str:
        return {
            "dedup": self.dedup_role,
            "extract": self.question_role,
            "research": self.research_role,
            "hypothesize": self.hypothesis_role,
            "design": self.design_role,
            "validate": self.validate_role,
            "conclude": self.conclude_role,
            "apply": self.apply_role,
            "synthesize": self.synthesis_role,
        }.get(stage, "")

    def model_for(self, stage: str) -> str | None:
        # Explicit stage/engine settings > the stage role's agent profile
        # (.lionagi/agents/<role>.md) > the shipped stage table.
        explicit = self.models.get(stage) or self.model
        if explicit:
            return explicit
        prof_model, _ = role_profile_route(self._stage_role(stage))
        return prof_model or self.STAGE_MODEL_DEFAULTS.get(stage)

    def effort_for(self, stage: str) -> str | None:
        explicit = self.efforts.get(stage) or self.effort
        if explicit:
            return explicit
        model = self.model_for(stage)
        if model:
            from lionagi.service.providers import _EFFORT_SUFFIX_RE  # noqa: PLC0415

            if _EFFORT_SUFFIX_RE.match(model.split("/", 1)[-1]):
                # The model spec bakes its own effort suffix — respect it
                # rather than overriding with a table default.
                return None
        _, prof_effort = role_profile_route(self._stage_role(stage))
        return prof_effort or self.STAGE_EFFORT_DEFAULTS.get(stage)

    def question_cap(self, gen: int) -> int:
        """Breadth halves each cycle: gen 0 gets ``max_questions``, gen 1 half, and so on, floored at 1 — recursion narrows instead of exploding."""
        return max(1, self.max_questions >> gen)

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
        run.observe(DedupChecked, lambda d, _c: self._on_dedup(run, d))
        run.observe(QuestionRaised, lambda q, _c: self._on_question(run, q))
        run.observe(HypothesisFormed, lambda h, _c: self._on_hypothesis(run, h))
        run.observe(ExperimentDesigned, lambda x, _c: self._on_experiment(run, x))
        run.observe(ResultRecorded, lambda r, _c: self._on_result(run, r))
        run.observe(ConclusionDrawn, lambda c, _c: self._on_conclusion(run, c))

        for seed in seeds:
            await run.emit(FindingPosted(description=seed, source="seed", gen=0))

        await run.wait_quiescence()
        # Total pipeline failure: stages errored and nothing beyond the seeds
        # was produced. Synthesizing an empty report would launder the failure
        # into a green-looking result — fail loud instead.
        produced = any(
            run.events_of(t)
            for t in (DedupChecked, QuestionRaised, EvidenceCollected, ConclusionDrawn)
        )
        if run._agent_errors and not produced:
            raise RuntimeError(
                "hypothesis pipeline produced no events beyond the seed findings; "
                f"{len(run._agent_errors)} stage failure(s), first: "
                f"{run._agent_errors[0]}"
            )
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
        if self.dedup_repo:
            run.spawn(self._guard(run, "dedup", self._dedup, f))
        else:
            run.spawn(self._guard(run, "extract", self._extract, f))

    def _on_dedup(self, run: HypothesisRun, d: DedupChecked) -> None:
        f = run.find(d.finding_ref)
        if not isinstance(f, FindingPosted):
            run.notify("dedup_orphan", eid=d.eid, finding_ref=d.finding_ref)
            return
        if d.verdict == "duplicate":
            run.notify(
                "finding_duplicate",
                eid=f.eid,
                issue=d.issue_number,
                state=d.issue_state,
            )
            return
        if f.eid in run.dedup_cleared:
            return
        run.dedup_cleared.add(f.eid)
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
            # agent_error feeds the run's terminal-failure accounting so a run
            # where every stage died is surfaced as failed, not green.
            run.notify("agent_error", agent=stage, error=str(exc))
            run.notify("stage_error", stage=stage, error=str(exc))

    async def _dedup(self, run: HypothesisRun, f: FindingPosted) -> None:
        # The novelty gate carries the recursive-cycle judge so junk findings
        # are rejected before a tracker-search agent is spent on them.
        if f.gen > 0 and not await self.judge(run, f.eid, _label(f) + _judge_bar(f.gen)):
            return
        emits = (DedupChecked,)
        cwd = self.dedup_cwd or self.validate_cwd
        async with run._sem:
            agent = await run.make_agent(
                self.dedup_role,
                name=f"dedup-{f.eid}",
                model=self.model_for("dedup"),
                effort=self.effort_for("dedup"),
                tools=self.dedup_tools,
                permissions="safe" if self.dedup_tools else None,
                cwd=cwd,
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _dedup_instruction(f, self.dedup_repo or "", self.dedup_seed_issues, bool(cwd)),
                arrived=lambda: any(d.finding_ref == f.eid for d in run.events_of(DedupChecked)),
                emits=emits,
                retries=self.repair_retries,
            )
        if not any(d.finding_ref == f.eid for d in run.events_of(DedupChecked)):
            # Fail open: an unanswered novelty check must not silently drop the
            # finding — a possible duplicate in the queue beats a lost finding.
            run.notify("dedup_missing", eid=f.eid)
            if f.eid not in run.dedup_cleared:
                run.dedup_cleared.add(f.eid)
                run.spawn(self._guard(run, "extract", self._extract, f))

    async def _extract(self, run: HypothesisRun, f: FindingPosted) -> None:
        # Seeds (gen 0) are caller-provided; agent-sourced findings (gen > 0)
        # pass the judge before spending more budget — unless the dedup gate
        # already judged them.
        if (
            f.gen > 0
            and f.eid not in run.dedup_cleared
            and not await self.judge(run, f.eid, _label(f) + _judge_bar(f.gen))
        ):
            return
        emits = (QuestionRaised,)
        async with run._sem:
            agent = await run.make_agent(
                self.question_role,
                name=f"extract-{f.eid}",
                model=self.model_for("extract"),
                effort=self.effort_for("extract"),
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _extract_instruction(f, run.decisions, self.question_cap(f.gen)),
                arrived=lambda: any(x.parent_ref == f.eid for x in run.events_of(QuestionRaised)),
                emits=emits,
                retries=self.repair_retries,
            )
        if not any(x.parent_ref == f.eid for x in run.events_of(QuestionRaised)):
            # Distinguish "finding fully settled, nothing to test" from a
            # malformed emission so an empty report is explainable.
            run.notify("no_questions", eid=f.eid, gen=f.gen)

    async def _research(self, run: HypothesisRun, q: QuestionRaised) -> None:
        subject = f"{_label(q)} | alternatives: {'; '.join(q.alternatives) or '(none)'}"
        if not await self.judge(run, q.eid, subject + _judge_bar(q.gen)):
            return
        emits = (EvidenceCollected, QuestionRaised)
        async with run._sem:
            researcher = await run.make_agent(
                self.research_role,
                name=f"research-{q.eid}",
                model=self.model_for("research"),
                effort=self.effort_for("research"),
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
                effort=self.effort_for("hypothesize"),
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
                effort=self.effort_for("design"),
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
                effort=self.effort_for("validate"),
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
                effort=self.effort_for("conclude"),
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
                effort=self.effort_for("apply"),
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
            effort=self.effort_for("synthesize"),
            exempt=True,
        )
        res = await synth.operate(instruction=_synthesis_instruction(run))
        return str(res) if res is not None else ""
