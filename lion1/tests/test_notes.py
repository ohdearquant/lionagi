"""Notes as a console finds them: a key can be deleted and stays gone on disk; a blank find is
refused instead of matching everything; find hits carry size and version and a note reads whole
through note.get; list prefixes are exact while find is case-insensitive."""

import asyncio
import copy
import fcntl
import io
import json
import os
import re
import threading
from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel

from hub.chat import entry_row
from lionagi import Actor, Kind, Profile, RunRequest
from lionagi.checkpoint import entry_of
from lionagi.lndl import guidance
from lionagi.notes import FileNoteStore, MemoryNoteStore
from tests.test_actor import scripted


def results(run) -> list:
    return [e.content for e in run.record if e.kind is Kind.RESULT]


def test_a_deleted_note_is_gone_from_the_store_and_from_the_file_and_deleting_again_is_none(tmp_path):
    store = FileNoteStore(tmp_path / "n.json")
    store.put("foo", "some text", run="r1", seq=1)
    store.put("bar", "other", run="r1", seq=2)
    v = store.version
    gone = store.delete("foo")
    assert gone is not None and gone.key == "foo" and store.get("foo") is None and store.version == v + 1
    assert store.delete("foo") is None and store.version == v + 1
    reloaded = FileNoteStore(tmp_path / "n.json")
    assert [n.key for n in reloaded.list()] == ["bar"] and reloaded.version == v + 1
    assert list(json.loads((tmp_path / "n.json").read_text())["notes"]) == ["bar"]


def test_two_stores_over_one_file_keep_each_others_notes(tmp_path):
    # two consoles in one directory: B's store was built before A's second note, and B's next write
    # rewrote the file from its own memory, erasing A's note
    path = tmp_path / "console.json"
    a = FileNoteStore(path)
    a.put("plan", "one", run="a", seq=1)
    b = FileNoteStore(path)
    a.put("findings", "two", run="a", seq=2)
    b.put("other", "three", run="b", seq=1)
    on_disk = json.loads(path.read_text())
    assert sorted(on_disk["notes"]) == ["findings", "other", "plan"] and on_disk["version"] == 3
    assert [n.key for n in b.list()] == ["findings", "other", "plan"] and a.get("other").value == "three"
    assert a.delete("findings").value == "two" and b.get("findings") is None and b.version == 4


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


def test_the_default_commands_get_delete_and_find_with_size_and_version(tmp_path):
    actor = Actor("n", notes_dir=tmp_path)
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
    run = asyncio.run(actor.run(profile, RunRequest(max_rounds=2), backend))
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
    assert listed[0].value == []  # deleted: gone from the list as well as the file
    assert FileNoteStore(tmp_path / "p.json").list() == []


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
    run = asyncio.run(actor.run(Profile("p", notes=store), RunRequest(max_rounds=2), backend))
    seen = run.record.get("p1")
    assert seen.content.value == ["first", "first", "first"] and seen.meta["notes"] == {"plan": 1}
    assert entry_of(entry_row(seen)).meta["notes"] == {"plan": 1}  # the log keeps the versions read
    now = run.record.get("g1").content.value
    assert now["value"] != "first" and now["version"] > 1  # note.get always reads the store
    assert "notes" not in run.record.get("g1").meta  # a command that points at no note names none


def test_note_read_reads_another_profiles_note_only_under_its_privilege(monkeypatch):
    actor = Actor("n-other", privileges={"note.read"})
    keeper = asyncio.run(
        actor.run(Profile("keeper"), RunRequest(max_rounds=1), scripted("<lvar note.plan>kept</lvar>"))
    )
    brought = MemoryNoteStore()
    courier = Profile("courier", notes=brought)
    asyncio.run(actor.run(courier, RunRequest(max_rounds=1), scripted("<lvar note.plan>carried</lvar>")))
    store = actor.notes_for(Profile("keeper"))
    reads = []
    monkeypatch.setattr(store, "get", lambda key: reads.append(key) or MemoryNoteStore.get(store, key))
    turn = (
        '<lact r1>note.read(profile="keeper", key="plan")</lact>'
        '<lact r2>note.read(profile="keeper", key="absent")</lact>'
        '<lact r3>note.read(profile="../keeper", key="plan")</lact>'
        '<lact r4>note.read(profile="courier", key="plan")</lact>'
    )
    lacking = asyncio.run(actor.run(Profile("stranger"), RunRequest(max_rounds=1), scripted(turn)))
    refused = lacking.record.get("r1")
    assert refused.content.reason == "privilege" and "note.read" in refused.content.error
    assert refused.meta["execution"] == "not executed" and reads == []
    reader = Profile("reader", privileges=frozenset({"note.read"}))
    allowed = asyncio.run(actor.run(reader, RunRequest(max_rounds=1), scripted(turn)))
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


def test_a_dotted_alias_without_the_note_prefix_is_named_as_a_missed_note_in_the_next_notice(tmp_path):
    actor = Actor("n", notes_dir=tmp_path)
    seen = []

    async def backend(messages):
        seen.append(messages)
        return ["<lvar qa.smoke>round two ready</lvar>", "<lvar note.qa.smoke>round two ready</lvar>"][
            len(seen) - 1
        ]

    run = asyncio.run(actor.run(Profile(name="p"), RunRequest(max_rounds=2), backend))
    notice = "\n".join(m.content for m in seen[1])
    assert "<lvar qa.smoke> names an output `qa` this run does not emit" in notice
    assert "a note is written as <lvar note.qa.smoke>" in notice
    assert [n.key for n in FileNoteStore(tmp_path / "p.json").list()] == [
        "qa.smoke"
    ]  # the second turn wrote it
    assert run.values["qa.smoke"][1] == "round two ready"  # the first turn's value is still a plain value


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


def test_a_file_write_interrupted_before_the_rename_leaves_the_file_as_it_was_and_no_temp_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "n.json"
    store = FileNoteStore(path)
    store.put("plan", "one", run="r", seq=1)
    before = path.read_bytes()
    temps = []

    def interrupted(src, dst):
        temps.append((Path(src), json.loads(Path(src).read_text())["notes"]["plan"]["value"]))
        raise Interrupted

    with monkeypatch.context() as m:
        m.setattr("lionagi.notes.os.replace", interrupted)
        with pytest.raises(Interrupted):
            store.put("plan", "two", run="r", seq=2)
    assert len(temps) == 1 and temps[0][1] == "two"  # the new payload had reached the temp file
    assert not temps[0][0].exists() and path.read_bytes() == before
    assert store.get("plan").value == "one" and store.version == 1


def test_a_file_write_interrupted_after_the_rename_keeps_the_new_file_and_raises_the_interrupt(
    tmp_path, monkeypatch
):
    path = tmp_path / "n.json"
    store = FileNoteStore(path)
    store.put("plan", "one", run="r", seq=1)
    real_replace = os.replace
    temps = []

    def replaced_then_interrupted(src, dst):
        temps.append(Path(src))
        real_replace(src, dst)
        raise Interrupted

    with monkeypatch.context() as m:
        m.setattr("lionagi.notes.os.replace", replaced_then_interrupted)
        with pytest.raises(Interrupted):
            store.put("plan", "two", run="r", seq=2)
    assert len(temps) == 1 and not temps[0].exists()
    reloaded = FileNoteStore(path)
    assert reloaded.get("plan").value == "two" and reloaded.version == 2


def test_a_value_the_file_cannot_carry_is_refused_before_the_store_takes_its_lock(tmp_path):
    store = FileNoteStore(tmp_path / "n.json")
    circular: list = []
    circular.append(circular)
    for value, said in (("a\ud800b", "surrogates not allowed"), (circular, "Circular reference")):
        with pytest.raises(
            ValueError, match=f"note 'k': .*{said}.*; the file store carries JSON values only"
        ):
            store.put("k", value, run="r", seq=1)
    assert store.get("k") is None and store.version == 0
    assert list(tmp_path.iterdir()) == []  # no file, and no lock file: the refusal came first


@pytest.mark.parametrize(
    "value, said",
    [
        ((1, 2), "JSON would read it back changed"),  # as a list
        ({"at": (0, 1)}, "JSON would read it back changed"),
        ({1: "one"}, "JSON would read it back changed"),  # its key as text
        ({1: "a", "1": "b"}, "JSON would read it back changed"),  # one key of two
        (float("nan"), "Out of range float values are not JSON compliant"),
        ([float("inf")], "Out of range float values are not JSON compliant"),
    ],
    ids=["tuple", "nested-tuple", "int-key", "colliding-keys", "nan", "infinity"],
)
def test_a_value_json_cannot_carry_whole_is_refused_before_the_lock(tmp_path, value, said):
    # NaN and Infinity are not standard JSON; the rest JSON would read back changed
    store = FileNoteStore(tmp_path / "n.json")
    with pytest.raises(ValueError, match=f"note 'k': {said}.*; the file store carries JSON values only"):
        store.put("k", value, run="r", seq=1)
    assert store.get("k") is None and store.version == 0
    assert list(tmp_path.iterdir()) == []


def test_a_value_json_reads_back_equal_is_kept_and_a_fresh_store_reads_it_equal(tmp_path):
    value = {"dish": "Crème Brûlée", "n": [1, 2.5, -0.0, True, None, ""], "empty": {}, "list": []}
    FileNoteStore(tmp_path / "n.json").put("k", value, run="r", seq=1)
    back = FileNoteStore(tmp_path / "n.json").get("k").value
    assert back == value and [type(x) for x in back["n"]] == [type(x) for x in value["n"]]


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.__setitem__("at", (0, 1)),
        lambda v: v.__setitem__("n", float("nan")),
        lambda v: v.__setitem__("a", "changed"),
    ],
    ids=["tuple", "nan", "plain"],
)
def test_a_value_changed_while_put_waits_for_the_lock_leaves_what_was_checked_in_the_file(
    tmp_path, monkeypatch, change
):
    path = tmp_path / "n.json"
    value = {"a": [1, 2], "b": "text"}
    checked = copy.deepcopy(value)
    store, other = FileNoteStore(path), FileNoteStore(path)
    waiting, errors = threading.Event(), []
    real_flock = fcntl.flock

    def announcing(lock, how):
        if threading.current_thread() is not threading.main_thread():
            waiting.set()
        return real_flock(lock, how)

    def writer():
        try:
            store.put("k", value, run="r", seq=1)
        except BaseException as e:
            errors.append(e)

    monkeypatch.setattr("lionagi.notes.fcntl.flock", announcing)
    with other._locked():  # another console holds the file while this put has passed its check
        thread = threading.Thread(target=writer)
        thread.start()
        assert waiting.wait(10)
        change(value)
    thread.join(10)
    assert not thread.is_alive() and errors == []
    assert json.loads(path.read_text())["notes"]["k"]["value"] == checked
    assert FileNoteStore(path).get("k").value == checked


def test_a_value_changed_after_put_returns_changes_nothing_stored(tmp_path):
    value = {"a": [1, 2], "b": "text"}
    checked = copy.deepcopy(value)
    store = FileNoteStore(tmp_path / "n.json")
    note = store.put("k", value, run="r", seq=1)
    value["a"].append(3)
    value["c"] = (1,)
    assert note.value == checked and note.value is not value
    assert store.get("k").value == checked and FileNoteStore(tmp_path / "n.json").get("k").value == checked


def _notes_file(value: str = '"x"', seq: str = "1", version: str = "1") -> str:
    row = f'{{"key": "k", "value": {value}, "run": "r", "seq": {seq}, "version": 1}}'
    return f'{{"version": {version}, "notes": {{"k": {row}}}}}'


def _refused_on_every_path(path: Path, store: FileNoteStore, said: str, text: str) -> None:
    for read_or_write in (
        lambda: FileNoteStore(path),
        lambda: store.get("k"),
        lambda: store.list(),
        lambda: store.find("k"),
        lambda: store.version,
        lambda: store.put("other", "x", run="r", seq=2),  # an unrelated write is refused, not re-emitting it
        lambda: store.delete("k"),
    ):
        with pytest.raises(ValueError, match=said):
            read_or_write()
    assert path.read_text() == text
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_file_holding_a_non_finite_number_is_refused_on_load_naming_the_file_and_the_constant(
    tmp_path, constant
):
    path = tmp_path / "n.json"
    store = FileNoteStore(path)  # built before the file holds anything
    text = _notes_file(value=constant)
    path.write_text(text)
    said = re.escape(f"{path}: the file holds {constant}, which is not standard JSON")
    _refused_on_every_path(path, store, said, text)


@pytest.mark.parametrize(
    "number, text",
    [
        ("1e999", _notes_file(value="1e999")),
        ("-1e999", _notes_file(value="-1e999")),
        ("1E+400", _notes_file(value="1E+400")),
        ("1.8e308", _notes_file(value="1.8e308")),  # just past the largest float
        ("1e999", _notes_file(value='{"a": [1, 1e999]}')),
        ("1e999", _notes_file(seq="1e999")),
        ("1e999", _notes_file(version="1e999")),
    ],
    ids=["value", "negative", "capital-exponent", "just-past-max", "nested", "seq", "version"],
)
def test_a_number_too_large_for_a_float_is_refused_on_load_naming_the_file_and_the_number(
    tmp_path, number, text
):
    # valid JSON that Python reads as infinity: a read would hand the value out and the next unrelated
    # write would stop at the flush
    path = tmp_path / "n.json"
    store = FileNoteStore(path)  # built before the file holds anything
    path.write_text(text)
    said = re.escape(f"{path}: the file holds {number}, which is not a finite number")
    _refused_on_every_path(path, store, said, text)
    path.write_text(_notes_file())  # repaired by hand: the store reads it and a write goes through
    assert store.get("k").value == "x"
    store.put("other", "y", run="r", seq=2)
    assert sorted(json.loads(path.read_text())["notes"]) == ["k", "other"]


def test_a_file_holding_the_largest_finite_floats_loads_and_a_later_write_keeps_them(tmp_path):
    path = tmp_path / "n.json"
    path.write_text(
        _notes_file(value="[1.7976931348623157e308, -1.7976931348623157e308, 1e-999, 5e-324, -0.0]")
    )
    store = FileNoteStore(path)
    kept = [1.7976931348623157e308, -1.7976931348623157e308, 0.0, 5e-324, -0.0]
    assert store.get("k").value == kept
    store.put("other", "y", run="r", seq=2)
    assert FileNoteStore(path).get("k").value == kept


_NARROW = pytest.mark.parametrize(
    "encoding, value",
    [("latin-1", "Crème Brûlée"), ("ascii", "Crème Brûlée"), ("cp1252", "東京")],
    ids=["latin-1", "ascii", "cp1252-cjk"],
)


@_NARROW
def test_the_file_is_written_as_utf_8_whatever_the_locale_encoding_is(tmp_path, monkeypatch, encoding, value):
    # an open() given no encoding takes the locale's; os.fdopen and Path.read_text ask io.text_encoding
    monkeypatch.setattr(io, "text_encoding", lambda given, stacklevel=2: encoding if given is None else given)
    path = tmp_path / "n.json"
    FileNoteStore(path).put("k", value, run="r", seq=1)
    assert json.loads(path.read_bytes().decode("utf-8"))["notes"]["k"]["value"] == value
    assert not list(tmp_path.glob("*.tmp"))


@_NARROW
def test_the_file_is_read_as_utf_8_whatever_the_locale_encoding_is(tmp_path, monkeypatch, encoding, value):
    monkeypatch.setattr(io, "text_encoding", lambda given, stacklevel=2: encoding if given is None else given)
    path = tmp_path / "n.json"
    path.write_bytes(_notes_file(value=json.dumps(value, ensure_ascii=False)).encode("utf-8"))
    assert FileNoteStore(path).get("k").value == value


def test_a_write_never_renders_a_non_finite_number_wherever_it_sits(tmp_path):
    path = tmp_path / "n.json"
    store = FileNoteStore(path)
    store.put("plan", "one", run="r", seq=1)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="not JSON compliant"):  # the sequence is no value, but a number
        store.put("other", "two", run="r", seq=float("nan"))
    assert path.read_bytes() == before and not list(tmp_path.glob("*.tmp"))
    assert [n.key for n in store.list()] == ["plan"] and store.version == 1
    store.put("third", "three", run="r", seq=3)
    on_disk = json.loads(path.read_text())
    assert sorted(on_disk["notes"]) == ["plan", "third"] and "NaN" not in path.read_text()


def test_a_value_nested_past_the_recursion_limit_is_refused_as_a_value_error(tmp_path, no_collection):
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    store = FileNoteStore(tmp_path / "n.json")
    # the interpreter words the overflow itself ("maximum recursion depth exceeded" to 3.13, "Stack
    # overflow" from 3.14); the preflight names it and says where it happened
    said = "note 'k': .*while encoding a JSON object; the file store carries JSON values only"
    with pytest.raises(ValueError, match=said):
        store.put("k", deep, run="r", seq=1)
    assert list(tmp_path.iterdir()) == []


def test_find_reads_every_note_when_one_value_cannot_be_rendered_as_json():
    store = MemoryNoteStore()
    circular: list = ["loop"]
    circular.append(circular)
    store.put("ring", circular, run="r", seq=1)
    store.put("plan", "ship it", run="r", seq=2)
    assert [n.key for n in store.find("ship")] == ["plan"]
    assert [n.key for n in store.find("LOOP")] == ["ring"]  # the value's own text stands in for its JSON


def test_a_first_write_that_fails_leaves_nothing_behind_and_the_next_write_carries_only_itself(
    tmp_path, monkeypatch
):
    # the file is the store. A first write whose replace failed left its note in memory, readable at
    # version 1 with no file on disk, and the next write carried it to the file as if it had landed
    def refuse(src, dst):
        raise OSError("replace refused")

    path = tmp_path / "notes.json"
    store = FileNoteStore(path)
    with monkeypatch.context() as m:
        m.setattr("lionagi.notes.os.replace", refuse)
        with pytest.raises(OSError, match="replace refused"):
            store.put("ghost", "uncommitted", run="r1", seq=1)
    assert not path.exists() and not list(tmp_path.glob("*.tmp"))
    store.put("real", "committed", run="r1", seq=2)  # no read in between: the write reconciles itself
    on_disk = json.loads(path.read_text())
    assert (sorted(on_disk["notes"]), on_disk["version"]) == (["real"], 1)
    assert store.get("ghost") is None and store.get("real").value == "committed"
    other = FileNoteStore(tmp_path / "other.json")
    with monkeypatch.context() as m:
        m.setattr("lionagi.notes.os.replace", refuse)
        with pytest.raises(OSError, match="replace refused"):
            other.put("ghost", "uncommitted", run="r1", seq=1)
    assert (other.get("ghost"), other.version) == (None, 0)
    # a failed write over an existing file keeps the file and the value it held
    old = path.read_bytes()
    with monkeypatch.context() as m:
        m.setattr("lionagi.notes.os.replace", refuse)
        with pytest.raises(OSError, match="replace refused"):
            store.put("real", "changed", run="r1", seq=3)
    assert path.read_bytes() == old and (store.get("real").value, store.version) == ("committed", 1)


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
        Actor("notes-ring").run(Profile("p", notes=store), RunRequest(max_rounds=1), scripted(turn))
    )
    found, listed = (run.record.get(a).content for a in ("f", "l"))
    assert found.ok and [h["key"] for h in found.value] == ["ring"], found
    assert listed.ok and [h["key"] for h in listed.value] == ["plan", "ring"], listed


def test_the_first_notice_names_the_notes_already_kept_and_later_ones_do_not(
    tmp_path,
):
    # notes an earlier run wrote were unseen until the model thought to call note.list
    store = FileNoteStore(tmp_path / "notes" / "p.json")
    store.put("plan", "step one", run="old1", seq=4)
    store.put("todo", ["a", "b"], run="old2", seq=9)
    actor = Actor("t", notes_dir=tmp_path / "notes")
    turn = "<lact l>note.list()</lact>"
    run = asyncio.run(
        actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=2), scripted(turn, "still"))
    )
    first, second = [e.content for e in run.record.of_kind(Kind.SYSTEM)][:2]
    assert "\nnotes kept: plan (8 chars, run old1), todo (10 chars, run old2)\n" in first, first
    assert "notes kept" not in second
    listed = run.record.get("l").content.value  # the sizes note.list gives for the same notes
    assert (
        "notes kept: " + ", ".join(f"{h['key']} ({h['size']} chars, run {h['run']})" for h in listed) in first
    )
    # a resumed run is a new run: its first notice lists them again
    again = asyncio.run(
        actor.run(
            Profile("p"),
            RunRequest(inputs=("again",), max_rounds=1, history=run.record.entries),
            scripted("ok"),
        )
    )
    resumed = [e for e in again.record.of_kind(Kind.SYSTEM) if e.seq >= len(run.record.entries)]
    assert "\nnotes kept: plan (8 chars, run old1), todo (10 chars, run old2)\n" in resumed[0].content
    # none kept: no line
    run = asyncio.run(Actor("t").run(Profile("q"), RunRequest(inputs=("go",), max_rounds=1), scripted("hm")))
    assert "notes kept" not in run.record.of_kind(Kind.SYSTEM)[0].content


def test_the_first_notice_names_ten_notes_counts_the_rest_and_says_an_unreadable_store(
    tmp_path,
):
    store = MemoryNoteStore()
    for i in range(12):
        store.put(f"k{i:02d}", "v", run="r0", seq=i)
    run = asyncio.run(
        Actor("t").run(Profile("p", notes=store), RunRequest(inputs=("go",), max_rounds=1), scripted("hm"))
    )
    notice = run.record.of_kind(Kind.SYSTEM)[0].content
    told = ", ".join(f"k{i:02d} (1 chars, run r0)" for i in range(10))
    assert f"\nnotes kept: {told}, and 2 more\n" in notice, notice
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "p.json").write_text("{not json")
    run = asyncio.run(
        Actor("t", notes_dir=tmp_path / "notes").run(
            Profile("p"), RunRequest(inputs=("go",), max_rounds=1), scripted("hm")
        )
    )
    notice = run.record.of_kind(Kind.SYSTEM)[0].content
    said = "\nnotes kept: unknown, the store could not be read: JSONDecodeError: "
    assert said in notice and run.record.of_kind(Kind.TEXT)


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
        run = asyncio.run(Actor("t").run(Profile("p", notes=store), RunRequest(max_rounds=1), scripted("hm")))
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
