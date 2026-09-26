"""Notes as a console finds them: a key can be deleted and stays gone on disk; a blank find is
refused instead of matching everything; find hits carry size and version and a note reads whole
through note.get; list prefixes are exact while find is case-insensitive."""

import asyncio
import json

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
