"""The system watches the thing being worked on, not the model's claims.

Three pieces, each one function. A section is called every round and its text is appended to the
notification: it reports the artifact (here a draft in a dict) and how long it has stood
unchanged. A stop rule ends the run when the artifact has stood long enough; the loop asks it right
after the section has looked, so it decides on what the model was just told. An accept gate refuses
`OUT{}` while the artifact is not acceptable, told to the model as a reason. This is the shape the
SWE bench runs with: the watcher reads `git diff`, the gate reads the tree and the model's own
acceptance checks. Scripted backend; runs offline.

    uv run python examples/watch_the_artifact.py
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from lionagi import Actor, Context, Kind, Profile, RunRequest


class Write(BaseModel):
    """Replace the draft with this text."""

    text: str


class Check(BaseModel):
    """Count the words in the draft."""


class Done(BaseModel):
    """The draft is finished."""

    title: str = Field(description="a title for it")


def scripted(*turns):
    it = iter(turns)

    async def backend(messages):
        return next(it)

    return backend


async def main() -> None:
    state = {"draft": "", "last": None, "stood": 0}
    actor = Actor("drafter")

    @actor.handler(Write)
    async def write(req: Write, ctx: Context) -> str:
        state["draft"] = req.text
        return f"draft is now {len(req.text)} characters"

    @actor.handler(Check)
    async def check(req: Check, ctx: Context) -> int:
        return len(state["draft"].split())

    @actor.section
    def draft_state(run) -> str | None:  # the artifact, as the model cannot hold it
        if not state["draft"]:
            return None
        if state["draft"] == state["last"]:
            state["stood"] += 1
        else:
            state["last"], state["stood"] = state["draft"], 0
        line = f"draft: {len(state['draft'])} characters, unchanged for {state['stood']} rounds"
        if state["stood"] >= 1:
            line += "; finish with OUT{} if it is done, or name what is missing"
        return line

    def settled() -> bool:  # asked after the section: what it just reported is what this decides on
        return state["stood"] >= 3

    async def accept(run, output) -> str | None:  # the gate on OUT{}: the artifact, not the claim
        if len(state["draft"].split()) < 5:
            return f"the draft has {len(state['draft'].split())} words; it needs at least five"
        return None

    print("=== a model that declares done too early is refused, then finishes")
    run = await actor.run(
        Profile("p"),
        RunRequest(inputs=("write a haiku",), emits=(Done,), max_rounds=6, accept=accept),
        scripted(
            '<lact w1>write(text="loop goes round")</lact>',
            'OUT{done: {title: "loop"}}',
            '<lact w2>write(text="loop goes round again and every round is one message")</lact>',
            'OUT{done: {title: "loop"}}',
        ),
    )
    for e in run.record:
        if e.kind is Kind.SYSTEM and e.round > 1:
            print(
                f"[{e.name}] "
                + " | ".join(ln for ln in e.content.splitlines()[1:-1] if not ln.startswith("context:"))
            )
    print(run.outcome, "\n")

    print("=== a model that never declares done is stopped once the draft has stood")
    state.update(draft="", last=None, stood=0)
    run = await actor.run(
        Profile("p"),
        RunRequest(inputs=("write a haiku",), emits=(Done,), max_rounds=10, accept=accept),
        scripted(
            '<lact w1>write(text="loop goes round again and every round is one message")</lact>',
            *[f"<lact c{i}>check()</lact>" for i in range(1, 8)],
        ),
        stop=settled,
    )
    for e in run.record:
        if e.kind is Kind.SYSTEM and e.round > 1:
            print(f"[{e.name}] " + e.content.splitlines()[-2])
    print(run.outcome)


if __name__ == "__main__":
    asyncio.run(main())
