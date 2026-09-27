"""A conversation: a turn without a command is the reply and the loop waits for the next message; a
turn with commands runs them and reads the results before replying; every message is in one record
that the model sees whole; the person ends it, as Stopped; and a log read back is the run's history."""

import asyncio
import json

from hub import files
from hub.chat import chat, entry_row, load_log
from lionagi import Actor, Kind, Profile
from lionagi.actor import DEFAULT_HANDLERS, Message, Stopped, _messages
from lionagi.checkpoint import CheckpointError, load, save, take, to_history
from tests.test_actor import scripted


def asks(*messages: str | None):
    it = iter(messages)

    async def ask() -> str | None:
        return next(it)

    return ask


def reader(directory) -> tuple[Actor, Profile]:
    actor = Actor("c", notes_dir=directory / "notes")
    specs = files(actor, directory) | {h.name for h in DEFAULT_HANDLERS}
    return actor, Profile(name="c", specs=frozenset(specs), system="Reply in plain text. Never write OUT{}.")


def test_a_plain_turn_is_the_reply_and_the_next_message_is_in_the_same_record(tmp_path):
    seen: list[list[Message]] = []
    inner = scripted("Hello. What would you like?", "Two, then.", "Bye.")

    async def backend(messages):
        seen.append(messages)
        return await inner(messages)

    actor, profile = reader(tmp_path)
    said: list = []
    run = asyncio.run(chat(actor, profile, backend, asks("hi", "how many?", "/quit"), say=said.append))
    assert isinstance(run.outcome, Stopped) and run.round == 2
    texts = [e.content for e in run.record if e.kind is Kind.TEXT]
    assert texts == ["Hello. What would you like?", "Two, then."]  # the third turn was never asked for
    inputs = [(e.content, e.meta.get("origin")) for e in run.record if e.kind is Kind.INPUT]
    assert inputs == [("hi", None), ("how many?", "console")]
    # the second call's view carries the whole conversation so far, and no idleness nag
    rendered = "\n".join(m.content for m in seen[1])
    assert "hi" in rendered and "Hello. What would you like?" in rendered and "how many?" in rendered
    assert "no LNDL" not in rendered and "nothing was executed" not in rendered
    # every entry said once, in order, as the record grew
    assert [e.seq for e in said] == [e.seq for e in run.record] == list(range(len(run.record)))


def test_a_turn_with_a_command_reads_its_result_before_replying_and_the_tools_read_the_directory(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "sub").mkdir()
    actor, profile = reader(tmp_path)
    backend = scripted(
        '<lact l1>list_dir(path=".")</lact>',
        "There are two entries: a.txt and sub/.",
        '<lact r1>read(path="a.txt")</lact>',
        "It says alpha.",
    )
    run = asyncio.run(chat(actor, profile, backend, asks("what is here?", "read a.txt", None)))
    assert isinstance(run.outcome, Stopped) and run.round == 4
    results = [e.content for e in run.record if e.kind is Kind.RESULT]
    assert [r.value for r in results] == [["a.txt", "sub/"], "alpha\n"]
    texts = [e.content for e in run.record if e.kind is Kind.TEXT]
    assert texts[1] == "There are two entries: a.txt and sub/." and texts[3] == "It says alpha."


def test_a_log_read_back_is_the_history_and_the_old_aliases_still_resolve(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    log = tmp_path / "one.jsonl"
    actor, profile = reader(tmp_path)
    first = scripted("<lvar p1>a.txt</lvar><lact r1>read(path=*p1)</lact>", "It says alpha.")
    with log.open("w") as fh:
        asyncio.run(
            chat(
                actor,
                profile,
                first,
                asks("read a.txt", None),
                say=lambda e: fh.write(json.dumps(entry_row(e), default=str) + "\n"),
            )
        )
    history = load_log(log)
    assert [e.kind.name for e in history][:4] == ["INPUT", "SYSTEM", "TEXT", "RESULT"]
    assert history[3].content.ok and history[3].content.value == "alpha\n"
    seen = []

    async def second(messages):
        seen.append(messages)
        turns = ["<lact r2>read(path=*p1)</lact>", "Still alpha; *r1 was too."]
        return turns[len(seen) - 1]

    actor2, profile2 = reader(tmp_path)
    run = asyncio.run(chat(actor2, profile2, second, asks("again?", None), history=history))
    assert run.record.by_seq(0).content == "read a.txt"
    assert run.record.by_seq(len(history)).content == "again?"
    rendered = "\n".join(m.content for m in seen[0])
    assert "read a.txt" in rendered and "It says alpha." in rendered  # the old conversation is in the view
    results = [e.content for e in run.record if e.kind is Kind.RESULT and e.seq >= len(history)]
    assert results[0].ok and results[0].value == "alpha\n"  # *p1 from the old session resolved
    assert run.resolve("r1") == "alpha\n"  # the old result's alias too


def test_a_paused_run_hands_the_turn_back_at_the_next_round_even_though_it_had_not_replied(tmp_path):
    """`run.pause` is the person's interrupt: the round in flight finishes, its results land, and the
    wait hook is called as if the model had replied; the person's next message continues the run, and
    the record holds every entry when the run ends."""
    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = reader(tmp_path)
    backend = scripted(
        '<lact r1>read(path="a.txt")</lact>',
        '<lact r2>list_dir(path=".")</lact>',
        "You spoke, so I answer: alpha.",
    )

    def on_round(ev):
        if ev.payload.get("round") == 2:
            actor.runs[ev.payload["run"]].pause = True  # pressed during round 2: it still runs whole

    off = actor.bus.subscribe("round.started", on_round)
    said: list = []
    try:
        run = asyncio.run(chat(actor, profile, backend, asks("go", "still there?", None), say=said.append))
    finally:
        off()
    assert isinstance(run.outcome, Stopped) and run.round == 3
    results = [e.alias for e in run.record if e.kind is Kind.RESULT]
    assert results == ["r1", "r2"]  # round 2's command ran and settled before the turn came back
    inputs = [(e.content, e.round) for e in run.record if e.kind is Kind.INPUT]
    assert inputs == [("go", 0), ("still there?", 3)]
    assert [e.seq for e in said] == list(range(len(run.record)))


def test_a_console_notice_reaches_the_model_as_inbound_in_the_first_round(tmp_path):
    actor, profile = reader(tmp_path)
    seen = []

    async def backend(messages):
        seen.append(messages)
        return "Noted: r9 never settled."

    run = asyncio.run(
        chat(actor, profile, backend, asks("hi", None), notice="the previous session left r9 unsettled")
    )
    rendered = "\n".join(m.content for m in seen[0])
    assert "r9 unsettled" in rendered and "hi" in rendered
    inputs = [(e.content, e.meta.get("origin")) for e in run.record if e.kind is Kind.INPUT]
    assert inputs == [("hi", None), ("the previous session left r9 unsettled", "console")]


def test_context_directives_ride_the_log_so_a_resumed_view_folds_the_same(tmp_path):
    from lionagi.record import fold

    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    actor, profile = reader(tmp_path)
    first = scripted(
        '<lact a1>read(path="a.txt")</lact><lact a2>read(path="b.txt")</lact>',
        '<lact h1>context.hide(refs=["a1"])</lact>'
        '<lact s1>context.summarize(refs=["a2"], text="b was beta")</lact>',
        "Folded.",
    )
    log = tmp_path / "one.jsonl"
    with log.open("w") as fh:
        run1 = asyncio.run(
            chat(
                actor,
                profile,
                first,
                asks("read both", None),
                say=lambda e: fh.write(json.dumps(entry_row(e)) + "\n"),
            )
        )
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    kept = [r["meta"]["directive"] for r in rows if "directive" in r["meta"]]
    assert [d["op"] for d in kept] == ["hide", "summarize"] and kept[1]["text"] == "b was beta"
    history = load_log(log)
    actor2, profile2 = reader(tmp_path)
    run2 = asyncio.run(chat(actor2, profile2, scripted("Still folded."), asks("and?", None), history=history))

    def shape(record):
        view = fold(record)
        return (
            {v.entry.seq for v in view if v.hidden},
            [(v.entry.seq, v.summary) for v in view if v.summary],
            {v.entry.seq for v in view if not v.hidden and v.summary is None},
        )

    hidden1, summaries1, shown1 = shape(run1.record)
    hidden2, summaries2, shown2 = shape(run2.record)
    assert hidden1 == hidden2 and summaries1 == summaries2 == [(4, "b was beta")]
    assert shown1 == {s for s in shown2 if s < len(history)}  # the old view, exactly; new entries after it


def test_a_log_cut_mid_write_recovers_its_whole_lines_and_names_the_cut(tmp_path):
    import pytest

    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = reader(tmp_path)
    log = tmp_path / "cut.jsonl"
    with log.open("w") as fh:
        asyncio.run(
            chat(
                actor,
                profile,
                scripted("Hi."),
                asks("hello", None),
                say=lambda e: fh.write(json.dumps(entry_row(e)) + "\n"),
            )
        )
    whole = load_log(log)
    log.write_text(log.read_text() + '{"seq": 99, "kind": "TEXT", "con')  # a write the process died inside
    torn: list[str] = []
    assert load_log(log, torn) == whole and len(torn) == 1 and "a write that was cut" in torn[0]
    with pytest.raises(ValueError, match="the last line is not JSON"):
        load_log(log)
    lines = log.read_text().splitlines()
    log.write_text("\n".join([lines[0], "{broken", *lines[1:]]) + "\n")
    with pytest.raises(ValueError, match="line 2, with"):
        load_log(log, [])


def test_each_entry_is_said_as_it_lands_not_at_the_next_round_boundary(tmp_path):
    # a slow tool beside an instant one: the turn and the instant result reach the log while the
    # slow one still runs; a signal in that window used to lose them and the resume notice went
    # silent about a command that had run (core review P1-1, 2026-09-22)
    from pydantic import BaseModel

    from lionagi import Actor, Context, Profile

    class Fast(BaseModel):
        pass

    class Slow(BaseModel):
        pass

    actor = Actor("c", notes_dir=tmp_path / "notes")
    said: list = []
    seen: list = []

    @actor.handler(Fast)
    async def fast(req: Fast, ctx: Context) -> str:
        return "quick"

    @actor.handler(Slow)
    async def slow(req: Slow, ctx: Context) -> str:
        await asyncio.sleep(0.2)
        seen.append([(e.kind.name, e.alias) for e in said])
        return "late"

    specs = {"fast", "slow"} | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="c", specs=frozenset(specs), system="x")
    backend = scripted("<lact f1>fast()</lact><lact s1>slow()</lact>", "done")
    run = asyncio.run(chat(actor, profile, backend, asks("go", None), say=said.append))
    assert ("TEXT", None) in seen[0] and ("RESULT", "f1") in seen[0] and ("RESULT", "s1") not in seen[0]
    assert [e.seq for e in said] == list(range(len(run.record)))  # and nothing said twice


def test_a_turn_with_out_is_refused_and_the_conversation_goes_on(tmp_path):
    actor, profile = reader(tmp_path)
    said: list = []
    backend = scripted('Here it is.\nOUT{done: "yes"}', "Still here.")
    run = asyncio.run(chat(actor, profile, backend, asks("hi", "/quit"), say=said.append))
    assert isinstance(run.outcome, Stopped) and run.round == 2
    sys_entries = [e.content for e in run.record if e.kind is Kind.SYSTEM]
    assert any("this conversation has no OUT{}" in c for c in sys_entries)
    assert [e.content for e in run.record if e.kind is Kind.TEXT][-1] == "Still here."


def test_a_checkpoint_of_the_record_resumes_the_view_the_run_last_showed(tmp_path):
    from lionagi.record import fold

    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    actor, profile = reader(tmp_path)
    first = scripted(
        '<lact a1>read(path="a.txt")</lact><lact a2>read(path="b.txt")</lact>',
        '<lact h1>context.hide(refs=["a1"])</lact>'
        '<lact s1>context.summarize(refs=["a2"], text="b was beta")</lact>',
        "Folded.",
    )
    log = tmp_path / "one.jsonl"
    with log.open("w") as fh:
        run1 = asyncio.run(
            chat(
                actor,
                profile,
                first,
                asks("read both", None),
                say=lambda e: fh.write(json.dumps(entry_row(e)) + "\n"),
            )
        )
    source = {"record": str(log), "run": run1.id}
    ckpt = take(source, [], actor="c", why="manual", budget=60_000, record=run1.record)
    save(ckpt, tmp_path)
    history = to_history(load(tmp_path))
    rows = lambda es: [(e.seq, e.kind, e.content, e.alias, e.meta) for e in es]  # noqa: E731
    assert rows(history) == rows(load_log(log)) == rows(run1.record)
    seen: list[list[Message]] = []
    inner = scripted("Still folded.")

    async def backend(messages):
        seen.append(messages)
        return await inner(messages)

    actor2, profile2 = reader(tmp_path)
    asyncio.run(chat(actor2, profile2, backend, asks("and?", None), history=history))
    # the resumed first view opens with exactly the messages the ended run's view rendered
    ended = [(m.role, m.content, m.name) for m in _messages(fold(run1.record))]
    assert [(m.role, m.content, m.name) for m in seen[0][1 : 1 + len(ended)]] == ended
    assert any(m.content.startswith("[hidden;") for m in seen[0]) and "b was beta" in seen[0][1 + 4].content
    # a record that no longer folds to the checkpoint's view is refused, never replayed
    lines = log.read_text().splitlines()
    cut = [json.loads(ln) for ln in lines]
    for r in cut:
        r["meta"].pop("directive", None)
    log.write_text("".join(json.dumps(r) + "\n" for r in cut))
    try:
        to_history(load(tmp_path))
    except CheckpointError as e:
        assert "no longer folds to the view" in str(e)
    else:
        raise AssertionError("a record without its directives replayed")
