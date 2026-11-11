# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Direct contracts for the scheduler liveness identity helpers."""

from __future__ import annotations

import time
import uuid

import lionhub.orchestration.scheduler.liveness as scheduler_liveness


def test_phantom_classifier_returns_no_reason_for_another_machines_row(tmp_path, monkeypatch):
    """The classifier that feeds the phantom reaper, checked directly — the local row beside it fixes what the answer would otherwise have been."""

    monkeypatch.setattr(scheduler_liveness.socket, "gethostname", lambda: "this-host")

    now = time.time()
    stale = now - 7200

    def _row(meta: dict) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "updated_at": stale,
            "artifacts_path": None,
            "node_metadata": meta,
        }

    class _Row(dict):
        def keys(self):  # noqa: D102 — sqlite3.Row-shaped access used by the classifier
            return super().keys()

    remote = _Row(_row({"pid": 4242, "pid_host": "some-other-host"}))
    local = _Row(_row({"pid": 4242, "pid_host": "this-host"}))

    monkeypatch.setattr(scheduler_liveness, "_pid_is_live", lambda pid: False)

    assert (
        scheduler_liveness._classify_phantom(remote, now=now, stale_seconds=3600, ps_snapshot="")
        is None
    ), "another host's stale row must produce no phantom reason"
    assert (
        scheduler_liveness._classify_phantom(local, now=now, stale_seconds=3600, ps_snapshot="")
        == "process_dead"
    ), "a local stale row with a dead pid must still classify — otherwise nothing is being tested"


def test_process_identity_is_foreign_reads_host_and_unknown_modes(monkeypatch):
    """Foreign means "not measurable here", which covers two distinct records."""

    monkeypatch.setattr(scheduler_liveness.socket, "gethostname", lambda: "this-host")
    foreign = scheduler_liveness.process_identity_is_foreign

    assert foreign({"node_metadata": {"pid_host": "other-host"}}) is True
    assert foreign({"node_metadata": {"process_identity_mode": "external"}}) is True
    # A mode this code does know how to check is not foreign on its own.
    assert foreign({"node_metadata": {"process_identity_mode": "local"}}) is False
    assert foreign({"node_metadata": {"process_identity_mode": "in_process"}}) is False
    assert foreign({"node_metadata": {"pid_host": "this-host"}}) is False
    assert foreign({"node_metadata": {"pid": 1}}) is False
    assert foreign({"node_metadata": None}) is False
    assert foreign({}) is False
    # Stored as JSON text by some read paths; the same answer either way.
    assert foreign({"node_metadata": '{"pid_host": "other-host"}'}) is True
    assert foreign({"node_metadata": "not json at all"}) is False

    # A wrong-typed marker still names a mode this code cannot check; reading it as absent
    # would put the row back in reach of reapers that treat non-True liveness as death.
    assert foreign({"node_metadata": {"process_identity_mode": 123}}) is True
    assert foreign({"node_metadata": {"process_identity_mode": {"kind": "remote"}}}) is True
    # Absence is the key not being there — an old row predates the marker entirely and is
    # still judged by the host check.
    assert foreign({"node_metadata": {}}) is False
    # A key present and set to null is not that row: no writer here emits null, so it can
    # only have come from outside this code's control, the same unreadable-marker case above.
    assert foreign({"node_metadata": {"process_identity_mode": None}}) is True
