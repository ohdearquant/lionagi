"""The experience stores as a later run finds them: a record is never replaced, a replay is a no-op,
a reopened file holds every concurrent addition, the scope is filtered before k, a blank query is a
skip, and a file that cannot be read raises instead of reading as empty."""

import ast
import asyncio
import dataclasses
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import lionagi
from lionagi.experience import (
    Experience,
    ExperienceRecord,
    FileExperienceStore,
    MemoryExperienceStore,
)

SCOPE = ("solver", "solver", None)


def record(lesson="add the lines of data.txt", ask="sum the numbers in data.txt", scope=SCOPE, **over):
    actor, profile, version = scope
    fields = dict(
        id=str(uuid.uuid4()),
        actor=actor,
        profile=profile,
        version=version,
        run="r1",
        ask=ask,
        outcome="Success",
        lesson=lesson,
    )
    return ExperienceRecord(**{**fields, **over})


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    return MemoryExperienceStore() if request.param == "memory" else FileExperienceStore(tmp_path / "e.json")


def test_a_replay_of_the_same_id_is_idempotent_and_other_bytes_are_refused(store):
    kept = record()
    asyncio.run(store.persist(kept))
    asyncio.run(store.persist(kept))  # a retry of the same record: no second copy, no error
    asyncio.run(store.persist(dataclasses.replace(kept)))  # equal bytes from another instance
    with pytest.raises(ValueError, match="already kept with other content; none is replaced"):
        asyncio.run(store.persist(dataclasses.replace(kept, lesson="something else")))
    hits = asyncio.run(store.recall("data.txt", 10, scope=SCOPE))
    assert hits == [kept] and hits[0].lesson == "add the lines of data.txt"
    assert not any(n.startswith(("update", "delete", "remove")) for n in dir(store))
    with pytest.raises(ValueError, match="is not a full UUID"):
        record(id=uuid.uuid4().hex[:8])
    with pytest.raises(ValueError, match="not a finite non-negative number"):
        record(cost=float("nan"))


def test_a_reopened_file_store_holds_every_concurrent_addition(tmp_path):
    path = tmp_path / "shared.json"
    records = [record(lesson=f"lesson {i} about data.txt") for i in range(24)]

    def add(r: ExperienceRecord) -> None:
        asyncio.run(FileExperienceStore(path).persist(r))  # one store instance per writer, as two processes

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, records + records[:4]))  # four retries of records already added
    reopened = FileExperienceStore(path)
    hits = asyncio.run(reopened.recall("data.txt", 100, scope=SCOPE))
    assert sorted(h.id for h in hits) == sorted(r.id for r in records)
    assert len(json.loads(path.read_text())["records"]) == 24


def test_scope_filters_before_k_and_a_blank_query_is_a_skip(store):
    other = ("solver", "solver", "v2")  # a version is part of the scope; None is a value, never a wildcard
    near = [record(lesson="sum the numbers in data.txt line by line", scope=other) for _ in range(3)]
    mine = record(lesson="data.txt")
    for r in [*near, mine, record(lesson=None), record(lesson="unrelated words only", ask="nothing alike")]:
        asyncio.run(store.persist(r))
    assert asyncio.run(store.recall("sum the numbers in data.txt", 1, scope=SCOPE)) == [mine]
    assert len(asyncio.run(store.recall("sum the numbers in data.txt", 5, scope=other))) == 3
    assert asyncio.run(store.recall("   ", 5, scope=SCOPE)) == []
    assert asyncio.run(store.recall("", 5, scope=other)) == []
    tied = sorted([record(lesson="tie"), record(lesson="tie")], key=lambda r: r.id)
    for r in reversed(tied):
        asyncio.run(store.persist(r))
    assert asyncio.run(store.recall("TIE", 2, scope=SCOPE)) == tied  # casefolded, ties by id


def test_a_malformed_file_raises_and_a_hit_is_a_copy(store, tmp_path):
    kept = record()
    asyncio.run(store.persist(kept))
    (hit,) = asyncio.run(store.recall("data.txt", 1, scope=SCOPE))
    assert hit == kept and hit is not kept
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.lesson = "rewritten"
    object.__setattr__(hit, "lesson", "rewritten behind the store's back")
    assert asyncio.run(store.recall("data.txt", 1, scope=SCOPE))[0].lesson == kept.lesson
    path = tmp_path / "bad.json"
    twice = json.dumps({"records": [json.loads(kept.canonical())] * 2})  # one id kept twice is not a store
    for body in ("{not json", '{"notes": []}', '{"records": [{"id": "x"}]}', twice):
        path.write_text(body)
        bad = FileExperienceStore(path)
        with pytest.raises(ValueError):
            asyncio.run(bad.recall("data.txt", 1, scope=SCOPE))
        with pytest.raises(ValueError):
            asyncio.run(bad.persist(record()))
        assert path.read_text() == body  # a store it could not read is never rewritten from nothing


def test_an_experience_takes_positive_bounds_only():
    for over in ({"k": 0}, {"max_chars": 0}, {"io_timeout": 0}, {"io_timeout": float("inf")}, {"k": True}):
        with pytest.raises(ValueError):
            Experience(MemoryExperienceStore(), **over)


def test_nothing_under_lionagi_imports_hub_or_a_khive_client():
    root = Path(lionagi.__file__).resolve().parent  # the package as imported: the source tree or the wheel
    imported = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module)
    assert "pydantic" in imported  # the walk reads imports at all
    assert not {m for m in imported if m.split(".")[0] in ("hub", "khive") or "khive" in m}


@pytest.mark.parametrize(
    "over, error",
    [
        ({"actor": 7}, TypeError),
        ({"version": 1}, TypeError),
        ({"used_ids": "abc"}, TypeError),
        ({"used_ids": (1,)}, TypeError),
        ({"costed": -1}, ValueError),
        ({"uncosted": True}, ValueError),
        ({"complete": False}, ValueError),
        ({"cost": float("nan")}, ValueError),
        ({"id": "r0"}, ValueError),
    ],
)
def test_a_record_refuses_a_field_of_the_wrong_kind(over, error):
    with pytest.raises(error):
        record(**over)


def test_a_write_that_cannot_land_leaves_no_file_behind(tmp_path, monkeypatch):
    path = tmp_path / "experience.json"
    store = FileExperienceStore(path)
    first = record()
    asyncio.run(store.persist(first))
    before = path.read_text()

    def refuse(src, dst):
        raise OSError("the volume is read-only")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        asyncio.run(store.persist(record()))
    assert path.read_text() == before and sorted(p.name for p in tmp_path.iterdir()) == [
        ".experience.json.lock",
        "experience.json",
    ]
