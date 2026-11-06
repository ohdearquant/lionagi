# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Monitor reads the live normalized stream without exposing non-text chunks."""

import errno
import json
import time
import uuid

import pytest

from lionagi.cli import monitor
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionhub.orchestration import runs as _runs


def _write_live_buffer(run_dir, branch_id, chunks):
    branches = run_dir / "branches"
    stream = run_dir / "stream"
    branches.mkdir(parents=True)
    stream.mkdir()
    (branches / f"{branch_id}.json").write_text("{}")
    (stream / f"{branch_id}.buffer.jsonl").write_text(
        "".join(json.dumps(chunk) + "\n" for chunk in chunks)
    )


def test_stream_tail_renders_normalized_text_and_keeps_legacy_shapes(tmp_path):
    run_dir = tmp_path / "run"
    _write_live_buffer(
        run_dir,
        "branch",
        [
            {"_lionagi_stream_chunk": True, "type": "text", "content": "normalized text"},
            {
                "_lionagi_stream_chunk": True,
                "type": "thinking",
                "content": "private reasoning",
                "text": "hidden thinking",
            },
            {
                "_lionagi_stream_chunk": True,
                "type": "tool_use",
                "content": "private input",
                "text": "hidden tool",
            },
            {
                "_lionagi_stream_chunk": True,
                "type": "tool_result",
                "tool_output": {"text": "private output"},
            },
            {"type": "content_block_delta", "delta": {"text": "legacy delta"}},
            {"text": "legacy text"},
            {"type": "text", "text": "legacy typed text"},
            {
                "_lionagi_stream_chunk": True,
                "type": "text",
                "content": "fallback text",
                "serialization_error": "unserializable",
            },
            {"_lionagi_stream_chunk": True, "type": "text", "content": "A" * 140},
        ],
    )

    assert monitor._stream_tail(run_dir, "branch", n_lines=20) == [
        "normalized text",
        "legacy delta",
        "legacy text",
        "legacy typed text",
        "fallback text",
        "A" * 120,
    ]


def test_stream_tail_keeps_old_reader_behavior_for_legacy_type_names(tmp_path):
    rows = [
        {"type": "result", "text": "legacy result"},
        {"type": "thinking", "delta": {"text": "legacy delta"}},
        {"type": "tool_use", "text": "legacy tool"},
        {"type": "result", "content": "legacy metadata", "text": "legacy content field"},
        {"_lionagi_stream_chunk": False, "type": "thinking", "text": "false marker"},
        {"_lionagi_stream_chunk": 1, "type": "result", "text": "integer marker"},
        {"_lionagi_stream_chunk": "true", "type": "error", "text": "string marker"},
        {"text": "legacy plain"},
    ]
    _write_live_buffer(tmp_path / "run", "branch", rows)
    old_reader_output = [
        ((row.get("delta") or {}).get("text") or row.get("text") or "")[:120] for row in rows
    ]
    assert monitor._stream_tail(tmp_path / "run", "branch", n_lines=20) == old_reader_output


def test_stream_tail_waits_for_a_newline_and_keeps_terminated_malformed_fallback(tmp_path):
    stream = tmp_path / "stream"
    stream.mkdir()
    path = stream / "branch.buffer.jsonl"
    partial = '{"_lionagi_stream_chunk":true,"type":"thinking","content":"private'
    path.write_text("malformed legacy line\n" + partial)
    assert monitor._stream_tail(tmp_path, "branch") == ["malformed legacy line"]
    path.write_text("malformed legacy line\n" + partial + '"}\n')
    assert monitor._stream_tail(tmp_path, "branch") == ["malformed legacy line"]
    path.write_text('{"text":"not yet complete"}')
    assert monitor._stream_tail(tmp_path, "branch") == []
    path.write_text("".join(json.dumps({"text": str(i)}) + "\n" for i in range(5)) + partial)
    assert monitor._stream_tail(tmp_path, "branch") == ["1", "2", "3", "4"]


def test_stream_tail_splits_only_on_line_feed(tmp_path):
    stream = tmp_path / "stream"
    stream.mkdir()
    path = stream / "branch.buffer.jsonl"
    path.write_bytes(b"malformed\rline\n")
    assert monitor._stream_tail(tmp_path, "branch") == ["malformedline"]
    path.write_bytes(b"malformed\rline\r\n")
    assert monitor._stream_tail(tmp_path, "branch") == ["malformedline"]
    path.write_bytes(b"")
    assert monitor._stream_tail(tmp_path, "branch") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["session", "play"])
@pytest.mark.parametrize("legacy", [False, True], ids=["distinct-run-id", "legacy-no-run-id"])
async def test_detail_view_reads_the_live_run_and_keeps_legacy_fallback(
    tmp_path, monkeypatch, view, legacy
):
    db_path = tmp_path / "state.db"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(monitor, "RUNS_ROOT", runs_root, raising=False)
    monkeypatch.setattr(monitor, "_stdout_is_tty", lambda: False)

    session_id = str(uuid.uuid4())
    run_id = None if legacy else "run-distinct-from-session"
    actual_dir = runs_root / (session_id if legacy else run_id)
    branch_id = str(uuid.uuid4())
    _write_live_buffer(
        actual_dir,
        branch_id,
        [
            {"_lionagi_stream_chunk": True, "type": "text", "content": "live output from the run"},
            {
                "_lionagi_stream_chunk": True,
                "type": "thinking",
                "content": "hidden private reasoning",
            },
            {
                "_lionagi_stream_chunk": True,
                "type": "tool_use",
                "tool_input": {"token": "hidden tool input"},
            },
        ],
    )
    if not legacy:
        # A plausible but wrong session-named directory must not win over run_id.
        _write_live_buffer(
            runs_root / session_id,
            branch_id,
            [{"text": "wrong session directory"}],
        )

    async with StateDB(db_path) as db:
        progression = str(uuid.uuid4())
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": session_id,
                "run_id": run_id,
                "progression_id": progression,
                "status": "running",
                "started_at": time.time(),
                "invocation_kind": "play" if view == "play" else "agent",
            }
        )
        if view == "session":
            output = await monitor._detail_session(db, await db.get_session(session_id))
        else:
            show_id, play_id = str(uuid.uuid4()), str(uuid.uuid4())
            await db.create_show(
                {"id": show_id, "topic": "fixture", "status": "active", "show_dir": str(tmp_path)}
            )
            await db.create_play(
                {
                    "id": play_id,
                    "show_id": show_id,
                    "name": "fixture-play",
                    "status": "running",
                    "session_id": session_id,
                    "started_at": time.time(),
                }
            )
            output = await monitor._detail_play(db, await db.get_play(play_id))

    assert "live output from the run" in output
    assert "hidden private reasoning" not in output
    assert "hidden tool input" not in output
    assert "wrong session directory" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["session", "play"])
@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "dotdot",
        "traversal",
        "absolute",
        "nul",
        "linked-out",
        "linked-alias",
        "linked-loop",
    ],
)
async def test_present_invalid_or_linked_run_id_never_falls_back(tmp_path, monkeypatch, view, kind):
    db_path = tmp_path / "state.db"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(monitor, "RUNS_ROOT", runs_root, raising=False)
    monkeypatch.setattr(monitor, "_stdout_is_tty", lambda: False)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    session_id = str(uuid.uuid4())
    branch_id = str(uuid.uuid4())
    _write_live_buffer(runs_root / session_id, branch_id, [{"text": "wrong legacy fallback"}])
    outside = tmp_path / "outside"
    _write_live_buffer(outside, branch_id, [{"text": "wrong external target"}])
    other_run = runs_root / "other-run"
    _write_live_buffer(other_run, branch_id, [{"text": "wrong aliased target"}])
    (runs_root / "link-out").symlink_to(outside, target_is_directory=True)
    (runs_root / "link-alias").symlink_to(other_run, target_is_directory=True)
    (runs_root / "link-loop").symlink_to("link-loop", target_is_directory=True)
    run_id = {
        "empty": "",
        "dotdot": "..",
        "traversal": "../outside",
        "absolute": str(outside),
        "nul": "bad\x00name",
        "linked-out": "link-out",
        "linked-alias": "link-alias",
        "linked-loop": "link-loop",
    }[kind]
    async with StateDB(db_path) as db:
        progression = str(uuid.uuid4())
        await db.create_progression(progression)
        await db.create_session(
            {
                "id": session_id,
                "run_id": run_id,
                "progression_id": progression,
                "status": "running",
                "started_at": time.time(),
            }
        )
        if view == "session":
            output = await monitor._detail_session(db, await db.get_session(session_id))
        else:
            show_id, play_id = str(uuid.uuid4()), str(uuid.uuid4())
            await db.create_show(
                {"id": show_id, "topic": "fixture", "status": "active", "show_dir": str(tmp_path)}
            )
            await db.create_play(
                {
                    "id": play_id,
                    "show_id": show_id,
                    "name": "fixture-play",
                    "status": "running",
                    "session_id": session_id,
                    "started_at": time.time(),
                }
            )
            output = await monitor._detail_play(db, await db.get_play(play_id))
    assert "wrong legacy fallback" not in output
    assert "wrong external target" not in output
    assert "wrong aliased target" not in output


def test_missing_run_id_uses_session_id_but_invalid_present_id_does_not(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs)
    (runs / "session").mkdir()
    assert monitor._session_run_dir("session", None) == (runs / "session").resolve()
    for invalid in ("", ".", "..", "bad/name", "bad\x00name", 42):
        assert monitor._session_run_dir("session", invalid) is None


def test_run_resolution_only_suppresses_symlink_loop_errors(monkeypatch):
    def fail(error):
        def resolve(_run_id):
            raise error

        monkeypatch.setattr(monitor, "_resolved_run_state_dir", resolve)

    fail(OSError(errno.ELOOP, "loop"))
    assert monitor._session_run_dir("session", "run") is None
    fail(PermissionError(errno.EACCES, "denied"))
    with pytest.raises(PermissionError):
        monitor._session_run_dir("session", "run")
