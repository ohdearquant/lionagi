"""Serving an actor: one offer over HTTP on loopback, and a client that reads the card, starts a run
and follows its stream to the end.

`Lion` serves an actor's offers: `lion.offer(profile, backend, request)`, then `lion.serve(actor)`.
The owner fixes the profile, the backend and the request template; a client chooses only the inputs,
bounds at or below the offer's, and a resume. The transport binds 127.0.0.1 and answers this machine
only; a POST carries `application/json`. Here the client is this script, over a real socket
with the standard library: `GET /` is the card, `POST /runs` starts a run, and
`GET /runs/{id}/events` is its stream of server-sent events. A stream opens with the run's state, so
a client that arrives after the end still reads it, and this one stops at the first state that says
ended, whether that is the first frame or a later one. Scripted backend; runs offline.

    uv run --extra serve python examples/serve_an_actor.py
"""

import asyncio
import atexit
import contextlib
import http.client
import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from hub.serve import Lion
from lion1 import Actor, Profile, RunRequest, Store
from lion1.khive import Khive

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
    """One request with the standard library's client, run off the loop: (status, the JSON answer)."""

    def call() -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            if body is None:
                conn.request(method, path)
            else:
                conn.request(method, path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            answer = conn.getresponse()
            return answer.status, json.loads(answer.read())
        finally:
            conn.close()

    return await asyncio.to_thread(call)


async def follow(port: int, sid: str) -> list[tuple[str, dict]]:
    """The run's stream, frame by frame, to the first state that says ended."""

    def read() -> list[tuple[str, dict]]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.request("GET", f"/runs/{sid}/events")
            stream = conn.getresponse()  # 200, text/event-stream
            frames, event = [], None
            while True:
                line = stream.readline().decode().rstrip("\n")
                # a line starting ':' is a comment, sent every 15 seconds to keep a quiet stream open
                if line.startswith("event: "):
                    event = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    frames.append((event, json.loads(line.removeprefix("data: "))))
                    if event == "state" and frames[-1][1]["phase"] == "ended":
                        return frames
        finally:
            conn.close()

    return await asyncio.to_thread(read)


def a_store() -> Store:
    """The store the run's rows go to: the khive binary, as the actor of a scratch directory."""
    scratch = Path(tempfile.mkdtemp())
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)  # the scratch goes with the process
    (scratch / ".khive").mkdir()
    (scratch / ".khive" / "config.toml").write_text(
        # the actor, and the lion pack's tables routed to their own file beside a main store (the
        # store refuses a config that would put them in the default one under the home directory)
        '[actor]\nid = "example"\n'
        f'\n[[backends]]\nname = "main"\npath = "{scratch / ".khive" / "main.db"}"\n'
        f'\n[[backends]]\nname = "lion"\npath = "{scratch / ".khive" / "lion.db"}"\n'
        '\n[packs.lion]\nbackend = "lion"\n'
    )
    return Store(Khive(scratch, "example"))


async def main() -> None:
    actor = Actor("extractor", store=a_store())

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
    lion = Lion(port=0).offer(profile, scripted(*TURNS), request)
    served = asyncio.create_task(lion.serve(actor, ready=lambda h, p: bound.set_result(p)))
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
