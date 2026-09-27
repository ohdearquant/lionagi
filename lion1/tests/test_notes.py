"""Notes as a console finds them: a key can be deleted and stays gone on disk; a blank find is
refused instead of matching everything; find hits carry size and version and a note reads whole
through note.get; list prefixes are exact while find is case-insensitive."""

import asyncio
import json
import os
from datetime import date
from pathlib import Path

import pytest

from lionagi import Actor, Kind, Profile, RunRequest
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
