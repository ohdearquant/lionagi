# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Transcript mirror service and persistent cursor contracts."

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lionagi.state.claude_mirror import _det, messages_for_event, mirror_session, session_db_id
from lionagi.state.db import StateDB
from lionhub.serve.mirror import (
    _derive_metadata,
    _fallback_project,
    _FileState,
    _first_prompt,
    _Lineage,
    _load_states,
    _one_pass,
    _parse_window,
    _read_new_events,
    _save_states,
    _seed_lineage,
    _since_window,
)
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._mirror_test_support import _OVERSIZED_TEXT as _OVERSIZED_TEXT
from tests._mirror_test_support import SID as SID
from tests._mirror_test_support import _assistant as _assistant
from tests._mirror_test_support import _claude_oversized_file as _claude_oversized_file
from tests._mirror_test_support import _conversation as _conversation
from tests._mirror_test_support import _iso as _iso
from tests._mirror_test_support import _response_functions as _response_functions
from tests._mirror_test_support import _session_contents as _session_contents
from tests._mirror_test_support import _tool_result as _tool_result
from tests._mirror_test_support import _user_text as _user_text
from tests._mirror_test_support import temp_db_path as temp_db_path


def _write_session_file(path: Path, uid: str, *, age_secs: float) -> None:
    """One transcript file for `uid` whose messages are `age_secs` old."""
    ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=age_secs))
    stem = path.stem
    events = [
        {
            "type": "user",
            "uuid": f"{stem}-u",
            "timestamp": ts,
            "sessionId": uid,
            "message": {"role": "user", "content": [{"type": "text", "text": f"prompt {stem}"}]},
        },
        {
            "type": "assistant",
            "uuid": f"{stem}-a",
            "timestamp": ts,
            "sessionId": uid,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "ok"}],
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _lineage_event(uid: str, euid: str, parent: str | None, role: str, text: str) -> dict:
    ev = {
        "type": role,
        "uuid": euid,
        "parentUuid": parent,
        "timestamp": _iso(datetime.now(timezone.utc)),
        "sessionId": uid,
        "cwd": "/tmp",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }
    if role == "assistant":
        ev["message"]["model"] = "claude-opus-4-8"
    return ev


def _write_lineage_file(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _codex_rollout_lines(uid: str, originator: str) -> str:
    meta = {
        "type": "session_meta",
        "timestamp": "2026-07-31T09:00:00Z",
        "payload": {"id": uid, "session_id": uid, "cwd": "/x", "originator": originator},
    }
    user = {
        "type": "response_item",
        "timestamp": "2026-07-31T09:00:01Z",
        "payload": {"type": "message", "role": "user", "id": "m1", "content": [{"text": "q"}]},
    }
    asst = {
        "type": "response_item",
        "timestamp": "2026-07-31T09:00:02Z",
        "payload": {"type": "message", "role": "assistant", "id": "m2", "content": [{"text": "a"}]},
    }
    return "".join(json.dumps(e) + "\n" for e in (meta, user, asst))


def _codex_oversized_file(root: Path, uid: str) -> Path:
    path = root / f"rollout-{uid}.jsonl"
    root.mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "session_meta",
        "timestamp": "2026-08-05T00:00:00Z",
        "payload": {"id": uid, "session_id": uid, "cwd": "/x", "originator": "Codex Desktop"},
    }
    user = {
        "type": "response_item",
        "timestamp": "2026-08-05T00:00:01Z",
        "payload": {
            "type": "message",
            "role": "user",
            "id": "m1",
            "content": [{"text": _OVERSIZED_TEXT}],
        },
    }
    path.write_text("".join(json.dumps(e) + "\n" for e in (meta, user)))
    return path


@pytest.mark.asyncio
async def test_one_pass_non_completed_terminal_reactivation_does_not_crash(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # The CLI's per-tail-pass loop must not propagate a TransitionRejectedError
    # out of _one_pass() when a live mirror session sits on a non-"completed"
    # terminal status (e.g. independently marked "failed" or "cancelled").
    root = tmp_path / "projects"
    uid = "dddddddd-0000-0000-0000-00000000000d"
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=uid, events=_conversation(), tool_names={}, status="running"
        )
        await db.update_status(
            "session",
            session_db_id(uid),
            new_status="failed",
            reason_code="run.failed.exception",
            source="admin",
            actor="test",
        )
        _write_session_file(root / "-w-proj" / f"{uid}.jsonl", uid, age_secs=5)
        await _one_pass(db, root, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_one_pass_terminal_reactivation_does_not_skip_later_uid_or_lineage(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # A live, non-"completed" terminal session reconciled earlier in the same
    # pass must not abort the loop before it reaches lineage resolution for an
    # unrelated pair of sessions processed in the same pass.
    root = tmp_path / "projects"
    terminal_uid = "eeeeeeee-0000-0000-0000-00000000000e"
    a, b = "aaaaaaaa-1111-0000-0000-000000000001", "bbbbbbbb-1111-0000-0000-000000000002"
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=terminal_uid, events=_conversation(), tool_names={}, status="running"
        )
        await db.update_status(
            "session",
            session_db_id(terminal_uid),
            new_status="cancelled",
            reason_code="run.cancelled.system",
            source="admin",
            actor="test",
        )
        _write_session_file(root / "-w-proj" / f"{terminal_uid}.jsonl", terminal_uid, age_secs=5)
        _write_lineage_file(
            root / "-w-proj" / f"{a}.jsonl",
            [
                _lineage_event(a, "a-1", None, "user", "start the work"),
                _lineage_event(a, "a-leaf", "a-1", "assistant", "done, ending here"),
            ],
        )
        _write_lineage_file(
            root / "-w-proj" / f"{b}.jsonl",
            [
                _lineage_event(b, "b-1", "a-leaf", "user", "continuing from before"),
                _lineage_event(b, "b-2", "b-1", "assistant", "picking it up"),
            ],
        )
        await _one_pass(db, root, {}, since=None, live_window=300)
        terminal_row = await db.get_session(session_db_id(terminal_uid))
        child = await db.get_session(session_db_id(b))
    assert terminal_row["status"] == "running"
    lineage = child["node_metadata"]["lineage"]
    assert lineage["parent_session_uid"] == a


@pytest.mark.asyncio
async def test_multifile_session_stays_running_when_any_file_is_fresh(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # A resumed session spans two transcript files sharing one sessionId: one old,
    # one with a recent message. The merged session must read as live (running) —
    # the regression guard against a per-file status decision burying an active one.
    root = tmp_path / "projects"
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_session_file(root / "-work-acme" / "old.jsonl", uid, age_secs=7200)
    _write_session_file(root / "-work-acme" / "fresh.jsonl", uid, age_secs=5)
    async with StateDB() as db:
        await _one_pass(db, root, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row is not None
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_multifile_session_completes_when_all_files_idle(
    temp_db_path: Path, tmp_path: Path
) -> None:
    root = tmp_path / "projects"
    uid = "11112222-3333-4444-5555-666677778888"
    _write_session_file(root / "-work-acme" / "a.jsonl", uid, age_secs=7200)
    _write_session_file(root / "-work-acme" / "b.jsonl", uid, age_secs=3600)
    async with StateDB() as db:
        await _one_pass(db, root, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row is not None
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_idle_transcript_status_reconciliation_quiesces_after_cold_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idle history costs one cold reconciliation, not one DB read per poll forever.

    This models Studio's default five-second ambient-mirror poll with a bounded
    but non-trivial transcript population.  Every file is already at EOF and
    older than the liveness window, so the first pass must still repair stale
    ``running`` rows after a daemon restart.  Once that check succeeds, another
    unchanged pass has no new liveness fact to ask StateDB about.
    """
    import lionhub.serve.mirror as service_mod

    root = tmp_path / "projects"
    now = 1_800_000_000.0
    states: dict[str, _FileState] = {}
    expected_uids: list[str] = []
    for index in range(64):
        uid = f"idle-{index:04d}"
        path = root / "-work-acme" / f"{uid}.jsonl"
        _write_session_file(path, uid, age_secs=3600)
        os.utime(path, (now - 600, now - 600))
        states[str(path)] = _FileState(
            session_uid=uid,
            offset=path.stat().st_size,
            project="acme",
            attr_peeked=True,
        )
        expected_uids.append(uid)

    calls: list[str] = []

    async def _record_reconcile(_db, uid, **_kwargs):
        calls.append(uid)
        return True

    monkeypatch.setattr(service_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.claude_mirror.reconcile_session_status",
        _record_reconcile,
    )

    await _one_pass(None, root, states, since=None, live_window=300)
    await _one_pass(None, root, states, since=None, live_window=300)

    assert calls == expected_uids


@pytest.mark.asyncio
async def test_live_transcript_reconciles_until_one_final_idle_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DB liveness verdict, not file mtime, controls the final idle check."""
    import lionhub.serve.mirror as service_mod

    root = tmp_path / "projects"
    uid = "live-then-idle"
    path = root / "-work-acme" / f"{uid}.jsonl"
    _write_session_file(path, uid, age_secs=10)
    modified_at = 1_800_000_000.0
    # A copied/restored transcript can have an old filesystem timestamp while
    # the event timestamp persisted in StateDB is still live.  File mtime is a
    # scan hint, never the liveness source of truth.
    os.utime(path, (modified_at - 600, modified_at - 600))
    states = {
        str(path): _FileState(
            session_uid=uid,
            offset=path.stat().st_size,
            project="acme",
            attr_peeked=True,
        )
    }
    calls: list[str] = []
    settled = {"value": False}

    async def _record_reconcile(_db, reconciled_uid, **_kwargs):
        calls.append(reconciled_uid)
        return settled["value"]

    monkeypatch.setattr(service_mod.time, "time", lambda: modified_at)
    monkeypatch.setattr(
        "lionagi.state.claude_mirror.reconcile_session_status",
        _record_reconcile,
    )

    # Still live according to the persisted message clock: keep observing even
    # though the file itself already looks old.
    await _one_pass(None, root, states, since=None, live_window=300)
    await _one_pass(None, root, states, since=None, live_window=300)
    # The DB reports the final idle transition as settled.  Later unchanged
    # polls are quiescent.
    settled["value"] = True
    await _one_pass(None, root, states, since=None, live_window=300)
    await _one_pass(None, root, states, since=None, live_window=300)

    assert calls == [uid, uid, uid]


@pytest.mark.asyncio
async def test_an_unwindowed_pass_does_not_stat_for_a_window_it_will_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`li mirror` defaults to no window, and mtime has no consumer but the
    window check. Reading it anyway costs one syscall per transcript per pass,
    on the loop whose whole purpose is not to touch settled files."""
    import lionhub.serve.mirror as service_mod

    root = tmp_path / "projects"
    uid = "unwindowed"
    path = root / "-work-acme" / f"{uid}.jsonl"
    _write_session_file(path, uid, age_secs=10)
    now = 1_800_000_000.0
    os.utime(path, (now - 600, now - 600))

    real_stat = Path.stat
    stats: list[Path] = []

    def _counting_stat(self: Path, *args, **kwargs):
        if self == path:
            stats.append(self)
        return real_stat(self, *args, **kwargs)

    async def _settled_reconcile(_db, _uid, **_kwargs):
        return True

    monkeypatch.setattr(service_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.claude_mirror.reconcile_session_status",
        _settled_reconcile,
    )

    async def _count(since: float | None) -> int:
        states = {
            str(path): _FileState(
                session_uid=uid,
                offset=path.stat().st_size,
                project="acme",
                attr_peeked=True,
            )
        }
        stats.clear()
        monkeypatch.setattr(Path, "stat", _counting_stat)
        try:
            await _one_pass(None, root, states, since=since, live_window=300)
        finally:
            monkeypatch.setattr(Path, "stat", real_stat)
        return len(stats)

    # One stat either way for the cursor's size check, plus the window's own
    # when there is a window to enforce.
    assert await _count(None) == 1
    assert await _count(3600.0) == 2


@pytest.mark.asyncio
async def test_an_unwindowed_codex_pass_does_not_stat_for_the_window_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule on the Codex sweep, which carries its own copy of the check."""
    import lionhub.serve.mirror as service_mod

    now = 1_800_000_000.0
    uid = "0199cccc-0000-0000-0000-0000000000cc"
    path = tmp_path / "2026" / "08" / "16" / "rollout-unwindowed.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    os.utime(path, (now - 600, now - 600))

    real_stat = Path.stat
    stats: list[Path] = []

    def _counting_stat(self: Path, *args, **kwargs):
        if self == path:
            stats.append(self)
        return real_stat(self, *args, **kwargs)

    async def _settled_reconcile(_db, _uid, **_kwargs):
        return True

    monkeypatch.setattr(service_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.reconcile_session_status",
        _settled_reconcile,
    )

    async def _count(since: float | None) -> int:
        states = {
            str(path): _FileState(
                session_uid=uid,
                offset=path.stat().st_size,
                head_checked=True,
                codex_provenance_peeked=True,
            )
        }
        stats.clear()
        monkeypatch.setattr(Path, "stat", _counting_stat)
        try:
            await service_mod._codex_pass(
                None, tmp_path, states, since=since, live_window=300, threads={}
            )
        finally:
            monkeypatch.setattr(Path, "stat", real_stat)
        return len(stats)

    windowed = await _count(3600.0)
    assert await _count(None) == windowed - 1


@pytest.mark.asyncio
async def test_idle_codex_rollout_status_reconciliation_also_quiesces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded polling rule covers Studio's default Codex tree too."""
    import lionhub.serve.mirror as service_mod

    now = 1_800_000_000.0
    uid = "0199bbbb-0000-0000-0000-000000000099"
    path = tmp_path / "2026" / "08" / "11" / "rollout-idle.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    os.utime(path, (now - 600, now - 600))
    state = _FileState(
        session_uid=uid,
        offset=path.stat().st_size,
        head_checked=True,
        codex_provenance_peeked=True,
    )
    states = {str(path): state}
    calls: list[str] = []

    async def _record_reconcile(_db, reconciled_uid, **_kwargs):
        calls.append(reconciled_uid)
        return True

    monkeypatch.setattr(service_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.reconcile_session_status",
        _record_reconcile,
    )

    await service_mod._codex_pass(None, tmp_path, states, since=None, live_window=300, threads={})
    await service_mod._codex_pass(None, tmp_path, states, since=None, live_window=300, threads={})

    assert calls == [uid]


@pytest.mark.asyncio
async def test_one_pass_reprocesses_batch_when_write_fails(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed mirror write must not advance the persisted cursor past the
    # unmirrored events: the next pass re-reads and mirrors them (at-least-once +
    # idempotent), so a transient DB error never silently drops a batch.
    root = tmp_path / "projects"
    uid = "ffffffff-0000-0000-0000-00000000000a"
    _write_session_file(root / "-w-proj" / f"{uid}.jsonl", uid, age_secs=5)

    from lionagi.state import claude_mirror as cm

    real_mirror_session = cm.mirror_session
    fail = {"on": True}

    async def _maybe_fail(*a, **k):
        if fail["on"]:
            raise RuntimeError("db down")
        return await real_mirror_session(*a, **k)

    monkeypatch.setattr("lionagi.state.claude_mirror.mirror_session", _maybe_fail)

    states: dict[str, _FileState] = {}
    async with StateDB() as db:
        # Pass 1: the write fails; the cursor must NOT be persisted past the batch.
        await _one_pass(db, root, states, since=None, live_window=300)
        assert await db.get_session(session_db_id(uid)) is None
        # cursor never advanced past the unmirrored batch
        assert [state.offset for state in states.values()] == [0]

        # Pass 2: write restored; the same batch is mirrored, not lost.
        fail["on"] = False
        await _one_pass(db, root, states, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
        msgs = await db.get_progression(row["progression_id"])
    assert row is not None
    assert len(msgs) == 2  # both events survived the earlier failure


@pytest.mark.asyncio
async def test_mirror_forever_retries_after_connection_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient failure to OPEN the StateDB connection — e.g. a locked or
    # half-migrated state.db during first-run startup, when the studio creates the
    # schema and checkpoints on another connection — must be retried, not silently
    # end the in-process tail for the whole life of the studio process. Regression
    # guard: the connection used to be opened once outside the loop, so the first
    # transient open error killed the tail permanently and invisibly.
    import asyncio

    import lionagi.state.db as dbmod
    import lionhub.serve.mirror as service_mod

    monkeypatch.setattr(dbmod, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(service_mod, "_OFFSETS_PATH", tmp_path / "mirror" / "offsets.json")

    attempts = {"open": 0}
    real_open = dbmod.StateDB.open

    async def _flaky_open(self) -> None:
        attempts["open"] += 1
        if attempts["open"] == 1:
            raise OSError("database is locked (simulated first-run race)")
        await real_open(self)

    monkeypatch.setattr(dbmod.StateDB, "open", _flaky_open)

    stop = asyncio.Event()
    # root is an empty dir: passes find no transcripts, so this isolates the
    # connection-lifecycle behaviour from any mirroring work.
    task = asyncio.create_task(
        service_mod.mirror_forever(stop, root=tmp_path, since="24h", interval=0.05)
    )
    try:
        for _ in range(100):  # let it fail the first open, back off, and reconnect
            if attempts["open"] >= 2:
                break
            await asyncio.sleep(0.02)
        alive = not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    assert attempts["open"] >= 2, "open was not retried after the first failure"
    assert alive, "tail died on a transient open failure instead of retrying"


def test_read_new_events_buffers_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n{"a":2}\n{"a":3')  # last line incomplete
    state = _FileState(session_uid="x")
    first, sources, new_offset, _ = _read_new_events(path, state)
    assert [e["a"] for e in first] == [1, 2]
    assert len(sources) == len(first)
    state.offset = new_offset  # caller commits the cursor only after a durable mirror
    # Complete the dangling line; the next read picks up only the new event.
    with path.open("a") as fh:
        fh.write("}\n")
    second, sources2, _, _ = _read_new_events(path, state)
    assert [e["a"] for e in second] == [3]
    assert len(sources2) == len(second)


def test_read_new_events_resets_on_truncation(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n')
    state = _FileState(session_uid="x", offset=9999)  # offset past EOF
    out, _, _, _ = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1]


def test_read_new_events_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\nnot json\n{"a":2}\n')
    state = _FileState(session_uid="x")
    out, _, _, unreadable = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1, 2]
    # The dropped line is reported, not silently absorbed: a damaged transcript
    # and an uninteresting one must not read the same downstream.
    assert unreadable == 1


def test_read_new_events_skips_non_dict_json_without_losing_followers(tmp_path: Path) -> None:
    # Valid JSON of the wrong shape (a bare list / scalar) must be dropped as
    # malformed, not handed downstream as an event, and must not swallow the
    # valid events that follow it on later lines.
    path = tmp_path / "t.jsonl"
    path.write_text('[]\n{"a":1}\n42\n{"a":2}\n')
    state = _FileState(session_uid="x")
    out, _, _, unreadable = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1, 2]
    assert unreadable == 2  # both the bare list and the bare scalar


def test_first_prompt_skips_meta_and_command_noise() -> None:
    events = [
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": [{"type": "text", "text": "meta"}]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "<command-name>/x</command-name>"}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "the real question"}]}},
    ]
    assert _first_prompt(events) == "the real question"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("30m", 1800.0), ("12h", 43200.0), ("7d", 604800.0), ("120", 120.0), ("", None)],
)
def test_parse_window(spec: str, expected: float | None) -> None:
    assert _parse_window(spec) == expected


def test_parse_window_raises_on_malformed() -> None:
    # A bad spec must fail loudly, not silently become an unbounded (None) scan.
    with pytest.raises(ValueError, match="unrecognized --since window"):
        _parse_window("bad")


def test_since_window_argparse_type() -> None:
    import argparse

    assert _since_window("12h") == 43200.0
    with pytest.raises(argparse.ArgumentTypeError):
        _since_window("nonsense")
    with pytest.raises(argparse.ArgumentTypeError):
        _since_window("")  # empty is rejected at the CLI boundary (default=None handles "all")


def test_fallback_project_uses_folder_name_when_dir_exists(tmp_path: Path) -> None:
    work = tmp_path / "my-workspace"
    work.mkdir()
    assert _fallback_project(str(work)) == ("my-workspace", "cwd_dir")


def test_fallback_project_uses_others_when_dir_missing() -> None:
    assert _fallback_project("/no/such/dir/anymore") == ("others", "cwd_missing")


def test_derive_metadata_falls_back_to_folder_name(tmp_path: Path) -> None:
    # A cwd that detect_project can't place (no git remote / config / override)
    # is bucketed by its own folder name rather than left unattributed.
    work = tmp_path / "loose-scripts"
    work.mkdir()
    state = _FileState(session_uid=SID)
    _derive_metadata(
        state, [_user_text("u1", "hi", ts="2026-06-20T00:00:00.000Z") | {"cwd": str(work)}]
    )
    assert state.project == "loose-scripts"
    assert state.project_source == "cwd_dir"


def test_derive_metadata_others_when_cwd_gone() -> None:
    state = _FileState(session_uid=SID)
    _derive_metadata(state, [_user_text("u1", "hi") | {"cwd": "/gone/missing/path"}])
    assert state.project == "others"
    assert state.project_source == "cwd_missing"


def test_derive_metadata_captures_raw_cwd_as_artifact_root(tmp_path: Path) -> None:
    # the raw cwd (not the bucketed project name) is the session's
    # artifact root -- every file the CLI touched lives under it.
    work = tmp_path / "my-workspace"
    work.mkdir()
    state = _FileState(session_uid=SID)
    _derive_metadata(
        state, [_user_text("u1", "hi", ts="2026-06-20T00:00:00.000Z") | {"cwd": str(work)}]
    )
    assert state.cwd == str(work)


@pytest.mark.asyncio
async def test_lineage_links_continued_session(temp_db_path: Path, tmp_path: Path) -> None:
    # Session B's first message points (parentUuid) at session A's last message:
    # B continues A. The mirror records that as a lineage link on B.
    root = tmp_path / "projects"
    a, b = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    _write_lineage_file(
        root / "-w-proj" / f"{a}.jsonl",
        [
            _lineage_event(a, "a-1", None, "user", "start the work"),
            _lineage_event(a, "a-leaf", "a-1", "assistant", "done, ending here"),
        ],
    )
    _write_lineage_file(
        root / "-w-proj" / f"{b}.jsonl",
        [
            _lineage_event(b, "b-1", "a-leaf", "user", "continuing from before"),
            _lineage_event(b, "b-2", "b-1", "assistant", "picking it up"),
        ],
    )
    async with StateDB() as db:
        await _one_pass(db, root, {}, since=None, live_window=300)
        child = await db.get_session(session_db_id(b))
    lineage = child["node_metadata"]["lineage"]
    assert lineage["parent_session_id"] == session_db_id(a)
    assert lineage["parent_session_uid"] == a
    assert lineage["parent_event_uuid"] == "a-leaf"


@pytest.mark.asyncio
async def test_no_lineage_for_self_rooted_session(temp_db_path: Path, tmp_path: Path) -> None:
    root = tmp_path / "projects"
    s = "cccccccc-0000-0000-0000-000000000003"
    _write_lineage_file(
        root / "-w-proj" / f"{s}.jsonl",
        [
            _lineage_event(s, "c-1", None, "user", "fresh start"),
            _lineage_event(s, "c-2", "c-1", "assistant", "ok"),
        ],
    )
    async with StateDB() as db:
        await _one_pass(db, root, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(s))
    assert "lineage" not in (row["node_metadata"] or {})


@pytest.mark.asyncio
async def test_no_lineage_for_same_session_across_files(temp_db_path: Path, tmp_path: Path) -> None:
    # Two files share one sessionId (a resumed session). File 2's head points at
    # file 1's leaf — same session, so it is NOT cross-session lineage.
    root = tmp_path / "projects"
    s = "dddddddd-0000-0000-0000-000000000004"
    _write_lineage_file(
        root / "-w-proj" / f"{s}-1.jsonl",
        [
            _lineage_event(s, "d-1", None, "user", "part one"),
            _lineage_event(s, "d-mid", "d-1", "assistant", "more"),
        ],
    )
    _write_lineage_file(
        root / "-w-proj" / f"{s}-2.jsonl",
        [
            _lineage_event(s, "d-3", "d-mid", "user", "part two same session"),
            _lineage_event(s, "d-4", "d-3", "assistant", "ok"),
        ],
    )
    async with StateDB() as db:
        await _one_pass(db, root, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(s))
    assert "lineage" not in (row["node_metadata"] or {})


def test_lineage_resolve_skips_unindexed_and_same_session() -> None:
    lin = _Lineage()
    lin.leaf_owner = {"leaf-A": "sessA"}
    lin.pending = {
        "sessB": "leaf-A",  # resolves to a different session -> link
        "sessC": "unknown-leaf",  # parent not indexed -> stays pending
        "sessA": "leaf-A",  # resolves to itself -> not lineage
    }
    links = lin.resolve()
    assert links == [("sessB", "sessA", "leaf-A")]
    assert lin.pending == {"sessC": "unknown-leaf"}  # unresolved stays for next pass
    assert "sessB" in lin.linked


@pytest.mark.asyncio
async def test_idle_session_backfilled_with_project(temp_db_path: Path, tmp_path: Path) -> None:
    # A session fully mirrored before attribution (row has no project) and now
    # idle (file at EOF, no new events) is still backfilled from its head cwd.
    work = tmp_path / "ghost-proj"
    work.mkdir()
    uid = "eeeeeeee-0000-0000-0000-000000000005"
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{uid}.jsonl"
    events = [
        _lineage_event(uid, "e-1", None, "user", "hi") | {"cwd": str(work)},
        _lineage_event(uid, "e-2", "e-1", "assistant", "ok"),
    ]
    _write_lineage_file(path, events)
    async with StateDB() as db:
        await mirror_session(db, session_uid=uid, events=events, tool_names={}, project=None)
        assert (await db.get_session(session_db_id(uid)))["project"] is None
        # Idle pass: file already fully read (offset at EOF) -> no streamed events.
        states = {str(path): _FileState(session_uid="", offset=path.stat().st_size)}
        await _one_pass(db, root, states, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["project"] == "ghost-proj"
    assert row["project_source"] == "cwd_dir"


@pytest.mark.asyncio
async def test_idle_session_backfilled_with_artifacts_path_even_when_project_already_set(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # the dominant NULL-artifacts_path population is sessions the
    # mirror already attributed a project to (in an earlier process) before this
    # fix existed -- artifacts_path must backfill on its own, not only when
    # project is also missing.
    work = tmp_path / "ghost-proj-2"
    work.mkdir()
    uid = "ffffffff-0000-0000-0000-000000000006"
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{uid}.jsonl"
    events = [
        _lineage_event(uid, "f-1", None, "user", "hi") | {"cwd": str(work)},
        _lineage_event(uid, "f-2", "f-1", "assistant", "ok"),
    ]
    _write_lineage_file(path, events)
    async with StateDB() as db:
        # Simulate a pre-fix row: project already attributed, artifacts_path never was.
        await mirror_session(
            db, session_uid=uid, events=events, tool_names={}, project="ghost-proj-2"
        )
        before = await db.get_session(session_db_id(uid))
        assert before["project"] == "ghost-proj-2"
        assert before["artifacts_path"] is None
        states = {str(path): _FileState(session_uid="", offset=path.stat().st_size)}
        await _one_pass(db, root, states, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["artifacts_path"] == str(work)
    assert row["project"] == "ghost-proj-2"


@pytest.mark.asyncio
async def test_attr_peeked_flag_not_set_on_backfill_failure(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same failure-atomicity requirement as the codex idle backfill
    (test_codex_provenance_peeked_flag_not_set_on_backfill_failure): a
    transient set_session_provenance failure inside the idle project
    backfill must not permanently mark this in-memory state as peeked, or no
    later pass in this process ever retries it."""
    from lionhub.serve import mirror as service_mod

    work = tmp_path / "ghost-proj-flaky"
    work.mkdir()
    uid = "eeeeeeee-0000-0000-0000-000000000099"
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{uid}.jsonl"
    events = [
        _lineage_event(uid, "g-1", None, "user", "hi") | {"cwd": str(work)},
        _lineage_event(uid, "g-2", "g-1", "assistant", "ok"),
    ]
    _write_lineage_file(path, events)

    attempts = []
    real_attribute = service_mod._attribute_idle

    async def flaky_attribute(db, state, cwd):
        attempts.append(state.session_uid)
        if len(attempts) == 1:
            raise RuntimeError("set_session_provenance transient failure")
        return await real_attribute(db, state, cwd)

    monkeypatch.setattr(service_mod, "_attribute_idle", flaky_attribute)

    async with StateDB() as db:
        await mirror_session(db, session_uid=uid, events=events, tool_names={}, project=None)
        assert (await db.get_session(session_db_id(uid)))["project"] is None

        states = {str(path): _FileState(session_uid="", offset=path.stat().st_size)}
        # _one_pass's own per-transcript exception handler swallows the raise.
        await _one_pass(db, root, states, since=None, live_window=300)
        state = states[str(path)]
        assert not state.attr_peeked, (
            "the flag was set even though the backfill raised, so no later pass will retry it"
        )

        await _one_pass(db, root, states, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert state.attr_peeked
    assert row["project"] == "ghost-proj-flaky"
    assert len(attempts) == 2, f"backfill was attempted {len(attempts)} time(s), not 2"


@pytest.mark.asyncio
async def test_peek_head_skips_non_dict_head_line(temp_db_path: Path, tmp_path: Path) -> None:
    # A valid-JSON-but-non-dict head line (e.g. `[]`) must not wedge the idle
    # reconcile path: _peek_head must skip it like _read_new_events, so an
    # already-mirrored, now-idle session still reconciles to completed instead of
    # being left stuck at running by an AttributeError swallowed mid-pass.
    root = tmp_path / "projects"
    uid = "abababab-0000-0000-0000-0000000000cd"
    path = root / "-w-proj" / f"{uid}.jsonl"
    _write_session_file(path, uid, age_secs=7200)  # idle -> should become completed
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    path.write_text("[]\n" + path.read_text())  # prepend a non-dict JSON head line
    async with StateDB() as db:
        await mirror_session(db, session_uid=uid, events=events, tool_names={}, status="running")
        assert (await db.get_session(session_db_id(uid)))["status"] == "running"
        # Idle pass: file already fully read (offset at EOF) -> no streamed events,
        # so _peek_head scans the head and hits the `[]` line.
        states = {str(path): _FileState(session_uid="", offset=path.stat().st_size)}
        await _one_pass(db, root, states, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_name_survives_restart_between_use_and_result(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A restart between a tool_use and its tool_result must not drop the response's
    # function name. tool_names is rebuilt from the persisted per-file state, so the
    # ActionResponse still labels the call instead of falling back to "".
    monkeypatch.setattr("lionhub.serve.mirror._OFFSETS_PATH", tmp_path / "offsets.json")
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{SID}.jsonl"
    _write_lineage_file(
        path,
        [
            _user_text("u1", "run it"),
            _assistant("a1", [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}}]),
        ],
    )
    async with StateDB() as db:
        # Pass 1: mirror the tool_use, persist the cursor + tool_names, then drop
        # all in-memory state — a restart.
        states1: dict[str, _FileState] = {}
        await _one_pass(db, root, states1, since=None, live_window=300)
        _save_states(states1)

        # Restart: state comes back from disk only.
        states2 = _load_states()

        # The tool_result lands after the restart.
        with path.open("a") as fh:
            fh.write(json.dumps(_tool_result("u2", "tool_1", "total 0")) + "\n")
        await _one_pass(db, root, states2, since=None, live_window=300)

        row = await db.get_session(session_db_id(SID))
        contents = []
        for mid in await db.get_progression(row["progression_id"]):
            c = (await db.get_message(mid))["content"]
            contents.append(json.loads(c) if isinstance(c, str) else c)
    resp = next(c for c in contents if "action_request_id" in c)
    assert resp["function"] == "Bash"  # recovered from persisted tool_names, not ""


@pytest.mark.asyncio
async def test_a_failed_batch_is_re_read_with_the_tool_names_it_consumed(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tool_use lands in one committed pass; its result arrives in a batch whose write
    # fails. The re-read of that batch must still find the use's name, so the map is only
    # adopted once the batch is durable, like the cursor.
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{SID}.jsonl"
    _write_lineage_file(
        path,
        [
            _user_text("u1", "run it"),
            _assistant("a1", [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}}]),
        ],
    )
    async with StateDB() as db:
        states: dict[str, _FileState] = {}
        await _one_pass(db, root, states, since=None, live_window=300)
        state = states[str(path)]
        assert state.tool_names == {"tool_1": "Bash"}
        offset_before = state.offset

        with path.open("a") as fh:
            fh.write(json.dumps(_tool_result("u2", "tool_1", "total 0")) + "\n")

        async def _fail(*args, **kwargs):
            raise RuntimeError("write failed")

        with monkeypatch.context() as m:
            m.setattr(db, "insert_messages", _fail)
            await _one_pass(db, root, states, since=None, live_window=300)
        assert state.offset == offset_before
        assert state.tool_names == {"tool_1": "Bash"}

        await _one_pass(db, root, states, since=None, live_window=300)
        assert state.tool_names == {}
        contents = await _session_contents(db)
    assert _response_functions(contents) == ["Bash"]


def test_a_legacy_tool_name_map_loads_as_its_newest_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lionhub.serve.mirror as service_mod

    offsets = tmp_path / "offsets.json"
    monkeypatch.setattr(service_mod, "_OFFSETS_PATH", offsets)
    names = {f"tool_{i}": f"Fn{i}" for i in range(100)}
    offsets.write_text(json.dumps({"f": {"offset": 5, "session_uid": "s", "tool_names": names}}))

    loaded = _load_states()["f"].tool_names

    keep = service_mod._LEGACY_TOOL_NAMES_KEEP
    assert list(loaded) == [f"tool_{i}" for i in range(100 - keep, 100)]
    assert loaded["tool_99"] == "Fn99"


def test_a_saved_pending_tool_name_map_loads_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lionhub.serve.mirror as service_mod

    monkeypatch.setattr(service_mod, "_OFFSETS_PATH", tmp_path / "offsets.json")
    # More uses waiting for a result than a legacy map keeps, as one message with many
    # parallel tool calls leaves them across a restart.
    names = {f"tool_{i}": f"Fn{i}" for i in range(service_mod._LEGACY_TOOL_NAMES_KEEP + 6)}
    _save_states({"f": _FileState(session_uid="s", offset=5, tool_names=dict(names))})

    assert _load_states()["f"].tool_names == names


def test_the_saved_map_is_also_written_under_the_key_earlier_releases_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lionhub.serve.mirror as service_mod

    offsets = tmp_path / "offsets.json"
    monkeypatch.setattr(service_mod, "_OFFSETS_PATH", offsets)
    names = {"tool_1": "Read", "tool_2": "Bash"}
    _save_states({"f": _FileState(session_uid="s", offset=5, tool_names=dict(names))})

    saved = json.loads(offsets.read_text())["f"]
    assert saved["pending_tool_names"] == names
    # An earlier release reads only "tool_names", and saves it alone; both directions keep
    # the names.
    assert saved["tool_names"] == names
    offsets.write_text(json.dumps({"f": {"offset": 5, "session_uid": "s", "tool_names": names}}))
    assert _load_states()["f"].tool_names == names


@pytest.mark.asyncio
async def test_a_failed_codex_batch_is_re_read_in_the_turn_it_started_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The walk updates the turn as it meets turn_context records. A batch whose write fails
    # after a turn change must be re-read from the turn it started in, or its leading
    # messages are attributed to the later turn.
    import lionagi.state.codex_mirror as codex_mod
    from lionhub.serve.mirror import _mirror_one_codex

    def turn(turn_id: str, model: str) -> dict:
        return {
            "type": "turn_context",
            "timestamp": "2026-07-29T12:00:00Z",
            "payload": {"model": model, "effort": "low", "turn_id": turn_id},
        }

    def said(mid: str, text: str) -> dict:
        return {
            "type": "response_item",
            "timestamp": "2026-07-29T12:00:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": mid,
                "content": [{"type": "output_text", "text": text}],
            },
        }

    path = tmp_path / "rollout-turn.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in (turn("t1", "m-one"), said("a1", "one"))))
    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await _mirror_one_codex(db, path, state, {})
        assert state.turn["turn_id"] == "t1"
        offset_before = state.offset
        with path.open("a") as fh:
            for r in (said("a2", "two"), turn("t2", "m-two"), said("a3", "three")):
                fh.write(json.dumps(r) + "\n")

        async def _fail(*args, **kwargs):
            raise RuntimeError("write failed")

        with monkeypatch.context() as m:
            m.setattr(codex_mod, "write_mirrored_messages", _fail)
            with pytest.raises(RuntimeError, match="write failed"):
                await _mirror_one_codex(db, path, state, {})
        assert state.offset == offset_before
        assert state.turn["turn_id"] == "t1"

        await _mirror_one_codex(db, path, state, {})
        assert state.turn["turn_id"] == "t2"
        turns = {}
        for mid in ("a1", "a2", "a3"):
            row = await db.get_message(codex_mod._det(path.stem, mid, "text"))
            meta = row["node_metadata"]
            meta = json.loads(meta) if isinstance(meta, str) else meta
            turns[mid] = meta["codex_turn"]["turn_id"]
    assert turns == {"a1": "t1", "a2": "t1", "a3": "t2"}


def test_an_unchanged_save_leaves_offsets_json_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lionhub.serve.mirror as service_mod

    offsets = tmp_path / "offsets.json"
    monkeypatch.setattr(service_mod, "_OFFSETS_PATH", offsets)
    states = {"f": _FileState(session_uid="s", offset=12, tool_names={"t1": "Read"})}

    _save_states(states)
    first = offsets.read_text()
    inode = offsets.stat().st_ino
    _save_states(states)
    assert offsets.read_text() == first
    assert offsets.stat().st_ino == inode  # not replaced

    states["f"].offset = 40
    _save_states(states)
    assert offsets.stat().st_ino != inode  # a changed state is written
    assert json.loads(offsets.read_text())["f"]["offset"] == 40

    # The comparison is with the file, so one removed or replaced from outside comes back.
    offsets.unlink()
    _save_states(states)
    assert json.loads(offsets.read_text())["f"]["offset"] == 40
    offsets.write_text("{}")
    _save_states(states)
    assert json.loads(offsets.read_text())["f"]["offset"] == 40


@pytest.mark.asyncio
async def test_lineage_resolves_after_restart(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parent transcript is mirrored, then state is dropped (restart) before the
    # continuation arrives. Its leaf is re-indexed from persisted state on reload,
    # so the child still links to it instead of losing the cross-session lineage.
    monkeypatch.setattr("lionhub.serve.mirror._OFFSETS_PATH", tmp_path / "offsets.json")
    root = tmp_path / "projects"
    a = "a1a1a1a1-0000-0000-0000-000000000aaa"
    b = "b2b2b2b2-0000-0000-0000-000000000bbb"
    _write_lineage_file(
        root / "-w-proj" / f"{a}.jsonl",
        [
            _lineage_event(a, "a-1", None, "user", "start the work"),
            _lineage_event(a, "a-leaf", "a-1", "assistant", "done, ending here"),
        ],
    )
    async with StateDB() as db:
        # Pass 1: mirror only the parent; persist its leaf index, then restart.
        states1: dict[str, _FileState] = {}
        await _one_pass(db, root, states1, since=None, live_window=300)
        _save_states(states1)

        # Restart: rebuild states + re-seed the leaf index from disk only.
        states2 = _load_states()
        lineage2 = _Lineage()
        _seed_lineage(lineage2, states2)

        # The continuation opens after the restart, pointing at the parent's leaf.
        _write_lineage_file(
            root / "-w-proj" / f"{b}.jsonl",
            [
                _lineage_event(b, "b-1", "a-leaf", "user", "continuing from before"),
                _lineage_event(b, "b-2", "b-1", "assistant", "picking it up"),
            ],
        )
        await _one_pass(db, root, states2, since=None, live_window=300, lineage=lineage2)
        child = await db.get_session(session_db_id(b))
    lineage = child["node_metadata"]["lineage"]
    assert lineage["parent_session_uid"] == a
    assert lineage["parent_event_uuid"] == "a-leaf"


async def test_codex_file_that_mirrors_nothing_is_reported_not_skipped(tmp_path, caplog):
    """A rollout read in full that produces no messages is surfaced, not passed over.

    Without this it is indistinguishable from a file the mirror has not reached:
    mirror_session writes no session row when a batch yields nothing, so there is
    no row carrying the counts either. The measured cause on a real corpus is the
    pre-2025-09-20 flat rollout format, which matches no record type read here.
    """
    import logging

    from lionagi.state.db import StateDB
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    path = tmp_path / "rollout-legacy.jsonl"
    # Flat legacy records: a type at the top level and no payload envelope.
    path.write_text(
        '{"id":"x","instructions":"y","timestamp":"2025-09-01T09:34:37Z"}\n'
        '{"type":"message","role":"user","content":[{"text":"hi"}]}\n'
        '{"type":"function_call","name":"shell","arguments":"{}"}\n'
    )
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        with caplog.at_level(logging.WARNING):
            written = await _mirror_one_codex(db, path, state, {})

    assert written == 0
    text = " ".join(r.message for r in caplog.records)
    assert "rollout-legacy.jsonl" in text
    assert "mirrored none" in text
    # The record types it did see are named, so the reason is diagnosable from the
    # log alone without re-reading the file.
    assert "function_call" in text and "message" in text
    # Reported once per file, not on every poll pass.
    assert state.barren_reported


async def test_orchestrated_rollout_is_never_mirrored(tmp_path):
    """A `codex exec` rollout is an orchestrator's run — the run that spawned it
    already has a session of its own, so mirroring it would show the same work
    twice (the agent's session plus an extra "codex" one)."""
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000001"
    path = tmp_path / "rollout-exec.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated
        assert await db.get_session(codex_sid(uid)) is None
        # Later passes stay skipped without re-reading the file.
        assert await _mirror_one_codex(db, path, state, {}) == 0


async def test_an_imported_rollout_is_named_from_its_first_prompt(tmp_path):
    """Every imported rollout was landing under the same generic name.

    The name is derived by reading the instruction off a mirrored message, and
    the message carries it on a content model rather than in a mapping. Asked
    for a mapping key, the derivation found nothing for every rollout ever
    imported and each one fell through to the fallback -- so a board of live
    sessions showed one repeated title with nothing to tell them apart.

    Driven through the whole mirror rather than the derivation alone: the two
    halves each look right in isolation, and it is the seam between them that
    carried the defect.
    """
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-00000000000a"
    path = tmp_path / "rollout-named.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) > 0
        session = await db.get_session(codex_sid(uid))

    assert session is not None
    assert session["name"] == "q", (
        f"named {session['name']!r}; the first prompt never reached the name"
    )


async def test_a_rollout_with_no_real_prompt_keeps_the_generic_name(tmp_path):
    """The arm that separates deriving the right name from deriving any name.

    Codex opens some rollouts with an injected context block and no human turn
    at all. Those carry nothing worth showing, so they are expected to keep the
    fallback -- without this, a derivation that returned the injected block
    would pass the test above while putting machine plumbing on the board.
    """
    import json as _json

    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-00000000000b"
    lines = _codex_rollout_lines(uid, "Codex Desktop").splitlines()
    injected = _json.loads(lines[1])
    injected["payload"]["content"] = [{"text": "<environment_context>cwd=/x</environment_context>"}]
    lines[1] = _json.dumps(injected)
    path = tmp_path / "rollout-injected.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await _mirror_one_codex(db, path, state, {})
        session = await db.get_session(codex_sid(uid))

    assert session is not None
    assert session["name"] == "Codex session", (
        f"named {session['name']!r}; an injected context block became the title"
    )


async def test_orchestrated_rollout_absorbs_an_earlier_import(tmp_path):
    """A row imported before this rule existed is removed the first time the
    mirror reclassifies its rollout, so old double entries heal on upgrade."""
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000002"
    path = tmp_path / "rollout-exec-old.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(
            db,
            rollout_uid=uid,
            records=[json.loads(line) for line in path.read_text().splitlines()],
            tool_names={},
            source_path=str(path),
        )
        assert await db.get_session(codex_sid(uid)) is not None

        state = _FileState(session_uid="")
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert await db.get_session(codex_sid(uid)) is None


async def test_interactive_rollout_still_mirrors(tmp_path):
    """The skip is scoped to orchestrated originators: desktop/TUI/IDE history —
    the mirror's actual subject — keeps mirroring exactly as before."""
    from lionagi.state.codex_mirror import SOURCE_KIND as CODEX_SOURCE_KIND
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000003"
    path = tmp_path / "rollout-desktop.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        written = await _mirror_one_codex(db, path, state, {})
        assert written == 2
        assert not state.orchestrated
        row = await db.get_session(codex_sid(uid))
        assert row is not None
        assert row["source_kind"] == CODEX_SOURCE_KIND


async def test_interactive_rollout_records_artifacts_path_from_header_cwd(tmp_path):
    # the session_meta header's cwd is the rollout's artifact root,
    # the same gap claude_mirror had.
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000005"
    path = tmp_path / "rollout-artifacts.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await _mirror_one_codex(db, path, state, {})
        row = await db.get_session(codex_sid(uid))
    assert row["artifacts_path"] == "/x"


async def test_idle_codex_rollout_backfills_artifacts_path_from_header_cwd(tmp_path):
    # An existing row (mirrored before cwd attribution existed, or by a process
    # that crashed before this pass) has artifacts_path=NULL. A later process
    # restarts with its offset already restored to EOF -- _read_new_events
    # yields no records even though the header (re-read on this fresh
    # _FileState) still carries cwd. Without an idle backfill this row's
    # artifacts_path would never be set.
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000007"
    path = tmp_path / "rollout-idle.jsonl"
    contents = _codex_rollout_lines(uid, "Codex Desktop")
    path.write_text(contents)

    records = [json.loads(line) for line in contents.splitlines()]
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(db, rollout_uid=uid, records=records, tool_names={}, cwd=None)
        before = await db.get_session(codex_sid(uid))
        assert before is not None
        assert before["artifacts_path"] is None

        # Fresh state (as after a restart), offset restored to EOF.
        state = _FileState(session_uid="", offset=len(contents.encode()))
        written = await _mirror_one_codex(db, path, state, {})
        assert written == 0

        row = await db.get_session(codex_sid(uid))
    assert row["artifacts_path"] == "/x"


async def test_codex_provenance_peeked_flag_not_set_on_backfill_failure(tmp_path, monkeypatch):
    """codex_provenance_peeked is never persisted -- it only exists to avoid a
    redundant DB round-trip on every idle pass within one process's lifetime.
    Setting it before the backfill it guards succeeds means a transient
    set_session_provenance failure (e.g. a DB hiccup) permanently blocks
    retry for the rest of that process, even though the flag's own docstring
    promise is "attempted", not "attempted once and given up on"."""
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve import mirror as service_mod
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000010"
    path = tmp_path / "rollout-idle-flaky.jsonl"
    contents = _codex_rollout_lines(uid, "Codex Desktop")
    path.write_text(contents)
    records = [json.loads(line) for line in contents.splitlines()]

    attempts = []
    real_attribute = service_mod._attribute_idle_codex

    async def flaky_attribute(db, state):
        attempts.append(state.session_uid)
        if len(attempts) == 1:
            raise RuntimeError("set_session_provenance transient failure")
        return await real_attribute(db, state)

    monkeypatch.setattr(service_mod, "_attribute_idle_codex", flaky_attribute)

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(db, rollout_uid=uid, records=records, tool_names={}, cwd=None)
        before = await db.get_session(codex_sid(uid))
        assert before["artifacts_path"] is None

        state = _FileState(session_uid="", offset=len(contents.encode()))
        with pytest.raises(RuntimeError):
            await _mirror_one_codex(db, path, state, {})
        assert not state.codex_provenance_peeked, (
            "the flag was set even though the backfill raised, so no later pass will retry it"
        )

        written = await _mirror_one_codex(db, path, state, {})
        assert written == 0
        assert state.codex_provenance_peeked

        row = await db.get_session(codex_sid(uid))
    assert row["artifacts_path"] == "/x"
    assert len(attempts) == 2, f"backfill was attempted {len(attempts)} time(s), not 2"


async def test_partial_header_defers_classification_instead_of_bypassing_it(tmp_path):
    """A rollout whose first line is still being written must not settle its
    classification: committing the head check on an unreadable header would let
    an orchestrated rollout mirror forever once the header completed."""
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000004"
    full = _codex_rollout_lines(uid, "codex_exec")
    header_line = full.splitlines(keepends=True)[0]
    path = tmp_path / "rollout-partial.jsonl"
    path.write_text(header_line[: len(header_line) // 2])  # torn mid-JSON, no newline

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert not state.head_checked  # classification deferred, not spent
        assert not state.orchestrated

        path.write_text(full)  # the writer finished the file
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated
        assert await db.get_session(codex_sid(uid)) is None


async def test_reclassification_absorbs_a_row_keyed_by_the_stem_fallback(tmp_path):
    """An earlier version that failed the header peek imported the file under
    its path stem. When classification finally lands, that stem-keyed row is
    absorbed too, not just the rollout-uid one."""
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000005"
    path = tmp_path / "rollout-stem-import.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    stem = path.stem

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(
            db,
            rollout_uid=stem,  # what the old fallback keyed the row by
            records=[json.loads(line) for line in path.read_text().splitlines()],
            tool_names={},
            source_path=str(path),
        )
        assert await db.get_session(codex_sid(stem)) is not None

        # Restart shape: the persisted state still carries the stem uid.
        state = _FileState(session_uid=stem)
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated
        assert await db.get_session(codex_sid(stem)) is None
        assert await db.get_session(codex_sid(uid)) is None


async def test_torn_header_defers_mirroring_so_one_rollout_stays_one_session(tmp_path):
    """When the header is torn the whole file waits: writing records under the
    path stem while the real UID arrives next pass would split one interactive
    rollout into two sessions."""
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000006"
    full = _codex_rollout_lines(uid, "Codex Desktop")
    header_line = full.splitlines(keepends=True)[0]
    path = tmp_path / "rollout-torn-interactive.jsonl"
    path.write_text(header_line[: len(header_line) // 2])  # torn mid-JSON, no newline

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert not state.head_checked
        assert not state.session_uid  # nothing was keyed under the stem
        assert await db.get_session(codex_sid(path.stem)) is None

        path.write_text(full)  # the writer finished the file
        assert await _mirror_one_codex(db, path, state, {}) == 2
        assert state.session_uid == uid
        assert await db.get_session(codex_sid(uid)) is not None
        assert await db.get_session(codex_sid(path.stem)) is None


async def test_headerless_rollout_still_mirrors_under_the_stem(tmp_path):
    """A complete first line that is not a session_meta settles classification:
    an append-only file never gains a header later, so the stem fallback is
    correct and the file keeps mirroring."""
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    full = _codex_rollout_lines("ignored-uid", "x")
    body = "".join(full.splitlines(keepends=True)[1:])  # drop the meta line
    path = tmp_path / "rollout-headerless.jsonl"
    path.write_text(body)

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 2
        assert state.head_checked
        assert state.session_uid == path.stem
        assert await db.get_session(codex_sid(path.stem)) is not None


async def test_complete_corrupt_header_settles_headerless_and_mirrors_the_body(tmp_path):
    """A newline-terminated first line that cannot parse is permanently corrupt
    (append-only file), not torn: the file settles as headerless and its valid
    body records still mirror instead of being suppressed forever."""
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    full = _codex_rollout_lines("ignored-uid", "x")
    body = "".join(full.splitlines(keepends=True)[1:])  # the two valid records

    corrupt = tmp_path / "rollout-corrupt-header.jsonl"
    corrupt.write_text('{"broken":\n' + body)  # complete but unparseable first line

    bad_utf8 = tmp_path / "rollout-bad-utf8-header.jsonl"
    bad_utf8.write_bytes(b"\xff\xfe garbage\n" + body.encode())

    # A BOM-shaped prefix surfaces as JSONDecodeError; a bare invalid byte
    # surfaces as UnicodeDecodeError — the body reader must survive both.
    bad_byte = tmp_path / "rollout-bad-byte-header.jsonl"
    bad_byte.write_bytes(b"\x80 invalid utf8\n" + body.encode())

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        for path in (corrupt, bad_utf8, bad_byte):
            state = _FileState(session_uid="")
            assert await _mirror_one_codex(db, path, state, {}) == 2
            assert state.head_checked
            assert state.session_uid == path.stem
            assert await db.get_session(codex_sid(path.stem)) is not None


async def test_valid_but_unterminated_header_stays_torn_until_the_newline(tmp_path):
    """A first line that parses as session_meta but has no trailing newline is
    still being written — appended bytes could extend or corrupt it — so the
    file defers instead of spending the identity fence on a provisional parse."""
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionhub.serve.mirror import _FileState, _mirror_one_codex, _peek_codex_head

    uid = "0199bbbb-0000-0000-0000-000000000007"
    full = _codex_rollout_lines(uid, "Codex Desktop")
    header_line = full.splitlines(keepends=True)[0]
    path = tmp_path / "rollout-unterminated-meta.jsonl"
    path.write_text(header_line.rstrip("\n"))  # valid JSON, newline not yet written

    assert _peek_codex_head(path) == ("torn", None)

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert not state.head_checked
        assert await db.get_session(codex_sid(path.stem)) is None

        path.write_text(full)  # the writer finished the header and the body
        assert await _mirror_one_codex(db, path, state, {}) == 2
        assert state.session_uid == uid
        assert await db.get_session(codex_sid(uid)) is not None


async def test_a_failed_absorption_does_not_retire_the_file(tmp_path, monkeypatch):
    """A contended teardown gives up rather than waiting, so absorption can fail
    for an ordinary reason. The file must stay eligible when it does.

    Marking the rollout absorbed before the absorption returns retires it on the
    early guard, and no later pass ever attempts the deletion again: a row nobody
    tears down, recorded as done. The discriminating assertion is the second
    attempt — with the flag set first, the observed attempt count is one.
    """
    from lionhub.serve import mirror as mirror_mod
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-00000000000f"
    path = tmp_path / "rollout-exec-contended.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    state = _FileState(session_uid="")

    attempts = []

    async def flaky_absorb(db, rollout_uid):
        attempts.append(rollout_uid)
        if len(attempts) == 1:
            raise RuntimeError("lock not available")
        return True

    monkeypatch.setattr(mirror_mod, "absorb_orchestrated_session", flaky_absorb, raising=False)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.absorb_orchestrated_session", flaky_absorb, raising=False
    )

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        with pytest.raises(RuntimeError):
            await _mirror_one_codex(db, path, state, {})
        assert not state.orchestrated, (
            "the rollout was marked absorbed even though absorption failed, so no "
            "later pass will retry the teardown"
        )

        # The next sweep reaches absorption again and completes.
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated

    assert len(attempts) == 2, f"absorption was attempted {len(attempts)} time(s), not 2"


async def test_a_failed_prior_stem_absorption_also_leaves_the_file_eligible(tmp_path, monkeypatch):
    """The orchestrated branch absorbs under two ids when the file was keyed by a
    stem before its header arrived. Failure-atomicity has to cover the second
    call as well as the first.

    The arm that matters is the id list on the retry. If any field were committed
    between the two calls, the retry would resolve `prior_uid` differently and the
    stem-keyed row would never be absorbed at all — a row nobody tears down, from
    a failure that looked like it had been retried.
    """
    from lionhub.serve import mirror as mirror_mod
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000010"
    path = tmp_path / "rollout-exec-prior.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    # A pre-header pass keyed this file by its stem.
    stem = path.stem
    state = _FileState(session_uid=stem)

    calls: list[str] = []

    async def flaky_absorb(db, rollout_uid):
        calls.append(rollout_uid)
        # Fail the SECOND call of the first pass, the prior-stem one.
        if len(calls) == 2:
            raise RuntimeError("lock not available")
        return True

    monkeypatch.setattr(mirror_mod, "absorb_orchestrated_session", flaky_absorb, raising=False)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.absorb_orchestrated_session", flaky_absorb, raising=False
    )

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        with pytest.raises(RuntimeError):
            await _mirror_one_codex(db, path, state, {})
        assert not state.orchestrated
        assert not state.head_checked
        assert state.session_uid == stem, (
            "session_uid was overwritten before both absorptions returned, so the "
            "retry can no longer tell which prior id this file was keyed by"
        )

        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated

    assert calls == [uid, stem, uid, stem], f"absorption ids across both passes: {calls}"


@pytest.mark.asyncio
async def test_live_claude_ingest_bounds_oversized_message_via_one_pass(
    temp_db_path: Path, tmp_path: Path
) -> None:
    """`_one_pass` (claude_mirror.py's live ingestion sweep) must write a bounded
    preview plus a resolvable source pointer for an oversized message, not the
    raw unbounded text."""
    from lionagi.state._mirror_common import resolve_mirrored_content

    uid = "0199dddd-0000-0000-0000-000000000001"
    root = tmp_path / "projects"
    path = _claude_oversized_file(root, uid)

    async with StateDB() as db:
        await _one_pass(db, root, {}, since=None, live_window=300)
        branch_id = _det(uid, "branch")
        rows = await db.get_branch_messages(branch_id)

    assert len(rows) == 1
    row = rows[0]
    stored = row["content"]["instruction"]
    assert stored != _OVERSIZED_TEXT
    assert len(stored) < len(_OVERSIZED_TEXT)
    pointer = row["node_metadata"]["mirror_source"]
    assert pointer["truncated"] is True
    assert pointer["source_path"] == str(path)
    assert pointer["pointer_kind"] == "mirror_jsonl_v1"

    def _reconstruct(record, session_uid, message_id):
        for m in messages_for_event(record, session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=_reconstruct)
    assert resolved.status == "resolved"
    assert resolved.content["instruction"] == _OVERSIZED_TEXT


@pytest.mark.asyncio
async def test_live_codex_ingest_bounds_oversized_message_via_mirror_one_codex(
    tmp_path: Path,
) -> None:
    """`_mirror_one_codex` (codex_mirror.py's live ingestion path) must write a
    bounded preview plus a resolvable source pointer for an oversized message."""
    from lionagi.state._mirror_common import resolve_mirrored_content
    from lionagi.state.codex_mirror import _det as codex_det
    from lionagi.state.codex_mirror import messages_for_record
    from lionhub.serve.mirror import _mirror_one_codex

    uid = "0199dddd-0000-0000-0000-000000000002"
    root = tmp_path / "codex"
    path = _codex_oversized_file(root, uid)
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        written = await _mirror_one_codex(db, path, state, {})
        branch_id = codex_det(uid, "branch")
        rows = await db.get_branch_messages(branch_id)

    assert written == 1
    assert len(rows) == 1
    row = rows[0]
    stored = row["content"]["instruction"]
    assert stored != _OVERSIZED_TEXT
    assert len(stored) < len(_OVERSIZED_TEXT)
    pointer = row["node_metadata"]["mirror_source"]
    assert pointer["truncated"] is True
    assert pointer["source_path"] == str(path)

    def _reconstruct(record, session_uid, message_id):
        for m in messages_for_record(record, session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=_reconstruct)
    assert resolved.status == "resolved"
    assert resolved.content["instruction"] == _OVERSIZED_TEXT


async def test_an_imported_rollout_leaves_the_role_field_empty(tmp_path):
    """`agent_name` is a role field, and an imported desktop thread has no role.

    Writing the engine name there was wrong at the definition site, and it had
    a visible consequence: the role tier sits ahead of the prompt tier in
    `resolve_display_name`, so every imported row rendered the engine and
    shadowed the informative prompt-derived name the mirror had just computed.

    Both rows are asserted. The session and the branch each carried the label,
    so checking only the session would pass while the branch kept it.
    """
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionagi.state.session_naming import resolve_display_name
    from lionhub.serve.mirror import _FileState, _mirror_one_codex

    uid = "0199cccc-0000-0000-0000-00000000000b"
    path = tmp_path / "rollout-role.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) > 0
        sid = codex_sid(uid)
        session = await db.get_session(sid)
        branches = await db.list_branches(sid)

    assert session is not None
    assert session["agent_name"] is None, (
        f"session carries role {session['agent_name']!r}; the engine is in a role field"
    )
    assert branches, "no branch was mirrored, so the branch assertion below proves nothing"
    for br in branches:
        assert br["agent_name"] is None, (
            f"branch carries role {br['agent_name']!r}; the session was cleared but its branch was not"
        )

    # The point of clearing the field: the prompt now reaches the display name.
    assert resolve_display_name(dict(session)) == "q"


def test_mirror_service_imports_neither_a_command_module_nor_the_studio() -> None:
    """The service module sits under the command module and above nothing in the studio."""
    from pathlib import Path

    from lionhub.serve import mirror

    from .cli._import_guard import command_module_imports, resolved_imports

    source = Path(mirror.__file__)
    imported = resolved_imports(source, "lionhub.serve")
    assert command_module_imports(imported) == set()
    assert not [name for name in imported if name.split(".")[:2] == ["lionagi", "studio"]]
