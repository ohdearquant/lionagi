# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Admission, retained-cursor, accounting and response-lifetime regressions."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.apps_studio_server.test_session_live_tail import append, bootstrap, data, seed
from tests.apps_studio_server.test_session_live_tail import live_backend as live_backend
from tests.apps_studio_server.test_session_live_tail import live_http as live_http


async def test_active_admission_rejects_changed_frontier_before_retained_data(
    live_backend, monkeypatch
):
    await seed(live_backend)
    registry, path, _url, _sessions, _signals, tail, sqlite = live_backend
    waits = asyncio.Queue()

    async def poll_wait(seconds):
        if seconds == 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        waits.put_nowait(future)
        await future

    monkeypatch.setattr(tail, "sleep", poll_wait)
    _detail, mark = await bootstrap(live_backend)
    original = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = original.frames()
    try:
        pending = await asyncio.wait_for(waits.get(), 3)
        await append(live_backend, ["one", "two"])
        pending.set_result(None)
        assert [(await data(iterator))[1]["id"] for _ in range(2)] == ["one", "two"]
        await asyncio.wait_for(waits.get(), 3)
        assert original.owner.task is not None and len(original.owner.log) == 2
        async with sqlite.connect(str(path)) as writer:
            await writer.execute("UPDATE progressions SET collection='[]' WHERE id='branch-pool'")
            await writer.commit()
        with pytest.raises(tail.StreamResyncError, match="frontier_shrunk"):
            await registry.subscribe("session", "messages", mark["message_cursor"])
        assert original.owner.subscribers == {original}
    finally:
        await iterator.aclose()


async def test_live_signal_admission_rejects_a_cut_no_longer_in_the_store(
    live_backend, monkeypatch
):
    await seed(live_backend, 1)
    registry, path, _url, _sessions, _signals, tail, sqlite = live_backend
    waiting = asyncio.Event()

    async def poll_wait(seconds):
        if seconds == 0:
            await asyncio.sleep(0)
            return
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(tail, "sleep", poll_wait)
    _detail, mark = await bootstrap(live_backend)
    first = await registry.subscribe("session", "messages", mark["message_cursor"])
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        async with sqlite.connect(str(path)) as writer:
            await writer.execute("DELETE FROM session_signals WHERE session_id='session'")
            await writer.commit()
        with pytest.raises(tail.StreamResyncError, match="frontier_unavailable"):
            await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=1)
    finally:
        await registry.unsubscribe(first)


async def test_admission_queries_wait_for_the_active_drain_transaction(live_backend, monkeypatch):
    await seed(live_backend)
    registry, *_rest = live_backend
    tail = live_backend[5]
    _detail, mark = await bootstrap(live_backend)
    first = await registry.subscribe("session", "messages", mark["message_cursor"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = tail._Tail._collections
    blocked = False

    async def collections(owner):
        nonlocal blocked
        if asyncio.current_task() is owner.task and not blocked:
            blocked = True
            assert owner.db.in_transaction
            entered.set()
            await release.wait()
        return await original(owner)

    monkeypatch.setattr(tail._Tail, "_collections", collections)
    pending = None
    try:
        # An idle drain deliberately skips collection reads until a commit.
        await append(live_backend, ["drain-before-admission"])
        await asyncio.wait_for(entered.wait(), 3)
        pending = asyncio.create_task(
            registry.subscribe("session", "signals", mark["message_cursor"], after_seq=0)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        second = await asyncio.wait_for(pending, 3)
        assert second.owner is first.owner and second.ready
        assert not second.owner.db.in_transaction
    finally:
        release.set()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            results = await asyncio.gather(pending, return_exceptions=True)
            for result in results:
                if hasattr(result, "owner"):
                    await registry.unsubscribe(result)
        await registry.unsubscribe(first)


async def test_indexed_cursor_cannot_alias_a_replacement_tail_generation(live_backend):
    await seed(live_backend)
    registry, path, _url, _sessions, _signals, tail, sqlite = live_backend
    _detail, old = await bootstrap(live_backend)
    first = await registry.subscribe("session", "messages", old["message_cursor"])
    iterator = first.frames()
    await append(live_backend, ["first", "second"])
    frame, _ = await data(iterator)
    cursor = next(line[4:] for line in frame.splitlines() if line.startswith("id: "))
    try:
        async with sqlite.connect(str(path)) as writer:
            await writer.execute("ALTER TABLE session_signals ADD COLUMN lifetime_test_column TEXT")
            await writer.commit()
        control = None
        for _ in range(3):
            value = (await data(iterator))[1]
            if value.get("type") == "resync_required":
                control = value
                break
        assert control is not None and control["reason"] == "frontier_unavailable"
    finally:
        await iterator.aclose()
    _detail, fresh = await bootstrap(live_backend)
    replacement = await registry.subscribe("session", "messages", fresh["message_cursor"])
    suffix = replacement.frames()
    try:
        await append(live_backend, ["third", "fourth"])
        assert [(await data(suffix))[1]["id"] for _ in range(2)] == ["third", "fourth"]
        assert old["message_cursor"] in registry.bootstraps
        assert replacement.owner.generation != first.owner.generation
        assert replacement.owner.message_floor == 0 and replacement.owner.message_index == 2
        with pytest.raises(tail.StreamResyncError, match="frontier_unavailable"):
            await registry.subscribe("session", "messages", cursor)
    finally:
        await suffix.aclose()


async def test_bootstrap_metadata_consumes_the_per_session_budget(live_backend, monkeypatch):
    await seed(live_backend)
    registry, *_rest = live_backend
    tail = live_backend[5]
    await bootstrap(live_backend)
    before = registry.retained_bytes
    monkeypatch.setattr(tail, "MAX_RETAINED_BYTES", before + 1)
    assert before < tail.MAX_RETAINED_BYTES < tail.MAX_TOTAL_RETAINED_BYTES
    with pytest.raises(tail.StreamResyncError, match="resource_limit"):
        await bootstrap(live_backend)
    assert registry.retained_bytes == before and len(registry.bootstraps) == 1


async def test_tail_frontier_metadata_consumes_the_per_session_budget(live_backend, monkeypatch):
    await seed(live_backend)
    registry, *_rest = live_backend
    tail = live_backend[5]
    _detail, mark = await bootstrap(live_backend)
    before = registry.retained_bytes
    monkeypatch.setattr(tail, "MAX_RETAINED_BYTES", before + 256)
    with pytest.raises(tail.StreamResyncError, match="resource_limit"):
        await registry.subscribe("session", "messages", mark["message_cursor"])
    assert not registry.tails and registry.retained_bytes == before


async def test_retention_pressure_releases_lagging_holders_not_a_healthy_viewer(
    live_backend, monkeypatch
):
    await seed(live_backend)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    monkeypatch.setattr(tail, "TAIL_BATCH_ROWS", 1)
    _detail, mark = await bootstrap(live_backend)
    slow = await registry.subscribe("session", "messages", mark["message_cursor"])
    fast = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = fast.frames()
    reader = None
    try:
        await append(live_backend, ["post-0"])
        assert (await data(iterator))[1]["id"] == "post-0"
        assert slow.queue and not fast.queue
        one = fast.owner.log[0].size
        monkeypatch.setattr(
            tail, "MAX_RETAINED_BYTES", registry.session_metadata_bytes("session") + 2 * one + 64
        )

        async def consume():
            return [(await data(iterator))[1]["id"] for _ in range(7)]

        reader = asyncio.create_task(consume())
        await append(live_backend, [f"post-{index}" for index in range(1, 8)])
        assert await asyncio.wait_for(reader, 5) == [f"post-{index}" for index in range(1, 8)]
        assert fast.control is None
        assert slow.control is not None and not slow.queue
        assert "overflow" in slow.control
        assert registry.session_retained_bytes("session") <= tail.MAX_RETAINED_BYTES
        assert registry.retained_bytes <= tail.MAX_TOTAL_RETAINED_BYTES
    finally:
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await iterator.aclose()
        await registry.unsubscribe(slow)


async def test_shared_payload_is_charged_until_its_final_reference_is_gone(live_backend):
    await seed(live_backend)
    registry, *_rest = live_backend
    _detail, mark = await bootstrap(live_backend)
    slow = await registry.subscribe("session", "messages", mark["message_cursor"])
    fast = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = fast.frames()
    try:
        await append(live_backend, ["shared-payload"])
        assert (await data(iterator))[1]["id"] == "shared-payload"
        item = fast.owner.log[0]
        assert item.references == 2 and registry.payload_bytes == item.size > 0
        charged = registry.payload_bytes
        await registry.unsubscribe(slow)
        assert item.references == 1 and registry.payload_bytes == charged
        await iterator.aclose()
        fast.owner._evict()
        assert registry.payload_bytes == 0 and fast.owner.retained_bytes == 0
        assert registry.retained_bytes == registry.metadata_bytes
    finally:
        await iterator.aclose()
        await registry.unsubscribe(slow)


async def test_last_unsubscribe_joins_a_close_already_in_progress(live_backend, monkeypatch):
    await seed(live_backend, terminal=True)
    registry, *_rest = live_backend
    tail = live_backend[5]
    closing, release = asyncio.Event(), asyncio.Event()
    original = tail._Tail._close_unlocked
    cancelled = []

    async def delayed_close(owner):
        if owner.resource is not None:
            closing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        await original(owner)

    monkeypatch.setattr(tail._Tail, "_close_unlocked", delayed_close)
    _detail, mark = await bootstrap(live_backend)
    first = await registry.subscribe("session", "messages", mark["message_cursor"])
    stopper = joiner = None
    try:
        await asyncio.wait_for(closing.wait(), 3)
        stopper = asyncio.create_task(registry.unsubscribe(first))
        joiner = asyncio.create_task(
            registry.subscribe("session", "messages", mark["message_cursor"])
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stopper.done() and not joiner.done() and not cancelled
        release.set()
        await asyncio.wait_for(stopper, 3)
        second = await asyncio.wait_for(joiner, 3)
        await registry.unsubscribe(second)
        assert not cancelled and first.owner.db is None and first.owner.task is None
    finally:
        release.set()
        for task in (stopper, joiner):
            if task is not None:
                results = await asyncio.gather(task, return_exceptions=True)
                for result in results:
                    if hasattr(result, "owner"):
                        await registry.unsubscribe(result)
        await registry.unsubscribe(first)


@pytest.mark.parametrize("failure", ["oserror", "cancel"])
async def test_response_start_failure_releases_an_unstarted_body(live_backend, failure):
    from starlette.requests import ClientDisconnect

    await seed(live_backend)
    registry, _path, _url, sessions, *_rest = live_backend
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "messages", mark["message_cursor"])
    response = sessions._live_tail_response(sub)
    started = asyncio.Event()
    sent = []

    async def send(value):
        sent.append(value["type"])
        started.set()
        if failure == "oserror":
            raise OSError("closed before headers")
        await asyncio.Future()

    async def receive():
        raise AssertionError("ASGI 2.4 should not read a disconnect channel")

    task = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ClientDisconnect):
                await task
        assert sent == ["http.response.start"]
        assert sub.released and not sub.owner.subscribers
        assert sub.owner.task is None and sub.owner.db is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.unsubscribe(sub)


async def test_registry_close_error_does_not_bypass_other_application_cleanup(monkeypatch):
    from importlib import import_module

    from lionagi.ln.concurrency import ExceptionGroup

    app = import_module("lionagi.studio.app")
    identity = import_module("lionagi.cli._code_identity")
    daemon_log = import_module("lionagi.studio.daemon_log")
    engine = import_module("lionagi.studio.scheduler.engine")
    lifecycle = import_module("lionagi.studio.services.lifecycle")
    operator = import_module("lionagi.studio.services.operator")
    launches = import_module("lionagi.studio.services.launches")
    notify = import_module("lionagi.state.lifecycle.notify_settings")
    calls = []

    async def noop():
        pass

    async def registry_close():
        calls.append("tail")
        raise RuntimeError("tail close failed")

    async def scheduler_stop():
        calls.append("scheduler")
        raise RuntimeError("scheduler close failed")

    async def operator_stop():
        calls.append("operator")

    async def launch_stop():
        calls.append("launches")

    async def mirror_stop(_stop, _task):
        calls.append("mirror")

    original_finalize = app._finalize_warmup

    async def finalize(task):
        calls.append("warmup")
        await original_finalize(task)

    monkeypatch.setattr(identity, "snapshot_git_position", lambda: None)
    monkeypatch.setattr(daemon_log, "daemon_log_handler", nullcontext)
    monkeypatch.setattr(engine.scheduler, "start", noop)
    monkeypatch.setattr(engine.scheduler, "stop", scheduler_stop)
    monkeypatch.setattr(lifecycle, "run_startup_reconciliation", noop)
    monkeypatch.setattr(operator, "operator_startup", noop)
    monkeypatch.setattr(operator, "operator_shutdown", operator_stop)
    monkeypatch.setattr(launches, "shutdown_launches", launch_stop)
    monkeypatch.setattr(notify, "register_settings_terminal_callback", lambda: None)
    monkeypatch.setattr(app, "arm_traceback_dump", lambda: None)
    monkeypatch.setattr(app, "disarm_traceback_dump", lambda: calls.append("disarm"))
    monkeypatch.setattr(app, "_emit_startup_warnings", lambda: None)
    monkeypatch.setattr(app, "begin_serving", lambda: None)
    monkeypatch.setattr(app, "_start_reconnect_channel", lambda: None)
    monkeypatch.setattr(app, "_start_claude_mirror", lambda: (None, None))
    monkeypatch.setattr(app, "_stop_claude_mirror", mirror_stop)
    monkeypatch.setattr(app, "_startup_warmup", noop)
    monkeypatch.setattr(app, "_finalize_warmup", finalize)
    instance = SimpleNamespace(
        state=SimpleNamespace(session_tail_registry=SimpleNamespace(aclose=registry_close))
    )
    with pytest.raises(ExceptionGroup) as caught:
        async with app.lifespan(instance):
            pass
    assert calls == ["tail", "disarm", "warmup", "mirror", "operator", "launches", "scheduler"]
    assert [str(error) for error in caught.value.exceptions] == [
        "tail close failed",
        "scheduler close failed",
    ]
    assert instance.state.session_tail_registry is None


async def _install_terminal_writer(live_backend, monkeypatch, *, own_kind=None, ddl=False):
    from lionagi.state.db import StateDB

    registry, path, url, _sessions, _signals, tail, sqlite = live_backend
    async with StateDB(url=url) as writer:
        await writer.create_progression("unrelated-pool")
        await writer.create_session(
            {
                "id": "unrelated",
                "progression_id": "unrelated-pool",
                "created_at": 1.0,
                "updated_at": 1.0,
                "name": "unrelated",
                "status": "running",
            }
        )
    original = tail._Tail._scalar
    state = {"observations": 0, "commits": 0, "own_inserted": False}

    async def interleave(owner, query):
        if query == "PRAGMA data_version":
            state["observations"] += 1
            if state["observations"] % 2 == 0:
                assert not owner.db.in_transaction
                async with sqlite.connect(str(path)) as writer:
                    if ddl:
                        await writer.execute("DROP TABLE IF EXISTS unrelated_terminal_noise")
                        await writer.execute(
                            "CREATE TABLE unrelated_terminal_noise (value INTEGER)"
                        )
                    else:
                        await writer.execute(
                            "UPDATE sessions SET updated_at=updated_at+1 WHERE id='unrelated'"
                        )
                    await writer.commit()
                state["commits"] += 1
                # Place own changes at the admitted consumer's running fence,
                # so an ignored activation drain cannot mask a missing check.
                if (
                    own_kind
                    and not state["own_inserted"]
                    and asyncio.current_task() is owner.task
                    and any(sub.ready for sub in owner.subscribers)
                ):
                    state["own_inserted"] = True
                    if own_kind == "message":
                        await append(live_backend, ["own-before-confirmation"])
                    elif own_kind == "signal":
                        async with StateDB(url=url) as writer:
                            await writer.insert_session_signal(
                                session_id="session",
                                kind="NodeCompleted",
                                op_id="own-before-confirmation",
                                ts=1.0,
                                payload={},
                            )
                    elif own_kind == "branch":
                        async with StateDB(url=url) as writer:
                            await writer.create_progression("confirmation-new-pool")
                            await writer.create_branch(
                                {
                                    "id": "confirmation-new-branch",
                                    "session_id": "session",
                                    "progression_id": "confirmation-new-pool",
                                    "created_at": 1.0,
                                }
                            )
                        await append(
                            live_backend,
                            ["own-before-confirmation"],
                            progression="confirmation-new-pool",
                        )
                    elif own_kind == "delete":
                        async with sqlite.connect(str(path)) as writer:
                            await writer.execute("DELETE FROM sessions WHERE id='session'")
                            await writer.commit()
        return await original(owner, query)

    monkeypatch.setattr(tail._Tail, "_scalar", interleave)
    return registry, state


async def test_terminal_progress_survives_unrelated_commits_at_every_fence(
    live_backend, monkeypatch
):
    await seed(live_backend, terminal=True)
    registry, state = await _install_terminal_writer(live_backend, monkeypatch)
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = sub.frames()
    try:
        assert (await data(iterator))[1] == {"type": "done"}
        assert state["commits"] >= 1
    finally:
        await iterator.aclose()


@pytest.mark.parametrize("kind", ["message", "signal", "branch"])
async def test_terminal_confirmation_covers_own_append_amid_unrelated_commits(
    live_backend, monkeypatch, kind
):
    await seed(live_backend, terminal=True)
    registry, state = await _install_terminal_writer(live_backend, monkeypatch, own_kind=kind)
    _detail, mark = await bootstrap(live_backend)
    channel = "signals" if kind == "signal" else "messages"
    sub = await registry.subscribe("session", channel, mark["message_cursor"], after_seq=0)
    iterator = sub.frames()
    try:
        value = (await data(iterator))[1]
        assert state["own_inserted"]
        assert value.get("type") is None
        if kind in {"message", "branch"}:
            assert value.get("id") == "own-before-confirmation"
            if kind == "branch":
                assert value.get("branch_id") == "confirmation-new-branch"
        else:
            assert value.get("seq") == 1 and value.get("op_id") == "own-before-confirmation"
        assert (await data(iterator))[1] == {"type": "done"}
    finally:
        await iterator.aclose()
        assert not sub.owner.subscribers and sub.owner.task is None and sub.owner.db is None


async def test_signal_table_appearance_invalidates_cached_absence_before_done(live_backend):
    await seed(live_backend)
    registry, path, _url, _sessions, _signals, _tail, sqlite = live_backend
    async with sqlite.connect(str(path)) as writer:
        cursor = await writer.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_signals'"
        )
        definition = (await cursor.fetchone())[0]
        await cursor.close()
        await writer.execute("DROP TABLE session_signals")
        await writer.commit()
    assert definition
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=0)
    iterator = sub.frames()
    try:
        assert not sub.owner.has_signals and sub.owner.signal_definition is None
        async with sqlite.connect(str(path)) as writer:
            await writer.execute(definition)
            await writer.execute(
                "INSERT INTO session_signals(id,session_id,seq,kind,op_id,ts,payload) "
                "VALUES('appeared','session',1,'NodeCompleted','appeared',1,'{}')"
            )
            await writer.execute(
                "UPDATE sessions SET status='completed',updated_at=1 WHERE id='session'"
            )
            await writer.commit()
        value = (await data(iterator))[1]
        assert value["type"] == "resync_required" and value["reason"] == "frontier_unavailable"
    finally:
        await iterator.aclose()


async def test_unrelated_ddl_refreshes_capability_without_starving_terminal(
    live_backend, monkeypatch
):
    await seed(live_backend, terminal=True)
    registry, state = await _install_terminal_writer(live_backend, monkeypatch, ddl=True)
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=0)
    iterator = sub.frames()
    try:
        assert (await data(iterator))[1] == {"type": "done"}
        assert state["commits"] >= 1 and sub.control is None
        assert sub.owner.has_signals and sub.owner.signal_definition is not None
    finally:
        await iterator.aclose()


async def test_terminal_done_follows_each_queued_suffix_and_late_join(live_backend):
    await seed(live_backend)
    registry, path, _url, _sessions, _signals, _tail, sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    slow_messages = await registry.subscribe("session", "messages", mark["message_cursor"])
    slow_signals = await registry.subscribe(
        "session", "signals", mark["message_cursor"], after_seq=0
    )
    fast_messages = await registry.subscribe("session", "messages", mark["message_cursor"])
    fast_signals = await registry.subscribe(
        "session", "signals", mark["message_cursor"], after_seq=0
    )
    iterators = [fast_messages.frames(), fast_signals.frames()]
    readers = []

    async def consume(iterator):
        values = []
        while True:
            value = (await data(iterator))[1]
            values.append(value)
            if value.get("type") == "done":
                return values

    try:
        readers = [asyncio.create_task(consume(iterator)) for iterator in iterators]
        await append(live_backend, ["queued-1", "queued-2", "queued-3"])
        async with sqlite.connect(str(path)) as writer:
            await writer.executemany(
                "INSERT INTO session_signals(id,session_id,seq,kind,op_id,ts,payload) "
                "VALUES(?,'session',?,'NodeCompleted',?,1,'{}')",
                [("queued-signal-1", 1, "one"), ("queued-signal-2", 2, "two")],
            )
            await writer.execute(
                "UPDATE sessions SET status='completed',updated_at=1 WHERE id='session'"
            )
            await writer.commit()
        fast_m, fast_s = await asyncio.wait_for(asyncio.gather(*readers), 5)
        assert [value["id"] for value in fast_m[:-1]] == ["queued-1", "queued-2", "queued-3"]
        assert [value["seq"] for value in fast_s[:-1]] == [1, 2]
        assert slow_messages.finished and len(slow_messages.queue) == 3
        assert slow_signals.finished and len(slow_signals.queue) == 2
        # A terminal-time join receives the retained suffix, then its own done.
        late = await registry.subscribe("session", "messages", mark["message_cursor"])
        late_iterator = late.frames()
        iterators.append(late_iterator)
        late_values = await asyncio.wait_for(consume(late_iterator), 3)
        assert [value["id"] for value in late_values[:-1]] == ["queued-1", "queued-2", "queued-3"]
        assert late_values[-1] == {"type": "done"}
        for subscriber, expected in [
            (slow_messages, ["queued-1", "queued-2", "queued-3"]),
            (slow_signals, [1, 2]),
        ]:
            iterator = subscriber.frames()
            iterators.append(iterator)
            values = await asyncio.wait_for(consume(iterator), 3)
            key = "id" if subscriber.channel == "messages" else "seq"
            assert [value[key] for value in values[:-1]] == expected
            assert values[-1] == {"type": "done"}
    finally:
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for iterator in iterators:
            await iterator.aclose()
        for subscriber in (slow_messages, slow_signals, fast_messages, fast_signals):
            await registry.unsubscribe(subscriber)


async def test_terminal_confirmation_resyncs_if_the_session_disappears(live_backend, monkeypatch):
    await seed(live_backend, terminal=True)
    registry, state = await _install_terminal_writer(live_backend, monkeypatch, own_kind="delete")
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = sub.frames()
    try:
        value = (await data(iterator))[1]
        assert state["own_inserted"]
        assert value["type"] == "resync_required" and value["reason"] == "frontier_unavailable"
    finally:
        await iterator.aclose()


async def test_signal_definition_bytes_reject_oversized_activation_without_uncached_retention(
    live_backend, monkeypatch
):
    await seed(live_backend)
    registry, path, _url, _sessions, _signals, tail, sqlite = live_backend
    padding = "schema_padding_" + "x" * 4_000
    async with sqlite.connect(str(path)) as writer:
        # The identifier is local fixture data, not external SQL input.
        await writer.execute(f'ALTER TABLE session_signals ADD COLUMN "{padding}" TEXT')  # noqa: S608
        cursor = await writer.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_signals'"
        )
        definition = (await cursor.fetchone())[0]
        await cursor.close()
        await writer.commit()
    _detail, mark = await bootstrap(live_backend)
    boot = registry.bootstraps[mark["message_cursor"]]
    probe = tail._Tail(registry, boot)
    frontier_only = registry.retained_bytes + probe.metadata_size
    raw_bytes = len(definition.encode("utf-8"))
    assert raw_bytes > 4_000
    monkeypatch.setattr(tail, "MAX_RETAINED_BYTES", frontier_only + raw_bytes // 2)
    with pytest.raises(tail.StreamResyncError, match="resource_limit"):
        await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=0)
    assert "session" not in registry.tails
    assert registry.metadata_bytes == boot.size and registry.payload_bytes == 0
    assert registry.session_retained_bytes("session") == boot.size
    # A larger legal budget is a positive control for the same real schema.
    monkeypatch.setattr(tail, "MAX_RETAINED_BYTES", frontier_only + 2 * raw_bytes)
    accepted = await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=0)
    try:
        assert accepted.owner.signal_definition == definition
        assert accepted.owner.signal_definition_bytes >= raw_bytes
        assert registry.session_retained_bytes("session") <= tail.MAX_RETAINED_BYTES
        assert registry.retained_bytes <= tail.MAX_TOTAL_RETAINED_BYTES
    finally:
        await registry.unsubscribe(accepted)
    await registry.aclose()
    assert accepted.owner.signal_definition is None
    assert accepted.owner.signal_definition_bytes == 0 and registry.metadata_bytes == 0


async def test_unrelated_schema_revalidation_does_not_double_charge_definition(live_backend):
    await seed(live_backend)
    registry, path, _url, _sessions, _signals, _tail, sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=0)
    try:
        definition = sub.owner.signal_definition
        charged = sub.owner.signal_definition_bytes
        metadata = registry.metadata_bytes
        assert definition and charged >= len(definition.encode("utf-8"))
        async with sqlite.connect(str(path)) as writer:
            await writer.execute("CREATE TABLE definition_accounting_unrelated (value INTEGER)")
            await writer.commit()
        await sub.owner.drain()
        assert sub.owner.signal_definition == definition
        assert sub.owner.signal_definition_bytes == charged
        assert registry.metadata_bytes == metadata and sub.control is None
    finally:
        await registry.unsubscribe(sub)


class _IdlePolls:
    def __init__(self):
        self.waits = asyncio.Queue()

    async def sleep(self, seconds):
        if seconds == 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        self.waits.put_nowait(future)
        await future

    async def barrier(self):
        return await asyncio.wait_for(self.waits.get(), 3)


def _count_idle_collections(monkeypatch, tail):
    counts = SimpleNamespace(collections=0)
    original = tail._Tail._collections

    async def counted(owner):
        counts.collections += 1
        return await original(owner)

    monkeypatch.setattr(tail._Tail, "_collections", counted)
    return counts


async def test_idle_collection_cache_rechecks_commits_and_finishes_batched_suffix(
    live_backend, monkeypatch
):
    await seed(live_backend)
    registry, path, _url, _sessions, _signals, tail, sqlite = live_backend
    gate = _IdlePolls()
    monkeypatch.setattr(tail, "sleep", gate.sleep)
    monkeypatch.setattr(tail, "TAIL_BATCH_ROWS", 1)
    counts = _count_idle_collections(monkeypatch, tail)
    _detail, mark = await bootstrap(live_backend)
    sub = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = sub.frames()
    try:
        pending = await gate.barrier()
        baseline = counts.collections
        for _ in range(5):
            pending.set_result(None)
            pending = await gate.barrier()
        assert counts.collections == baseline
        assert not sub.owner.db.in_transaction

        # The cache depends on the reader being unable to commit its own DML
        # or DDL, which SQLite's connection-local data_version would not notice.
        async with sub.owner.io_lock:
            assert await sub.owner._scalar("PRAGMA query_only") == 1
            version = await sub.owner._scalar("PRAGMA data_version")
            total_changes = sub.owner.db.total_changes
            for statement in [
                "UPDATE sessions SET name='forbidden' WHERE id='session'",
                "CREATE TABLE forbidden_tail_write (value INTEGER)",
            ]:
                try:
                    with pytest.raises(sqlite.OperationalError, match="readonly"):
                        async with sub.owner.db.execute(statement):
                            pass
                finally:
                    await sub.owner.db.rollback()
            assert sub.owner.db.total_changes == total_changes
            assert await sub.owner._scalar("PRAGMA data_version") == version
        pending.set_result(None)
        pending = await gate.barrier()
        assert counts.collections == baseline

        # An unrelated schema commit must still revalidate capability once.
        async with sqlite.connect(str(path)) as writer:
            await writer.execute("CREATE TABLE idle_cache_noise (value INTEGER)")
            await writer.commit()
        pending.set_result(None)
        pending = await gate.barrier()
        assert counts.collections == baseline + 1
        assert sub.owner.error is None
        baseline = counts.collections

        await append(live_backend, ["idle-one", "idle-two", "idle-three"])
        pending.set_result(None)
        pending = await gate.barrier()
        assert [(await data(iterator))[1]["id"] for _ in range(3)] == [
            "idle-one",
            "idle-two",
            "idle-three",
        ]
        assert counts.collections >= baseline + 4
        baseline = counts.collections
        for _ in range(5):
            pending.set_result(None)
            pending = await gate.barrier()
        assert counts.collections == baseline

        # Retention can change without a SQLite write. Old cuts still fail
        # synchronously, while the healthy active consumer keeps its stream.
        sub.owner._evict()
        with pytest.raises(tail.StreamResyncError, match="frontier_unavailable"):
            await registry.subscribe("session", "messages", mark["message_cursor"])
        assert counts.collections == baseline and sub.control is None
        pending.set_result(None)
        await gate.barrier()
        assert counts.collections == baseline and sub.control is None
    finally:
        await iterator.aclose()
        await registry.unsubscribe(sub)
        assert sub.owner.task is None and sub.owner.db is None


async def test_reopened_tail_does_not_reuse_another_connections_completed_version(
    live_backend, monkeypatch
):
    await seed(live_backend)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    gate = _IdlePolls()
    monkeypatch.setattr(tail, "sleep", gate.sleep)
    original = tail._Tail._scalar

    async def same_numeric_version(owner, query):
        # Equal numeric cookies on different connections do not imply that
        # the database stayed unchanged between those connections.
        return 17 if query == "PRAGMA data_version" else await original(owner, query)

    monkeypatch.setattr(tail._Tail, "_scalar", same_numeric_version)
    _detail, mark = await bootstrap(live_backend)
    first = await registry.subscribe("session", "messages", mark["message_cursor"])
    await gate.barrier()
    owner = first.owner
    assert owner.completed_data_version == 17
    await registry.unsubscribe(first)
    assert owner.task is None and owner.db is None
    await append(live_backend, ["after-reopen"])
    second = await registry.subscribe("session", "messages", mark["message_cursor"])
    iterator = second.frames()
    try:
        assert second.owner is owner
        assert (await data(iterator))[1]["id"] == "after-reopen"
    finally:
        await iterator.aclose()
        assert owner.task is None and owner.db is None


@pytest.mark.parametrize("kind", [None, "message", "signal", "branch"])
async def test_cached_terminal_age_confirms_own_snapshot_before_done(
    live_backend, monkeypatch, kind
):
    from lionagi.state.db import StateDB

    await seed(live_backend, terminal=True)
    registry, path, url, sessions, _signals, tail, sqlite = live_backend
    async with sqlite.connect(str(path)) as writer:
        await writer.execute("UPDATE sessions SET updated_at=100 WHERE id='session'")
        await writer.commit()
    clock = SimpleNamespace(now=100 + sessions.SESSION_DONE_STABLE_SECS - 1)
    monkeypatch.setattr(
        tail, "time", SimpleNamespace(time=lambda: clock.now, monotonic=tail.time.monotonic)
    )
    gate = _IdlePolls()
    monkeypatch.setattr(tail, "sleep", gate.sleep)
    counts = _count_idle_collections(monkeypatch, tail)
    original = tail._Tail._scalar
    state = {"armed": False, "fences": 0, "inserted": False}

    async def interleave(owner, query):
        if state["armed"] and query == "PRAGMA data_version":
            state["fences"] += 1
            if state["fences"] == 2 and kind is not None:
                assert not owner.db.in_transaction
                state["inserted"] = True
                if kind == "signal":
                    async with StateDB(url=url) as writer:
                        await writer.insert_session_signal(
                            session_id="session",
                            kind="NodeCompleted",
                            op_id="cached-terminal",
                            ts=1.0,
                            payload={},
                        )
                else:
                    progression = "branch-pool"
                    if kind == "branch":
                        progression = "cached-terminal-pool"
                        async with StateDB(url=url) as writer:
                            await writer.create_progression(progression)
                            await writer.create_branch(
                                {
                                    "id": "cached-terminal-branch",
                                    "session_id": "session",
                                    "progression_id": progression,
                                    "created_at": 1.0,
                                }
                            )
                    await append(live_backend, ["before-cached-done"], progression=progression)
        return await original(owner, query)

    monkeypatch.setattr(tail._Tail, "_scalar", interleave)
    _detail, mark = await bootstrap(live_backend)
    channel = "signals" if kind == "signal" else "messages"
    sub = await registry.subscribe("session", channel, mark["message_cursor"], after_seq=0)
    iterator = sub.frames()
    try:
        pending = await gate.barrier()
        baseline = counts.collections
        for _ in range(3):
            pending.set_result(None)
            pending = await gate.barrier()
        assert counts.collections == baseline and not sub.finished
        state["armed"] = True
        clock.now = 100 + sessions.SESSION_DONE_STABLE_SECS + 1
        pending.set_result(None)
        value = (await data(iterator))[1]
        if kind is None:
            assert value == {"type": "done"}
            assert counts.collections == baseline + 1
        else:
            assert state["inserted"] and value.get("type") is None
            if kind == "signal":
                assert value["seq"] == 1 and value["op_id"] == "cached-terminal"
            else:
                assert value["id"] == "before-cached-done"
                if kind == "branch":
                    assert value["branch_id"] == "cached-terminal-branch"
            assert (await data(iterator))[1] == {"type": "done"}
        assert sub.control is None
    finally:
        await iterator.aclose()
        await registry.unsubscribe(sub)
        assert sub.owner.task is None and sub.owner.db is None


async def test_cancelled_joiner_does_not_cancel_a_closing_reader(live_backend, monkeypatch):
    await seed(live_backend, terminal=True)
    registry, *_rest = live_backend
    tail = live_backend[5]
    closing, release = asyncio.Event(), asyncio.Event()
    original = tail._Tail._close_unlocked
    cancelled = []
    closed = []

    async def delayed_close(owner):
        if owner.resource is not None:
            closing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            await original(owner)
            closed.append(True)
        else:
            await original(owner)

    monkeypatch.setattr(tail._Tail, "_close_unlocked", delayed_close)
    _detail, mark = await bootstrap(live_backend)
    first = await registry.subscribe("session", "messages", mark["message_cursor"])
    reader = first.owner.task
    joiner = None
    try:
        await asyncio.wait_for(closing.wait(), 3)
        assert reader is not None and not reader.done()
        joiner = asyncio.create_task(
            registry.subscribe("session", "messages", mark["message_cursor"])
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not joiner.done()
        joiner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await joiner
        assert not cancelled and not reader.done()
        release.set()
        await asyncio.wait_for(asyncio.shield(reader), 3)
        assert closed == [True]
        assert first.owner.db is None and first.owner.resource is None
        assert first.owner.task is None
        assert first.owner.subscribers == {first}
    finally:
        release.set()
        if joiner is not None:
            if not joiner.done():
                joiner.cancel()
            await asyncio.gather(joiner, return_exceptions=True)
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        await registry.unsubscribe(first)


async def test_failed_first_retention_does_not_consume_a_broker_slot(live_backend, monkeypatch):
    await seed(live_backend)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    boot = registry.bootstraps[mark["message_cursor"]]
    monkeypatch.setattr(tail, "MAX_BROKERS", 1)
    for _ in range(3):
        with pytest.raises(tail.StreamResyncError, match="frontier_unavailable"):
            await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=1)
        assert not registry.tails
        assert registry.metadata_bytes == boot.size and registry.payload_bytes == 0
    accepted = await registry.subscribe("session", "messages", mark["message_cursor"])
    try:
        assert accepted.ready and registry.tails["session"] is accepted.owner
    finally:
        await registry.unsubscribe(accepted)


async def test_failed_first_admission_releases_its_registered_tail(live_backend, monkeypatch):
    await seed(live_backend, 1)
    registry, path, _url, _sessions, _signals, tail, sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    boot = registry.bootstraps[mark["message_cursor"]]
    original = tail._Tail.admit
    rejected = []

    async def remove_cut_before_admission(owner, sub, generation):
        rejected.append(owner)
        async with sqlite.connect(str(path)) as writer:
            await writer.execute("DELETE FROM session_signals WHERE session_id='session'")
            await writer.commit()
        await original(owner, sub, generation)

    monkeypatch.setattr(tail._Tail, "admit", remove_cut_before_admission)
    with pytest.raises(tail.StreamResyncError, match="frontier_unavailable"):
        await registry.subscribe("session", "signals", mark["message_cursor"], after_seq=1)
    assert len(rejected) == 1 and not registry.tails
    owner = rejected[0]
    assert not owner.subscribers and owner.task is None
    assert owner.db is None and owner.resource is None and owner.metadata_size == 0
    assert not owner.log and owner.retained_bytes == 0
    assert registry.metadata_bytes == boot.size and registry.payload_bytes == 0
    monkeypatch.setattr(tail._Tail, "admit", original)
    accepted = await registry.subscribe("session", "messages", mark["message_cursor"])
    try:
        assert accepted.ready and accepted.owner is not owner
    finally:
        await registry.unsubscribe(accepted)


async def test_cancelled_initial_open_keeps_cancellation_and_releases_admission(
    live_backend, monkeypatch
):
    await seed(live_backend)
    registry, _path, _url, _sessions, _signals, tail, _sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    boot = registry.bootstraps[mark["message_cursor"]]
    entered = asyncio.Event()
    exits = []

    class PendingOpen:
        async def __aenter__(self):
            entered.set()
            await asyncio.Future()

        async def __aexit__(self, *_exc):
            exits.append(True)

    monkeypatch.setattr(tail, "open_db", lambda _path: PendingOpen())
    pending = asyncio.create_task(registry.subscribe("session", "messages", mark["message_cursor"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert exits == [True]
        assert not registry.tails
        assert registry.metadata_bytes == boot.size and registry.payload_bytes == 0
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("channel", ["messages", "signals"])
@pytest.mark.parametrize("failure", ["factory", "enter"])
async def test_repeated_live_store_open_failure_has_one_resync_response(
    live_backend, live_http, monkeypatch, channel, failure
):
    await seed(live_backend, terminal=True)
    registry, path, _url, _sessions, _signals, tail, _sqlite = live_backend
    _detail, mark = await bootstrap(live_backend)
    boot = registry.bootstraps[mark["message_cursor"]]
    original = tail.open_db
    attempts = []

    class UnavailableOpen:
        async def __aenter__(self):
            raise OSError("store unavailable")

        async def __aexit__(self, *_exc):
            pass

    def unavailable_open(actual_path):
        attempts.append(actual_path)
        if failure == "factory":
            raise OSError("store unavailable")
        return UnavailableOpen()

    monkeypatch.setattr(tail, "open_db", unavailable_open)
    if channel == "messages":
        route = "/api/sessions/session/stream"
        params = {"mode": "live", "cursor": mark["message_cursor"]}
    else:
        route = "/api/sessions/session/signals"
        params = {"mode": "live", "snapshot": mark["message_cursor"], "after_seq": 0}
    for _ in range(2):
        response = await live_http.get(route, params=params)
        assert response.status_code == 409
        assert response.json() == {
            "detail": {"code": "stream_resync_required", "reason": "frontier_unavailable"}
        }
        assert not registry.tails
        assert registry.metadata_bytes == boot.size and registry.payload_bytes == 0
    assert attempts == [str(path), str(path)]
    monkeypatch.setattr(tail, "open_db", original)
    response = await asyncio.wait_for(live_http.get(route, params=params), 10)
    assert response.status_code == 200 and response.text == 'data: {"type":"done"}\n\n'


def _admission_only_registry(monkeypatch):
    from lionagi.studio.services import _session_tail as tail

    registry = tail.SessionTailRegistry("unused")
    started = []

    async def start(owner):
        started.append(owner.session_id)

    async def admit(owner, sub, generation):
        sub.ready = True
        for item in owner.log:
            sub.offer(item)

    monkeypatch.setattr(tail._Tail, "start", start)
    monkeypatch.setattr(tail._Tail, "admit", admit)
    return registry, tail, started


async def _idle_signal_payload(registry):
    mark = registry.snapshot("old", {}, 0)
    sub = await registry.subscribe("old", "signals", mark["message_cursor"])
    sub.owner.publish("signals", {"seq": 1, "payload": "x" * 4096})
    await registry.unsubscribe(sub)
    assert not sub.owner.subscribers and sub.owner.log
    assert sub.owner.task is None and sub.owner.resource is None
    return sub.owner


@pytest.mark.parametrize("channel", ["messages", "signals"])
@pytest.mark.parametrize("delivered", [False, True])
async def test_resync_marker_excludes_queued_payloads(monkeypatch, channel, delivered):
    registry, _tail, _started = _admission_only_registry(monkeypatch)
    mark = registry.snapshot("session", {}, 7)
    sub = await registry.subscribe("session", channel, mark["message_cursor"], after_seq=7)
    iterator = sub.frames()
    try:
        for index in range(2):
            if channel == "messages":
                sub.owner.publish(
                    "messages",
                    {"id": f"message-{index}"},
                    branch_id="branch",
                    position=index,
                    message_id=f"message-{index}",
                    progression_id="progression",
                )
            else:
                sub.owner.publish("signals", {"seq": 8 + index})
        expected = mark["message_cursor"] if channel == "messages" else 7
        if delivered:
            frame, value = await data(iterator)
            expected = (
                next(line[4:] for line in frame.splitlines() if line.startswith("id: "))
                if channel == "messages"
                else value["seq"]
            )
        sub.resync("overflow")
        _frame, control = await data(iterator)
        key = "resume_cursor" if channel == "messages" else "resume_after_seq"
        assert control[key] == expected
        assert control["type"] == "resync_required" and control["stream"] == channel
        assert not sub.queue and sub.queued_bytes == 0
    finally:
        await iterator.aclose()
        await registry.aclose()


async def test_snapshot_reclaims_idle_payload_before_rejecting_budget(monkeypatch):
    registry, tail, _started = _admission_only_registry(monkeypatch)
    try:
        owner = await _idle_signal_payload(registry)
        limit = registry.retained_bytes
        monkeypatch.setattr(tail, "MAX_TOTAL_RETAINED_BYTES", limit)
        assert not registry.can_fit("new", 1)
        mark = registry.snapshot("new", {}, 0)
        assert mark["message_cursor"] in registry.bootstraps
        assert not owner.log and registry.payload_bytes == 0
        assert registry.retained_bytes <= limit
        assert set(registry.tails) == {"old"}
    finally:
        await registry.aclose()


async def test_new_tail_reclaims_idle_payload_before_opening_reader(monkeypatch):
    registry, tail, started = _admission_only_registry(monkeypatch)
    try:
        mark = registry.snapshot("new", {}, 0)
        owner = await _idle_signal_payload(registry)
        limit = registry.retained_bytes
        monkeypatch.setattr(tail, "MAX_TOTAL_RETAINED_BYTES", limit)
        assert not registry.can_fit("new", 1)
        sub = await registry.subscribe("new", "signals", mark["message_cursor"])
        assert started == ["old", "new"]
        assert sub.owner is registry.tails["new"]
        assert not owner.log and registry.payload_bytes == 0
        assert registry.retained_bytes <= limit
        await registry.unsubscribe(sub)
    finally:
        await registry.aclose()


@pytest.mark.parametrize("admission", ["snapshot", "tail"])
async def test_impossible_metadata_admission_preserves_retained_payload(monkeypatch, admission):
    registry, tail, started = _admission_only_registry(monkeypatch)
    try:
        mark = registry.snapshot("new", {}, 0) if admission == "tail" else None
        owner = await _idle_signal_payload(registry)
        retained = tuple(owner.log)
        payload_bytes = registry.payload_bytes
        metadata_bytes = registry.metadata_bytes
        monkeypatch.setattr(tail, "MAX_TOTAL_RETAINED_BYTES", metadata_bytes)
        with pytest.raises(tail.StreamResyncError, match="resource_limit"):
            if mark is None:
                registry.snapshot("new", {}, 0)
            else:
                await registry.subscribe("new", "signals", mark["message_cursor"])
        assert tuple(owner.log) == retained
        assert registry.payload_bytes == payload_bytes
        assert registry.metadata_bytes == metadata_bytes
        assert set(registry.tails) == {"old"} and started == ["old"]
    finally:
        await registry.aclose()


async def test_invalid_new_tail_cannot_evict_another_session(monkeypatch):
    registry, tail, started = _admission_only_registry(monkeypatch)
    try:
        mark = registry.snapshot("new", {}, 7)
        owner = await _idle_signal_payload(registry)
        retained = tuple(owner.log)
        payload_bytes = registry.payload_bytes
        metadata_bytes = registry.metadata_bytes
        monkeypatch.setattr(tail, "MAX_TOTAL_RETAINED_BYTES", registry.retained_bytes)
        with pytest.raises(tail.StreamResyncError, match="frontier_unavailable"):
            await registry.subscribe("new", "signals", mark["message_cursor"], after_seq=8)
        assert tuple(owner.log) == retained
        assert registry.payload_bytes == payload_bytes
        assert registry.metadata_bytes == metadata_bytes
        assert set(registry.tails) == {"old"} and started == ["old"]
    finally:
        await registry.aclose()
