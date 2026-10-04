"""Serving an actor: one offer over HTTP on loopback, and a client that reads the card, starts a run
and follows its stream to the end.

`Actor.serve(profile, backend, request=...)` serves one offer. The owner fixes the profile, the
backend and the request template; a client chooses only the inputs, bounds at or below the offer's,
and a resume. The transport binds 127.0.0.1 and answers this machine only, one request per
connection; a POST carries `application/json`. Here the client is this script, over a real socket
with the standard library: `GET /` is the card, `POST /runs` starts a run, and
`GET /runs/{id}/events` is its stream of server-sent events. A stream opens with the run's state, so
a client that arrives after the end still reads it, and this one stops at the first state that says
ended, whether that is the first frame or a later one. Scripted backend; runs offline.

    uv run python examples/serve_an_actor.py
"""

import asyncio
import contextlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from lion1 import Actor, Profile, RunRequest

NOTE = (
    "58-year-old man with type 2 diabetes, seen for follow-up. Takes metformin 500 mg by mouth twice "
    "daily and lisinopril 10 mg PO once daily."
)

ROUTES = {"by mouth": "oral", "po": "oral", "iv": "iv", "sc": "subcutaneous"}


class Medication(BaseModel):
    """A medication the note says the patient takes."""

    name: str
    dose: str = Field(description='amount and unit as written, such as "500 mg"')
    route: Literal["oral", "iv", "im", "subcutaneous", "topical", "inhaled", "unknown"]
    frequency: str


class Extraction(BaseModel):
    """The medications the note states, nothing inferred."""

    medications: list[Medication]


class Route(BaseModel):
    """The standard name of a route as a note writes it."""

    written: str


TURNS = (
    '<lact r1>route(written="by mouth")</lact>\n<lact r2>route(written="PO")</lact>',
    """OUT{extraction: {medications: [
  {name: "metformin", dose: "500 mg", route: "oral", frequency: "twice daily"},
  {name: "lisinopril", dose: "10 mg", route: "oral", frequency: "once daily"}
]}}""",
)


def scripted(*turns: str):
    it = iter(turns)

    async def backend(messages) -> str:
        return next(it)

    return backend


async def ask(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """One request on its own connection, read to the server's close: (status, the JSON answer)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = b"" if body is None else json.dumps(body).encode()
    head = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
    if body is not None:
        head += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
    writer.write(head.encode() + b"\r\n" + data)
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    status, _, rest = raw.partition(b"\r\n")
    return int(status.split()[1]), json.loads(rest.partition(b"\r\n\r\n")[2])


async def follow(port: int, sid: str) -> list[tuple[str, dict]]:
    """The run's stream, frame by frame, to the first state that says ended."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=1 << 20)
    try:
        writer.write(f"GET /runs/{sid}/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await reader.readuntil(b"\r\n\r\n")  # the head: 200, text/event-stream
        frames = []
        while True:
            block = (await reader.readuntil(b"\n\n")).decode()
            if block.startswith(":"):  # a comment line, sent every 15 seconds, keeps a quiet stream open
                continue
            event, data = block.split("\n")[:2]
            frames.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
            if frames[-1][0] == "state" and frames[-1][1]["phase"] == "ended":
                return frames
    finally:
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    actor = Actor("extractor")

    @actor.handler(Route, effect="pure")
    async def route(req: Route, ctx) -> str:
        return ROUTES.get(req.written.lower(), "unknown")

    profile = Profile(
        "extractor",
        specs=frozenset({"route"}),  # the one command this offer grants; the card lists no other
        system="Extract the medications the note states. Quote; do not infer.",
    )
    request = RunRequest(emits=(Extraction,), max_turns=4)
    bound = asyncio.get_running_loop().create_future()
    served = asyncio.create_task(
        actor.serve(
            profile, scripted(*TURNS), request=request, port=0, ready=lambda h, p: bound.set_result(p)
        )
    )
    await asyncio.wait({bound, served}, return_when=asyncio.FIRST_COMPLETED)
    if served.done():  # it could not bind: the error says why
        served.result()
    port = bound.result()
    try:
        print("=== GET /: the card says what a client may start and what the model is told it may do")
        status, card = await ask(port, "GET", "/")
        (offer,) = card["offers"]
        print(status, "| actor:", card["actor"], "| protocol:", card["protocol"], "| offer:", offer["name"])
        for command in offer["commands"]:
            print("command:", command["name"], "|", command["effect"], "|", command["doc"])
        print("outputs:", [o["name"] for o in offer["outputs"]], "| converse:", offer["converse"])
        print("bounds:", ", ".join(f"{k} {v}" for k, v in offer["bounds"].items() if v is not None))

        print("\n=== POST /runs for what the owner never offered: refused, and nothing starts")
        print(*await ask(port, "POST", "/runs", {"offer": "writer"}))
        print(*await ask(port, "POST", "/runs", {"offer": "extractor", "bounds": {"max_turns": 9}}))

        print("\n=== POST /runs: the run starts with the note as its input")
        status, state = await ask(port, "POST", "/runs", {"offer": "extractor", "inputs": [NOTE]})
        print(status, "| offer:", state["offer"], "| resumed:", state["resumed"])

        print("\n=== GET /runs/{id}/events: read to the first state that says ended")
        frames = await follow(port, state["id"])
        final = frames[-1][1]
        print("the stream opened with:", frames[0][0])
        print("phase:", final["phase"], "| outcome:", final["outcome"]["kind"], "| turn:", final["turn"])
        print("output:", json.dumps(final["output"], indent=1))

        print("\n=== GET /runs/{id}/record: the rows the log holds, by kind")
        status, record = await ask(port, "GET", f"/runs/{state['id']}/record")
        print(status, "| more:", record["more"], "|", [row["kind"] for row in record["rows"]])
    finally:
        served.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await served


if __name__ == "__main__":
    asyncio.run(main())
