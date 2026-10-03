"""The offline gate of examples/experience_twice.py: the second run of one ask carries the first run's
lesson on its record, on the bus and in its first backend call, and nothing of it once only the
persist is removed or the store's file is gone. Transport, never improvement."""

import asyncio
import json
import tempfile

import pytest

from lion1 import Kind
from lion1.experience import FileExperienceStore
from tests.test_examples import load


@pytest.fixture
def example(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # the script's scratch directory lands here
    return load("experience_twice")


def carried(example, actor, second, seen) -> tuple[dict, list, bool]:
    notice = next(e for e in second.record if e.kind is Kind.SYSTEM)
    events = [e.payload for e in actor.bus.events("memory.recalled") if e.payload["run"] == second.id]
    in_call = any(example.LESSON in m.content for m in seen[0])
    return json.loads(notice.meta["experience"]), events, in_call


def test_the_second_run_recalls_the_first_runs_lesson_on_record_bus_and_call(example, tmp_path):
    actor, first, second, seen = asyncio.run(example.ask_twice(tmp_path))
    persisted = json.loads(first.record.entries[-1].meta["experience"])
    assert persisted["status"] == "ok"
    receipt, events, in_call = carried(example, actor, second, seen)
    assert receipt["status"] == "ok" and receipt["ids"] == [persisted["id"]]
    assert [json.loads(p["receipt_json"]) for p in events] == [receipt] and in_call
    notice = next(m for m in seen[0] if m.content.startswith("<system round=1/"))
    assert f'    lesson: "{example.LESSON}"' in notice.content.splitlines()


def test_removing_only_the_persist_recalls_nothing(example, tmp_path, monkeypatch):
    async def nothing(self, record):
        return None

    monkeypatch.setattr(FileExperienceStore, "persist", nothing)
    actor, first, second, seen = asyncio.run(example.ask_twice(tmp_path))
    assert first.record.get("l1").content.ok  # the model left its lesson; only the persist is gone
    receipt, events, in_call = carried(example, actor, second, seen)
    assert receipt["ids"] == [] and len(events) == 1 and not in_call
    assert not any("experience:" in m.content for m in seen[0])


def test_a_reopened_empty_store_recalls_nothing_from_the_actors_memory(example, tmp_path):
    existed = []

    def delete(path):
        existed.append(path.exists())
        path.unlink()

    actor, first, second, seen = asyncio.run(example.ask_twice(tmp_path, between=delete))
    assert existed == [True] and json.loads(first.record.entries[-1].meta["experience"])["status"] == "ok"
    receipt, events, in_call = carried(example, actor, second, seen)
    assert receipt["status"] == "ok" and receipt["ids"] == [] and not in_call
