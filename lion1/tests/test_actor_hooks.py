"""Two hook points on the actor. `before` gates a command after validation and privilege: None lets
it run, a str refuses it (the model reads `refused: ...`), a request replaces its arguments, and a
hook that raises refuses it. `after` sees every command that ran, ok or not: None keeps the result, a
Result replaces it, and a hook that raises is told in the next notice while the result stands."""

import asyncio

from pydantic import BaseModel

from lionagi import Actor, Kind, Profile, Result, RunRequest
from lionagi.actor import DEFAULT_HANDLERS
from tests.test_actor import scripted


class Echo(BaseModel):
    text: str


class Boom(BaseModel):
    why: str


def make() -> tuple[Actor, Profile]:
    actor = Actor("hooked")

    @actor.handler(Echo)
    async def echo(req: Echo, ctx) -> str:
        return req.text

    @actor.handler(Boom)
    async def boom(req: Boom, ctx) -> str:
        raise RuntimeError(req.why)

    specs = {"echo", "boom"} | {h.name for h in DEFAULT_HANDLERS}
    return actor, Profile(name="hooked", specs=frozenset(specs), system="x")


def run(actor: Actor, profile: Profile, *turns: str):
    request = RunRequest(inputs=("go",), max_rounds=len(turns) + 1)
    return asyncio.run(actor.run(profile, request, scripted(*turns, "still here")))


def results(run):
    return {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}


def notices(run):
    return "\n".join(str(e.content) for e in run.record if e.kind is Kind.SYSTEM)


def test_a_before_hook_refuses_replaces_or_lets_a_command_run():
    actor, profile = make()

    @actor.before
    def gate(run, la, req):
        if isinstance(req, Echo) and "secret" in req.text:
            return "secrets stay closed"
        if isinstance(req, Echo) and req.text == "rewrite me":
            return Echo(text="rewritten")
        return None

    r = results(
        run(
            actor, profile, '<lact a1>echo(text="secret x")</lact>', '<lact a2>echo(text="rewrite me")</lact>'
        )
    )
    assert (
        r["a1"].ok is False
        and r["a1"].reason == "refused"
        and r["a1"].error == "refused: secrets stay closed"
    )
    assert r["a2"].ok and r["a2"].value == "rewritten"


def test_a_before_hook_that_raises_refuses_the_command_and_the_model_reads_why():
    actor, profile = make()

    @actor.before
    def broken(run, la, req):
        raise RuntimeError("gate down")

    out = run(actor, profile, '<lact a1>echo(text="hi")</lact>')
    r = results(out)["a1"]
    assert (
        r.ok is False
        and r.reason == "refused"
        and r.error == "refused: broken failed, RuntimeError: gate down"
    )
    assert "not executed:\n  a1: refused: broken failed, RuntimeError: gate down" in notices(out)


def test_an_after_hook_sees_every_result_and_may_replace_it_and_its_own_failure_is_told():
    actor, profile = make()
    seen: list[tuple[str, bool]] = []

    @actor.after
    def observe(run, la, req, result):
        seen.append((la.alias, result.ok))
        if result.ok and isinstance(req, Echo):
            return Result(result.spec, True, value=result.value.upper())
        return None

    @actor.after
    def flaky(run, la, req, result):
        if la.alias == "a2":
            raise RuntimeError("log full")

    out = run(actor, profile, '<lact a1>echo(text="hi")</lact><lact a2>boom(why="bang")</lact>')
    r = results(out)
    assert r["a1"].ok and r["a1"].value == "HI"  # replaced by the first after hook
    assert r["a2"].ok is False and r["a2"].reason == "failed"  # the handler's own failure, seen too
    assert sorted(seen) == [("a1", True), ("a2", False)]
    assert (
        "hooks failed (the commands ran; their results stand):\n  a2: flaky: RuntimeError: log full"
        in notices(out)
    )
