"""Notes that outlive a run, and a view the model curates without touching the record.

A name with the `note.` prefix is written to the profile's note store as well as declared on the
run; the store carries which run and sequence wrote each note (here the memory store; a served
actor's is the graph's, ADR-0025). A second run of the same profile lists and points at them.
`context.hide` and `context.summarize` change what the model sees, never what the record holds:
hidden entries keep their names and resolve, and `context.show` brings them back. The model carries
its big picture in notes and its details behind pointers. Scripted backend; runs offline.

    uv run python examples/notes_and_context.py
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel

from lion1 import Actor, Context, Kind, Profile, RunRequest, fold

root = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, root, ignore_errors=True)  # the scratch goes with the process
(root / "spec.md").write_text("# spec\n\nthe port is 8080 and the workers are 4\n" * 20)


class ReadFile(BaseModel):
    """Read a file in the working directory."""

    path: str


class Echo(BaseModel):
    """Return the text you point at."""

    text: str


def scripted(*turns):
    it = iter(turns)

    async def backend(messages):
        return next(it)

    return backend


def names_in_view(run):
    return [v.entry.name for v in fold(run.record) if not v.hidden]


async def main() -> None:
    actor = Actor("clerk", privileges={"fs"})
    clerk = Profile("clerk", privileges=frozenset({"fs"}))

    @actor.handler(ReadFile, requires={"fs"})
    async def read_file(req: ReadFile, ctx: Context) -> str:
        return (root / req.path).read_text()

    @actor.handler(Echo)
    async def echo(req: Echo, ctx: Context) -> str:
        return req.text

    print("=== run 1: read, take a note, hide the big result, summarize a span")
    first = await actor.run(
        clerk,
        RunRequest(inputs=("what port does the spec name?",), max_turns=4),
        scripted(
            '<lact f1>read_file(path="spec.md")</lact>',
            "<lvar note.port>the spec names port 8080; workers 4</lvar>\n"
            '<lact h1>context.hide(refs=["f1"])</lact>',
            '<lact s1>context.summarize(refs=["_m2", "_r3", "_m5"], text="read spec.md; port noted")</lact>',
            "done",
        ),
    )
    print("in view at the end:", names_in_view(first))
    print("f1 still resolves:", len(first.resolve("f1")), "characters")
    print(
        "notification 3 told the model:",
        [ln for ln in first.record.by_seq(7).content.splitlines() if "notes" in ln][0],
    )

    print("\n=== run 2, same profile: the note is there, listed and pointed at")
    second = await actor.run(
        clerk,
        RunRequest(inputs=("what did you learn last time?",), max_turns=3),
        scripted(
            "<lact l1>note.list()</lact>",
            "<lact e1>echo(text=*note.port)</lact>",
            "done",
        ),
    )
    for e in second.record:
        if e.kind is Kind.RESULT:
            print(f"  [{e.name} {e.alias}] {e.content.preview(120)}")
    note = actor.notes_for(clerk).get("port")
    print(
        f"\nstore: {note.key} = {note.value!r}, written by run {note.run} at seq {note.seq}, v{note.version}"
    )
    print("store:", [n.key for n in actor.notes_for(clerk).list()])


if __name__ == "__main__":
    asyncio.run(main())
