"""Both transcript mirrors store a bounded message as its preview behind a pointer to
its own source: the pointer names the adapter's source kind and session uid, and the
preview is computed from the row exactly as the unbounded write stores it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lionagi.state import claude_mirror, codex_mirror
from lionagi.state._mirror_common import SourceLine, bound_mirror_content
from lionagi.state.db import StateDB

CLAUDE_UID = "11111111-2222-3333-4444-555555555555"
ROLLOUT_UID = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
LONG_TEXT = "a message long enough to be cut by the preview bound"
PREVIEW_CHARS = 8


def _write(path: Path, lines: list[dict]) -> list[tuple[int, int, str]]:
    sources: list[tuple[int, int, str]] = []
    offset = 0
    blob = b""
    for line in lines:
        raw = (json.dumps(line) + "\n").encode()
        sources.append((offset, len(raw) - 1, hashlib.sha256(raw[:-1]).hexdigest()))
        offset += len(raw)
        blob += raw
    path.write_bytes(blob)
    return sources


def _claude_input() -> list[dict]:
    return [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-06-20T00:00:00.000Z",
            "sessionId": CLAUDE_UID,
            "message": {"role": "user", "content": [{"type": "text", "text": LONG_TEXT}]},
        }
    ]


def _codex_input() -> list[dict]:
    return [
        {
            "type": "session_meta",
            "timestamp": "2026-07-29T12:00:00Z",
            "payload": {"id": ROLLOUT_UID, "cwd": "/x"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-29T12:00:01Z",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "m1",
                "content": [{"text": LONG_TEXT}],
            },
        },
    ]


async def _mirror(adapter: str, db: StateDB, lines, path: Path, sources, max_preview_chars):
    if adapter == "claude":
        await claude_mirror.mirror_session(
            db,
            session_uid=CLAUDE_UID,
            events=lines,
            tool_names={},
            source_path=str(path),
            event_sources=sources,
            max_preview_chars=max_preview_chars,
        )
        return claude_mirror.session_db_id(CLAUDE_UID)
    await codex_mirror.mirror_session(
        db,
        rollout_uid=ROLLOUT_UID,
        records=lines,
        tool_names={},
        source_path=str(path),
        event_sources=sources,
        max_preview_chars=max_preview_chars,
    )
    return codex_mirror.session_db_id(ROLLOUT_UID)


def _loaded(value):
    return json.loads(value) if isinstance(value, str) else value


async def _rows(db: StateDB, sid: str) -> list[dict]:
    session = await db.get_session(sid)
    rows = []
    for mid in await db.get_progression(session["progression_id"]):
        row = dict(await db.get_message(mid))
        row["content"] = _loaded(row["content"])
        row["node_metadata"] = _loaded(row.get("node_metadata")) or {}
        rows.append(row)
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "build", "source_kind", "session_uid", "source_index"),
    [
        ("claude", _claude_input, "claude_jsonl", CLAUDE_UID, 0),
        ("codex", _codex_input, "codex_jsonl", ROLLOUT_UID, 1),
    ],
)
async def test_a_bounded_row_is_the_unbounded_row_behind_its_own_source(
    tmp_path: Path, adapter, build, source_kind, session_uid, source_index
) -> None:
    lines = build()
    path = tmp_path / "transcript.jsonl"
    sources = _write(path, lines)

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'unbounded.db'}") as db:
        sid = await _mirror(adapter, db, lines, path, sources, None)
        unbounded = await _rows(db, sid)
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'bounded.db'}") as db:
        await _mirror(adapter, db, lines, path, sources, PREVIEW_CHARS)
        bounded = await _rows(db, sid)

    assert len(unbounded) == len(bounded) == 1
    full, cut = unbounded[0], bounded[0]
    assert "mirror_source" not in full["node_metadata"]
    offset, byte_count, sha = sources[source_index]
    preview, pointer = bound_mirror_content(
        full["content"],
        full["id"],
        SourceLine(
            value=lines[source_index],
            source_path=str(path),
            source_offset=offset,
            source_byte_count=byte_count,
            source_sha256=sha,
        ),
        source_kind=source_kind,
        source_session_uid=session_uid,
        max_preview_chars=PREVIEW_CHARS,
    )
    assert pointer["truncated"] is True
    assert cut["id"] == full["id"]
    assert cut["content"] == preview
    assert cut["node_metadata"] == {**full["node_metadata"], "mirror_source": pointer}
