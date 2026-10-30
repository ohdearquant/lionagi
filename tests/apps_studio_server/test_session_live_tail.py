# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression coverage for snapshot-bound session live delivery."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(loop_scope="function")
async def live_backend(tmp_path, monkeypatch):
    import aiosqlite

    import lionagi.state.db as db_mod
    from lionagi.studio.services import _session_tail, sessions, signals

    path = tmp_path / "live.sqlite3"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("LIONAGI_STATE_DB_URL", url)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url})
    )
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    registry = _session_tail.SessionTailRegistry(str(path))
    yield registry, path, url, sessions, signals, _session_tail, aiosqlite
    await registry.aclose()


async def seed(live_backend, count=0, *, terminal=False):
    from lionagi.state.db import StateDB

    _registry, path, url, _sessions, _signals, _tail, aiosqlite = live_backend
    ids = [f"{index:032x}" for index in range(1, count + 1)]
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
            }
        )
        if ids:
            await db.insert_message(
                {
                    "id": ids[0],
                    "created_at": 1001.0,
                    "role": "assistant",
                    "content": {"text": "message"},
                }
            )
        if terminal:
            await db.update_status(
                "session", "session", new_status="completed", reason_code="run.completed.ok"
            )
    async with aiosqlite.connect(str(path)) as writer:
        if ids:
            cursor = await writer.execute("SELECT lion_class FROM messages WHERE id=?", (ids[0],))
            lion_class = (await cursor.fetchone())[0]
            await cursor.close()
            await writer.executemany(
                "INSERT INTO messages(id,created_at,content,role,lion_class) VALUES(?,?,?,'assistant',?)",
                (
                    (mid, 1000.0 + index, '{"text":"message"}', lion_class)
                    for index, mid in enumerate(ids[1:], 2)
                ),
            )
        await writer.execute(
            "UPDATE progressions SET collection=? WHERE id='branch-pool'", (json.dumps(ids),)
        )
        await writer.executemany(
            "INSERT INTO session_signals(id,session_id,seq,kind,op_id,ts,payload) "
            "VALUES(?,'session',?,'NodeStarted',?,?,'{}')",
            ((f"signal-{i}", i, f"op-{i}", 1000.0 + i) for i in range(1, count + 1)),
        )
        if terminal:
            await writer.execute("UPDATE sessions SET updated_at=1 WHERE id='session'")
        await writer.commit()
    return ids


async def append(live_backend, ids, *, timestamp=1.0, progression="branch-pool"):
    from lionagi.state.db import StateDB

    async with StateDB(url=live_backend[2]) as db:
        await db.insert_messages(
            [
                {"id": mid, "created_at": timestamp, "role": "assistant", "content": {"text": mid}}
                for mid in ids
            ],
            progression_ids=(progression,),
            session_id="session",
            activity_at=100.0,
        )


async def bootstrap(live_backend):
    registry, _path, _url, sessions, *_ = live_backend
    result = await sessions.get_session("session", live_snapshot=True, tail_registry=registry)
    return result, result["stream_snapshot"]


async def data(iterator):
    while True:
        frame = await asyncio.wait_for(iterator.__anext__(), 3)
        value = next(
            json.loads(line[6:]) for line in frame.splitlines() if line.startswith("data: ")
        )
        if value.get("type") != "heartbeat":
            return frame, value


async def test_live_bootstrap_is_optional_and_creates_no_reader(live_backend):
    await seed(live_backend)
    registry, *_ = live_backend
    sessions = live_backend[3]
    assert "stream_snapshot" not in await sessions.get_session("session")
    _detail, mark = await bootstrap(live_backend)
    assert set(mark) == {"version", "message_cursor", "signal_seq"}
    assert mark["version"] == 1 and mark["signal_seq"] == 0
    assert mark["message_cursor"]
    assert not registry.tails
    page = await sessions.session_signals_page("session", through_seq=0, after_seq=0, limit=500)
    assert page == {"signals": [], "next_after_seq": 0, "through_seq": 0, "has_more": False}


async def test_same_snapshot_marks_and_backdated_handoff(live_backend, monkeypatch):
    from lionagi.state.db import StateDB

    await seed(live_backend, 1)
    registry, _path, url, sessions, *_ = live_backend
    original = sessions._fetch_message_bounds
    inserted = False

    async def interleave(db, ids, **kwargs):
        nonlocal inserted
        result = await original(db, ids, **kwargs)
        if not inserted:
            inserted = True
            await append(live_backend, ["late-lower-id"], timestamp=1.0)
            async with StateDB(url=url) as writer:
                await writer.insert_session_signal(
                    session_id="session", kind="NodeCompleted", op_id="late", ts=1.0, payload={}
                )
        return result

    monkeypatch.setattr(sessions, "_fetch_message_bounds", interleave)
    detail, mark = await bootstrap(live_backend)
    assert detail["branches"][0]["message_total"] == 1
    assert mark["signal_seq"] == 1
    messages = await registry.subscribe("session", "messages", mark["message_cursor"])
    signals = await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=1)
    miter, siter = messages.frames(), signals.frames()
    try:
        assert (await data(miter))[1]["id"] == "late-lower-id"
        assert (await data(siter))[1]["seq"] == 2
    finally:
        await miter.aclose()
        await siter.aclose()


async def test_signal_pages_pin_upper_cut_and_preserve_legacy_after_semantics(live_backend):
    from lionagi.state.db import StateDB

    await seed(live_backend, 501)
    _detail, mark = await bootstrap(live_backend)
    async with StateDB(url=live_backend[2]) as writer:
        await writer.insert_session_signal(
            session_id="session", kind="NodeCompleted", op_id="later", ts=1.0, payload={}
        )
    sessions, signals = live_backend[3:5]
    after, seen = 0, []
    while True:
        page = await sessions.session_signals_page(
            "session", through_seq=mark["signal_seq"], after_seq=after, limit=113
        )
        assert len(page["signals"]) <= 113
        seen.extend(row["seq"] for row in page["signals"])
        after = page["next_after_seq"]
        if not page["has_more"]:
            break
    # The client rejects a last page that stops below the cut, so the server's last page ends at it.
    assert after == mark["signal_seq"] == 501
    assert seen == list(range(1, 502))
    assert (await signals.get_signals_after("session", 501))[0]["seq"] == 502


async def test_empty_cut_observes_a_new_branch_without_replay(live_backend):
    from lionagi.state.db import StateDB

    await seed(live_backend)
    _detail, mark = await bootstrap(live_backend)
    registry = live_backend[0]
    sub = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = sub.frames()
    try:
        async with StateDB(url=live_backend[2]) as writer:
            await writer.create_progression("new-pool")
            await writer.create_branch(
                {
                    "id": "new-branch",
                    "session_id": "session",
                    "progression_id": "new-pool",
                    "created_at": 100.0,
                }
            )
        await append(live_backend, ["new-id"], progression="new-pool")
        value = (await data(iterator))[1]
        assert (value["id"], value["branch_id"]) == ("new-id", "new-branch")
    finally:
        await iterator.aclose()


@pytest.mark.parametrize(
    "change,reason", [(["1"], "frontier_shrunk"), (["1", "2", "replaced"], "anchor_mismatch")]
)
async def test_frontier_changes_explicitly_resync(live_backend, change, reason):
    await seed(live_backend, 3)
    _detail, mark = await bootstrap(live_backend)
    registry, path, *_ = live_backend
    sub = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = sub.frames()
    try:
        async with live_backend[6].connect(str(path)) as writer:
            await writer.execute(
                "UPDATE progressions SET collection=? WHERE id='branch-pool'", (json.dumps(change),)
            )
            await writer.commit()
        value = (await data(iterator))[1]
        assert value == {
            "type": "resync_required",
            "reason": reason,
            "stream": "messages",
            "resume_cursor": mark["message_cursor"],
        }
    finally:
        await iterator.aclose()


async def test_tied_partial_reconnect_uses_the_retained_suffix(live_backend):
    await seed(live_backend)
    _detail, mark = await bootstrap(live_backend)
    registry = live_backend[0]
    first = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = first.frames()
    await append(live_backend, ["z", "a", "m"], timestamp=1.0)
    frame, value = await data(iterator)
    assert value["id"] == "z"
    cursor = next(line[4:] for line in frame.splitlines() if line.startswith("id: "))
    await iterator.aclose()
    resumed = await registry.subscribe("session", "messages", cursor)
    suffix = resumed.frames()
    try:
        assert [(await data(suffix))[1]["id"] for _ in range(2)] == ["a", "m"]
    finally:
        await suffix.aclose()


async def test_slow_viewer_overflow_does_not_block_fast_viewer(live_backend, monkeypatch):
    await seed(live_backend)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    monkeypatch.setattr(tail, "TAIL_BATCH_ROWS", 1)
    monkeypatch.setattr(tail, "MAX_QUEUE_ROWS", 2)
    _detail, mark = await bootstrap(live_backend)
    slow = await registry.subscribe("session", "messages", mark["message_cursor"])
    fast = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = fast.frames()

    async def consume():
        return [(await data(iterator))[1]["id"] for _ in range(8)]

    reader = asyncio.create_task(consume())
    try:
        await append(live_backend, [f"post-{i}" for i in range(8)])
        assert await asyncio.wait_for(reader, 5) == [f"post-{i}" for i in range(8)]
        assert slow.control is not None and not slow.queue and slow.queued_bytes == 0
        control = next(
            json.loads(line[6:]) for line in slow.control.splitlines() if line.startswith("data: ")
        )
        assert control["reason"] == "overflow"
        assert control["resume_cursor"] == mark["message_cursor"]
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await iterator.aclose()
        await registry.unsubscribe(slow)
        assert not fast.owner.subscribers and fast.owner.task is None and fast.owner.db is None


@pytest.mark.parametrize("viewers", [1, 20])
async def test_shared_resource_has_no_transaction_or_capability_probe_at_idle(
    live_backend, monkeypatch, viewers
):
    await seed(live_backend, 100)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    opened, closed, statements = [], [], []
    waits = asyncio.Queue()
    original = tail.open_db
    original_collections = tail._Tail._collections
    original_validate = tail._validate
    work = {"collections": 0, "validations": 0}

    async def collections(owner):
        work["collections"] += 1
        return await original_collections(owner)

    def validate(frontiers, current):
        work["validations"] += 1
        return original_validate(frontiers, current)

    @asynccontextmanager
    async def counted(path):
        async with original(path) as db:
            opened.append(db)
            await db.set_trace_callback(statements.append)
            try:
                yield db
            finally:
                closed.append(db)

    async def poll_wait(seconds):
        assert seconds == tail.POLL_SECONDS
        future = asyncio.get_running_loop().create_future()
        waits.put_nowait(future)
        await future

    monkeypatch.setattr(tail, "open_db", counted)
    monkeypatch.setattr(tail._Tail, "_collections", collections)
    monkeypatch.setattr(tail, "_validate", validate)
    monkeypatch.setattr(tail, "sleep", poll_wait)
    _detail, mark = await bootstrap(live_backend)
    subs = []
    for _ in range(viewers):
        subs.append(await registry.subscribe("session", "messages", mark["message_cursor"]))
        subs.append(
            await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=100)
        )
    pending = await asyncio.wait_for(waits.get(), 3)
    owner = subs[0].owner
    assert all(sub.owner is owner for sub in subs)
    assert len(opened) == 1 and opened[0] is owner.db
    assert not owner.db.in_transaction
    statements.clear()
    baseline = dict(work)
    for _ in range(3):
        pending.set_result(None)
        pending = await asyncio.wait_for(waits.get(), 3)
        assert not owner.db.in_transaction
    assert work == baseline
    assert not any(sql.lstrip().upper().startswith(("BEGIN", "SELECT")) for sql in statements)
    assert len(opened) == 1 and not closed
    assert not any("sqlite_master" in sql for sql in statements)
    for sub in subs:
        await registry.unsubscribe(sub)
    assert owner.task is None and owner.db is None and len(closed) == 1


async def test_final_same_connection_fence_retries_a_commit_before_decision(
    live_backend, monkeypatch
):
    from lionagi.state.db import StateDB

    await seed(live_backend, terminal=True)
    registry, _path, url, _sessions, _signals, tail, _sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    original = tail._Tail._scalar
    observations = 0

    async def interleave(owner, query):
        nonlocal observations
        if query == "PRAGMA data_version":
            observations += 1
            if observations == 4:
                assert not owner.db.in_transaction
                async with StateDB(url=url) as writer:
                    await writer.insert_session_signal(
                        session_id="session",
                        kind="NodeCompleted",
                        op_id="fenced",
                        ts=1.0,
                        payload={},
                    )
        return await original(owner, query)

    monkeypatch.setattr(tail._Tail, "_scalar", interleave)
    sub = await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=0)
    iterator = sub.frames()
    try:
        assert (await data(iterator))[1]["seq"] == 1
        assert (await data(iterator))[1] == {"type": "done"}
    finally:
        await iterator.aclose()


async def test_foreign_epoch_and_expired_bootstrap_are_explicit(live_backend, monkeypatch):
    await seed(live_backend)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    other = tail.SessionTailRegistry(registry.path)
    try:
        with pytest.raises(tail.StreamResyncError, match="foreign_epoch"):
            await other.subscribe("session", "messages", mark["message_cursor"])
        monkeypatch.setattr(tail, "IDLE_SECONDS", -1)
        with pytest.raises(tail.StreamResyncError, match="expired"):
            await registry.subscribe("session", "messages", mark["message_cursor"])
    finally:
        await other.aclose()


async def test_one_oversized_frame_has_a_reserved_control_path(live_backend, monkeypatch):
    await seed(live_backend)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = sub.frames()
    monkeypatch.setattr(tail, "MAX_QUEUE_BYTES", 1)
    try:
        await append(live_backend, ["large-envelope"])
        value = (await data(iterator))[1]
        assert value.get("type") == "resync_required" and value.get("reason") == "resource_limit"
        assert value["resume_cursor"] == mark["message_cursor"]
        assert not sub.queue and sub.queued_bytes == 0
    finally:
        await iterator.aclose()
        assert not sub.owner.subscribers and sub.owner.task is None and sub.owner.db is None


@pytest.mark.parametrize(
    "channel,continuation",
    [
        ("messages", False),
        ("messages", True),
        ("signals", True),
    ],
)
async def test_legacy_frames_match_the_frozen_base_bytes(
    live_backend, monkeypatch, channel, continuation
):
    """Compare actual legacy frames with the frozen generators and signal reader."""
    ids = await seed(live_backend, 3, terminal=True)
    sessions, signals = live_backend[3:5]
    fixture = Path(__file__).parents[1] / "fixtures/studio/session_legacy_stream_reference.json"
    reference = json.loads(fixture.read_text())
    assert reference["source_revision"] == "6adc5127ff56256ce17555c366ecd0161ce3881d"
    for name, expected in reference["unchanged_helpers"].items():
        node = ast.parse(inspect.getsource(getattr(sessions, name))).body[0]
        expected_node = ast.parse(expected).body[0]
        assert ast.dump(node, include_attributes=False) == ast.dump(
            expected_node, include_attributes=False
        ), name

    def frozen(name, module):
        namespace = dict(vars(module))
        # The checked-in fixture contains only immutable product functions.
        code = compile(reference["functions"][name], str(fixture), "exec")
        exec(code, namespace)  # noqa: S102
        return namespace[name]

    async def collect(response):
        iterator = response.body_iterator
        try:
            frames = []
            async for frame in iterator:
                frames.append(frame.encode() if isinstance(frame, str) else frame)
            return frames
        finally:
            await iterator.aclose()

    if channel == "messages":
        name = "stream_session_route"
        cursor = sessions._encode_session_stream_cursor("session", 1001.0, ids[0], "branch")
        params = {"cursor": cursor if continuation else None}
    else:
        name = "stream_signals"
        params = {"after_seq": 1 if continuation else 0}
    base_route = frozen(name, sessions)
    with monkeypatch.context() as context:
        context.setattr(signals, "get_signals_after", frozen("get_signals_after", signals))
        expected = await asyncio.wait_for(collect(await base_route("session", **params)), 5)
    actual = await asyncio.wait_for(collect(await getattr(sessions, name)("session", **params)), 5)
    assert expected and expected[-1] == b'data: {"type":"done"}\n\n'
    assert len(expected) == (3 if continuation else 4)
    assert actual == expected


@pytest_asyncio.fixture(loop_scope="function")
async def live_http(live_backend, monkeypatch):
    import httpx

    from lionagi.studio.app import app

    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(app.state, "session_tail_registry", live_backend[0], raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        yield client


async def test_http_detail_prefix_and_both_live_channels_without_history_replay(
    live_backend, live_http
):
    await seed(live_backend, 3, terminal=True)
    response = await live_http.get("/api/sessions/session", params={"live_snapshot": "true"})
    assert response.status_code == 200
    snapshot = response.json()["stream_snapshot"]
    assert snapshot["signal_seq"] == 3
    page = await live_http.get(
        "/api/sessions/session/signals/page", params={"through_seq": 3, "limit": 2}
    )
    assert page.status_code == 200
    assert [row["seq"] for row in page.json()["signals"]] == [1, 2]
    assert page.json()["has_more"] is True
    assert page.json()["next_after_seq"] == 2
    messages = await asyncio.wait_for(
        live_http.get(
            "/api/sessions/session/stream",
            params={"mode": "live", "cursor": snapshot["message_cursor"]},
        ),
        timeout=10,
    )
    signals = await asyncio.wait_for(
        live_http.get(
            "/api/sessions/session/signals",
            params={"mode": "live", "snapshot": snapshot["message_cursor"], "after_seq": 3},
        ),
        timeout=10,
    )
    for result in (messages, signals):
        assert result.status_code == 200
        assert result.text == 'data: {"type":"done"}\n\n'
    tail = live_backend[0].tails["session"]
    assert not tail.subscribers and tail.task is None and tail.db is None


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"through_seq": -1},
        {"through_seq": 1, "after_seq": 2},
        {"through_seq": 1, "after_seq": -1},
        {"through_seq": 1, "limit": 0},
        {"through_seq": 1, "limit": 501},
    ],
)
async def test_signal_prefix_http_refuses_invalid_bounds(live_backend, live_http, params):
    await seed(live_backend)
    response = await live_http.get("/api/sessions/session/signals/page", params=params)
    assert response.status_code == 422
    assert not live_backend[0].tails


@pytest.mark.parametrize(
    "path,params,status",
    [
        ("/api/sessions/missing/signals/page", {"through_seq": 0}, 404),
        ("/api/sessions/session/stream", {"mode": "unknown"}, 400),
        ("/api/sessions/session/stream", {"cursor": "invalid-v1"}, 400),
        ("/api/sessions/session/stream", {"mode": "live"}, 409),
        ("/api/sessions/session/signals", {"mode": "live"}, 409),
        ("/api/sessions/session/signals", {"mode": "live", "snapshot": "unknown"}, 409),
    ],
)
async def test_live_http_misses_never_fall_back_to_replay(
    live_backend, live_http, path, params, status
):
    await seed(live_backend)
    response = await live_http.get(path, params=params)
    assert response.status_code == status
    if status == 409:
        assert response.json()["detail"]["code"] == "stream_resync_required"
    assert not live_backend[0].tails


async def test_signal_prefix_http_uses_the_existing_bearer_boundary(
    live_backend, live_http, monkeypatch
):
    await seed(live_backend)
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-only-token")
    path = "/api/sessions/session/signals/page?through_seq=0"
    assert (await live_http.get(path)).status_code == 401
    assert (await live_http.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (
        await live_http.get(path, headers={"Authorization": "Bearer test-only-token"})
    ).status_code == 200


@pytest.mark.parametrize(
    ("path", "admitted"),
    [
        ("/api/sessions/session?live_snapshot=true", 200),
        ("/api/sessions/session/stream?mode=live", 409),
        ("/api/sessions/session/signals?mode=live", 409),
    ],
)
async def test_live_modes_use_the_existing_bearer_boundary(
    live_backend, live_http, monkeypatch, path, admitted
):
    # The bearer gate reads the path, never the query, so a live read is refused as
    # its plain form is; the admitted status is the handler's own answer.
    await seed(live_backend)
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-only-token")
    assert (await live_http.get(path)).status_code == 401
    assert (await live_http.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
    response = await live_http.get(path, headers={"Authorization": "Bearer test-only-token"})
    assert response.status_code == admitted


async def test_signal_prefix_http_refuses_non_file_store(live_backend, live_http, monkeypatch):
    import lionagi.state.db as db_mod

    await seed(live_backend)
    url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("LIONAGI_STATE_DB_URL", url)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url})
    )
    response = await live_http.get("/api/sessions/session/signals/page?through_seq=0")
    assert response.status_code == 501
    assert "in-memory sqlite" in response.json()["detail"]
