# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Measure session-stream connection setup and persisted-history replay work."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from collections import Counter, defaultdict
from types import SimpleNamespace

import pytest


class ConnectionTrace:
    """Observe real connections and DB fetches without retaining SQLite cursors."""

    def __init__(self, monkeypatch, aiosqlite):
        self.phase = "warmup"
        self.opens = Counter()
        self.closes = Counter()
        self.queries = defaultdict(Counter)
        self.rows = defaultdict(Counter)
        self.connections = weakref.WeakValueDictionary()
        self.closing = set()
        self.closed = set()
        self.cursor_labels = {}
        self.next_id = 0
        connect = aiosqlite.connect

        def observed_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            self.next_id += 1
            token = self.next_id
            self.opens[self.phase] += 1
            self.connections[token] = connection
            execute = connection._execute
            close = connection.close

            async def observed_execute(fn, *values, **options):
                result = await execute(fn, *values, **options)
                name = getattr(fn, "__name__", "")
                if name == "execute" and values and isinstance(values[0], str):
                    label = (self.phase, self.kind(values[0]))
                    self.queries[label[0]][label[1]] += 1
                    self.cursor_labels[(token, id(result))] = label
                elif name in {"fetchone", "fetchmany", "fetchall"}:
                    label = self.cursor_labels.get((token, id(getattr(fn, "__self__", None))))
                    if label:
                        count = int(result is not None) if name == "fetchone" else len(result)
                        self.rows[label[0]][label[1]] += count
                return result

            async def observed_close():
                self.closing.add(token)
                try:
                    return await close()
                finally:
                    self.closing.discard(token)
                    if token not in self.closed:
                        self.closes[self.phase] += 1
                        self.closed.add(token)

            # Preserve the actual Connection's await/context-manager protocol.
            connection._execute = observed_execute
            connection.close = observed_close
            return connection

        monkeypatch.setattr(aiosqlite, "connect", observed_connect)

    @staticmethod
    def kind(sql):
        sql = " ".join(sql.lower().split())
        if sql.startswith("pragma "):
            return "pragma"
        if "sqlite_master" in sql and "session_signals" in sql:
            return "signal_capability"
        if "from session_signals" in sql and re.search(r"\bseq\s*>\s*\?", sql):
            # Confirmation returns an indicator, not a signal payload row.
            return "signal_confirmation" if sql.startswith("select 1") else "signal_tail"
        if ("join json_each(p.collection)" in sql and "where b.session_id" in sql) or (
            "from json_each(?) c join messages" in sql
        ):
            return "message_tail"
        if "p.collection" in sql and "from branches b" in sql:
            return "message_membership"
        if sql.startswith("select updated_at") and "status from sessions" in sql:
            return "terminal_state"
        if sql.startswith("select 1 from sessions"):
            return "admission"
        return "other"

    def metrics(self, phase):
        return {
            "opens": self.opens[phase],
            "closes": self.closes[phase],
            "queries": dict(self.queries[phase]),
            "fetched_rows": dict(self.rows[phase]),
            "closing_connections": sorted(self.closing),
            "active_transactions": {
                str(token): connection.in_transaction
                for token, connection in list(self.connections.items())
                if token not in self.closed and token not in self.closing
            },
        }


async def prove_trace(path, trace, aiosqlite):
    trace.phase = "positive_control"
    assert (
        trace.kind("SELECT 1 FROM session_signals WHERE session_id=? AND seq>? LIMIT 1")
        == "signal_confirmation"
    )
    assert (
        trace.kind("SELECT id FROM session_signals WHERE seq > ? ORDER BY seq LIMIT ?")
        == "signal_tail"
    )
    async with aiosqlite.connect(str(path)) as connection:
        row = await (await connection.execute("SELECT 1")).fetchone()
        assert row == (1,)
    observed = trace.metrics("positive_control")
    assert observed["opens"] == observed["closes"] == 1
    assert observed["queries"] == {"other": 1}
    assert observed["fetched_rows"] == {"other": 1}
    assert observed["closing_connections"] == []
    assert observed["active_transactions"] == {}
    trace.phase = "warmup"


class PollGate:
    """A release advances one actual generator past its real poll sleep."""

    def __init__(self):
        self.arrivals = asyncio.Queue()
        self.pending = set()

    async def sleep(self, _delay):
        future = asyncio.get_running_loop().create_future()
        self.pending.add(future)
        self.arrivals.put_nowait(future)
        try:
            await future
        finally:
            self.pending.discard(future)

    async def barrier(self, size):
        return [await asyncio.wait_for(self.arrivals.get(), 10) for _ in range(size)]

    @staticmethod
    def release(futures):
        for future in futures:
            if not future.done():
                future.set_result(None)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    import aiosqlite

    import lionagi.state.db as db_mod
    from lionagi.studio.services import sessions, signals

    path = tmp_path / "stream.sqlite3"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("LIONAGI_STATE_DB_URL", url)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url})
    )
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    assert db_mod.state_db_file() == path
    return path, url, sessions, signals, aiosqlite


async def seed_history(backend, history, *, terminal):
    from lionagi.state.db import StateDB

    path, url, _sessions, _signals, aiosqlite = backend
    ids = [f"{index:032x}" for index in range(1, history + 1)]
    async with StateDB(url=url) as db:
        await db.create_progression("session-pool")
        await db.create_progression("branch-pool")
        await db.create_session(
            {
                "id": "session",
                "progression_id": "session-pool",
                "created_at": 100.0,
                "updated_at": 100.0,
                "name": "stream",
                "status": "running",
            }
        )
        await db.create_branch(
            {
                "id": "branch",
                "session_id": "session",
                "progression_id": "branch-pool",
                "created_at": 100.0,
                "name": "worker",
            }
        )
        if ids:
            await db.insert_message(
                {
                    "id": ids[0],
                    "created_at": 1001.0,
                    "role": "assistant",
                    "content": {"text": "message"},
                    "node_metadata": {},
                }
            )
        if terminal:
            await db.update_status(
                "session", "session", new_status="completed", reason_code="run.completed.ok"
            )
    # Bulk owned-fixture seeding, not a claim about production writer throughput.
    async with aiosqlite.connect(str(path)) as writer:
        if ids:
            row = await (
                await writer.execute("SELECT lion_class FROM messages WHERE id = ?", (ids[0],))
            ).fetchone()
            await writer.executemany(
                "INSERT INTO messages(id, created_at, content, role, lion_class) "
                "VALUES(?, ?, ?, 'assistant', ?)",
                (
                    (mid, 1000.0 + index, '{"text":"message"}', row[0])
                    for index, mid in enumerate(ids[1:], 2)
                ),
            )
        collection = json.dumps(ids)
        await writer.execute("UPDATE progressions SET collection = ?", (collection,))
        await writer.executemany(
            "INSERT INTO session_signals(id, session_id, seq, kind, op_id, ts, payload) "
            "VALUES(?, 'session', ?, 'NodeStarted', ?, ?, ?)",
            (
                (
                    f"{history + index:032x}",
                    index,
                    f"step-{index % 3}",
                    1000.0 + index,
                    json.dumps({"name": f"step-{index % 3}"}),
                )
                for index in range(1, history + 1)
            ),
        )
        if terminal:
            await writer.execute("UPDATE sessions SET updated_at = 1 WHERE id = 'session'")
        await writer.commit()


def events(frame):
    text = frame.decode() if isinstance(frame, bytes) else str(frame)
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def drain(iterator, *, max_events):
    counts = Counter()
    async for frame in iterator:
        for event in events(frame):
            if event.get("type") == "done":
                counts["done"] += 1
                return dict(counts)
            if "seq" in event:
                counts["signal"] += 1
            elif "id" in event and "branch_id" in event:
                counts["message"] += 1
            else:
                counts["control"] += 1
            assert sum(counts.values()) <= max_events, "unexpected unbounded stream"
    raise AssertionError("stream ended without done")


async def close_readers(tasks, iterators):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for iterator in iterators:
        await iterator.aclose()


@pytest.mark.parametrize("viewers", [1, 20])
async def test_idle_tail_work_does_not_multiply_by_viewers(
    backend, monkeypatch, record_property, viewers
):
    """Idle polling work stays independent of the number of subscribed viewers."""
    await seed_history(backend, 1, terminal=False)
    path, _url, sessions, _signals, aiosqlite = backend
    from starlette.requests import Request

    from lionagi.studio.services import _session_tail

    registry = _session_tail.SessionTailRegistry(str(path))
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(session_tail_registry=registry)),
        }
    )
    trace, gate = ConnectionTrace(monkeypatch, aiosqlite), PollGate()
    await prove_trace(path, trace, aiosqlite)
    monkeypatch.setattr(_session_tail, "sleep", gate.sleep)
    iterators, tasks = [], []
    try:
        for _ in range(viewers):
            detail = await sessions.get_session(
                "session", live_snapshot=True, tail_registry=registry
            )
            mark = detail["stream_snapshot"]
            for response in [
                await sessions.stream_session_route(
                    "session", cursor=mark["message_cursor"], mode="live", request=request
                ),
                await sessions.stream_signals(
                    "session",
                    after_seq=mark["signal_seq"],
                    snapshot=mark["message_cursor"],
                    mode="live",
                    request=request,
                ),
            ]:
                iterator = response.body_iterator
                iterators.append(iterator)
                tasks.append(asyncio.create_task(drain(iterator, max_events=100)))
        waiting = await gate.barrier(1)
        trace.phase = "steady"
        for _ in range(3):
            gate.release(waiting)
            waiting = await gate.barrier(1)
        metrics = trace.metrics("steady")
        record_property("idle_poll_work", json.dumps({"viewers": viewers, "ticks": 3, **metrics}))
        q = metrics["queries"]
        assert q.get("message_membership", 0) == 3 and q.get("signal_tail", 0) == 3
        violations = {
            "queries_scale_with_viewers": sum(
                q.get(name, 0) for name in ["message_membership", "signal_tail", "terminal_state"]
            )
            > 3 * 3,
            "new_connections_each_tick": metrics["opens"] != 0,
            "capability_reprobed_each_tick": q.get("signal_capability", 0) != 0,
            "held_read_transaction": any(metrics["active_transactions"].values()),
        }
        assert not any(violations.values()), {"violations": violations, "actual": metrics}
    finally:
        await close_readers(tasks, iterators)
        await registry.aclose()
        assert not trace.closing
        assert trace.next_id == len(trace.closed)


@pytest.mark.parametrize("history", [100, 100_000])
async def test_legacy_cursorless_replay_counts_are_explicit(
    backend, monkeypatch, record_property, history
):
    """Cursorless clients retain complete replay while its cost is recorded."""
    await seed_history(backend, history, terminal=True)
    path, _url, sessions, _signals, aiosqlite = backend
    trace = ConnectionTrace(monkeypatch, aiosqlite)
    await prove_trace(path, trace, aiosqlite)
    trace.phase = "detail"
    detail = await sessions.get_session("session", message_limit=200)
    assert detail["branches"][0]["message_total"] == history
    trace.phase = "legacy_replay"
    responses = [
        await sessions.stream_session_route("session", cursor=None),
        await sessions.stream_signals("session", after_seq=0),
    ]
    iterators = [response.body_iterator for response in responses]
    tasks = []
    try:
        for iterator in iterators:
            tasks.append(asyncio.create_task(drain(iterator, max_events=history + 10)))
        result = await asyncio.wait_for(asyncio.gather(*tasks), 150)
        record_property(
            "legacy_replay_work",
            json.dumps(
                {
                    "history": history,
                    "frames": result,
                    "detail": trace.metrics("detail"),
                    "stream": trace.metrics("legacy_replay"),
                }
            ),
        )
        assert result == [{"message": history, "done": 1}, {"signal": history, "done": 1}]
    finally:
        await close_readers(tasks, iterators)
    # The same persisted histories also exercise the new default detail/live handoff.
    from starlette.requests import Request

    from lionagi.studio.services import _session_tail

    registry = _session_tail.SessionTailRegistry(str(path))
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(session_tail_registry=registry)),
        }
    )
    trace.phase = "live_detail"
    detail = await sessions.get_session(
        "session", message_limit=200, live_snapshot=True, tail_registry=registry
    )
    mark = detail["stream_snapshot"]
    assert detail["branches"][0]["message_total"] == history
    assert mark["signal_seq"] == history
    trace.phase = "explicit_signal_prefix"
    after, count = 0, 0
    while True:
        page = await sessions.session_signals_page(
            "session", after_seq=after, through_seq=mark["signal_seq"], limit=500
        )
        assert len(page["signals"]) <= 500
        count += len(page["signals"])
        after = page["next_after_seq"]
        if not page["has_more"]:
            break
    assert count == history
    trace.phase = "live_handoff"
    iterators, tasks = [], []
    try:
        for response in [
            await sessions.stream_session_route(
                "session", cursor=mark["message_cursor"], mode="live", request=request
            ),
            await sessions.stream_signals(
                "session",
                after_seq=mark["signal_seq"],
                snapshot=mark["message_cursor"],
                mode="live",
                request=request,
            ),
        ]:
            iterator = response.body_iterator
            iterators.append(iterator)
            tasks.append(asyncio.create_task(drain(iterator, max_events=10)))
        result = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        metrics = trace.metrics("live_handoff")
        record_property(
            "live_handoff_work",
            json.dumps(
                {
                    "history": history,
                    "frames": result,
                    "detail": trace.metrics("live_detail"),
                    "explicit_signal_prefix": trace.metrics("explicit_signal_prefix"),
                    "stream": metrics,
                }
            ),
        )
        assert result == [{"done": 1}, {"done": 1}]
        assert metrics["fetched_rows"].get("message_tail", 0) == 0
        assert metrics["fetched_rows"].get("signal_tail", 0) == 0
    finally:
        await close_readers(tasks, iterators)
        await registry.aclose()
        assert not trace.closing
        assert trace.next_id == len(trace.closed)
