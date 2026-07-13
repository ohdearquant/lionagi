# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import ClassVar, Literal

from pydantic import Field, field_validator

from lionagi.models import HashableModel


class ReActAnalysis(HashableModel):
    """Chain-of-thought output for one ReAct round: analysis, extension flag, action strategy. Round budget is framed as headroom, not a countdown (avoids early wrap-up)."""

    FIRST_EXT_PROMPT: ClassVar[str] = (
        "This is a multi-step task. You have room for up to {extensions} reason-act "
        "rounds — that is headroom to do the job well, not a target to fill or a clock "
        "to race. Plan across rounds, and within each round batch every independent "
        "tool call together (concurrent actions) so you move fast without wasting "
        "rounds. Set extension_needed=True while the task is unfinished — the normal "
        "state as you work. Set it to False ONLY once you have done the work and "
        "verified it succeeded; never for a plan or an interim summary. Stopping before "
        "the task is complete and verified is the only failure."
    )
    CONTINUE_EXT_PROMPT: ClassVar[str] = (
        "Keep going — about {extensions} rounds of headroom remain, finish "
        "properly. Batch independent tool calls in this round to stay efficient, take "
        "the next actions the task needs, and observe the results. Set "
        "extension_needed=False only once the work is genuinely done and verified."
    )
    ANSWER_PROMPT: ClassVar[str] = (
        "Given your reasoning and actions, please now provide the final answer "
        "to the user's request:\n\n{instruction}"
    )

    analysis: str = Field(
        default="",
        description=(
            "Free-form reasoning or chain-of-thought summary. Must be consistent with"
            " the plan. Commonly used for divide_and_conquer, brainstorming, reflections, "
            "regurgitation, review_checkpoints ...etc."
        ),
    )

    extension_needed: bool = Field(
        False,
        description=(
            "True while the task is still in progress — the normal state mid-task. Set "
            "False ONLY when the work is genuinely complete and verified, never for an "
            "interim or planned answer."
        ),
    )

    action_strategy: Literal["sequential", "concurrent"] = Field(
        "concurrent",
        description=(
            "Specifies how to invoke the planned actions:\n"
            "'sequential' => Each action is run in order, \n"
            "'concurrent' => All actions run in parallel, \n"
        ),
    )

    # action_requests/action_responses are added dynamically by Step.request_operative()
    # when actions=True — not defined here.


class Analysis(HashableModel):
    answer: str | None = None

    @field_validator("answer", mode="before")
    def _validate_answer(cls, value):
        if not value:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        if not isinstance(value, str):
            raise ValueError("Answer must be a non-empty string.")
        return value.strip()
