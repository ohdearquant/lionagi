# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The CLI retry writer keeps the message observed at submission time."""

from __future__ import annotations

import pytest

from lionagi.cli._runs import _make_message_handler


@pytest.mark.parametrize("role", ("user", "system"))
@pytest.mark.parametrize("already_seen", (False, True))
async def test_live_message_retry_keeps_a_deep_submission_snapshot(role, already_seen):
    shared_parts = [{"text": "before"}]
    payload = {
        "id": "message-1",
        "created_at": 123.0,
        "role": role,
        "content": {"parts": shared_parts},
        "node_metadata": {"parts": shared_parts},
    }

    class Message:
        def to_dict(self, *, mode):
            assert mode == "db"
            return payload

    class BusyStore:
        def __init__(self):
            self.refuse = True
            self.writes = []

        async def _persist_live_message(self, message, **context):
            if self.refuse:
                raise RuntimeError("simulated writer contention")
            self.writes.append((message, context))

    store = BusyStore()
    queues = []
    persisted_ids = []
    handler = _make_message_handler(
        store,
        "branch-1",
        "session-1",
        "branch-progression",
        "session-progression",
        dedup_set={"message-1"} if already_seen else set(),
        new_msg_ids_list=persisted_ids,
        message_retry_queues=queues,
    )
    await handler(Message())
    assert len(queues) == 1
    assert queues[0].pending_count == 1
    assert store.writes == []
    assert persisted_ids == []

    # The caller is free to edit its object while the failed write waits. Both
    # nested aliases must retain the submitted value in the eventual write.
    shared_parts[0]["text"] = "after"
    shared_parts.append({"text": "new"})
    payload["role"] = "assistant"
    store.refuse = False
    assert await queues[0].flush_final() is True

    assert queues[0].pending_count == 0
    assert len(store.writes) == 1
    message, context = store.writes[0]
    assert message == {
        "id": "message-1",
        "created_at": 123.0,
        "role": role,
        "content": {"parts": [{"text": "before"}]},
        "node_metadata": {"parts": [{"text": "before"}]},
    }
    assert message is not payload
    assert message["content"]["parts"] is message["node_metadata"]["parts"]
    assert message["content"]["parts"] is not shared_parts
    assert context == {
        "session_id": "session-1",
        "branch_progression_id": None if already_seen else "branch-progression",
        "session_progression_id": None if already_seen else "session-progression",
        "system_branch_id": "branch-1" if role == "system" else None,
        "system_branch_update_before_activity": False,
        "activity_at": 123.0,
    }
    assert persisted_ids == ([] if already_seen else ["message-1"])
