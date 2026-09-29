"""The paths of the actor a run takes less often: notes kept per profile, a handler's Context, a handler
registered as a plain callable, profiles refused before a run starts, backends and gates that do not
await, a conversation whose commands all landed, OUT{} forms that do not assemble, delegation whose ask
has ended or was cancelled, the fold stopping at half its budget, and a command task that leaves by a
door the loop does not watch."""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from lionagi import (
    Actor,
    Exhausted,
    Handle,
    Inbound,
    Kind,
    MemoryNoteStore,
    Profile,
    ProfileError,
    Record,
    Result,
    RunRequest,
    Stopped,
    Success,
)
from tests.test_actor import Answer, Ask, Done, Echo, Tally, _big_actor, _echo_actor, scripted


class Boom(BaseModel):
    why: str


class Peek(BaseModel):
    name: str


class Stamp(BaseModel):
    text: str


class Join(BaseModel):
    parts: list[str]


class MakeTally(BaseModel):
    n: int = 3


class Loose(BaseModel):
    value: Any


class Halter(BaseModel):
    pass


class Pairs(BaseModel):
    pass


class Find(BaseModel):
    text: str


class Cut(BaseModel):
    target: str


class Halt(BaseException):
    pass


def results(run) -> dict:
    return {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}


def notices(run) -> list[str]:
    return [e.content for e in run.record if e.kind is Kind.SYSTEM]


def once(actor: Actor, profile: Profile, *turns: str, **request):
    request.setdefault("inputs", ("go",))
    request.setdefault("max_rounds", len(turns))
    return asyncio.run(actor.run(profile, RunRequest(**request), scripted(*turns)))


def test_a_note_is_read_back_by_pointer_in_a_later_run_of_the_same_profile_and_no_other():
    actor = _echo_actor("edge-notes-memory")  # no notes_dir: the actor keeps a store per profile name
    once(actor, Profile("writer"), "<lvar note.plan>ship it</lvar>")
    later = once(actor, Profile("writer"), "<lact a1>echo(text=*note.plan)</lact>")
    assert later.resolve("a1") == "ship it"
    assert later.known("note.plan") is True and later.known("note.other") is False
    other = once(actor, Profile("reader"), "<lact a1>echo(text=*note.plan)</lact>")
    assert results(other)["a1"] == Result(
        "echo", False, error="pointer *note.plan names nothing", reason="invalid"
    )


def test_a_profile_that_brings_its_own_note_store_writes_its_notes_there():
    actor = Actor("edge-notes-own")
    store = MemoryNoteStore()
    once(actor, Profile("own", notes=store), "<lvar note.plan>mine</lvar>")
    assert store.get("plan").value == "mine"
    assert actor.notes_for(Profile("own")).get("plan") is None  # the actor's own store for the name


def test_a_handler_reads_a_value_and_emits_an_event_through_its_context():
    actor = Actor("edge-context")

    @actor.handler(Peek)
    async def peek(req: Peek, ctx):
        value = ctx.get(req.name)
        ctx.emit("peek.read", name=req.name)
        return value

    run = once(actor, Profile("p"), '<lvar v>seen</lvar><lact p1>peek(name="v")</lact>')
    assert run.resolve("p1") == "seen"
    (event,) = [e for e in actor.bus.log if e.kind == "peek.read"]
    assert event.payload == {"run": run.id, "alias": "p1", "name": "v"}


def test_register_takes_a_callable_carrying_its_spec_and_requires_and_refuses_anything_else():
    class Stamper:
        spec = Stamp
        requires = ("write",)

        def __call__(self, req: Stamp, ctx) -> str:
            return f"stamped {req.text}"

    class Inert:
        spec = Stamp

    class Loud:
        spec = dict

        def __call__(self, req, ctx) -> str:
            return "never"

    actor = Actor("edge-register", privileges=("write",))
    handler = actor.register(Stamper())
    assert handler.name == "stamp" and handler.requires == frozenset({"write"})
    turn = '<lact s1>stamp(text="x")</lact>'
    writer = once(actor, Profile("w", privileges=frozenset({"write"})), turn)
    assert results(writer)["s1"] == Result("stamp", True, value="stamped x")
    reader = once(actor, Profile("r"), turn)
    assert results(reader)["s1"] == Result(
        "stamp", False, error="stamp requires ['write']", reason="privilege"
    )
    for bad in (object(), Inert(), Loud()):
        with pytest.raises(TypeError, match=r"register\(\) takes a Handler or an object with a \.spec"):
            actor.register(bad)


def test_a_profile_asking_beyond_its_actor_is_refused_before_the_run_starts():
    actor = _echo_actor("edge-check")
    called: list[int] = []

    def backend(messages) -> str:
        called.append(1)
        return "never"

    with pytest.raises(ProfileError, match=r"^p names specs this actor has no handler for: \['nope'\]$"):
        asyncio.run(actor.run(Profile("p", specs=frozenset({"echo", "nope"})), RunRequest(), backend))
    with pytest.raises(ProfileError, match=r"^p asks privileges beyond this actor: \['root'\]$"):
        asyncio.run(actor.run(Profile("p", privileges=frozenset({"root"})), RunRequest(), backend))
    assert called == [] and actor.bus.log == []


def test_a_backend_that_returns_its_text_directly_is_not_awaited():
    def backend(messages) -> str:
        return 'OUT{answer: "sync"}'

    run = asyncio.run(Actor("edge-sync").run(Profile("p"), RunRequest(inputs=("go",), max_rounds=1), backend))
    assert run.outcome == Success({"answer": "sync"})


def test_a_backend_that_hands_back_something_other_than_text_aborts_the_run():
    # ADR-0009/C1 has the backend hand back text; anything else is an abort with the outcome unset
    actor = Actor("edge-not-text")
    live = []

    def backend(messages):
        live.extend(actor.runs.values())
        return None

    with pytest.raises(TypeError):
        asyncio.run(actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=2), backend))
    (run,) = live
    assert run.outcome is None and actor.runs == {}
    assert run.record.latest(Kind.TEXT).content is None
    assert "run.completed" not in [e.kind for e in actor.bus.log]


def test_with_reply_on_commands_a_turn_whose_commands_all_landed_is_the_reply_and_a_failed_one_is_not():
    actor = _echo_actor("edge-reply-on-commands")

    @actor.handler(Boom)
    async def boom(req: Boom, ctx) -> str:
        raise RuntimeError(req.why)

    asked: list[int] = []
    mail = iter(["second", "third"])

    async def wait(run) -> bool:
        asked.append(run.round)
        text = next(mail, None)
        if text is None:
            return False
        run.record.append(Kind.INPUT, text)
        return True

    run = once(
        actor,
        Profile("p"),
        '<lact a1>echo(text="one")</lact>',
        '<lact b1>boom(why="disk full")</lact>',  # failed: the model gets one more turn to read it
        "the disk is full",
        "bye",
        inputs=("first",),
        max_rounds=5,
        wait=wait,
        reply_on_commands=True,
    )
    assert asked == [2, 4, 5]
    assert run.outcome == Stopped(5) and run.round == 4


def test_an_awaitable_accept_gate_and_an_awaitable_after_hook_are_awaited():
    actor = _echo_actor("edge-awaited")
    judged: list[str] = []

    async def accept(run, output) -> str | None:
        judged.append(output["done"].summary)
        return "not yet" if len(judged) == 1 else None

    @actor.after
    async def shout(run, la, req, result):
        return Result(result.spec, True, value=result.value.upper()) if result.ok else None

    run = once(
        actor,
        Profile("p"),
        '<lact a1>echo(text="hi")</lact>OUT{done: {summary: "first", files: []}}',
        'OUT{done: {summary: "second", files: []}}',
        emits=(Done,),
        accept=accept,
    )
    assert results(run)["a1"] == Result("echo", True, value="HI")
    assert judged == ["first", "second"] and run.output["done"].summary == "second"
    assert "OUT{} refused: not yet" in notices(run)[1]


@pytest.mark.parametrize(
    "reasons, told",
    [
        (["not yet", "not yet"], "(the same error 2 rounds running): OUT{} refused: not yet"),
        (["not yet", "still not"], "could not be read: OUT{} refused: still not"),
    ],
)
def test_the_same_refusal_on_consecutive_turns_is_told_with_its_count(reasons, told):
    judged = iter([*reasons, None])
    finish = 'OUT{done: {summary: "s", files: []}}'
    run = once(
        _echo_actor("edge-refused-twice"),
        Profile("p"),
        finish,
        finish,
        finish,
        emits=(Done,),
        accept=lambda run, output: next(judged),
    )
    assert "rounds running" not in notices(run)[1] and "OUT{} refused: not yet" in notices(run)[1]
    assert told in notices(run)[2]
    assert isinstance(run.outcome, Success) and run.retry_error is None and run.same_errors == 0


def test_an_alias_the_runtime_would_generate_may_recur_in_a_later_round():
    actor = _echo_actor("edge-auto-alias")
    run = once(
        actor, Profile("p"), '<lact auto_1>echo(text="a")</lact>', '<lact auto_1>echo(text="b")</lact>', "x"
    )
    assert [(alias, r.value) for alias, r in results(run).items()] == [("auto_1", "a"), ("auto_1_6", "b")]
    assert "not executed" not in notices(run)[2]
    chosen = ('<lact a1>echo(text="a")</lact>', '<lact a1>echo(text="b")</lact>', "x")
    user = once(_echo_actor("edge-own-alias"), Profile("p"), *chosen)
    assert list(results(user)) == ["a1"]  # an alias the model chose is held to uniqueness


def test_a_generated_alias_a_value_holds_runs_under_a_free_suffix():
    actor = _echo_actor("edge-auto-value")
    run = once(
        actor,
        Profile("p"),
        "<lvar auto_1>kept</lvar>",
        '<lvar auto_1_5>also kept</lvar><lact auto_1>echo(text="b")</lact>',
        "x",
    )
    assert [(alias, r.value) for alias, r in results(run).items()] == [("auto_1_6", "b")]
    assert run.values["auto_1"][1] == "kept" and run.values["auto_1_5"][1] == "also kept"
    assert "already names" not in notices(run)[2]
    chosen = ("<lvar a1>kept</lvar>", '<lact a1>echo(text="b")</lact>', "x")
    user = once(_echo_actor("edge-own-value"), Profile("p"), *chosen)
    assert results(user) == {} and "alias 'a1' already names a value" in notices(user)[2]


def test_an_unknown_command_and_a_pointer_to_nothing_settle_without_their_handler():
    actor = _echo_actor("edge-refusals")
    run = once(
        actor,
        Profile("p", specs=frozenset({"echo"})),
        "<lact a1>nope(x=1)</lact><lact a2>note.list()</lact><lact a3>echo(text=*ghost)</lact>",
        "x",
    )
    r = results(run)
    assert r["a1"] == Result("nope", False, error="unknown command 'nope'", reason="unknown")
    # the actor has note.list; the profile's subset does not, so the model has no such command
    assert r["a2"] == Result("note.list", False, error="unknown command 'note.list'", reason="unknown")
    assert r["a3"] == Result("echo", False, error="pointer *ghost names nothing", reason="invalid")


def test_a_pointer_inside_a_tuple_argument_is_resolved():
    actor = Actor("edge-tuple")

    @actor.handler(Join)
    def join(req: Join, ctx) -> str:
        return "-".join(req.parts)

    run = once(actor, Profile("p"), '<lvar a>A</lvar><lact j>join(parts=(*a, "x"))</lact>')
    assert run.resolve("j") == "A-x"


def test_an_out_that_does_not_assemble_is_a_retry_that_names_what_is_wrong():
    turns_and_errors = [
        ('OUT{done: "just text"}', "OUT{done: ...}: done has several fields; give a dict literal or aliases"),
        ('OUT{done: {summary: "s"}}', "OUT{done: ...}: files: Field required"),
        ("OUT{done: [ghost]}", "OUT{done: ...}: 'ghost' was never declared"),
        (
            "<lvar a>x</lvar><lvar b>y</lvar><lvar c>z</lvar>OUT{done: [a, b, c]}",
            "OUT{done: ...}: give one alias holding the whole value, or one per field as "
            "<lvar done.field alias>",
        ),
        (
            "<lvar done.summary s>only the summary</lvar>OUT{done: [s]}",
            "OUT{done: ...}: files: Field required",
        ),
        (
            "<lvar w>plain words</lvar>OUT{done: [w]}",
            "done has fields ['summary', 'files']: give one <lvar done.<field> alias> per field and "
            "OUT{done: [the aliases]}, or one alias holding a JSON object",
        ),
    ]
    finish = '<lvar j>{"summary": "s", "files": ["a.py"]}</lvar>OUT{done: [j]}'  # one alias, a JSON object
    run = once(
        Actor("edge-out-errors"), Profile("p"), *(t for t, _ in turns_and_errors), finish, emits=(Done,)
    )
    assert run.outcome == Success({"done": Done(summary="s", files=["a.py"])})
    told = notices(run)
    for i, (_, error) in enumerate(turns_and_errors, start=1):
        assert f"your last turn could not be read: {error}" in told[i], (i, told[i])

    two = once(
        Actor("edge-out-missing"),
        Profile("p"),
        'OUT{done: {summary: "s", files: []}}',
        'OUT{done: {summary: "s", files: []}, echo: {text: "e"}}',
        emits=(Done, Echo),
    )
    assert "your last turn could not be read: OUT{...} must fill: ['echo']" in notices(two)[1]
    assert isinstance(two.outcome, Success) and two.output["echo"] == Echo(text="e")


def test_a_result_that_already_is_the_output_fills_it_as_it_is():
    actor = Actor("edge-whole-model")
    made: list[Tally] = []

    @actor.handler(MakeTally)
    def make_tally(req: MakeTally, ctx) -> Tally:
        made.append(Tally(value=req.n))
        return made[-1]

    run = once(actor, Profile("p"), "<lact a>make_tally()</lact>", "OUT{tally: [a]}", emits=(Tally,))
    assert run.output["tally"] is run.resolve("a") == made[0]  # the RESULT holds a capture (ADR-0007/C1)


@pytest.mark.parametrize(
    "text, expected",
    [("[1, 2]", [1, 2]), ("(1, 2)", (1, 2)), ("just words", "just words")],
)
def test_a_field_typed_any_reads_its_text_as_a_literal_when_it_is_one(text, expected):
    turn = f"<lvar loose.value v>{text}</lvar>OUT{{loose: [v]}}"
    run = once(Actor("edge-any"), Profile("p"), turn, emits=(Loose,))
    value = run.output["loose"].value
    assert value == expected and type(value) is type(expected)


def test_a_command_pointing_into_a_circle_it_is_not_part_of_waits_and_is_not_blamed_for_it():
    actor = _echo_actor("edge-circle-bystander")
    turn = "<lact z>echo(text=*x)</lact><lact x>echo(text=*y)</lact><lact y>echo(text=*x)</lact>"
    run = asyncio.run(asyncio.wait_for(actor.run(Profile("p"), RunRequest(max_rounds=1), scripted(turn)), 5))
    r = results(run)
    assert list(r) == ["x", "y", "z"]  # z waited for x, although it was dispatched first
    assert r["x"].error == (
        "*y waits on x: a circle of pointers never settles, and a command that writes or reads waits for "
        "earlier commands of its turn"
    )
    assert r["z"].reason == "invalid" and "circle" not in r["z"].error


def test_the_fold_stops_hiding_once_the_view_is_back_under_half_its_budget():
    actor = _big_actor("edge-fold-half")
    run = once(
        actor,
        Profile("p"),
        "<lact a1>big(n=4000)</lact><lact a2>big(n=40)</lact>",
        "<lact a3>big()</lact>",
        "done",
        view_budget=1200,
    )
    (folded,) = [e for e in run.record if e.kind is Kind.SYSTEM and "folded:" in e.content]
    a1 = run.record.get("a1")
    assert f"1 older entries ({a1.name} to {a1.name}, ~" in folded.content
    assert folded.meta["directive"].refs == (a1.name,)
    view = actor._fold(run)
    assert [v.entry.alias for v in view if v.hidden] == ["a1"]  # a2 was old too, and stays


@pytest.mark.parametrize("budget, shown", [(190, "/3m10s "), (3700, "/1h01m ")])
def test_the_header_shows_a_time_budget_in_minutes_or_hours(budget, shown):
    run = once(Actor("edge-clock"), Profile("p"), "x", time_budget=budget)
    header = notices(run)[0].splitlines()[0]
    assert shown in header and run.outcome == Exhausted(1)


def test_a_result_json_cannot_carry_is_shown_by_its_repr():
    actor = Actor("edge-repr")

    @actor.handler(Pairs)
    def pairs(req: Pairs, ctx) -> dict:
        return {(1, 2): "x"}

    run = once(actor, Profile("p"), "<lact a1>pairs()</lact>", "x")
    assert "  a1 (_r3, pairs): dict[1] {(1, 2): 'x'}" in notices(run)[1]


def test_a_command_task_ending_in_neither_a_result_nor_a_cancel_aborts_the_run():
    # ADR-0002/S5: the turn's settle raises it and never drops it
    actor = Actor("edge-halt")
    live = []

    @actor.handler(Halter)
    async def halter(req: Halter, ctx):
        live.append(ctx.run)
        raise Halt("out by a door the loop does not watch")

    with pytest.raises(Halt, match="out by a door the loop does not watch"):
        once(actor, Profile("p"), "<lact h1>halter()</lact>", "never read")
    (run,) = live
    assert run.outcome is None and run.round == 1 and run.record.get("h1") is None
    assert actor.runs == {} and "run.completed" not in [e.kind for e in actor.bus.log]


def test_an_answer_carrying_models_in_a_list_reaches_the_asker_as_plain_data():
    peer, asker, tasks = Actor("edge-plain-peer"), Actor("edge-plain-asker"), []

    @peer.handler(Find)
    def find(req: Find, ctx) -> Answer:
        return Answer(text=req.text)

    @asker.handler(Ask)
    async def ask(req: Ask, ctx):
        handle = peer.queue(req, ctx)
        tasks.append(
            asyncio.create_task(
                peer.run(
                    Profile("p"),
                    RunRequest(max_rounds=2),
                    scripted(
                        '<lact f1>find(text="x")</lact><lact f2>find(text="y")</lact><lvar n>a note</lvar>',
                        "OUT{found: [f1, f2, n]}",
                    ),
                )
            )
        )
        return handle

    async def main():
        run = await asyncio.wait_for(
            asker.run(
                Profile("p"),
                RunRequest(inputs=("go",), max_rounds=2),
                scripted('<lact q1>ask(question="x")</lact>', "done"),
            ),
            5,
        )
        await asyncio.gather(*tasks)
        return run

    run = asyncio.run(main())
    assert tasks[0].result().output == {"found": [Answer(text="x"), Answer(text="y"), "a note"]}
    assert run.resolve("q1") == {"found": [{"text": "x"}, {"text": "y"}, "a note"]}


def test_an_answer_whose_asking_run_has_ended_comes_back_as_a_reply_and_only_a_success_does():
    peer, asker = Actor("edge-late-peer"), Actor("edge-late-asker")
    handles: list[Handle] = []

    @asker.handler(Ask)
    async def ask(req: Ask, ctx) -> str:
        handles.append(peer.queue(req, ctx))
        return "queued"  # not the handle: the command settles now and the run ends with the ask open

    def ask_once(question: str):
        return once(asker, Profile("p"), f'<lact q>ask(question="{question}")</lact>')

    def answer(turn: str):
        return asyncio.run(peer.run(Profile("p"), RunRequest(emits=(Answer,), max_rounds=1), scripted(turn)))

    first = ask_once("first")
    assert first.asked[handles[0].id][1].cancelled()  # the ask closed with the run
    assert answer("no answer").outcome == Exhausted(1)
    assert asker.inbox == []  # a failed peer sends nothing to an origin that is not waiting (ADR-0010/C3)

    ask_once("second")
    answered = answer('OUT{answer: "42"}')
    (reply,) = asker.inbox
    assert (reply.handle, reply.content, reply.origin_actor, reply.origin_run, reply.reply) == (
        handles[1],
        answered.output,
        "edge-late-peer",
        answered.id,
        True,
    )
    assert [e.payload["handle"] for e in peer.bus.log if e.kind == "inbox.answered"] == [handles[1].id]

    thanks = scripted('OUT{answer: "thanks"}')
    third = asyncio.run(asker.run(Profile("p"), RunRequest(max_rounds=1), thanks))
    assert f"answer from edge-late-peer (handle {handles[1].id})" in notices(third)[0]
    assert third.consumed == [reply] and isinstance(third.outcome, Success)
    assert asker.inbox == [] and peer.inbox == []  # a reply is never answered back
    assert not any(e.kind == "inbox.answered" for e in asker.bus.log)


def test_an_inbound_from_an_actor_no_longer_in_the_registry_is_consumed_and_answered_to_nobody():
    actor = Actor("edge-orphan")
    actor.inbox.append(
        Inbound(
            handle=Handle("orphan01"),
            content="are you there",
            origin_actor="edge-never-registered",
            origin_run="r0",
            origin_alias="q1",
        )
    )
    request = RunRequest(emits=(Answer,), max_rounds=1)
    run = asyncio.run(actor.run(Profile("p"), request, scripted('OUT{answer: "yes"}')))
    assert run.outcome == Success({"answer": Answer(text="yes")})
    assert "request from edge-never-registered (handle orphan01)" in notices(run)[0]
    assert actor.inbox == [] and not any(e.kind == "inbox.answered" for e in actor.bus.log)


def test_a_late_answer_to_an_ask_whose_command_was_cancelled_leaves_the_peers_run_whole():
    peer, asker = Actor("edge-cancelled-ask-peer"), Actor("edge-cancelled-ask-asker")
    queued: list[str] = []
    answered = []

    @asker.handler(Ask)
    async def ask(req: Ask, ctx):
        handle = peer.queue(req, ctx)
        queued.append(ctx.alias)
        return handle

    @asker.handler(Cut, effect="pure")  # beside the command it stops: a write would wait for it
    async def cut(req: Cut, ctx) -> str:
        while req.target not in queued:
            await asyncio.sleep(0)
        ctx.run.pending[req.target].cancel()  # the asking command stops while its answer is out
        late = scripted('OUT{answer: "late"}')
        answered.append(await peer.run(Profile("p"), RunRequest(emits=(Answer,), max_rounds=1), late))
        return type(answered[-1].outcome).__name__

    run = once(
        asker, Profile("p"), '<lact q1>ask(question="x")</lact><lact c1>cut(target="q1")</lact>', "done"
    )
    assert run.resolve("c1") == "Success"
    assert answered[0].output == {"answer": Answer(text="late")}
    ((alias, answer),) = run.asked.values()
    assert alias == "q1" and answer.cancelled()


class Hold(BaseModel):
    pass


def test_a_command_cancelled_before_it_started_settles_as_cancelled_and_nothing_stays_pending():
    actor = Actor("edge-never-started")
    held: list[str] = []

    @actor.handler(Cut)
    async def cut(req: Cut, ctx) -> str:
        ctx.run.pending[
            req.target
        ].cancel()  # dispatched after this one: scheduled, its coroutine not yet run
        return "cut"

    @actor.handler(Hold)
    async def hold(req: Hold, ctx) -> str:
        held.append("ran")
        return "held"

    run = once(actor, Profile("p"), '<lact k>cut(target="h")</lact><lact h>hold()</lact>', "x")
    r = results(run)
    assert r["k"].value == "cut" and held == []
    assert r["h"] == Result("hold", False, error="cancelled before it started", reason="cancelled")
    assert run.record.get("h").meta["execution"] == "not executed" and run.pending == {}
    assert "h: cancelled before it started" in notices(run)[1] and "still running" not in notices(run)[1]
    settled = [e.payload for e in actor.bus.log if e.kind == "command.settled" and e.payload["alias"] == "h"]
    assert [(s["ok"], s["reason"], s["execution"]) for s in settled] == [(False, "cancelled", "not executed")]


def test_an_out_literal_with_an_unhashable_key_is_a_retry_that_names_the_field():
    turns = ('OUT{tally: {value: "{[]: 1}"}}', "OUT{tally: {value: 3}}")
    run = once(Actor("edge-unhashable"), Profile("p"), *turns, emits=(Tally,))
    assert "your last turn could not be read: OUT{tally: ...}: value: " in notices(run)[1]
    assert run.output["tally"] == Tally(value=3)


class Said(BaseModel):
    text: str


@pytest.mark.parametrize("forward", [False, True], ids=["one-command", "a-pointer-at-a-later-one"])
@pytest.mark.parametrize("eager", [False, True], ids=["default-factory", "eager-factory"])
def test_every_command_of_a_turn_is_admitted_before_any_of_them_runs(eager, forward):
    # under an eager task factory a command's coroutine ran inside create_task: registered as pending
    # only afterwards, it settled twice (a second RESULT under its alias), and a pointer at a later
    # command of the same turn found nothing to wait for and resolved invalid
    if eager and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("eager tasks arrive in Python 3.12")
    actor = Actor("edge-admitted")

    @actor.handler(Said, effect="pure")
    async def said(req: Said, ctx) -> str:
        return req.text

    turn = ("<lact a>said(text=*b)</lact>" if forward else "") + '<lact b>said(text="ok")</lact>'

    async def scenario():
        loop = asyncio.get_running_loop()
        errors: list[str] = []
        loop.set_exception_handler(lambda _loop, c: errors.append(repr(c.get("exception"))))
        if eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        run = await actor.run(Profile("p"), RunRequest(max_rounds=1), scripted(turn))
        await asyncio.sleep(0)  # any callback on a task that was already complete lands here
        await asyncio.sleep(0)
        rows = [(e.alias, e.content.ok, e.content.value) for e in run.record if e.kind is Kind.RESULT]
        return sorted(rows), sorted(run.pending), errors

    rows, pending, errors = asyncio.run(scenario())
    assert rows == ([("a", True, "ok")] if forward else []) + [("b", True, "ok")]
    assert pending == [] and errors == []


@pytest.mark.parametrize("settled", [0, 1], ids=["first-turn", "after-a-settled-turn"])
def test_a_backend_that_raises_leaves_the_run_with_no_outcome(settled):
    # a settled turn without OUT{} leaves Continue in run.outcome, and the abort left it there: the
    # run read as settled while it ended by a raise (ADR-0002/C4: an abort is not one of the four)
    actor = Actor("edge-abort")
    seen: list = []
    calls = 0

    def backend(messages):
        nonlocal calls
        calls += 1
        if calls <= settled:
            return "<lvar v>kept</lvar>"
        seen.extend(actor.runs.values())
        raise RuntimeError("backend down")

    with pytest.raises(RuntimeError, match="backend down"):
        asyncio.run(actor.run(Profile("p"), RunRequest(max_rounds=settled + 1), backend))
    assert len(seen) == 1 and actor.runs == {} and seen[0].outcome is None
    assert not any(e.kind == "run.completed" for e in actor.bus.log)
    if settled:
        assert seen[0].resolve("v") == "kept"


@pytest.mark.parametrize("twice", [False, True], ids=["replays", "does-not-replay"])
def test_a_history_that_does_not_replay_leaves_no_live_run(twice):
    # the run was registered before its history replayed: a history refused (an alias twice) raised
    # with a run that never ran still live in `actor.runs`
    actor = Actor("edge-history")
    entry = Record("old").append(Kind.INPUT, "kept", alias="prior")
    calls: list[int] = []

    def backend(messages):
        calls.append(1)
        return 'OUT{answer: "ok"}'

    request = RunRequest(max_rounds=1, history=(entry, entry) if twice else (entry,))
    if twice:
        with pytest.raises(ValueError):
            asyncio.run(actor.run(Profile("p"), request, backend))
        assert calls == []
    else:
        asyncio.run(actor.run(Profile("p"), request, backend))
        assert calls == [1]
    assert actor.runs == {}
