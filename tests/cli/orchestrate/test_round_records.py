# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The durable records a manifest round leaves behind.

Two properties carry the weight and both are about readers that share nothing
with the writer: the quiescence domain a reaper sweeps is read from these
files rather than from a live runner's memory, and `round_state` tells any
reader — including one that knows nothing about manifest rounds — whether the
leg facts are all in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lionagi.cli.orchestrate._round_records import (
    LEG_FAILED,
    LEG_SUCCEEDED,
    LEG_TIMED_OUT,
    RECORDED_BY_KILL_REAPER,
    RECORDED_BY_RUNNER,
    RESULT_COMPLETED,
    RESULT_FAILED,
    RESULT_PARTIAL,
    ROUND_STATE_COMPLETE,
    ROUND_STATE_PENDING,
    ROUND_VERSION,
    LegDispatch,
    complete_leg_record,
    flip_round_complete,
    legs_dir,
    read_leg_records,
    read_round_summary,
    recorded_control_groups,
    round_path,
    round_result,
    write_leg_dispatch,
    write_round_summary,
)


def _dispatch(
    label: str = "review-a",
    *,
    pgid: int | None = 41230,
    pid_create_time: float | None = 1785800000.5,
) -> LegDispatch:
    return LegDispatch(
        label=label,
        cwd=f"/abs/worktrees/{label}",
        model="claude/claude-sonnet-5",
        env_keys=("CARGO_TARGET_DIR",),
        brief_hash="blake2b:deadbeef",
        started_at="2026-08-03T12:00:00+00:00",
        pid=pgid,
        pgid=pgid,
        pid_create_time=pid_create_time,
    )


class TestLegDispatchRecord:
    def test_the_spawn_write_carries_the_facts_that_exist_before_the_leg_runs(self, tmp_path):
        write_leg_dispatch(tmp_path, _dispatch())

        record = json.loads((legs_dir(tmp_path) / "review-a.json").read_text())
        assert record["label"] == "review-a"
        assert record["cwd"] == "/abs/worktrees/review-a"
        assert record["pgid"] == 41230
        assert record["pid_create_time"] == 1785800000.5
        assert record["brief_hash"] == "blake2b:deadbeef"
        # The end is not knowable yet, and null says so rather than a default
        # that reads like an answer.
        assert record["status"] is None
        assert record["artifacts"] is None
        assert record["recorded_by"] is None

    def test_env_values_never_reach_the_record(self, tmp_path):
        """The record is readable by any caller. Keys say what was configured;
        the manifest snapshot is the durable source for the values."""
        write_leg_dispatch(tmp_path, _dispatch())

        text = (legs_dir(tmp_path) / "review-a.json").read_text()
        assert "CARGO_TARGET_DIR" in text
        record = json.loads(text)
        assert record["env_keys"] == ["CARGO_TARGET_DIR"]
        assert "env" not in record

    def test_completing_a_record_keeps_the_dispatch_facts(self, tmp_path):
        write_leg_dispatch(tmp_path, _dispatch())

        assert complete_leg_record(
            tmp_path,
            "review-a",
            status=LEG_SUCCEEDED,
            finished_at="2026-08-03T12:05:00+00:00",
            harvest_state="harvested-2",
            harvest_detail={"files": 2, "bytes": 40, "skipped": []},
            artifacts=["verdict.md", "notes.md"],
            recorded_by=RECORDED_BY_RUNNER,
        )

        record = json.loads((legs_dir(tmp_path) / "review-a.json").read_text())
        assert record["status"] == LEG_SUCCEEDED
        assert record["artifacts"] == ["verdict.md", "notes.md"]
        assert record["recorded_by"] == RECORDED_BY_RUNNER
        # Still there: a reaper writing the end must not erase what the runner
        # recorded at spawn, and the group plus its start time in particular are
        # the sweep's domain.
        assert record["pgid"] == 41230
        assert record["pid_create_time"] == 1785800000.5
        assert record["brief_hash"] == "blake2b:deadbeef"

    def test_first_write_wins_and_the_second_writer_is_told(self, tmp_path):
        """A claim can change hands between a dead holder and its successor.
        The successor must not overwrite facts the dead one established."""
        write_leg_dispatch(tmp_path, _dispatch())
        complete_leg_record(
            tmp_path,
            "review-a",
            status=LEG_SUCCEEDED,
            finished_at="2026-08-03T12:05:00+00:00",
            harvest_state="harvested-1",
            harvest_detail=None,
            artifacts=["verdict.md"],
            recorded_by=RECORDED_BY_RUNNER,
        )

        wrote = complete_leg_record(
            tmp_path,
            "review-a",
            status=LEG_FAILED,
            finished_at="2026-08-03T12:09:00+00:00",
            harvest_state="harvest_failed",
            harvest_detail=None,
            artifacts=[],
            recorded_by=RECORDED_BY_KILL_REAPER,
        )

        assert wrote is False
        record = json.loads((legs_dir(tmp_path) / "review-a.json").read_text())
        assert record["status"] == LEG_SUCCEEDED
        assert record["recorded_by"] == RECORDED_BY_RUNNER

    def test_a_reaper_can_complete_a_leg_whose_dispatch_record_is_the_only_thing_on_disk(
        self, tmp_path
    ):
        write_leg_dispatch(tmp_path, _dispatch())

        assert complete_leg_record(
            tmp_path,
            "review-a",
            status=LEG_TIMED_OUT,
            finished_at="2026-08-03T12:20:00+00:00",
            harvest_state="harvest_failed",
            harvest_detail={"reason": "scratch unreadable"},
            artifacts=[],
            recorded_by=RECORDED_BY_KILL_REAPER,
        )

        record = json.loads((legs_dir(tmp_path) / "review-a.json").read_text())
        assert record["recorded_by"] == RECORDED_BY_KILL_REAPER
        # An empty artifact list beside harvest_failed is a different claim
        # from an empty list beside a successful harvest, and the state field
        # is what tells them apart.
        assert record["harvest_state"] == "harvest_failed"
        assert record["harvest_detail"] == {"reason": "scratch unreadable"}


class TestWritesAreAtomic:
    def test_a_reader_never_sees_a_half_written_record(self, tmp_path, monkeypatch):
        """Written to a temp file and renamed: a reader observes either the
        previous file or the whole new one, never a truncated parse."""
        import lionagi.cli.orchestrate._round_records as records_mod

        write_leg_dispatch(tmp_path, _dispatch())
        target = legs_dir(tmp_path) / "review-a.json"
        seen: list[dict | None] = []

        real_replace = records_mod.os.replace

        def observing_replace(src, dst):
            # Mid-write: the destination still holds the old, parseable record.
            try:
                seen.append(json.loads(Path(dst).read_text()))
            except (OSError, ValueError):
                seen.append(None)
            return real_replace(src, dst)

        monkeypatch.setattr(records_mod.os, "replace", observing_replace)

        complete_leg_record(
            tmp_path,
            "review-a",
            status=LEG_SUCCEEDED,
            finished_at="2026-08-03T12:05:00+00:00",
            harvest_state="harvested-1",
            harvest_detail=None,
            artifacts=["verdict.md"],
            recorded_by=RECORDED_BY_RUNNER,
        )

        assert seen and seen[0] is not None
        assert seen[0]["status"] is None  # the pre-rename state, fully parseable
        assert json.loads(target.read_text())["status"] == LEG_SUCCEEDED

    def test_a_failed_write_leaves_no_temp_file_behind(self, tmp_path, monkeypatch):
        import lionagi.cli.orchestrate._round_records as records_mod

        def failing_replace(src, dst):
            raise OSError("disk went away")

        monkeypatch.setattr(records_mod.os, "replace", failing_replace)

        with pytest.raises(OSError):
            write_leg_dispatch(tmp_path, _dispatch())

        leftovers = list(legs_dir(tmp_path).glob("*"))
        assert leftovers == []


class TestReadingRecords:
    def test_an_unreadable_record_does_not_poison_the_others(self, tmp_path):
        """One damaged file must not take the round's other facts with it, and
        must not masquerade as a leg that was never dispatched."""
        write_leg_dispatch(tmp_path, _dispatch("review-a"))
        write_leg_dispatch(tmp_path, _dispatch("review-b", pgid=41231))
        (legs_dir(tmp_path) / "review-b.json").write_text("{ not json")

        records = read_leg_records(tmp_path)

        assert set(records) == {"review-a", "review-b"}
        assert records["review-a"] is not None
        assert records["review-b"] is None

    def test_an_absent_legs_directory_reads_as_no_records(self, tmp_path):
        assert read_leg_records(tmp_path) == {}

    def test_json_that_is_not_an_object_reads_as_unreadable(self, tmp_path):
        write_leg_dispatch(tmp_path, _dispatch())
        (legs_dir(tmp_path) / "review-a.json").write_text("[1, 2, 3]")

        assert read_leg_records(tmp_path)["review-a"] is None


class TestRecordedControlGroups:
    def test_the_sweep_domain_comes_from_disk_not_from_a_live_runner(self, tmp_path):
        """A reaper that shared nothing with a dead runner must sweep the same
        groups the runner would have. That is only possible because each group
        was written at spawn."""
        write_leg_dispatch(tmp_path, _dispatch("review-a", pgid=41230))
        write_leg_dispatch(tmp_path, _dispatch("review-b", pgid=41231))

        assert recorded_control_groups(tmp_path) == [41230, 41231]

    def test_a_record_without_a_group_contributes_nothing_to_the_domain(self, tmp_path):
        """A sweep cannot certify a group it was never told about. The absence
        is the caller's to report; silently treating it as swept is what makes
        an empty domain indistinguishable from a clean one."""
        write_leg_dispatch(tmp_path, _dispatch("review-a", pgid=None))
        write_leg_dispatch(tmp_path, _dispatch("review-b", pgid=41231))

        assert recorded_control_groups(tmp_path) == [41231]

    def test_an_unreadable_record_contributes_nothing_and_does_not_raise(self, tmp_path):
        write_leg_dispatch(tmp_path, _dispatch("review-a", pgid=41230))
        write_leg_dispatch(tmp_path, _dispatch("review-b", pgid=41231))
        (legs_dir(tmp_path) / "review-a.json").write_text("broken")

        assert recorded_control_groups(tmp_path) == [41231]

    def test_a_nonsense_group_value_is_not_swept(self, tmp_path):
        for bad in (0, -1, "41230", None):
            (legs_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
            (legs_dir(tmp_path) / "review-a.json").write_text(
                json.dumps({"pgid": bad, "pid_create_time": 1785800000.5})
            )
            assert recorded_control_groups(tmp_path) == []

    def test_a_group_without_a_start_time_is_not_a_domain(self, tmp_path):
        """A group id on its own names whatever the kernel has since put at that
        number. Sweeping it would report a stranger as this run's straggler, and
        finding it empty would certify nothing, so the record contributes no
        domain at all rather than a plausible-looking one."""
        write_leg_dispatch(tmp_path, _dispatch("review-a", pgid=41230, pid_create_time=None))
        write_leg_dispatch(tmp_path, _dispatch("review-b", pgid=41231))

        assert recorded_control_groups(tmp_path) == [41231]

    def test_a_nonsense_start_time_is_not_a_start_time(self, tmp_path):
        for bad in ("1785800000.5", [], {}):
            (legs_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
            (legs_dir(tmp_path) / "review-a.json").write_text(
                json.dumps({"pgid": 41230, "pid_create_time": bad})
            )
            assert recorded_control_groups(tmp_path) == []


class TestRoundSummary:
    def test_the_summary_exists_before_any_leg_runs_and_says_it_is_pending(self, tmp_path):
        """A terminal status published by ANY writer at ANY point — including
        one that knows nothing about manifest rounds — must be observably
        pending rather than silently incomplete."""
        write_round_summary(tmp_path, labels=["review-a", "review-b"])

        summary = json.loads(round_path(tmp_path).read_text())
        assert summary["round_version"] == ROUND_VERSION
        assert summary["round_state"] == ROUND_STATE_PENDING
        assert summary["result"] is None
        assert summary["legs_total"] == 2
        assert summary["legs"] == ["review-a", "review-b"]

    def test_the_flip_to_complete_is_the_last_write(self, tmp_path):
        write_round_summary(tmp_path, labels=["review-a", "review-b"])

        assert flip_round_complete(tmp_path, result=RESULT_PARTIAL, legs_succeeded=1)

        summary = read_round_summary(tmp_path)
        assert summary["round_state"] == ROUND_STATE_COMPLETE
        assert summary["result"] == RESULT_PARTIAL
        assert summary["legs_succeeded"] == 1
        assert summary["legs_total"] == 2

    def test_flipping_a_summary_that_was_never_written_reports_failure(self, tmp_path):
        """A round nobody opened is not a round anyone may close. Saying so
        beats creating a summary that claims completeness for legs no record
        describes."""
        assert flip_round_complete(tmp_path, result=RESULT_COMPLETED, legs_succeeded=0) is False
        assert read_round_summary(tmp_path) is None

    def test_an_unreadable_summary_reads_as_absent_rather_than_as_data(self, tmp_path):
        write_round_summary(tmp_path, labels=["review-a"])
        round_path(tmp_path).write_text("{ truncated")

        assert read_round_summary(tmp_path) is None


class TestRoundResult:
    def test_every_leg_succeeded_and_nothing_failed_to_harvest(self):
        assert round_result([LEG_SUCCEEDED, LEG_SUCCEEDED], [False, False]) == RESULT_COMPLETED

    def test_a_harvest_failure_degrades_a_fully_successful_round(self):
        """Artifacts were, or may have been, written and cannot be served. The
        result must not paper over that, even though every leg succeeded."""
        assert round_result([LEG_SUCCEEDED, LEG_SUCCEEDED], [False, True]) == RESULT_PARTIAL

    def test_one_success_among_failures_is_partial(self):
        assert round_result([LEG_SUCCEEDED, LEG_FAILED, LEG_TIMED_OUT], [False] * 3) == (
            RESULT_PARTIAL
        )

    def test_no_success_at_all_is_failed(self):
        assert round_result([LEG_FAILED, LEG_TIMED_OUT], [False, False]) == RESULT_FAILED

    def test_a_round_with_no_legs_is_failed_not_completed(self):
        """`all()` over an empty list is True, so the vacuous case would
        otherwise report a round of nothing as completed."""
        assert round_result([], []) == RESULT_FAILED

    @pytest.mark.parametrize(
        "statuses",
        [
            [LEG_SUCCEEDED],
            [LEG_FAILED],
            [LEG_SUCCEEDED, LEG_FAILED],
            [LEG_TIMED_OUT, LEG_TIMED_OUT],
            [LEG_SUCCEEDED, LEG_SUCCEEDED, LEG_FAILED],
        ],
    )
    @pytest.mark.parametrize("harvest", [True, False])
    def test_the_three_rules_are_total(self, statuses, harvest):
        """Every combination of leg states and harvest states lands on exactly
        one result. No mixed round is undecided."""
        result = round_result(statuses, [harvest] * len(statuses))
        assert result in (RESULT_COMPLETED, RESULT_PARTIAL, RESULT_FAILED)
