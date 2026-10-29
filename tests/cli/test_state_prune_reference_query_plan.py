# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Retained-reference filtering avoids repeated unindexed outbox scans."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import event

from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from tests.cli.test_state_prune_retained_refs import TABLES, _check_rows, _seed
from tests.cli.test_state_prune_retained_refs import isolated_store as isolated_store


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "delete"])
async def test_retained_reference_queries_avoid_correlated_outbox_scan_and_preserve_nulls(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    ids, before = await _seed()
    async with StateDB() as db:
        await db.execute(
            "INSERT INTO dispatch_outbox (id, kind, deliver_to, payload, next_attempt_at, "
            "session_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("outbox-null", "test", "recipient", "{}", time.time(), None, time.time()),
        )
        before["dispatch_outbox"] = [
            dict(row) for row in await db.fetch_all("SELECT * FROM dispatch_outbox ORDER BY id")
        ]

        class BorrowedDB:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *_exc):
                return None

        monkeypatch.setattr(db_mod, "StateDB", BorrowedDB)
        plans = []

        def capture_plan(conn, _cursor, statement, parameters, _context, _executemany):
            sql = statement.lstrip()
            if (
                sql.startswith(
                    (
                        "SELECT COUNT(*) FROM prune_victims WHERE ",
                        "DELETE FROM prune_victims WHERE ",
                    )
                )
                and "dispatch_outbox" in sql
            ):
                plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
                plans.append((statement, [(int(row[0]), int(row[1]), str(row[3])) for row in plan]))

        event.listen(db._engine.sync_engine, "before_cursor_execute", capture_plan)
        try:
            result = await state_mod._prune(keep_days=30, keep_n=0, dry_run=dry_run)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", capture_plan)

    assert result["sessions"] == 1
    assert result["skipped_sessions"] == 6
    assert result["skipped_by_reference"] == dict.fromkeys(TABLES, 2)
    await _check_rows(ids, before, dry_run=dry_run)
    assert len(plans) == 2, plans
    for statement, plan in plans:
        nodes = {node_id: (parent_id, detail) for node_id, parent_id, detail in plan}
        outbox_scans = [
            node_id
            for node_id, _parent_id, detail in plan
            if detail.upper().startswith("SCAN DISPATCH_OUTBOX")
        ]
        assert any("dispatch_outbox" in detail for _, _, detail in plan), (statement, plan)
        for node_id in outbox_scans:
            parent_id = nodes[node_id][0]
            seen = set()
            while parent_id in nodes and parent_id not in seen:
                seen.add(parent_id)
                parent_id, detail = nodes[parent_id]
                assert "CORRELATED" not in detail.upper(), (statement, plan)
