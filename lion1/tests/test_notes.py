"""Notes as a console finds them: a key can be deleted and stays gone in the store; a blank find is
refused instead of matching everything; find hits carry size and version and a note reads whole
through note.get; list prefixes are exact while find is case-insensitive."""

import asyncio
import sys
from datetime import date

import pytest
from pydantic import BaseModel

from hub.chat import entry_row
from lion1 import Actor, KhiveNoteStore, Kind, Profile, RunRequest, Store
from lion1.checkpoint import entry_of
from lion1.khive import Khive
from lion1.lndl import guidance
from lion1.notes import Note
from tests.lionfake import LionKhive, routed_config
from tests.lionfake import store as fresh_store
from tests.test_actor import scripted


def results(run) -> list:
    return [e.content for e in run.record if e.kind is Kind.RESULT]


def test_a_blank_find_is_refused_and_a_letter_query_is_case_insensitive_while_list_prefix_is_exact(store):
    notes = store.notes("p")
    notes.put("Foo", "capital key", run="r", seq=1)
    notes.put("case", "MixedCase Content (here)", run="r", seq=2)
    with pytest.raises(ValueError, match="a blank query would match every note"):
        notes.find("")
    with pytest.raises(ValueError):
        notes.find("   ")
    assert [n.key for n in notes.find("mixedcase")] == ["case"]
    assert [n.key for n in notes.find("(here)")] == ["case"]  # punctuation is plain substring
    assert [n.key for n in notes.list("Foo")] == ["Foo"] and notes.list("foo") == []
    notes.put("Straße", "a key", run="r", seq=3)
    notes.put("road", "Hauptstraße 5", run="r", seq=4)
    notes.put("ROAD", "HAUPTSTRASSE 7", run="r", seq=5)
    # the pack folds ASCII case only (ADR-0025 D6), in the query, the key and the value alike: ß is not ss
    assert [n.key for n in notes.find("STRASSE")] == ["ROAD"]
    assert [n.key for n in notes.find("hauptstra")] == ["ROAD", "road"]
    assert [n.key for n in notes.find("STRA")] == ["ROAD", "Straße", "road"]


class Holding(KhiveNoteStore):
    """A store holding the notes it is given, whatever their values: what a store the pack did not write
    may hold. Its `list` is those notes, in the order given."""

    def __init__(self, *held: Note):
        super().__init__(LionKhive(), "p")
        self.held = held

    def list(self, prefix=""):
        return [n for n in self.held if n.key.startswith(prefix)]


class Busy(KhiveNoteStore):
    """A store another writer puts to right after every read."""

    def get(self, key):
        note = super().get(key)
        self.put(key, f"moved {self.version}", run="other", seq=0)
        return note


class Pair(BaseModel):
    a: str
    b: str


def test_a_command_reads_a_note_once_whatever_is_put_meanwhile_and_its_result_names_the_version():
    store = Busy(LionKhive(), "p")
    store.put("plan", "first", run="earlier", seq=0)
    actor = Actor("n-once", store=fresh_store())

    @actor.handler(Pair)
    async def pair(req, ctx):
        return [req.a, req.b, ctx.get("note.plan")]

    backend = scripted(
        "<lact p1>pair(a=*note.plan, b=*note.plan)</lact>",
        '<lact g1>note.get(key="plan")</lact>',
    )
    run = asyncio.run(actor.run(Profile("p", notes=store), RunRequest(max_turns=2), backend))
    seen = run.record.get("p1")
    assert seen.content.value == ["first", "first", "first"] and seen.meta["notes"] == {"plan": 1}
    assert entry_of(entry_row(seen)).meta["notes"] == {"plan": 1}  # the log keeps the versions read
    now = run.record.get("g1").content.value
    assert now["value"] != "first" and now["version"] > 1  # note.get always reads the store
    assert "notes" not in run.record.get("g1").meta  # a command that points at no note names none


def test_note_read_reads_another_profiles_note_only_under_its_privilege(monkeypatch):
    actor = Actor("n-other", privileges={"note.read"}, store=fresh_store())
    keeper = asyncio.run(
        actor.run(Profile("keeper"), RunRequest(max_turns=1), scripted("<lvar note.plan>kept</lvar>"))
    )
    brought = fresh_store().notes("p")
    courier = Profile("courier", notes=brought)
    asyncio.run(actor.run(courier, RunRequest(max_turns=1), scripted("<lvar note.plan>carried</lvar>")))
    store = actor.notes_for(Profile("keeper"))
    reads = []
    monkeypatch.setattr(store, "get", lambda key: reads.append(key) or KhiveNoteStore.get(store, key))
    turn = (
        '<lact r1>note.read(profile="keeper", key="plan")</lact>'
        '<lact r2>note.read(profile="keeper", key="absent")</lact>'
        '<lact r3>note.read(profile="../keeper", key="plan")</lact>'
        '<lact r4>note.read(profile="courier", key="plan")</lact>'
    )
    lacking = asyncio.run(actor.run(Profile("stranger"), RunRequest(max_turns=1), scripted(turn)))
    refused = lacking.record.get("r1")
    assert refused.content.reason == "privilege" and "note.read" in refused.content.error
    assert refused.meta["execution"] == "not executed" and reads == []
    reader = Profile("reader", privileges=frozenset({"note.read"}))
    allowed = asyncio.run(actor.run(reader, RunRequest(max_turns=1), scripted(turn)))
    assert allowed.record.get("r1").content.value == {
        "profile": "keeper",
        "key": "plan",
        "value": "kept",
        "run": keeper.id,
        "version": 1,
    }
    assert "no note 'absent' in the notes of keeper" in allowed.record.get("r2").content.error
    assert "letters, digits" in allowed.record.get("r3").content.error
    # the actor's own store under that name, never the one a profile brought
    assert brought.get("plan").value == "carried" and not allowed.record.get("r4").content.ok
    assert reads == ["plan", "absent"]


def test_the_guidance_names_get_and_delete_only_when_the_profile_has_them():
    full = guidance(commands=["note.list", "note.find", "note.get", "note.delete"])
    assert 'note.get(key="...") reads one whole, note.delete(key="...") removes one, and' in full
    assert "note.get" not in guidance(commands=["note.list", "note.find"])


def test_a_note_store_counts_every_write_and_an_empty_overwrite_keeps_the_key(store):
    notes = store.notes("p")
    assert notes.version == 0
    notes.put("plan", "draft", run="r1", seq=1)
    written = notes.version
    kept = notes.put("plan", "", run="r2", seq=5)
    # an empty value is still a value: a key leaves the store only by a delete (ADR-0007/C7)
    assert (kept.value, kept.version, kept.run, kept.seq) == ("", 2, "r2", 5)
    assert notes.get("plan") == kept and [n.key for n in notes.list()] == ["plan"]
    assert notes.version > written > 0  # the version rises with every put
    assert notes.delete("absent") is None
    assert notes.delete("plan") == kept and notes.get("plan") is None


def test_find_reads_a_structured_value_as_its_json_text_and_a_date_is_refused(store):
    notes = store.notes("p")
    notes.put("menu", {"dish": "Crème Brûlée", "served": "2026-09-26"}, run="r", seq=1)
    notes.put("count", 30, run="r", seq=2)
    assert [n.key for n in notes.find("DISH")] == ["menu"]  # a field name is in the value's JSON text
    assert [n.key for n in notes.find("2026-09-26")] == ["menu"]
    assert [n.key for n in notes.find("30")] == ["count"]
    with pytest.raises(ValueError, match="a value JSON cannot carry whole"):
        notes.put("when", {"served": date(2026, 9, 26)}, run="r", seq=3)  # a date is not JSON
    assert notes.get("when") is None


def test_the_store_refuses_a_value_json_cannot_carry_whole_and_writes_nothing(store):
    notes = store.notes("p")
    # JSON object keys are text only, so a map keyed by tuples has no JSON text at all
    with pytest.raises(ValueError, match="a value JSON cannot carry whole"):
        notes.put("grid", {(0, 1): "Wall"}, run="r", seq=1)
    assert notes.get("grid") is None and notes.list() == [] and notes.version == 0


class Interrupted(BaseException):
    pass


@pytest.fixture
def int_digit_limit():
    # the limit on an integer's decimal text belongs to the interpreter, and an environment can lift it
    # (`PYTHONINTMAXSTRDIGITS=0`): the case below needs the default, so it sets it and puts back what it found
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    yield
    sys.set_int_max_str_digits(before)


def test_find_reads_every_note_when_one_value_cannot_be_rendered_as_json(store):
    notes = store.notes("p")
    circular: list = ["loop"]
    circular.append(circular)
    with pytest.raises(ValueError, match="a value JSON cannot carry whole"):
        notes.put("ring", circular, run="r", seq=1)  # refused whole, so no note of it can break a find
    notes.put("plan", "ship it", run="r", seq=2)
    assert [n.key for n in notes.find("ship")] == ["plan"]
    assert notes.find("LOOP") == [] and notes.get("ring") is None


def test_note_find_and_note_list_render_a_value_that_refers_to_itself(store):
    # a store the pack did not write may hold such a value: the commands render each hit's preview
    # through their own text, which caught TypeError alone, and failed whole on the ValueError
    ring: list = ["loop"]
    ring.append(ring)

    class Ringed(Holding):
        def find(self, query):
            return [n for n in self.held if query.lower() in n.key or query.lower() in str(n.value).lower()]

    notes = Ringed(Note("ring", ring, "r1", 1), Note("plan", "ship it", "r1", 2))
    assert [n.key for n in notes.find("LOOP")] == ["ring"]
    turn = '<lact f>note.find(query="loop")</lact><lact l>note.list()</lact>'
    run = asyncio.run(
        Actor("notes-ring", store=store).run(
            Profile("p", notes=notes), RunRequest(max_turns=1), scripted(turn)
        )
    )
    found, listed = (run.record.get(a).content for a in ("f", "l"))
    assert found.ok and [h["key"] for h in found.value] == ["ring"], found
    assert listed.ok and [h["key"] for h in listed.value] == ["ring", "plan"], listed


def test_the_notes_kept_line_sorts_quotes_what_a_store_supplies_and_survives_what_it_raises():
    class Failing(KhiveNoteStore):
        def __init__(self, error):
            super().__init__(LionKhive(), "p")
            self.error = error

        def list(self, prefix=""):
            raise self.error

    class Unsayable(Exception):
        def __str__(self):
            raise ValueError("no text")

    class Unprintable:
        def __str__(self):
            raise RuntimeError("no text")

    def kept(store):
        run = asyncio.run(
            Actor("t", store=fresh_store()).run(
                Profile("p", notes=store), RunRequest(max_turns=1), scripted("hm")
            )
        )
        assert run.record.of_kind(Kind.TEXT)  # the run went on
        notice = run.record.of_kind(Kind.SYSTEM)[0].content
        lines = [x for x in notice.splitlines() if x.startswith("notes kept:")]
        assert len(lines) == 1, notice
        return lines[0]

    # a store's list carries no order of its own: this one lists its notes last key first
    store = Holding(
        Note("x (9 chars, run r9), y", "v", "r0", 3),  # text that reads as more entries
        Note("k" * 100, "v", "r0", 4),
        Note("c", "v", "r0\nnotes kept: forged", 5),  # a run id is the store's to supply too
        Note("b", "v", "r0", 1),
        Note("a\nnotes kept: forged", "v", "r0", 2),  # a line break
    )
    assert kept(store) == (
        "notes kept: 'a\\nnotes kept: forged' (1 chars, run r0), b (1 chars, run r0), "
        "c (1 chars, run 'r0\\nnotes kept: forged'), " + "k" * 57 + "... (1 chars, run r0), "
        "'x (9 chars, run r9), y' (1 chars, run r0)"
    )
    store = Holding(Note("odd", Unprintable(), "r0", 1))  # the pack holds JSON only: this store does not
    assert kept(store) == "notes kept: unknown, the store could not be read: RuntimeError: no text"
    said = kept(Failing(OSError("disk down\nnotes kept: forged")))
    assert said.endswith("could not be read: OSError: disk down\\nnotes kept: forged")
    assert (
        kept(Failing(Unsayable()))
        == "notes kept: unknown, the store could not be read: an error that cannot be printed"
    )
    long = kept(Failing(OSError("x" * 500)))
    assert long == "notes kept: unknown, the store could not be read: OSError: " + "x" * 188 + "..."
    named = type("Bad\nnotes kept: forged", (Exception,), {})  # a type's name is the store's text too
    assert kept(Failing(named("down"))).endswith("read: Bad\\nnotes kept: forged: down")

    class Formatted(str):  # a key whose own formatting would say more than the key
        def __format__(self, spec):
            return "safe\nnotes kept: forged"

    store = Holding(Note(Formatted("plan"), "v", Formatted("r0"), 1))
    assert kept(store) == "notes kept: plan (1 chars, run r0)"


def test_reading_profiles_that_have_no_notes_leaves_the_actors_stores_as_they_were(store):
    actor = Actor("n-absent", privileges={"note.read"}, store=store)
    reader = Profile("reader", privileges=frozenset({"note.read"}))
    turn = "".join(f'<lact r{i}>note.read(profile="ghost{i}", key="plan")</lact>' for i in range(50))
    run = asyncio.run(actor.run(reader, RunRequest(max_turns=1), scripted(turn)))
    for i in range(50):
        assert f"no note 'plan' in the notes of ghost{i}" in run.record.get(f"r{i}").content.error
    assert sorted(actor._notes) == ["reader"]  # the reader's own; none made for a name only read


def test_note_read_reads_another_profiles_rows_and_keeps_no_store_for_it(tmp_path, kkernel_stub):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("lion1:n-file", tmp_path))
    store = Store(Khive(tmp_path, "lion1:n-file"))
    store.notes("keeper").put("plan", "kept", run="earlier", seq=3)
    actor = Actor("n-file", privileges={"note.read"}, store=store)
    reader = Profile("reader", privileges=frozenset({"note.read"}))
    turn = (
        '<lact r1>note.read(profile="keeper", key="plan")</lact>'
        '<lact r2>note.read(profile="keeper", key="absent")</lact>'
    )
    run = asyncio.run(actor.run(reader, RunRequest(max_turns=1), scripted(turn)))
    assert run.record.get("r1").content.value == {
        "profile": "keeper",
        "key": "plan",
        "value": "kept",
        "run": "earlier",
        "version": 1,
    }
    assert "no note 'absent' in the notes of keeper" in run.record.get("r2").content.error
    assert sorted(actor._notes) == ["reader"]
    # and the second run of the same profile reads what the first wrote, through the store alone (ADR-0025 C6)
    again = Actor("n-file", privileges={"note.read"}, store=Store(Khive(tmp_path, "lion1:n-file")))
    run = asyncio.run(again.run(reader, RunRequest(max_turns=1), scripted(turn)))
    assert run.record.get("r1").content.value["value"] == "kept"


def test_the_default_commands_get_delete_and_find_with_size_and_version(store):
    actor = Actor("n", store=store)
    profile = Profile(name="p")
    backend = scripted(
        "<lvar note.plan>step one\nstep two</lvar>"
        '<lact g1>note.get(key="plan")</lact>'
        '<lact f1>note.find(query="STEP")</lact>'
        '<lact d1>note.delete(key="plan")</lact>',
        '<lact g2>note.get(key="plan")</lact>'
        '<lact d2>note.delete(key="plan")</lact>'
        '<lact f2>note.find(query="")</lact>'
        "<lact l1>note.list()</lact>",
    )
    run = asyncio.run(actor.run(profile, RunRequest(max_turns=2), backend))
    got = [r for r in results(run) if r.spec == "note.get" and r.ok]
    assert got and got[0].value == {"key": "plan", "value": "step one\nstep two", "run": run.id, "version": 1}
    found = [r for r in results(run) if r.spec == "note.find" and r.ok]
    assert found[0].value == [
        {"key": "plan", "preview": "step one\nstep two", "size": 17, "run": run.id, "version": 1}
    ]
    deleted = [r for r in results(run) if r.spec == "note.delete"]
    assert deleted[0].ok and deleted[0].value == f"deleted note 'plan' (version 1, written by run {run.id})"
    assert not deleted[1].ok and "no note 'plan'; nothing deleted" in deleted[1].error
    missing = [r for r in results(run) if r.spec == "note.get" and not r.ok]
    assert missing and "no note 'plan'" in missing[0].error
    blank = [r for r in results(run) if r.spec == "note.find" and not r.ok]
    assert blank and "a blank query would match every note" in blank[0].error
    listed = [r for r in results(run) if r.spec == "note.list"]
    assert listed[0].value == []  # deleted: gone from the list as well
