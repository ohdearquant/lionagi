# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from lionagi.state.claude_mirror import session_db_id
from lionagi.state.db import StateDB

SID = "11111111-2222-3333-4444-555555555555"


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


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _response_functions(contents: list[dict]) -> list[str]:
    return [c["function"] for c in contents if "action_request_id" in c]


async def _session_contents(db: StateDB) -> list[dict]:
    row = await db.get_session(session_db_id(SID))
    contents = []
    for mid in await db.get_progression(row["progression_id"]):
        c = (await db.get_message(mid))["content"]
        contents.append(json.loads(c) if isinstance(c, str) else c)
    return contents


_OVERSIZED_TEXT = "z" * 5000  # far beyond MIRROR_PREVIEW_CHARS default (500)


def _claude_oversized_file(root: Path, uid: str) -> Path:
    path = root / "-proj" / f"{uid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "user",
        "uuid": f"{uid}-u",
        "timestamp": "2026-08-05T00:00:00.000Z",
        "sessionId": uid,
        "message": {"role": "user", "content": [{"type": "text", "text": _OVERSIZED_TEXT}]},
    }
    path.write_text(json.dumps(event) + "\n")
    return path
