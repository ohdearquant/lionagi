"""Two hook points on the actor. `before` gates a command after validation and privilege: None lets
it run, a str refuses it (the model reads `refused: ...`), a request replaces its arguments, and a
hook that raises refuses it. `after` sees every command that ran, ok or not: None keeps the result, a
Result replaces it, and a hook that raises is told in the next notice while the result stands."""

import asyncio

from pydantic import BaseModel, ConfigDict, computed_field, field_validator

from lionagi import Actor, Kind, Message, Profile, Result, RunRequest
from lionagi.actor import DEFAULT_HANDLERS
from tests.test_actor import scripted


class Echo(BaseModel):
    text: str


class Boom(BaseModel):
    why: str


class Padded(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def trimmed(cls, v: str) -> str:
        return v.strip()


class Gather(BaseModel):
    items: list[str]


class Counted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int

    @computed_field
    @property
    def label(self) -> str:
        return f"n={self.n}"


class Pair(BaseModel):
    items: tuple[list[str], str]


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


def test_a_before_hooks_replacement_is_a_dict_or_the_specs_instance_rebuilt_through_its_validator():
    actor = Actor("hooked-rebuild")

    @actor.handler(Padded)
    async def padded(req: Padded, ctx) -> str:
        return req.text

    @actor.before
    def swap(run, la, req):
        if la.alias == "a1":
            return {"text": "  from a dict  "}
        return Padded.model_construct(text="  never validated  ")  # a class is no receipt

    r = results(run(actor, Profile("p"), '<lact a1>padded(text="x")</lact><lact a2>padded(text="y")</lact>'))
    assert r["a1"].ok and r["a1"].value == "from a dict"
    assert r["a2"].ok and r["a2"].value == "never validated"


def test_a_replacement_instance_is_rebuilt_from_its_declared_fields_not_its_computed_ones():
    actor = Actor("hooked-computed")

    @actor.handler(Counted)
    async def counted(req: Counted, ctx) -> str:
        return req.label

    @actor.before
    def bump(run, la, req):
        return Counted(n=req.n + 1)

    r = results(run(actor, Profile("p"), "<lact c1>counted(n=1)</lact>"))["c1"]
    assert r.ok and r.value == "n=2"


def test_a_replacement_of_another_class_or_one_its_spec_refuses_is_the_hooks_refusal():
    actor, profile = make()

    @actor.before
    def swap(run, la, req):
        if la.alias == "a1":
            return Boom(why="another class")
        if la.alias == "a2":
            return {"text": 5}
        if la.alias == "a3":
            req.text = "changed in place"  # its own copy: nothing reaches the handler
            return None
        return 5  # neither a verdict nor a replacement: the command runs as asked

    out = run(
        actor,
        profile,
        '<lact a1>echo(text="one")</lact><lact a2>echo(text="two")</lact>'
        '<lact a3>echo(text="three")</lact><lact a4>echo(text="four")</lact>',
    )
    r = results(out)
    assert (
        r["a1"].reason == "refused"
        and r["a1"].error == "refused: swap returned Boom, neither a dict nor Echo"
    )
    assert r["a2"].reason == "refused"
    assert r["a2"].error.startswith("refused: swap failed, ValidationError: 1 validation error for Echo")
    assert [out.record.get(a).meta["execution"] for a in ("a1", "a2")] == ["not executed"] * 2
    assert r["a3"].ok and r["a3"].value == "three"
    assert r["a4"].ok and r["a4"].value == "four"


def test_the_request_entered_and_the_return_before_the_after_hooks_ride_the_entry_as_copies():
    actor = Actor("hooked-captures")
    kept: list[list[str]] = []
    seen: list[tuple[str, list[str], list[str]]] = []

    @actor.handler(Gather)
    async def gather(req: Gather, ctx) -> list[str]:
        kept.append([*req.items, "found"])
        req.items.append("handler touched its request")
        return kept[-1]

    @actor.after
    def scribble(run, la, req, result):
        seen.append(("scribble", list(req.items), list(result.value)))
        result.value.append("scribbled")
        req.items.append("scribbled")

    @actor.after
    def replace(run, la, req, result):
        seen.append(("replace", list(req.items), list(result.value)))
        return Result(result.spec, True, value=[*result.value, "replaced"])

    @actor.after
    def broken(run, la, req, result):
        result.value.append("dropped")
        raise RuntimeError("log full")

    views: list[list[Message]] = []
    inner = scripted('<lact g1>gather(items=["a"])</lact>', "done")

    async def backend(messages):
        views.append(messages)
        return await inner(messages)

    out = asyncio.run(actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=2), backend))
    kept[0].append("the handler kept its return")
    entry = out.record.get("g1")
    assert seen == [("scribble", ["a"], ["a", "found"]), ("replace", ["a"], ["a", "found"])]
    assert entry.content.value == ["a", "found", "replaced"] == out.resolve("g1")
    assert entry.meta["request"] == {"items": ["a"]}
    assert entry.meta["returned"] == {
        "spec": "gather",
        "ok": True,
        "value": ["a", "found"],
        "error": None,
        "reason": None,
    }
    assert (
        "hooks failed (the commands ran; their results stand):\n  g1: broken: RuntimeError: log full"
        in notices(out)
    )
    (shown,) = [m for m in views[1] if m.name == entry.name]
    assert shown.content == f'[{entry.name} g1 gather] ["a", "found", "replaced"]'
    assert not any("touched" in m.content or "scribbled" in m.content for m in views[1])


def test_a_capture_copies_a_list_that_holds_itself_once():
    actor = Actor("hooked-ring")
    made: list[list] = []

    @actor.handler(Gather)
    async def ring(req: Gather, ctx) -> list:
        made.append([*req.items])
        made[-1].append(made[-1])
        return made[-1]

    returned = results(run(actor, Profile("p"), '<lact g1>gather(items=["a"])</lact>'))["g1"].value
    assert returned is not made[0] and returned[0] == "a" and returned[1] is returned


def test_a_capture_copies_what_a_tuple_holds():
    actor = Actor("hooked-tuple")
    made: list[tuple] = []

    @actor.handler(Pair)
    async def pair(req: Pair, ctx) -> tuple:
        made.append((["kept"], req.items[1]))
        return made[-1]

    @actor.after
    def scribble(run, la, req, result):
        result.value[0].append("scribbled")
        req.items[0].append("scribbled")

    entry = next(
        e
        for e in run(actor, Profile("p"), '<lact p1>pair(items=[["x"], "y"])</lact>').record
        if e.alias == "p1"
    )
    assert made[0][0] == ["kept"] and entry.meta["returned"]["value"] == (["kept"], "y")
    assert entry.meta["request"]["items"] == (["x"], "y") and entry.content.value == (["kept"], "y")
