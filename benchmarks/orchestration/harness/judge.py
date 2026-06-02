"""Judge — score a RunResult against a task's ground-truth labels.

Anti-circularity discipline (CLAUDE.md): the judge is itself VALIDATED against
hand labels before its numbers are trusted. The question it answers is
deliberately CONSTRAINED — "does this output match THIS known label?" — not the
open-ended "is this good?", which keeps judge bias low. All raw outputs are
saved by run.py so every judge call can be spot-checked by a human.

Severity ordinal: none=0, low=1, medium=2, high=3, critical=4.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from lionagi.cli._providers import build_imodel_from_spec
from lionagi.session.branch import Branch

from .cost import cost_of
from .task import Label, RunResult, ScoredResult

logger = logging.getLogger("orchbench.judge")

_SEV = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Judge model family MUST differ from the agents under test to avoid
# self-preference bias (CR-Bench arxiv 2603.11078 uses Claude to judge GPT
# agents for exactly this reason). Codex/GPT agents → Claude judge.
_JUDGE_SPEC = "claude-code/sonnet"


class JudgeVerdict(BaseModel):
    """Blind judgment of one review output at ONE code location.

    The judge is NOT told whether the location holds a real defect or intended
    behaviour (audit F2/F3) — that would let it string-match the answer. It only
    reads the agent's own words and reports what the agent CONCLUDED. The harness
    applies the ground-truth label afterward.
    """

    examined: bool = Field(
        description="Did the review substantively examine/discuss the code AT THIS "
        "LOCATION — reach a conclusion about it (defect OR fine), not merely "
        "mention the file? Answer purely from the review's text."
    )
    flagged_as_defect: bool = Field(
        description="Did the review assert this location is a REAL defect/bug "
        "(not 'looks fine', not 'intended/by-design')?"
    )
    reported_severity: str = Field(
        description="The severity the review assigned to this location: "
        "none|low|medium|high|critical. Use 'none' if it concluded the code is fine."
    )
    rationale: str = Field(description="One sentence quoting the review's own words.")


def _judge_model():
    # Different-family judge (Claude) for codex/GPT agents — removes self-
    # preference circularity. Built via the canonical CLI resolver.
    return build_imodel_from_spec(_JUDGE_SPEC, yolo=True, effort_override="low")


async def _judge_one(output: str, label: Label, model) -> JudgeVerdict:
    branch = Branch(chat_model=model)
    # BLIND: give the judge the LOCATION POINTER only (so it can find the
    # relevant passage in a possibly-long review) — never the kind, the summary,
    # or the true severity. The judge reports what the agent concluded; whether
    # that conclusion is right is decided by the harness against the label.
    instruction = (
        "You are reading a code-review output. Focus ONLY on what the review says "
        "about this specific code location:\n\n"
        f"  LOCATION: {label.location}\n\n"
        "Do not assume whether this location is buggy or correct — report ONLY "
        "what the REVIEW concluded about it, in its own words.\n\n"
        "REVIEW OUTPUT:\n"
        f"{output[:6000]}\n\n"
        "Answer the structured questions about the review's treatment of this location."
    )
    return await branch.operate(instruction=instruction, response_format=JudgeVerdict)


async def score(result: RunResult, labels: tuple[Label, ...]) -> ScoredResult:
    """Score one RunResult. Aggregates across the run's outputs (the chain's
    final synthesis dominates, but any output flagging counts)."""
    cost = cost_of(result.input_tokens, result.cached_tokens, result.output_tokens, result.model)
    compute = dict(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cached_tokens=result.cached_tokens,
        est_cost_usd=cost,
        usage_source=result.usage_source,
        reasoning_disclosed=result.reasoning_disclosed,
        model=result.model,
    )
    if result.error or not result.outputs:
        return ScoredResult(
            task_id=result.task_id,
            config_key=result.config_key,
            trial=result.trial,
            found_defect=False,
            false_positive=False,
            engaged=False,
            reported_severity=None,
            severity_error=None,
            wall_seconds=result.wall_seconds,
            notes=f"run error: {result.error}",
            **compute,
        )
    model = _judge_model()
    label = labels[0]  # MVP: one label per task
    # Judge the LAST output (the synthesis / final verdict) as authoritative.
    verdict = await _judge_one(result.outputs[-1], label, model)

    reported = verdict.reported_severity.strip().lower()
    rep_ord = _SEV.get(reported, 0)
    true_ord = _SEV.get(label.true_severity, 0)

    if label.kind == "defect":
        found = verdict.flagged_as_defect
        fp = False
        sev_err = abs(rep_ord - true_ord) if found else None
    else:  # intended: flagging it medium+ is the false positive
        found = False
        fp = verdict.flagged_as_defect and rep_ord >= 2
        sev_err = rep_ord  # how far above "none" it wrongly rated it

    return ScoredResult(
        task_id=result.task_id,
        config_key=result.config_key,
        trial=result.trial,
        found_defect=found,
        false_positive=fp,
        engaged=verdict.examined,
        reported_severity=reported,
        severity_error=sev_err,
        wall_seconds=result.wall_seconds,
        notes=verdict.rationale[:200],
        **compute,
    )
