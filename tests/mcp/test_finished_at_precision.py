# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`finished_at` says when a run ended; this says whether anyone saw it end.

Two of the paths that end a run cannot know the end time. They write the moment
the end was *noticed*, which is a real upper bound and is not a duration. Stored
in the same field as an observed end and with nothing to tell them apart, a
noticed time reads as a run that took that long, and it reads that way in one
direction only: too slow, never too fast.

So the precision is recorded beside the value, by the path that knows which it
is. These tests are about every terminal path answering, and about the two that
must answer "upper_bound" being the two that actually do.
"""

from __future__ import annotations

from typing import Any

import pytest

from lionagi.mcp import _notify_hook, config, jobs

_SPAWNED_AT = 1_700_000_000.0


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    return tmp_path


@pytest.fixture
def no_delivery(monkeypatch):
    def _fake(run_id, job, status, **kw):
        return {"attempted": True, "ok": True, "exit_code": 0, "error": None, "command": "notify"}

    monkeypatch.setattr(_notify_hook, "deliver_terminal_notice", _fake)


def _started(**fields: Any) -> str:
    rid = jobs.new_run_id()
    base: dict[str, Any] = {
        "run_id": rid,
        "pid": 4242,
        "pid_create_time": _SPAWNED_AT,
        "pgid": 4242,
        "kind": "flow",
        "label": "a-label",
        "status": "running",
        "spawn_state": "started",
        "submitted_at": "2026-07-25T00:00:00+00:00",
        "finished_at": None,
        "log": None,
    }
    base.update(fields)
    jobs._write_job(base)
    return rid


def _precision(run_id: str) -> str | None:
    return jobs._read_job(run_id).get("finished_at_precision")


class TestTheReaperSaysItOnlyNoticed:
    def test_a_reaped_run_records_an_upper_bound_not_an_observation(
        self, sandbox, monkeypatch, no_delivery
    ):
        # Nobody watched this process exit. The stored time is when the loss was
        # established, so it bounds the end rather than being it.
        monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
        rid = _started()
        result = jobs.reap_orphan(
            rid, finding=jobs.FINDING_PID_ABSENT, observed_at="2026-07-25T02:00:00+00:00"
        )

        assert result.won_transition is True
        assert _precision(rid) == jobs.FINISHED_AT_UPPER_BOUND

    def test_it_still_records_the_bound_it_has(self, sandbox, monkeypatch, no_delivery):
        """Positive control: the bound is real and is still written.

        Marking the value untrusted must not turn into dropping it, which would
        leave a terminal run with no end time at all.
        """
        monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
        rid = _started()
        jobs.reap_orphan(
            rid, finding=jobs.FINDING_PID_ABSENT, observed_at="2026-07-25T02:00:00+00:00"
        )

        assert jobs._read_job(rid)["finished_at"] == "2026-07-25T02:00:00+00:00"


class TestALifecycleRecordWithoutAnEndTime:
    """The same gap as the reaper's, reached by a different path.

    `_cache_lifecycle_end` prefers the lifecycle record's own end time and falls
    back to now when there is none. The fallback is the same substitution, so it
    gets the same answer.
    """

    def test_an_end_time_in_the_record_is_an_observation(self, sandbox, monkeypatch, no_delivery):
        rid = _started()
        job = jobs._read_job(rid)
        out = jobs._cache_lifecycle_end(
            job, {"terminal": True, "status": "completed", "ended_at": _SPAWNED_AT + 60}
        )
        assert out["finished_at_precision"] == jobs.FINISHED_AT_OBSERVED

    def test_a_record_with_no_end_time_falls_back_and_says_so(
        self, sandbox, monkeypatch, no_delivery
    ):
        rid = _started()
        job = jobs._read_job(rid)
        out = jobs._cache_lifecycle_end(job, {"terminal": True, "status": "completed"})
        assert out["finished_at"] is not None
        assert out["finished_at_precision"] == jobs.FINISHED_AT_UPPER_BOUND


class TestASpawnFailureRecordsAnObservation:
    """`_record_spawn_failure` has two write arms — no record yet for this run
    (the directory is reserved but `job.json` never landed) and a record that
    already exists with `finished_at=None` — and both must stamp "observed":
    the caller that caught the failed spawn watched it happen just now.
    """

    def test_the_file_create_arm_records_an_observation(self, sandbox):
        rid = jobs.new_run_id()
        config.job_dir(rid).mkdir(parents=True)
        jobs._record_spawn_failure(rid, OSError(8, "Exec format error"))
        assert _precision(rid) == jobs.FINISHED_AT_OBSERVED

    def test_the_merge_existing_arm_records_an_observation(self, sandbox):
        rid = _started()
        jobs._record_spawn_failure(rid, OSError(8, "Exec format error"))
        assert _precision(rid) == jobs.FINISHED_AT_OBSERVED


class TestThePathsThatWatchedItEnd:
    def test_a_kill_records_an_observation(self, sandbox, monkeypatch, no_delivery):
        rid = _started()
        monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
        jobs._mark_killed(jobs._read_job(rid))
        assert _precision(rid) == jobs.FINISHED_AT_OBSERVED

    def test_the_terminal_hook_records_an_observation(self, sandbox, no_delivery):
        rid = _started()
        jobs.mark_terminal(rid, "completed")
        assert _precision(rid) == jobs.FINISHED_AT_OBSERVED


class TestReadingItBack:
    def test_a_record_written_before_the_field_existed_reads_as_unknown(self, sandbox):
        """Absent is its own answer.

        Defaulting it to "observed" would restate the same confident guess one
        layer up, on exactly the records that carry it.
        """
        rid = _started(status="completed", finished_at="2026-07-25T01:00:00+00:00")
        assert _precision(rid) is None

    def test_status_reports_a_legacy_record_as_unknown(self, sandbox):
        """The public reader, not just the raw record, must say "unknown".

        `status()` reads `finished_at_precision` with `.get()` (jobs.py:1634), so
        a record written before the field existed must still come back through
        the real function with the value None rather than raising or defaulting.
        """
        rid = _started(status="completed", finished_at="2026-07-25T01:00:00+00:00")
        st = jobs.status(rid)
        assert st["finished_at"] == "2026-07-25T01:00:00+00:00"
        assert st["finished_at_precision"] is None

    def test_list_jobs_reports_a_legacy_record_as_unknown(self, sandbox):
        """`list_jobs()` copies the field from `status()` (jobs.py:2152) — a
        legacy record must flow through without a KeyError and with precision
        None in the row, not just in the underlying `status()` call.
        """
        rid = _started(status="completed", finished_at="2026-07-25T01:00:00+00:00")
        rows = jobs.list_jobs()
        row = next(r for r in rows if r["run_id"] == rid)
        assert row["finished_at"] == "2026-07-25T01:00:00+00:00"
        assert row["finished_at_precision"] is None

    def test_every_terminal_path_answers(self, sandbox, monkeypatch, no_delivery):
        """A field only some writers set is a field a reader cannot use.

        Stated as a set so a new terminal path that forgets it fails here rather
        than shipping a silent null.
        """
        monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
        answers = {}

        reaped = _started()
        jobs.reap_orphan(
            reaped, finding=jobs.FINDING_PID_ABSENT, observed_at="2026-07-25T02:00:00+00:00"
        )
        answers["reaper"] = _precision(reaped)

        killed = _started()
        jobs._mark_killed(jobs._read_job(killed))
        answers["kill"] = _precision(killed)

        hooked = _started()
        jobs.mark_terminal(hooked, "completed")
        answers["hook"] = _precision(hooked)

        assert None not in answers.values(), answers
        assert answers["reaper"] == jobs.FINISHED_AT_UPPER_BOUND
        assert answers["kill"] == jobs.FINISHED_AT_OBSERVED
        assert answers["hook"] == jobs.FINISHED_AT_OBSERVED
