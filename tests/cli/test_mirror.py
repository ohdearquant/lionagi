# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li mirror` — Claude Code transcript -> StateDB mirror."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lionagi.cli.mirror import (
    _derive_metadata,
    _fallback_project,
    _FileState,
    _first_prompt,
    _Lineage,
    _one_pass,
    _parse_window,
    _read_new_events,
)
from lionagi.state.claude_mirror import (
    messages_for_event,
    mirror_session,
    reconcile_session_status,
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
async def test_reconcile_flips_running_to_completed_when_idle(temp_db_path: Path) -> None:
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=_conversation(), tool_names={}, status="running"
        )
        before = await db.get_session(session_db_id(SID))
        # Wall-clock well past the last message -> idle -> completed.
        await reconcile_session_status(db, SID, now=before["updated_at"] + 10_000, live_window=300)
        after = await db.get_session(session_db_id(SID))
    assert before["status"] == "running"
    assert after["status"] == "completed"


@pytest.mark.asyncio
async def test_reconcile_reactivates_completed_when_fresh(temp_db_path: Path) -> None:
    # A mirror session's "completed" is dormant, not terminal: when the transcript
    # resumes, reconcile brings it back to running (green check -> live spinner).
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=_conversation(), tool_names={}, status="completed"
        )
        before = await db.get_session(session_db_id(SID))
        # "now" within the live window of the last message -> running.
        await reconcile_session_status(db, SID, now=before["updated_at"] + 1, live_window=300)
        after = await db.get_session(session_db_id(SID))
    assert before["status"] == "completed"
    assert after["status"] == "running"


@pytest.mark.asyncio
async def test_mirror_session_empty_no_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        n = await mirror_session(db, session_uid=SID, events=[], tool_names={}, status="running")
        row = await db.get_session(session_db_id(SID))
    assert n == 0
    assert row is None


# ── watcher passes: session-level liveness across multiple files ─────────────


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
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
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row is not None
    assert row["status"] == "completed"


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


# ── project attribution fallback ─────────────────────────────────────────────


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


@pytest.mark.asyncio
async def test_mirror_session_backfills_missing_project(temp_db_path: Path) -> None:
    # A session first mirrored with no project must be backfilled on a later pass
    # once a project is derived — without disturbing updated_at (the liveness clock).
    events = _conversation()
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, project=None)
        before = await db.get_session(session_db_id(SID))
        assert before["project"] is None

        await mirror_session(
            db,
            session_uid=SID,
            events=events,
            tool_names={},
            project="acme/widget",
            project_source="cwd_dir",
        )
        after = await db.get_session(session_db_id(SID))
    assert after["project"] == "acme/widget"
    assert after["project_source"] == "cwd_dir"
    # Provenance backfill is not activity: the liveness clock must not move.
    assert after["updated_at"] == before["updated_at"]


# ── conversation-lineage detector ────────────────────────────────────────────


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
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
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
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
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
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
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
        offsets = {str(path): path.stat().st_size}
        await _one_pass(db, root, {}, offsets, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["project"] == "ghost-proj"
    assert row["project_source"] == "cwd_dir"
