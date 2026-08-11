# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Server-owned Run Detail file summaries (GitHub #3128)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


def _request(
    message_id: str,
    function: str | None,
    arguments: dict,
    *,
    timestamp: float,
) -> dict:
    content: dict = {"arguments": arguments}
    if function is not None:
        content["function"] = function
    return {
        "id": message_id,
        "lion_class": "ActionRequest",
        "timestamp": timestamp,
        "content": content,
    }


def test_run_files_deduplicates_normalized_paths_and_merges_reliable_access() -> None:
    from lionagi.studio.services.sessions import _derive_run_files

    root = Path("/workspace/run")
    summary = _derive_run_files(
        [
            _request(
                "read-absolute",
                "Read",
                {"file_path": "/workspace/run/src/app.py"},
                timestamp=1.0,
            ),
            _request(
                "write-relative",
                "functions.write_file",
                {"path": "src/app.py"},
                timestamp=3.0,
            ),
            _request(
                "multi-edit",
                "MultiEdit",
                {"changes": [{"path": "src/other.py"}, {"path": "src/app.py"}]},
                timestamp=2.0,
            ),
            # A structured path is useful provenance even when a future tool
            # name is unknown, but it must not be guessed read or write.
            _request(
                "future-tool",
                "FutureFileTool",
                {"file_path": "docs/maybe.md"},
                timestamp=4.0,
            ),
            _request(
                "native-reader",
                "reader_tool",
                {"action": "read", "path": "docs/native.md"},
                timestamp=5.0,
            ),
            _request(
                "native-editor",
                "editor",
                {"action": "edit", "path": "docs/native.md"},
                timestamp=6.0,
            ),
            # A native directory listing is not evidence that a file was
            # touched and must not become a misleading file entry.
            _request(
                "native-list",
                "reader",
                {"action": "list_dir", "path": "src"},
                timestamp=7.0,
            ),
        ],
        artifact_root=root,
    )

    assert summary == {
        "items": [
            {"path": "docs/native.md", "access": ["read", "write"], "openable": True},
            {"path": "docs/maybe.md", "access": [], "openable": True},
            {"path": "src/app.py", "access": ["read", "write"], "openable": True},
            {"path": "src/other.py", "access": ["write"], "openable": True},
        ],
        "total": 4,
        "shown": 4,
        "truncated": False,
        "redacted_count": 0,
    }


def test_run_files_redacts_outside_traversal_absolute_layout_and_credentials() -> None:
    from lionagi.studio.services.sessions import _derive_run_files

    root = Path("/workspace/run")
    summary = _derive_run_files(
        [
            _request("safe", "Read", {"file_path": "src/../src/safe.py"}, timestamp=1.0),
            _request(
                "outside", "Read", {"file_path": "/Users/alice/private/key.py"}, timestamp=2.0
            ),
            _request("traversal", "Write", {"file_path": "../../secrets.txt"}, timestamp=3.0),
            _request("protected", "Read", {"file_path": ".env"}, timestamp=4.0),
            _request(
                "credential-url",
                None,
                {"file_path": "https://alice:password@example.test/private.py"},
                timestamp=5.0,
            ),
            _request("windows", "Read", {"file_path": r"C:\\Users\\alice\\key.py"}, timestamp=6.0),
        ],
        artifact_root=root,
    )

    assert summary["items"] == [{"path": "src/safe.py", "access": ["read"], "openable": True}]
    assert summary["total"] == 1
    assert summary["redacted_count"] == 5
    serialized = json.dumps(summary)
    for secret in ("alice", "password", "/Users", "C:\\\\Users", "secrets.txt", ".env"):
        assert secret not in serialized


def test_run_files_hard_caps_thousands_to_a_recent_window() -> None:
    from lionagi.studio.services.sessions import MAX_RUN_FILE_ITEMS, _derive_run_files

    messages = [
        _request(
            f"read-{index}",
            "read_file",
            {"file_path": f"src/file-{index:04d}.py"},
            timestamp=float(index),
        )
        for index in range(2_500)
    ]

    # Even an internal caller asking for an excessive limit cannot enlarge the
    # serialized surface past the product cap.
    summary = _derive_run_files(
        messages,
        artifact_root=Path("/workspace/run"),
        limit=10_000,
    )

    assert len(summary["items"]) == MAX_RUN_FILE_ITEMS == 100
    assert summary["shown"] == 100
    assert summary["total"] == 2_500
    assert summary["truncated"] is True
    assert summary["items"][0]["path"] == "src/file-2499.py"
    assert summary["items"][-1]["path"] == "src/file-2400.py"


async def test_get_session_exposes_only_safe_bounded_server_file_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lionagi.studio.services.sessions as sessions_svc

    db_path = tmp_path / "state.db"
    artifact_root = tmp_path / "workspace"
    artifact_root.mkdir()
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    request_ids = ["req-read", "req-write", "req-outside", "req-secret"]
    async with StateDB(db_path) as db:
        await db.create_progression("session-progression")
        await db.create_session(
            {
                "id": "run-files-session",
                "progression_id": "session-progression",
                "status": "completed",
                "artifacts_path": str(artifact_root),
            }
        )
        await db.create_progression("branch-progression", request_ids)
        await db.create_branch(
            {
                "id": "branch-files",
                "session_id": "run-files-session",
                "progression_id": "branch-progression",
            }
        )
        payloads = [
            ("req-read", 1.0, "Read", {"file_path": str(artifact_root / "src/app.py")}),
            ("req-write", 2.0, "Edit", {"path": "src/app.py"}),
            ("req-outside", 3.0, "Read", {"file_path": "/Users/alice/private.py"}),
            ("req-secret", 4.0, "Read", {"file_path": ".env"}),
        ]
        for message_id, created_at, function, arguments in payloads:
            await db.insert_message(
                {
                    "id": message_id,
                    "created_at": created_at,
                    "content": {"function": function, "arguments": arguments},
                    "sender": "worker",
                    "recipient": "user",
                    "role": "action",
                    "node_metadata": {
                        "lion_class": ("lionagi.protocols.messages.action_request.ActionRequest")
                    },
                }
            )

    result = await sessions_svc.get_session("run-files-session")

    assert result is not None
    assert result["run_files"] == {
        "items": [{"path": "src/app.py", "access": ["read", "write"], "openable": True}],
        "total": 1,
        "shown": 1,
        "truncated": False,
        "redacted_count": 2,
    }
    # The legacy flat surface remains for file-reference resolution, but now
    # mirrors the bounded safe paths rather than leaking raw tool arguments.
    assert result["message_stats"]["files"] == ["src/app.py"]
