# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li mirror` — Claude Code transcript -> StateDB mirror."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lionagi.cli.mirror import (
    _FileState,
    _first_prompt,
    _parse_window,
    _read_new_events,
)
from lionagi.state.claude_mirror import (
    messages_for_event,
    mirror_session,
    session_db_id,
)
from lionagi.state.db import StateDB

SID = "11111111-2222-3333-4444-555555555555"


# ── Event builders (verified Claude JSONL shapes) ────────────────────────────


def _user_text(uuid: str, text: str, *, ts: str = "2026-06-20T00:00:00.000Z") -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": SID,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(uuid: str, blocks: list[dict], *, ts: str = "2026-06-20T00:00:01.000Z") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": SID,
        "message": {"role": "assistant", "model": "claude-opus-4-8", "content": blocks},
    }


def _tool_result(uuid: str, tool_use_id: str, content, *, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-06-20T00:00:02.000Z",
        "sessionId": SID,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }


def _db_content(msg) -> dict:
    c = msg.to_dict(mode="db")["content"]
    return json.loads(c) if isinstance(c, str) else c


# ── messages_for_event: mapping + ordering + linkage ─────────────────────────


def test_user_text_maps_to_single_instruction() -> None:
    out = messages_for_event(_user_text("u1", "hello there"), SID, {})
    assert [type(m).__name__ for m in out] == ["Instruction"]


def test_bare_string_user_content_supported() -> None:
    ev = {
        "type": "user",
        "uuid": "u1",
        "timestamp": "2026-06-20T00:00:00Z",
        "sessionId": SID,
        "message": {"role": "user", "content": "plain string content"},
    }
    out = messages_for_event(ev, SID, {})
    assert [type(m).__name__ for m in out] == ["Instruction"]


def test_command_noise_user_text_is_dropped() -> None:
    ev = _user_text("u1", "<command-name>/clear</command-name>")
    assert messages_for_event(ev, SID, {}) == []


def test_meta_event_is_dropped() -> None:
    ev = _user_text("u1", "real text")
    ev["isMeta"] = True
    assert messages_for_event(ev, SID, {}) == []


def test_assistant_text_then_tool_preserves_order() -> None:
    ev = _assistant(
        "a1",
        [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "done"},
        ],
    )
    tool_names: dict[str, str] = {}
    out = messages_for_event(ev, SID, tool_names)
    assert [type(m).__name__ for m in out] == [
        "AssistantResponse",
        "ActionRequest",
        "AssistantResponse",
    ]
    # tool_use records its function name for the later tool_result to label.
    assert tool_names["tool_1"] == "Bash"
    # micro-incremented timestamps keep intra-event order stable.
    assert out[0].created_at < out[1].created_at < out[2].created_at


def test_thinking_block_is_skipped() -> None:
    ev = _assistant(
        "a1",
        [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}],
    )
    out = messages_for_event(ev, SID, {})
    assert [type(m).__name__ for m in out] == ["AssistantResponse"]


def test_action_request_response_linkage() -> None:
    tool_names: dict[str, str] = {}
    req = messages_for_event(
        _assistant("a1", [{"type": "tool_use", "id": "tool_x", "name": "Read", "input": {"p": 1}}]),
        SID,
        tool_names,
    )[0]
    resp = messages_for_event(_tool_result("u2", "tool_x", "file contents"), SID, tool_names)[0]
    assert type(req).__name__ == "ActionRequest"
    assert type(resp).__name__ == "ActionResponse"
    rc = _db_content(resp)
    # The response points back at the request id, with the recovered function name.
    assert rc["action_request_id"] == str(req.id)
    assert rc["function"] == "Read"
    assert rc["output"] == "file contents"


def test_tool_result_error_flag_recorded() -> None:
    out = messages_for_event(_tool_result("u2", "t", "boom", is_error=True), SID, {"t": "Bash"})
    assert _db_content(out[0])["error"] == "error"


def test_tool_result_block_list_flattened() -> None:
    out = messages_for_event(
        _tool_result("u2", "t", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]),
        SID,
        {"t": "Grep"},
    )
    assert _db_content(out[0])["output"] == "a\nb"


def test_deterministic_ids_are_idempotent() -> None:
    ev = _assistant(
        "a1",
        [
            {"type": "text", "text": "x"},
            {"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}},
        ],
    )
    ids1 = [m.id for m in messages_for_event(ev, SID, {})]
    ids2 = [m.id for m in messages_for_event(ev, SID, {})]
    assert ids1 == ids2


# ── mirror_session: idempotent write + status lifecycle ──────────────────────


def _conversation() -> list[dict]:
    return [
        _user_text("u1", "do the thing"),
        _assistant(
            "a1",
            [
                {"type": "text", "text": "okay"},
                {"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {"command": "ls"}},
            ],
        ),
        _tool_result("u2", "tool_1", "total 0"),
    ]


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.mark.asyncio
async def test_mirror_session_creates_rich_session_row(temp_db_path: Path) -> None:
    async with StateDB() as db:
        n = await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            project="acme/widget",
            project_source="cwd",
            model="claude-opus-4-8",
            name="do the thing",
            status="running",
        )
        row = await db.get_session(session_db_id(SID))
    assert n > 0
    assert row["status"] == "running"
    assert row["invocation_kind"] == "agent"
    assert row["agent_name"] == "claude-code"
    assert row["project"] == "acme/widget"
    assert row["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_mirror_session_is_idempotent(temp_db_path: Path) -> None:
    events = _conversation()
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="completed")
        row = await db.get_session(session_db_id(SID))
        first = await db.get_progression(row["progression_id"])
        # Re-run from scratch (fresh tool_names, as after a restart).
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="completed")
        second = await db.get_progression(row["progression_id"])
    assert len(first) > 0
    assert first == second  # no duplicate appends


@pytest.mark.asyncio
async def test_mirror_session_running_then_completed_flips(temp_db_path: Path) -> None:
    events = _conversation()
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="running")
        before = await db.get_session(session_db_id(SID))
        # A later idle pass with no new events still flips the status.
        await mirror_session(db, session_uid=SID, events=[], tool_names={}, status="completed")
        after = await db.get_session(session_db_id(SID))
    assert before["status"] == "running"
    assert after["status"] == "completed"


@pytest.mark.asyncio
async def test_mirror_session_empty_no_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        n = await mirror_session(db, session_uid=SID, events=[], tool_names={}, status="running")
        row = await db.get_session(session_db_id(SID))
    assert n == 0
    assert row is None


# ── watcher helpers: tailing + parsing ───────────────────────────────────────


def test_read_new_events_buffers_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n{"a":2}\n{"a":3')  # last line incomplete
    state = _FileState(session_uid="x")
    first = _read_new_events(path, state)
    assert [e["a"] for e in first] == [1, 2]
    # Complete the dangling line; the next read picks up only the new event.
    with path.open("a") as fh:
        fh.write("}\n")
    second = _read_new_events(path, state)
    assert [e["a"] for e in second] == [3]


def test_read_new_events_resets_on_truncation(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n')
    state = _FileState(session_uid="x", offset=9999)  # offset past EOF
    out = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1]


def test_read_new_events_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\nnot json\n{"a":2}\n')
    state = _FileState(session_uid="x")
    out = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1, 2]


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
    [("30m", 1800.0), ("12h", 43200.0), ("7d", 604800.0), ("120", 120.0), ("bad", None)],
)
def test_parse_window(spec: str, expected: float | None) -> None:
    assert _parse_window(spec) == expected
