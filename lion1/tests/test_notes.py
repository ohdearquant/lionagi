"""Notes as a console finds them: a key can be deleted and stays gone in the store; a blank find is
refused instead of matching everything; find hits carry size and version and a note reads whole
through note.get; list prefixes are exact while find is case-insensitive."""

import asyncio
import sys
from datetime import date

import pytest
from pydantic import BaseModel

from hub.chat import entry_row
from lion1 import Actor, Kind, Profile, RunRequest, Store
from lion1.checkpoint import entry_of
from lion1.khive import Khive
from lion1.lndl import guidance
from lion1.notes import MemoryNoteStore
from tests.test_actor import scripted


def results(run) -> list:
    return [e.content for e in run.record if e.kind is Kind.RESULT]


def test_a_blank_find_is_refused_and_a_letter_query_is_case_insensitive_while_list_prefix_is_exact():
    store = MemoryNoteStore()
    store.put("Foo", "capital key", run="r", seq=1)
    store.put("case", "MixedCase Content (here)", run="r", seq=2)
    with pytest.raises(ValueError, match="a blank query would match every note"):
        store.find("")
    with pytest.raises(ValueError):
        store.find("   ")
    assert [n.key for n in store.find("mixedcase")] == ["case"]
    assert [n.key for n in store.find("(here)")] == ["case"]  # punctuation is plain substring
    assert [n.key for n in store.list("Foo")] == ["Foo"] and store.list("foo") == []
    store.put("Straße", "a key", run="r", seq=3)
    store.put("road", "Hauptstraße 5", run="r", seq=4)
    store.put("ROAD", "HAUPTSTRASSE 7", run="r", seq=5)
    # a case is folded as Unicode folds it, query, key and value alike: ß is ss
    assert [n.key for n in store.find("STRASSE")] == ["ROAD", "Straße", "road"]
    assert [n.key for n in store.find("hauptstraße")] == ["ROAD", "road"]


class Busy(MemoryNoteStore):
    """A store another writer puts to right after every read."""

    def get(self, key):
        note = super().get(key)
        self.put(key, f"moved {self.version}", run="other", seq=0)
        return note


class Pair(BaseModel):
    a: str
    b: str


def test_a_command_reads_a_note_once_whatever_is_put_meanwhile_and_its_result_names_the_version():
    store = Busy()
    store.put("plan", "first", run="earlier", seq=0)
    actor = Actor("n-once")

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
    actor = Actor("n-other", privileges={"note.read"})
    keeper = asyncio.run(
        actor.run(Profile("keeper"), RunRequest(max_turns=1), scripted("<lvar note.plan>kept</lvar>"))
    )
    brought = MemoryNoteStore()
    courier = Profile("courier", notes=brought)
    asyncio.run(actor.run(courier, RunRequest(max_turns=1), scripted("<lvar note.plan>carried</lvar>")))
    store = actor.notes_for(Profile("keeper"))
    reads = []
    monkeypatch.setattr(store, "get", lambda key: reads.append(key) or MemoryNoteStore.get(store, key))
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


def test_a_memory_store_counts_every_write_and_an_empty_overwrite_keeps_the_key():
    store = MemoryNoteStore()
    assert store.version == 0
    store.put("plan", "draft", run="r1", seq=1)
    kept = store.put("plan", "", run="r2", seq=5)
    # an empty value is still a value: a key leaves the store only by a delete (ADR-0007/C7)
    assert (kept.value, kept.version, kept.run, kept.seq) == ("", 2, "r2", 5)
    assert store.get("plan") == kept and [n.key for n in store.list()] == ["plan"] and store.version == 2
    assert store.delete("absent") is None and store.version == 2
    assert store.delete("plan") == kept and store.get("plan") is None and store.version == 3


def test_find_reads_a_structured_value_as_its_json_text_with_accents_kept_and_a_date_as_text():
    store = MemoryNoteStore()
    store.put("menu", {"dish": "Crème Brûlée", "served": date(2026, 9, 26)}, run="r", seq=1)
    store.put("count", 30, run="r", seq=2)
    assert [n.key for n in store.find("CRÈME BRÛLÉE")] == ["menu"]
    assert [n.key for n in store.find("2026-09-26")] == ["menu"] and store.find("datetime") == []
    assert [n.key for n in store.find("30")] == ["count"]


def test_find_falls_back_to_the_values_own_text_where_json_cannot_render_it():
    store = MemoryNoteStore()
    # JSON object keys are text only, so a map keyed by tuples has no JSON text at all
    store.put("grid", {(0, 1): "Wall"}, run="r", seq=1)
    assert [n.key for n in store.find("wall")] == ["grid"]
    assert [n.key for n in store.find("(0, 1)")] == ["grid"]


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


def test_find_reads_every_note_when_one_value_cannot_be_rendered_as_json():
    store = MemoryNoteStore()
    circular: list = ["loop"]
    circular.append(circular)
    store.put("ring", circular, run="r", seq=1)
    store.put("plan", "ship it", run="r", seq=2)
    assert [n.key for n in store.find("ship")] == ["plan"]
    assert [n.key for n in store.find("LOOP")] == ["ring"]  # the value's own text stands in for its JSON


def test_note_find_and_note_list_render_a_value_that_refers_to_itself():
    # the store's text of such a value falls back to str(); the commands rendered each hit's preview
    # through their own text, which caught TypeError alone, and failed whole on the ValueError
    store = MemoryNoteStore()
    ring: list = ["loop"]
    ring.append(ring)
    store.put("ring", ring, run="r1", seq=1)
    store.put("plan", "ship it", run="r1", seq=2)
    assert [n.key for n in store.find("LOOP")] == ["ring"]
    turn = '<lact f>note.find(query="loop")</lact><lact l>note.list()</lact>'
    run = asyncio.run(
        Actor("notes-ring").run(Profile("p", notes=store), RunRequest(max_turns=1), scripted(turn))
    )
    found, listed = (run.record.get(a).content for a in ("f", "l"))
    assert found.ok and [h["key"] for h in found.value] == ["ring"], found
    assert listed.ok and [h["key"] for h in listed.value] == ["plan", "ring"], listed


def test_the_notes_kept_line_sorts_quotes_what_a_store_supplies_and_survives_what_it_raises():
    class Reversed(MemoryNoteStore):  # a store's list carries no order of its own
        def list(self, prefix=""):
            return list(reversed(super().list(prefix)))

    class Failing(MemoryNoteStore):
        def __init__(self, error):
            super().__init__()
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
        run = asyncio.run(Actor("t").run(Profile("p", notes=store), RunRequest(max_turns=1), scripted("hm")))
        assert run.record.of_kind(Kind.TEXT)  # the run went on
        notice = run.record.of_kind(Kind.SYSTEM)[0].content
        lines = [x for x in notice.splitlines() if x.startswith("notes kept:")]
        assert len(lines) == 1, notice
        return lines[0]

    store = Reversed()
    store.put("b", "v", run="r0", seq=1)
    store.put("a\nnotes kept: forged", "v", run="r0", seq=2)  # a line break
    store.put("x (9 chars, run r9), y", "v", run="r0", seq=3)  # text that reads as more entries
    store.put("k" * 100, "v", run="r0", seq=4)
    store.put("c", "v", run="r0\nnotes kept: forged", seq=5)  # a run id is the store's to supply too
    assert kept(store) == (
        "notes kept: 'a\\nnotes kept: forged' (1 chars, run r0), b (1 chars, run r0), "
        "c (1 chars, run 'r0\\nnotes kept: forged'), " + "k" * 57 + "... (1 chars, run r0), "
        "'x (9 chars, run r9), y' (1 chars, run r0)"
    )
    store = MemoryNoteStore()
    store.put("odd", Unprintable(), run="r0", seq=1)
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

    store = MemoryNoteStore()
    store.put(Formatted("plan"), "v", run=Formatted("r0"), seq=1)
    assert kept(store) == "notes kept: plan (1 chars, run r0)"


def test_reading_profiles_that_have_no_notes_leaves_the_actors_stores_as_they_were():
    actor = Actor("n-absent", privileges={"note.read"})
    reader = Profile("reader", privileges=frozenset({"note.read"}))
    turn = "".join(f'<lact r{i}>note.read(profile="ghost{i}", key="plan")</lact>' for i in range(50))
    run = asyncio.run(actor.run(reader, RunRequest(max_turns=1), scripted(turn)))
    for i in range(50):
        assert f"no note 'plan' in the notes of ghost{i}" in run.record.get(f"r{i}").content.error
    assert sorted(actor._notes) == ["reader"]  # the reader's own; none made for a name only read


def test_note_read_reads_another_profiles_rows_and_keeps_no_store_for_it(tmp_path, kkernel_stub):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lion1:n-file"\n')
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


def test_the_default_commands_get_delete_and_find_with_size_and_version():
    actor = Actor("n")
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
