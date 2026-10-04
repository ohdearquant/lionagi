"""Tools as handlers, profiles as subsets, privileges as the second gate, the bus as the trace.

A tool is a handler: a Spec (what the model may say) and a function (what happens). A library of
tools is a list of objects with a `.spec` and a `__call__(request, context)`, registered in one
loop. A profile names a subset of the actor's Specs and of its privileges; the prompt is generated
from that subset, so a command outside it is never presented (the first gate), and a handler's
`requires` is checked at dispatch (the second). Every step is an event on the bus, which is the
run's trace. Scripted backend; runs offline.

    uv run python examples/tools_and_gates.py
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from lion1 import Actor, Context, Kind, Profile, ProfileError, RunRequest

root = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, root, ignore_errors=True)  # the scratch goes with the process
(root / "a.txt").write_text("alpha\n")


class ReadFile(BaseModel):
    """Read a file in the working directory."""

    path: str


class WriteFile(BaseModel):
    """Write a file in the working directory."""

    path: str
    text: str = Field(description="the whole new content")


class Shell(BaseModel):
    """Run a shell command in the working directory."""

    cmd: str


class Tool:
    """One tool: a Spec, the privilege it needs, and what it does."""

    def __init__(self, spec, requires, fn):
        self.spec, self.requires, self.fn = spec, frozenset(requires), fn

    async def __call__(self, request, context: Context):
        return await self.fn(request)


async def read_file(req: ReadFile) -> str:
    return (root / req.path).read_text()


async def write_file(req: WriteFile) -> str:
    (root / req.path).write_text(req.text)
    return f"wrote {req.path}"


async def shell(req: Shell) -> str:
    proc = await asyncio.create_subprocess_shell(
        req.cmd, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return f"exit {proc.returncode}\n{out.decode()}"


TOOLS = [
    Tool(ReadFile, {"fs.read"}, read_file),
    Tool(WriteFile, {"fs.write"}, write_file),
    Tool(Shell, {"shell"}, shell),
]

READER = Profile("reader", specs=frozenset({"read_file"}), privileges=frozenset({"fs.read"}))
EDITOR = Profile("editor", privileges=frozenset({"fs.read"}))  # every Spec presented, one privilege


def scripted(*turns):
    it = iter(turns)

    async def backend(messages):
        return next(it)

    return backend


async def main() -> None:
    actor = Actor("worker", privileges={"fs.read", "fs.write", "shell"})
    for tool in TOOLS:
        actor.register(tool)

    @actor.bus.on("command.")  # a prefix pattern: command.requested and command.settled
    def trace(event):
        p = event.payload
        print(
            f"  bus {event.kind:<18} {p.get('alias')} {p.get('name')}"
            + (f" ok={p['ok']} reason={p['reason']}" if "ok" in p else "")
        )

    print("=== what the reader profile is told (its subset only)")
    print(actor.subset(READER).guidance())

    print("\n=== reader asks for write_file: not in its subset, so unknown at dispatch")
    run = await actor.run(
        READER,
        RunRequest(inputs=("go",), max_turns=2),
        scripted(
            '<lact r1>read_file(path="a.txt")</lact>\n<lact w1>write_file(path="b.txt", text="x")</lact>',
            "done",
        ),
    )
    print([e.content for e in run.record if e.kind is Kind.SYSTEM][1])

    print("\n=== editor sees every command but holds only fs.read: write_file is refused by privilege")
    run = await actor.run(
        EDITOR,
        RunRequest(inputs=("go",), max_turns=2),
        scripted(
            '<lact w1>write_file(path="b.txt", text="x")</lact>\n<lact s1>shell(cmd="ls")</lact>', "done"
        ),
    )
    print([e.content for e in run.record if e.kind is Kind.SYSTEM][1])

    print("\n=== a profile asking for what the actor lacks is refused before the first turn")
    try:
        await actor.run(Profile("root", privileges=frozenset({"sudo"})), RunRequest(), scripted("x"))
    except ProfileError as e:
        print(" ", e)


if __name__ == "__main__":
    asyncio.run(main())
