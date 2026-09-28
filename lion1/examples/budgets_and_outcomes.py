"""Every way a run can end, constructed once, and the bounds a request carries.

A `RunRequest` carries the bounds: `max_rounds`, `time_budget` in seconds, `max_refusals` of the
accept gate in a row, and `view_budget`, the estimated tokens the view may hold before the runtime
folds the oldest results to placeholders. The run ends as a value the caller reads, never a string: `Success`,
`Exhausted(rounds)`, `Exhausted(rounds, "time")`, `Refused(why, times)`, `Stopped(round)`. A turn
that could not be read is a `Retry` inside the run, told to the model and counted when it repeats,
never an end. Scripted backends; runs offline.

    uv run python examples/budgets_and_outcomes.py
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lionagi import Actor, Context, Kind, Profile, RunRequest, fold


class Echo(BaseModel):
    """Return the text."""

    text: str


class Answer(BaseModel):
    text: str


def scripted(*turns):
    it = iter(turns)

    async def backend(messages):
        return next(it)

    return backend


def make() -> Actor:
    actor = Actor("a")

    @actor.handler(Echo)
    async def echo(req: Echo, ctx: Context) -> str:
        return req.text

    return actor


def notes(run):
    return [e.content for e in run.record if e.kind is Kind.SYSTEM]


async def main() -> None:
    p = Profile("p")

    def say(i):
        return f'<lact e{i}>echo(text="{i}")</lact>'

    run = await make().run(
        p, RunRequest(inputs=("go",), emits=(Answer,), max_rounds=5), scripted(say(1), 'OUT{answer: "done"}')
    )
    print("Success        ", run.outcome, "| output:", run.output["answer"].text)

    run = await make().run(p, RunRequest(inputs=("go",), max_rounds=2), scripted(say(1), say(2), say(3)))
    print("Exhausted      ", run.outcome, "| the round count is the request's max_rounds")

    turns = iter([say(1), say(2), say(3)])

    async def slow(messages):
        await asyncio.sleep(0.4)
        return next(turns)

    run = await make().run(p, RunRequest(inputs=("go",), max_rounds=5, time_budget=0.7), slow)
    print("Exhausted/time ", run.outcome, "| read at the top of each round; the header carries the clock")

    async def accept(run, output):
        return None if output["answer"].text == "ok" else "the answer must be 'ok'"

    run = await make().run(
        p,
        RunRequest(inputs=("go",), emits=(Answer,), max_rounds=5, accept=accept, max_refusals=2),
        scripted('OUT{answer: "no"}', 'OUT{answer: "nope"}'),
    )
    print(
        "Refused        ",
        run.outcome,
        "| two refusals in a row; a working turn between them starts the count over",
    )

    asked: list[int] = []
    run = await make().run(
        p,
        RunRequest(inputs=("go",), max_rounds=5),
        scripted(say(1), say(2)),
        stop=lambda: asked.append(1) or len(asked) >= 2,
    )
    print("Stopped        ", run.outcome, "| asked after each notification; names the round that did not run")

    run = await make().run(
        p,
        RunRequest(inputs=("go",), max_rounds=3),
        scripted("<lact e1>echo(1)</lact>", "<lact e1>echo(1)</lact>", "done"),
    )
    told = [ln for ln in notes(run)[2].splitlines() if "could not be read" in ln][0]
    print("Retry (inside) ", run.outcome, "| the run went on; the third notification said:", told[:70], "...")

    run = await make().run(
        p,
        RunRequest(inputs=("go",), max_rounds=4, view_budget=60),
        scripted(*(f'<lact e{i}>echo(text="{"x" * 200}")</lact>' for i in range(1, 4)), "done"),
    )
    view = fold(run.record)
    events = sum("folded:" in e.content for e in run.record if e.kind is Kind.SYSTEM)
    print(
        "view_budget    ",
        [v.entry.name for v in view if not v.hidden],
        "| placeholders:",
        [v.entry.name for v in view if v.hidden],
        f"| {events} fold events; e1 still resolves:",
        len(run.resolve("e1")),
    )


if __name__ == "__main__":
    asyncio.run(main())
