# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the team-lifecycle building blocks in `lionagi.cli.team`:
the pure `compute_quiescence` predicate and the structured done/finished/
wakeup signal writers (`post_done_signal` et al.), including their
flock-safety under concurrent writers."""

from __future__ import annotations

import json
import threading

import pytest

from lionagi.cli import team


@pytest.fixture(autouse=True)
def _teams_dir(tmp_path, monkeypatch):
    """Redirect TEAMS_DIR to an isolated tmp dir for every test in this file."""
    monkeypatch.setattr(team, "TEAMS_DIR", tmp_path / "teams")
    return tmp_path / "teams"


def _make_team(team_id: str, members: list[str], messages: list[dict] | None = None) -> None:
    path = team._teams_dir() / f"{team_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": team_id,
                "name": "t",
                "members": members,
                "messages": messages or [],
            }
        )
    )


# ── compute_quiescence: pure predicate ──────────────────────────────────────


class TestComputeQuiescence:
    def test_no_signals_yet_is_neither_quiescent_nor_continuing(self):
        """Nobody has posted 'done' — the run is presumed still executing its
        first turn, so the coordinator has nothing to decide yet."""
        state = team.compute_quiescence(
            [], worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert not state.quiescent
        assert not state.should_continue
        assert state.active_workers == frozenset({"alice", "bob"})

    def test_all_done_no_pending_mail_is_quiescent(self):
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert state.quiescent
        assert not state.should_continue
        assert state.idle_workers == frozenset({"alice", "bob"})
        assert state.pending_targets == frozenset()

    def test_unread_message_to_an_idle_worker_triggers_should_continue(self):
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["alice"], "kind": "message", "content": "hey", "read_by": {}},
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert not state.quiescent
        assert state.should_continue
        assert state.pending_targets == frozenset({"alice"})

    def test_read_message_does_not_trigger_a_round(self):
        """Same as above but alice already read it — no round needed."""
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {
                "from": "bob",
                "to": ["alice"],
                "kind": "message",
                "content": "hey",
                "read_by": {"alice": "2026-01-01T00:00:00"},
            },
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert state.quiescent
        assert not state.should_continue

    def test_broadcast_message_counts_as_pending_for_every_idle_worker(self):
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "carol", "to": ["*"], "kind": "message", "content": "fyi all", "read_by": {}},
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert state.pending_targets == frozenset({"alice", "bob"})
        assert state.should_continue

    def test_message_to_a_still_active_worker_is_not_pending(self):
        """A worker that hasn't posted done yet will see its mail when it
        next calls receive on its own — no round needed on its behalf."""
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "alice", "to": ["bob"], "kind": "message", "content": "hey", "read_by": {}},
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        # bob never posted done -> still "active" -> not eligible for a round.
        assert not state.quiescent  # bob still active
        assert state.pending_targets == frozenset()

    def test_finished_worker_is_never_woken_by_a_later_wakeup(self):
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "finished", "content": "bye", "read_by": {}},
            {"from": "coord", "to": ["alice"], "kind": "wakeup", "content": "", "read_by": {}},
            {"from": "bob", "to": ["alice"], "kind": "message", "content": "hey", "read_by": {}},
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert state.retired_workers == frozenset({"alice"})
        # alice is retired, not idle, so the unread message to her is not pending.
        assert "alice" not in state.pending_targets

    def test_wakeup_flips_a_done_worker_back_to_active(self):
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "coord", "to": ["alice"], "kind": "wakeup", "content": "", "read_by": {}},
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        # alice active again -> not all settled -> neither quiescent nor
        # ready for ANOTHER round yet (she needs to finish this new turn).
        assert "alice" in state.active_workers
        assert not state.quiescent
        assert not state.should_continue

    def test_broadcast_wakeup_reactivates_every_non_retired_worker(self):
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["*"], "kind": "finished", "content": "x", "read_by": {}},
            {"from": "coord", "to": ["*"], "kind": "wakeup", "content": "", "read_by": {}},
        ]
        state = team.compute_quiescence(
            msgs, worker_names=["alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert state.active_workers == frozenset({"alice"})
        assert state.retired_workers == frozenset({"bob"})

    def test_rounds_exhausted_forces_quiescence_despite_pending_mail(self):
        msgs = [
            {"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}},
            {"from": "bob", "to": ["alice"], "kind": "message", "content": "hey", "read_by": {}},
        ]
        state = team.compute_quiescence(msgs, worker_names=["alice"], rounds_run=2, max_rounds=2)
        assert state.rounds_exhausted
        assert state.quiescent
        assert not state.should_continue

    def test_coordinator_wants_round_forces_a_round_even_with_no_pending_mail(self):
        msgs = [{"from": "alice", "to": ["*"], "kind": "done", "content": "x", "read_by": {}}]
        state = team.compute_quiescence(
            msgs,
            worker_names=["alice"],
            rounds_run=0,
            max_rounds=2,
            coordinator_wants_round=True,
        )
        assert state.should_continue
        assert not state.quiescent

    def test_empty_roster_is_quiescent_trivially(self):
        state = team.compute_quiescence([], worker_names=[], rounds_run=0, max_rounds=2)
        assert state.quiescent
        assert not state.should_continue

    def test_duplicate_worker_names_are_deduplicated(self):
        state = team.compute_quiescence(
            [], worker_names=["alice", "alice", "bob"], rounds_run=0, max_rounds=2
        )
        assert state.active_workers == frozenset({"alice", "bob"})

    def test_done_from_an_unknown_sender_is_ignored(self):
        """A message kind='done' from a name outside worker_names (e.g. a
        stray or future teammate) must not silently mutate unrelated state."""
        msgs = [{"from": "eve", "to": ["*"], "kind": "done", "content": "x", "read_by": {}}]
        state = team.compute_quiescence(msgs, worker_names=["alice"], rounds_run=0, max_rounds=2)
        assert state.active_workers == frozenset({"alice"})
        assert not state.quiescent


# ── post_done_signal / post_finished_signal / post_wakeup_signal ───────────


class TestSignalWriters:
    def test_post_done_signal_writes_structured_entry(self):
        _make_team("t1", ["orchestrator", "alice"])
        msg = team.post_done_signal(
            "t1", worker="alice", summary="finished the audit", artifacts=["out/report.md"]
        )
        assert msg["kind"] == "done"
        assert msg["from"] == "alice"
        assert msg["to"] == ["*"]
        assert msg["content"] == "finished the audit"
        assert msg["artifacts"] == ["out/report.md"]

        data = team._load_team("t1")
        assert data["messages"] == [msg]

    def test_post_finished_signal_writes_finished_kind(self):
        _make_team("t2", ["orchestrator", "alice"])
        msg = team.post_finished_signal("t2", worker="alice", summary="done for good")
        assert msg["kind"] == "finished"
        assert "artifacts" not in msg  # no artifacts passed -> key omitted

    def test_post_wakeup_signal_defaults_sender_to_coordinator(self):
        _make_team("t3", ["orchestrator", "alice"])
        msg = team.post_wakeup_signal("t3", target="alice", content="new mail")
        assert msg["kind"] == "wakeup"
        assert msg["from"] == "coordinator"
        assert msg["to"] == ["alice"]

    def test_post_done_signal_raises_on_missing_team(self):
        with pytest.raises(FileNotFoundError):
            team.post_done_signal("nonexistent", worker="alice", summary="x")

    def test_pop_unread_messages_marks_read_and_filters_signal_kinds(self):
        _make_team(
            "t4",
            ["orchestrator", "alice", "bob"],
            messages=[
                {
                    "id": "m1",
                    "from": "bob",
                    "to": ["alice"],
                    "content": "coordination note",
                    "kind": "message",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:00",
                },
                {
                    "id": "m2",
                    "from": "bob",
                    "to": ["*"],
                    "content": "should not appear",
                    "kind": "done",
                    "read_by": {},
                    "timestamp": "2026-01-01T00:00:01",
                },
            ],
        )
        unread = team.pop_unread_messages("t4", "alice")
        assert len(unread) == 1
        assert unread[0]["content"] == "coordination note"

        # Marked read — a second call returns nothing new.
        assert team.pop_unread_messages("t4", "alice") == []
        data = team._load_team("t4")
        assert "alice" in data["messages"][0]["read_by"]


# ── flock-safety under concurrent writers ───────────────────────────────────


class TestConcurrentWrites:
    def test_post_done_signal_survives_concurrent_writers_no_lost_updates(self):
        """N threads each post one done-signal for a distinct worker under
        the same team's flock; every message must land — the whole point of
        holding the lock across read-modify-write in `_locked_team`."""
        team_id = "concurrent-team"
        n = 12
        workers = [f"worker-{i}" for i in range(n)]
        _make_team(team_id, ["orchestrator", *workers])

        errors: list[BaseException] = []

        def _post(name: str) -> None:
            try:
                team.post_done_signal(team_id, worker=name, summary=f"{name} done")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_post, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        data = team._load_team(team_id)
        senders = {m["from"] for m in data["messages"]}
        assert senders == set(workers)
        assert len(data["messages"]) == n  # no message lost to a lost write

    def test_mixed_done_and_wakeup_writers_all_land(self):
        team_id = "concurrent-mixed"
        workers = [f"w{i}" for i in range(6)]
        _make_team(team_id, ["orchestrator", *workers])

        def _post_done(name: str) -> None:
            team.post_done_signal(team_id, worker=name, summary="x")

        def _post_wakeup(name: str) -> None:
            team.post_wakeup_signal(team_id, target=name, content="wake")

        threads = [threading.Thread(target=_post_done, args=(w,)) for w in workers]
        threads += [threading.Thread(target=_post_wakeup, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        data = team._load_team(team_id)
        assert len(data["messages"]) == 2 * len(workers)
        done_count = sum(1 for m in data["messages"] if m["kind"] == "done")
        wakeup_count = sum(1 for m in data["messages"] if m["kind"] == "wakeup")
        assert done_count == len(workers)
        assert wakeup_count == len(workers)
