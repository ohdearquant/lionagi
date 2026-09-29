"""A handler's effect class orders its turn (ADR-0005/C6): a `write`, the default, waits for every
earlier command of the turn but the `pure` ones, a `read` for the nearest earlier `write`, a `pure`
command for its pointers only; reads overlap reads."""

import asyncio

import pytest
from pydantic import BaseModel

from lionagi import Actor, Handler, Kind, Profile, RunRequest
from tests.test_actor import scripted


class Put(BaseModel):
    name: str


class Look(BaseModel):
    name: str


class Calc(BaseModel):
    name: str


def _actor(name: str, log: list[tuple[str, str]], released: asyncio.Event) -> Actor:
    actor = Actor(name)

    async def step(req) -> str:
        log.append(("start", req.name))
        for _ in range(3):
            await asyncio.sleep(0)
        log.append(("end", req.name))
        return req.name

    @actor.handler(Put)
    async def put(req: Put, ctx) -> str:
        if req.name == "w2":
            released.set()
        return await step(req)

    @actor.handler(Look, effect="read")
    async def look(req: Look, ctx) -> str:
        return await step(req)

    @actor.handler(Calc, effect="pure")
    async def calc(req: Calc, ctx) -> str:
        log.append(("start", req.name))
        await released.wait()  # set by a later write: a write that waited for this would never start
        log.append(("end", req.name))
        return req.name

    return actor


def test_a_settled_commands_wait_row_is_released():
    log: list[tuple[str, str]] = []
    released = asyncio.Event()
    actor = _actor("waits-released", log, released)
    turns = ["".join(f'<lact {t}{i}>put(name="w{i}")</lact>' for i in range(3)) for t in ("a", "b")]
    run = asyncio.run(
        actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=3), scripted(*turns, "done"))
    )
    assert [n for at, n in log if at == "end"].count("w2") == 2  # two turns of three writes each ran
    assert run.pending == {} and run.waits == {}  # nothing reads a settled command's row


def test_each_class_waits_for_what_c6_says_and_reads_overlap():
    log: list[tuple[str, str]] = []
    turn = (
        '<lact r0>look(name="r0")</lact><lact w1>put(name="w1")</lact><lact r1>look(name="r1")</lact>'
        '<lact r2>look(name="r2")</lact><lact p1>calc(name="p1")</lact><lact w2>put(name="w2")</lact>'
        '<lact r3>look(name="r3")</lact>'
    )

    async def main():
        actor = _actor("effects-order", log, asyncio.Event())
        return await asyncio.wait_for(actor.run(Profile("p"), RunRequest(max_rounds=1), scripted(turn)), 5)

    run = asyncio.run(main())
    assert all(e.content.ok for e in run.record if e.kind is Kind.RESULT)
    at = {event: i for i, event in enumerate(log)}

    def start(a: str) -> int:
        return at[("start", a)]

    def end(a: str) -> int:
        return at[("end", a)]

    assert start("r0") == 0  # a read with no earlier write waits for nothing
    assert start("w1") > end("r0")  # a write waits for the reads before it
    assert start("p1") < end("r0")  # a pure command waits for its pointers only
    assert min(start("r1"), start("r2")) > end("w1")  # a read waits for the nearest earlier write
    assert max(start("r1"), start("r2")) < min(end("r1"), end("r2"))  # and reads overlap reads
    assert start("w2") > max(end("w1"), end("r1"), end("r2"))  # every earlier command but the pure one
    assert start("w2") < end("p1")
    assert start("r3") > end("w2")


def test_a_write_pointing_at_a_later_write_is_a_circle_and_a_pure_one_resolves():
    actor = Actor("effects-circle")

    @actor.handler(Put)
    async def put(req: Put, ctx) -> str:
        return req.name

    @actor.handler(Calc, effect="pure")
    async def calc(req: Calc, ctx) -> str:
        return req.name

    turn = (
        '<lact a>put(name=*b)</lact><lact b>put(name="b")</lact>'
        '<lact c>calc(name=*d)</lact><lact d>calc(name="d")</lact>'
    )
    run = asyncio.run(asyncio.wait_for(actor.run(Profile("p"), RunRequest(max_rounds=1), scripted(turn)), 5))
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["a"].reason == "invalid" and r["a"].error == (
        "*b waits on a: a circle of pointers never settles, and a command that writes or reads waits for "
        "earlier commands of its turn"
    )
    assert r["b"].ok and r["b"].value == "b"
    assert r["c"].ok and r["c"].value == "d"


def test_the_class_is_declared_on_the_handler_write_by_default_and_checked():
    async def fn(req, ctx):
        return None

    class Looker:
        spec = Look
        effect = "read"

        async def __call__(self, req, ctx):
            return None

    actor = Actor("effects-declared")
    assert Handler(Put, fn).effect == "write"
    assert actor.register(Looker()).effect == "read"
    assert actor.register(Handler(Calc, fn, effect="pure")).effect == "pure"
    with pytest.raises(ValueError, match="handler effect 'parallel' is not one of write, read, pure"):
        Handler(Put, fn, effect="parallel")
