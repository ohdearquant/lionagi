"""`li state prune` against a PostgreSQL state store."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from lionagi.cli.state import _prune
from lionagi.state.db import StateDB
from tests.cli.test_state_commands import _seed_session_with_messages

pytestmark = pytest.mark.integration

# Far older than anything another test seeds, so the prune selects only this test's rows
# even though the PostgreSQL store is shared across the session.
_ANCIENT_DAYS = 3650


async def test_prune_deletes_old_messages_on_postgresql(pg_url, monkeypatch):
    # AppSettings is frozen, so swap the module reference StateDB() reads its URL from.
    monkeypatch.setattr("lionagi.state.db.settings", SimpleNamespace(LIONAGI_STATE_DB_URL=pg_url))
    old_ts = time.time() - (_ANCIENT_DAYS + 30) * 86400
    async with StateDB() as db:
        assert db.dialect == "postgresql"
        old_sid, old_msgs = await _seed_session_with_messages(db, n_messages=3, updated_at=old_ts)
        new_sid, new_msgs = await _seed_session_with_messages(db, n_messages=2)

    result = await _prune(keep_days=_ANCIENT_DAYS, keep_n=0, dry_run=False)
    assert result["sessions"] >= 1
    assert result["messages"] >= len(old_msgs)

    async with StateDB() as db:
        assert await db.get_session(old_sid) is None
        assert await db.get_session(new_sid) is not None
        for mid in old_msgs:
            assert await db.fetch_one("SELECT id FROM messages WHERE id = :id", {"id": mid}) is None
        for mid in new_msgs:
            assert (
                await db.fetch_one("SELECT id FROM messages WHERE id = :id", {"id": mid})
                is not None
            )
