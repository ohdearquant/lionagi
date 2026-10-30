# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Captured progression JSON avoids redundant statistics encoding without changing membership."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services import sessions as svc  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(svc, "store_path", lambda: str(path))
    return path


async def _seed(path, members_by_branch, *, raw_by_branch=None, rows=None):
    if rows is None:
        unique = dict.fromkeys(
            mid for members in members_by_branch for mid in members if isinstance(mid, str)
        )
        rows = [
            (mid, "assistant" if index % 2 else "user", float(index + 1))
            for index, mid in enumerate(unique)
        ]
    async with StateDB(path) as db:
        await db.create_progression("session-pool")
        await db.create_session(
            {
                "id": "session",
                "progression_id": "session-pool",
                "created_at": 100.0,
                "updated_at": 100.0,
                "status": "completed",
                "name": "session",
            }
        )
        for index, members in enumerate(members_by_branch):
            await db.create_progression(f"branch-{index}-pool")
            await db.create_branch(
                {
                    "id": f"branch-{index}",
                    "session_id": "session",
                    "progression_id": f"branch-{index}-pool",
                    "created_at": float(index),
                    "name": "branch",
                }
            )
        if rows:
            await db.insert_messages(
                [
                    {
                        "id": mid,
                        "created_at": created,
                        "role": role,
                        "content": {"text": "message"},
                        "node_metadata": {},
                    }
                    for mid, role, created in rows
                ]
            )
    raw_by_branch = raw_by_branch or [json.dumps(members) for members in members_by_branch]
    async with aiosqlite.connect(str(path)) as writer:
        mode = await (await writer.execute("PRAGMA journal_mode=WAL")).fetchone()
        assert mode[0] == "wal"
        for index, raw in enumerate(raw_by_branch):
            await writer.execute(
                "UPDATE progressions SET collection = ? WHERE id = ?",
                (raw, f"branch-{index}-pool"),
            )
        await writer.commit()
    return raw_by_branch


class _StatsMeter:
    """Counts actual encoder inputs, SQL binds and VM work; no timing or RSS claim."""

    def __init__(self, monkeypatch, raw_values):
        self.active = None
        self.encodes = []
        self.binds = []
        self.decoded_bytes = 0
        self.vm_steps = {"roles": 0, "bounds": 0}
        dumps, loads = json.dumps, json.loads

        def count_dumps(value, *args, **kwargs):
            encoded = dumps(value, *args, **kwargs)
            if self.active:
                self.encodes.append((self.active, encoded))
            return encoded

        def count_loads(value, *args, **kwargs):
            if isinstance(value, str) and value in raw_values:
                self.decoded_bytes += len(value.encode())
            return loads(value, *args, **kwargs)

        monkeypatch.setattr(json, "dumps", count_dumps)
        monkeypatch.setattr(json, "loads", count_loads)
        for name, stage in [("_fetch_role_counts", "roles"), ("_fetch_message_bounds", "bounds")]:
            original = getattr(svc, name)

            async def capture(*args, _original=original, _stage=stage, **kwargs):
                assert self.active is None
                self.active = _stage
                try:
                    return await _original(*args, **kwargs)
                finally:
                    self.active = None

            monkeypatch.setattr(svc, name, capture)
        open_db = svc._open_db

        @asynccontextmanager
        async def measured_open(path):
            async with open_db(path) as db:
                execute = db.execute

                async def counted_execute(sql, params=()):
                    if self.active:
                        self.binds.append((self.active, tuple(params)))
                    return await execute(sql, params)

                def step():
                    if self.active:
                        self.vm_steps[self.active] += 1
                    return 0

                monkeypatch.setattr(db, "execute", counted_execute)
                await db.set_progress_handler(step, 1)
                try:
                    yield db
                finally:
                    await db.set_progress_handler(None, 0)

        monkeypatch.setattr(svc, "_open_db", measured_open)

    def cost(self, size):
        return {
            "membership_size": size,
            "statistics_json_encode_calls": len(self.encodes),
            "statistics_json_encoded_bytes": sum(len(value.encode()) for _, value in self.encodes),
            "statistics_sql_bind_bytes": sum(
                len(value.encode()) for _, params in self.binds for value in params
            ),
            "membership_json_decoded_bytes": self.decoded_bytes,
            "statistics_vm_steps": self.vm_steps,
        }


@pytest.mark.parametrize("size", [128, 4096])
async def test_single_branch_statistics_reuse_captured_json(
    store, monkeypatch, record_property, size
):
    members = [f"{index + 1:032x}" for index in range(size)]
    raw = (await _seed(store, [members]))[0]
    meter = _StatsMeter(monkeypatch, {raw})

    detail = await svc.get_session("session", message_limit=200)

    record_property("statistics_work", json.dumps(meter.cost(size)))
    branch = detail["branches"][0]
    assert branch["message_total"] == size
    assert branch["message_stats"] == {
        "message_count": size,
        "roles": {"user": size // 2, "assistant": size // 2},
    }
    assert (branch["first_message_at"], branch["last_message_at"]) == (1.0, float(size))
    assert [stage for stage, _ in meter.binds] == ["roles", "bounds"]
    assert all(params == (raw,) for _, params in meter.binds)
    assert meter.decoded_bytes == len(raw.encode())
    assert all(count > size for count in meter.vm_steps.values())
    assert len(meter.encodes) == 0, meter.cost(size)


@pytest.mark.parametrize("kind", ["escaped", "unicode", "whitespace", "numeric"])
async def test_nontrivial_collections_keep_canonical_statistics_inputs(store, monkeypatch, kind):
    if kind == "escaped":
        members = [f"m{index}" for index in range(8)]
        raw = r'["m\u0030","m1","m2","m3","m4","m5","m6","m7"]'
        assert len(raw) <= sum(len(mid) + 4 for mid in members)
    elif kind == "unicode":
        members, raw = ["é"], '["é"]'
    elif kind == "whitespace":
        members, raw = ["m0"], '[          "m0"          ]'
    else:
        members, raw = [1, True, None], "[1,true,null]"
    await _seed(store, [members], raw_by_branch=[raw])
    canonical = json.dumps(members)
    meter = _StatsMeter(monkeypatch, {raw})

    detail = await svc.get_session("session")

    assert detail["branches"][0]["message_total"] == len(members)
    assert [stage for stage, _ in meter.binds] == ["roles", "bounds"]
    assert [encoded for _, encoded in meter.encodes] == [canonical, canonical]
    assert [params for _, params in meter.binds] == [(canonical,), (canonical,)]
    if kind == "numeric":
        assert detail["message_stats"]["message_count"] == 0
        assert detail["branches"][0]["first_message_at"] is None
        assert detail["branches"][0]["last_message_at"] is None


async def test_multiple_branches_keep_separate_canonical_statistics_inputs(store, monkeypatch):
    raw = await _seed(store, [["a"], ["b"]])
    meter = _StatsMeter(monkeypatch, set(raw))

    detail = await svc.get_session("session")

    assert [branch["message_total"] for branch in detail["branches"]] == [1, 1]
    assert detail["message_stats"]["message_count"] == 2
    assert [stage for stage, _ in meter.binds] == ["roles", "bounds", "roles", "bounds"]
    assert [encoded for _, encoded in meter.encodes] == [raw[0], raw[0], raw[1], raw[1]]


async def test_duplicate_and_pruned_members_preserve_exact_statistics(store, monkeypatch):
    members = ["a", "a", "pruned", "b"]
    raw = (await _seed(store, [members], rows=[("a", "assistant", 10.0), ("b", "user", 20.0)]))[0]
    meter = _StatsMeter(monkeypatch, {raw})

    detail = await svc.get_session("session")

    branch = detail["branches"][0]
    assert branch["message_total"] == 4
    assert branch["message_stats"] == {"message_count": 3, "roles": {"assistant": 2, "user": 1}}
    assert (branch["first_message_at"], branch["last_message_at"]) == (10.0, 20.0)
    assert meter.encodes == []
    assert [params for _, params in meter.binds] == [(raw,), (raw,)]


@pytest.mark.parametrize(
    "raw", [pytest.param("[]", id="empty"), pytest.param("{broken", id="malformed")]
)
async def test_empty_and_failed_decode_do_not_run_statistics_queries(store, monkeypatch, raw):
    await _seed(store, [[]], raw_by_branch=[raw])
    meter = _StatsMeter(monkeypatch, {raw})

    detail = await svc.get_session("session")

    branch = detail["branches"][0]
    assert branch["message_total"] == 0
    assert branch["message_stats"] == {"message_count": 0, "roles": {}}
    assert (branch["first_message_at"], branch["last_message_at"]) == (None, None)
    assert meter.encodes == [] and meter.binds == []


async def test_statistics_keep_membership_captured_before_awaits(store, monkeypatch):
    raw = (
        await _seed(store, [["old"]], rows=[("old", "assistant", 11.0), ("later", "user", 77.0)])
    )[0]
    meter = _StatsMeter(monkeypatch, {raw})
    action_reader = svc._fetch_action_messages
    mutation_finished = False

    async def change_membership_after_windows(*args, **kwargs):
        nonlocal mutation_finished
        result = await action_reader(*args, **kwargs)
        async with aiosqlite.connect(str(store)) as writer:
            await writer.execute(
                "UPDATE progressions SET collection = ? WHERE id = 'branch-0-pool'",
                (json.dumps(["later"]),),
            )
            await writer.commit()
        mutation_finished = True
        return result

    monkeypatch.setattr(svc, "_fetch_action_messages", change_membership_after_windows)

    detail = await svc.get_session("session")

    assert mutation_finished
    branch = detail["branches"][0]
    assert branch["message_stats"] == {"message_count": 1, "roles": {"assistant": 1}}
    assert (branch["first_message_at"], branch["last_message_at"]) == (11.0, 11.0)
    assert [params for _, params in meter.binds] == [(raw,), (raw,)]
    async with aiosqlite.connect(str(store)) as reader:
        row = await (
            await reader.execute("SELECT collection FROM progressions WHERE id = 'branch-0-pool'")
        ).fetchone()
    assert row[0] == json.dumps(["later"])


@pytest.mark.parametrize(
    "raw",
    [pytest.param('[{"a":1,"a":2}]', id="object"), pytest.param("[[1,2]]", id="array")],
)
async def test_nested_members_keep_the_existing_sql_binding_error(store, monkeypatch, raw):
    await _seed(store, [json.loads(raw)], raw_by_branch=[raw], rows=[])
    meter = _StatsMeter(monkeypatch, {raw})

    with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
        await svc.get_session("session")

    assert meter.binds == [] and meter.encodes == []


async def test_lone_surrogate_member_keeps_the_existing_encoding_error(store, monkeypatch):
    raw = r'["\ud800"]'
    await _seed(store, [json.loads(raw)], raw_by_branch=[raw], rows=[])
    meter = _StatsMeter(monkeypatch, {raw})

    with pytest.raises(UnicodeEncodeError):
        await svc.get_session("session")

    assert meter.binds == [] and meter.encodes == []
