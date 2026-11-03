# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Monitor reads the live normalized stream without exposing non-text chunks."""

import json
import time
import uuid

import pytest

from lionagi.cli import monitor
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB


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
            {"type": "text", "content": "normalized text"},
            {"type": "thinking", "content": "private reasoning", "text": "hidden thinking"},
            {"type": "tool_use", "content": "private input", "text": "hidden tool"},
            {"type": "tool_result", "tool_output": {"text": "private output"}},
            {"delta": {"text": "legacy delta"}},
            {"text": "legacy text"},
            {"type": "text", "content": "A" * 140},
        ],
    )

    assert monitor._stream_tail(run_dir, "branch", n_lines=20) == [
        "normalized text",
        "legacy delta",
        "legacy text",
        "A" * 120,
    ]


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
    monkeypatch.setattr(monitor, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(monitor, "_stdout_is_tty", lambda: False)

    session_id = str(uuid.uuid4())
    run_id = None if legacy else "run-distinct-from-session"
    actual_dir = runs_root / (session_id if legacy else run_id)
    branch_id = str(uuid.uuid4())
    _write_live_buffer(
        actual_dir,
        branch_id,
        [
            {"type": "text", "content": "live output from the run"},
            {"type": "thinking", "content": "hidden private reasoning"},
            {"type": "tool_use", "tool_input": {"token": "hidden tool input"}},
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
