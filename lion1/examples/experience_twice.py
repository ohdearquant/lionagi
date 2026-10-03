"""One ask, run twice: what the first run learned is in the second run's first view.

The profile binds an `Experience` over a `FileExperienceStore`. Run 1 reads the data, leaves a lesson
with `experience.lesson` and finishes; after the reap the runtime persists one immutable record and
the closure carries the store's receipt. The store is then opened again from its file, and run 2 of
the same ask recalls the record before its first call: the lesson is on run 2's record, on the bus,
and in the view its backend is handed. This shows the transport and nothing more: whether a lesson
makes a run better is a measurement, not something an example can show. Scripted backend; runs
offline.

    uv run python examples/experience_twice.py
"""

from __future__ import annotations

import asyncio
import atexit
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from lion1 import Actor, Context, Kind, Message, Profile, Run, RunRequest
from lion1.experience import Experience, FileExperienceStore

root = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, root, ignore_errors=True)  # the scratch goes with the process
(root / "data.txt").write_text("3\n4\n5\n")

ASK = "what is the sum of the numbers in data.txt?"
LESSON = "data.txt holds one number per line: read it with read_file and add the lines"


class ReadFile(BaseModel):
    """Read a file in the working directory."""

    path: str


def scripted(*turns: str, seen: list[list[Message]] | None = None):
    it = iter(turns)

    async def backend(messages: list[Message]) -> str:
        if seen is not None:
            seen.append(messages)
        return next(it)

    return backend


def solver(path: Path) -> Profile:
    """The profile, with a store opened from the file each time it is built."""
    return Profile("solver", privileges=frozenset({"fs"}), experience=Experience(FileExperienceStore(path)))


async def ask_twice(
    directory: Path, between: Callable[[Path], None] | None = None
) -> tuple[Actor, Run, Run, list[list[Message]]]:
    """The ask, a store opened again from its file (after `between`, when given), the ask again: both
    runs, and every view run 2's backend was handed."""
    actor = Actor("solver", privileges={"fs"})

    @actor.handler(ReadFile, requires={"fs"}, effect="read")
    async def read_file(req: ReadFile, ctx: Context) -> str:
        return (root / req.path).read_text()

    path = directory / "experience.json"
    first = await actor.run(
        solver(path),
        RunRequest(inputs=(ASK,), max_rounds=3),
        scripted(
            f'<lact r1>read_file(path="data.txt")</lact>\n<lact l1>experience.lesson(text="{LESSON}")</lact>',
            'OUT{answer: "12"}',
        ),
    )
    if between is not None:
        between(path)
    seen: list[list[Message]] = []
    second = await actor.run(
        solver(path), RunRequest(inputs=(ASK,), max_rounds=2), scripted('OUT{answer: "12"}', seen=seen)
    )
    return actor, first, second, seen


async def main() -> None:
    actor, first, second, seen = await ask_twice(root)
    print("=== run 1: read the data, leave a lesson, finish")
    print("outcome:", first.outcome)
    print("lesson:", first.record.get("l1").content.preview(100))
    persisted = json.loads(first.record.entries[-1].meta["experience"])
    print("closure, the store's receipt:", persisted["status"], persisted["id"])

    print("\n=== the store's file after both runs: one record per run, never one rewritten")
    kept = json.loads((root / "experience.json").read_text())["records"]
    print("lessons, in the order kept:", [r["lesson"] is not None for r in kept])

    print("\n=== run 2, the same ask: what its first notification, the bus and the first call carried")
    notice = next(e for e in second.record if e.kind is Kind.SYSTEM)
    print("\n".join(ln for ln in notice.content.splitlines() if ln.startswith(("experience:", "  "))))
    recalled = [e for e in actor.bus.events("memory.recalled") if e.payload["run"] == second.id]
    print("bus memory.recalled, receipt ids:", json.loads(recalled[0].payload["receipt_json"])["ids"])
    print("the lesson is in the first call's view:", any(LESSON in m.content for m in seen[0]))


if __name__ == "__main__":
    asyncio.run(main())
