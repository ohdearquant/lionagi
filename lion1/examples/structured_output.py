"""Structured output: the run ends in a Pydantic model, held to its types, its validators and a gate.

`RunRequest(emits=(Extraction,))` names the output. The model is told the output's fields and
finishes with `OUT{extraction: {...}}`, a dict literal validated into `Extraction`, or declares the
fields one by one with `<lvar extraction.field alias>` and finishes with `OUT{extraction: [aliases]}`.
A value a field refuses, a `Literal` off its list or a dose a validator rejects, is told back as the
exact validation error and the run goes on. The request's `accept` gate judges the validated object
last, here that every quoted evidence is in the note verbatim; its refusal is told the same way and
counted against `max_refusals`. The caller reads `run.output["extraction"]` as the object, never as
text. The guidance lists a nested model by its name only, so the role prompt describes the nested
models with `render_guidance`. The note is invented; scripted backend; runs offline.

    uv run python examples/structured_output.py
"""

import asyncio
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from lionagi import Actor, Kind, Message, Profile, RunRequest, render_guidance

NOTE = (
    "58-year-old man with type 2 diabetes and hypertension, seen for follow-up. Takes metformin 500 mg "
    "by mouth twice daily and lisinopril 10 mg once daily. Allergic to penicillin (rash). Denies chest "
    "pain. Blood pressure is well controlled; the cough he reported last visit has resolved."
)

_DOSE = re.compile(r"^\d+(\.\d+)? ?(mg|g|mcg|ml|units?)$")


class Medication(BaseModel):
    """A medication the note says the patient takes."""

    name: str
    dose: str | None = Field(default=None, description='amount and unit as written, such as "500 mg"')
    route: Literal["oral", "iv", "im", "subcutaneous", "topical", "inhaled", "unknown"] = "unknown"
    frequency: str | None = Field(default=None, description='as written, such as "twice daily"')
    evidence: str = Field(description="the words of the note this was read from, verbatim")

    @field_validator("dose")
    @classmethod
    def a_dose_is_an_amount_and_a_unit(cls, dose: str | None) -> str | None:
        if dose is not None and not _DOSE.match(dose):
            raise ValueError(f"{dose!r} is not an amount and a unit (mg, g, mcg, ml, units)")
        return dose


class Condition(BaseModel):
    """A condition the note names, with the status the note gives it."""

    name: str
    status: Literal["active", "resolved", "history", "suspected"]
    evidence: str = Field(description="the words of the note this was read from, verbatim")


class Allergy(BaseModel):
    """An allergy the note states."""

    substance: str
    reaction: str | None = None
    evidence: str = Field(description="the words of the note this was read from, verbatim")


class Extraction(BaseModel):
    """What the note states about the patient: nothing inferred, each item quoting its evidence."""

    medications: list[Medication] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    allergies: list[Allergy] = Field(default_factory=list)
    negated: list[str] = Field(default_factory=list, description="findings the note says are absent")


NESTED = (Medication, Condition, Allergy)

SYSTEM = (
    "Extract what the note states. Quote the note; do not infer.\n\nThe items of the output:\n"
    + "\n".join(render_guidance(model) for model in NESTED)
)


async def evidence_is_in_the_note(run, output) -> str | None:
    """The gate on the validated object: a quote the note does not hold is a refusal the model reads."""
    extraction: Extraction = output["extraction"]
    items = [*extraction.medications, *extraction.conditions, *extraction.allergies]
    missing = [item.evidence for item in items if item.evidence not in NOTE]
    return f"evidence not in the note, quote it verbatim: {missing}" if missing else None


def scripted(*turns: str, seen: list[list[Message]]):
    it = iter(turns)

    async def backend(messages: list[Message]) -> str:
        seen.append(messages)
        return next(it)

    return backend


def told_back(run) -> list[str]:
    """What each notification said about the turn before it, when that turn did not end the run."""
    return [
        line
        for e in run.record
        if e.kind is Kind.SYSTEM
        for line in e.content.splitlines()
        if line.startswith("your last turn could not be read")
    ]


GOOD = """OUT{extraction: {
  medications: [
    {name: "metformin", dose: "500 mg", route: "oral", frequency: "twice daily",
     evidence: "metformin 500 mg by mouth twice daily"},
    {name: "lisinopril", dose: "10 mg", route: "oral", frequency: "once daily",
     evidence: "lisinopril 10 mg once daily"}
  ],
  conditions: [
    {name: "type 2 diabetes", status: "active", evidence: "type 2 diabetes"},
    {name: "hypertension", status: "active", evidence: "hypertension"},
    {name: "cough", status: "resolved", evidence: "the cough he reported last visit has resolved"}
  ],
  allergies: [{substance: "penicillin", reaction: "rash", evidence: "Allergic to penicillin (rash)"}],
  negated: ["chest pain"]
}}"""

# turn 1: a route off the Literal and a dose the validator rejects; turn 2: the types right, one quote
# paraphrased; turn 3: as the note says
TURNS = (
    GOOD.replace(
        'route: "oral", frequency: "twice daily"', 'route: "by mouth", frequency: "twice daily"'
    ).replace('dose: "10 mg"', 'dose: "10 milligrams"'),
    GOOD.replace('evidence: "Allergic to penicillin (rash)"', 'evidence: "penicillin allergy, rash"'),
    GOOD,
)

# the same output declared field by field across two turns; a field not declared keeps its default
BY_FIELD = (
    '<lvar extraction.medications meds>[{"name": "metformin", "dose": "500 mg", "route": "oral",'
    ' "frequency": "twice daily", "evidence": "metformin 500 mg by mouth twice daily"}]</lvar>\n'
    "Reading the conditions next.",
    '<lvar extraction.conditions conds>[{"name": "hypertension", "status": "active",'
    ' "evidence": "hypertension"}]</lvar>\n'
    '<lvar extraction.negated none>["chest pain"]</lvar>\n'
    "OUT{extraction: [meds, conds, none]}",
)


async def main() -> None:
    actor = Actor("extractor")
    profile = Profile("extractor", system=SYSTEM)
    request = RunRequest(inputs=(NOTE,), emits=(Extraction,), max_rounds=5, accept=evidence_is_in_the_note)

    seen: list[list[Message]] = []
    run = await actor.run(profile, request, scripted(*TURNS, seen=seen))

    print("=== what the model is told: the role prompt describes the items, the OUTPUT section the output")
    sections = seen[0][0].content.split("\n\n")
    print(next(s for s in sections if s.startswith("The items of the output:")))
    print(next(s for s in sections if s.startswith("OUTPUT\n")))

    print("\n=== turns 1 and 2 did not end the run; each notification after them opened with why")
    for line in told_back(run):
        print(" ", line[:160] + (" ..." if len(line) > 160 else ""))

    print("\n=== turn 3 ended the run in the object")
    extraction: Extraction = run.output["extraction"]
    print(
        "outcome:", type(run.outcome).__name__, "| rounds:", run.round, "| type:", type(extraction).__name__
    )
    print(extraction.model_dump_json(indent=1))

    print("\n=== the output declared field by field, across two turns, and finished by naming the aliases")
    again = await actor.run(profile, request, scripted(*BY_FIELD, seen=[]))
    extraction = again.output["extraction"]
    print("outcome:", type(again.outcome).__name__, "| rounds:", again.round)
    print(
        "medications:",
        [m.name for m in extraction.medications],
        "| conditions:",
        [c.name for c in extraction.conditions],
    )
    print(
        "negated:",
        extraction.negated,
        "| allergies, never declared, at the field's default:",
        extraction.allergies,
    )


if __name__ == "__main__":
    asyncio.run(main())
