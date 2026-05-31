# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.runtime.scheduler.

Covers:
 - ScheduleItem creation
 - SchedulerEngine.add (interval and cron)
 - SchedulerEngine.get_due_items
 - pause / resume state transitions
 - mark_started / mark_completed / mark_failed transitions
 - max_runs enforcement
 - remove
 - list_items with status filter
 - parse_cron basic patterns
 - next_cron_fire computation
 - Invalid cron raises ValueError
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.runtime.scheduler import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    ScheduleItem,
    SchedulerEngine,
    next_cron_fire,
    parse_cron,
)

# ---------------------------------------------------------------------------
# ScheduleItem creation
# ---------------------------------------------------------------------------


class TestScheduleItemCreation:
    def test_defaults_are_set(self):
        item = ScheduleItem(name="test", next_run_at=1234.0)
        assert item.name == "test"
        assert item.status == STATUS_ACTIVE
        assert item.run_count == 0
        assert item.max_runs is None
        assert item.last_run_at is None
        assert item.flow_spec == {}
        assert uuid.UUID(item.item_id)  # valid UUID

    def test_custom_fields(self):
        item = ScheduleItem(
            name="custom",
            cron_expr="*/5 * * * *",
            next_run_at=999.0,
            max_runs=3,
            flow_spec={"flow_type": "play"},
        )
        assert item.cron_expr == "*/5 * * * *"
        assert item.max_runs == 3
        assert item.flow_spec == {"flow_type": "play"}

    def test_item_id_is_unique(self):
        a = ScheduleItem(name="a", next_run_at=0.0)
        b = ScheduleItem(name="b", next_run_at=0.0)
        assert a.item_id != b.item_id

    def test_model_copy_is_independent(self):
        item = ScheduleItem(name="x", next_run_at=1.0)
        copy = item.model_copy()
        copy.status = STATUS_PAUSED
        assert item.status == STATUS_ACTIVE


# ---------------------------------------------------------------------------
# SchedulerEngine.add — interval
# ---------------------------------------------------------------------------


class TestEngineAddInterval:
    def test_add_interval_computes_next_run_at(self):
        engine = SchedulerEngine()
        before = time.time()
        item = engine.add("pulse", {}, interval_seconds=60.0)
        after = time.time()
        assert item.status == STATUS_ACTIVE
        assert before + 60.0 <= item.next_run_at <= after + 60.0

    def test_add_interval_stores_interval(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=30.0)
        assert item.interval_seconds == 30.0

    def test_add_zero_interval_raises(self):
        engine = SchedulerEngine()
        with pytest.raises(ValueError, match="positive"):
            engine.add("bad", {}, interval_seconds=0)

    def test_add_negative_interval_raises(self):
        engine = SchedulerEngine()
        with pytest.raises(ValueError, match="positive"):
            engine.add("bad", {}, interval_seconds=-5)

    def test_add_both_cron_and_interval_raises(self):
        engine = SchedulerEngine()
        with pytest.raises(ValueError, match="at most one"):
            engine.add("bad", {}, cron_expr="*/5 * * * *", interval_seconds=60)

    def test_add_max_runs(self):
        engine = SchedulerEngine()
        item = engine.add("limited", {}, interval_seconds=10, max_runs=5)
        assert item.max_runs == 5


# ---------------------------------------------------------------------------
# SchedulerEngine.add — cron
# ---------------------------------------------------------------------------


class TestEngineAddCron:
    def test_add_cron_computes_next_run_at(self):
        engine = SchedulerEngine()
        item = engine.add("cron-item", {}, cron_expr="*/5 * * * *")
        assert item.next_run_at > time.time()
        assert item.cron_expr == "*/5 * * * *"

    def test_add_invalid_cron_raises(self):
        engine = SchedulerEngine()
        with pytest.raises(ValueError):
            engine.add("bad", {}, cron_expr="bad expr")

    def test_add_cron_midnight_weekly(self):
        engine = SchedulerEngine()
        item = engine.add("weekly", {}, cron_expr="0 0 * * 1")
        # Next Monday midnight should be in the future
        assert item.next_run_at > time.time()


# ---------------------------------------------------------------------------
# get_due_items
# ---------------------------------------------------------------------------


class TestGetDueItems:
    def test_no_items_due_when_all_in_future(self):
        engine = SchedulerEngine()
        engine.add("future", {}, interval_seconds=3600)
        assert engine.get_due_items() == []

    def test_item_due_when_next_run_in_past(self):
        engine = SchedulerEngine()
        item = engine.add("past", {}, interval_seconds=3600)
        with engine._lock:
            engine._items[item.item_id].next_run_at = time.time() - 1
        due = engine.get_due_items()
        assert len(due) == 1
        assert due[0].item_id == item.item_id

    def test_paused_item_not_returned_as_due(self):
        engine = SchedulerEngine()
        item = engine.add("p", {}, interval_seconds=3600)
        with engine._lock:
            engine._items[item.item_id].next_run_at = time.time() - 1
        engine.pause(item.item_id)
        assert engine.get_due_items() == []

    def test_due_items_sorted_by_next_run_at(self):
        engine = SchedulerEngine()
        now = time.time()
        item_b = engine.add("b", {}, interval_seconds=3600)
        item_a = engine.add("a", {}, interval_seconds=3600)
        with engine._lock:
            engine._items[item_a.item_id].next_run_at = now - 2
            engine._items[item_b.item_id].next_run_at = now - 1
        due = engine.get_due_items()
        assert len(due) == 2
        assert due[0].item_id == item_a.item_id  # earlier next_run_at first
        assert due[1].item_id == item_b.item_id

    def test_completed_item_not_returned(self):
        engine = SchedulerEngine()
        item = engine.add("c", {}, interval_seconds=3600, max_runs=1)
        with engine._lock:
            engine._items[item.item_id].next_run_at = time.time() - 1
        engine.mark_started(item.item_id)
        engine.mark_completed(item.item_id)
        assert engine.get_due_items() == []


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    def test_pause_active_item(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        assert engine.pause(item.item_id)
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_PAUSED

    def test_resume_paused_item(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        engine.pause(item.item_id)
        assert engine.resume(item.item_id)
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_ACTIVE

    def test_pause_nonexistent_returns_false(self):
        engine = SchedulerEngine()
        assert not engine.pause("does-not-exist")

    def test_resume_nonexistent_returns_false(self):
        engine = SchedulerEngine()
        assert not engine.resume("does-not-exist")

    def test_pause_running_item_returns_false(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        engine.mark_started(item.item_id)
        assert not engine.pause(item.item_id)

    def test_resume_non_paused_returns_false(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        # Active, not paused
        assert not engine.resume(item.item_id)

    def test_resume_recomputes_next_run_at_for_interval(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        engine.pause(item.item_id)
        before = time.time()
        engine.resume(item.item_id)
        fetched = engine.get_item(item.item_id)
        # Should be ~60s in the future from now, not from original creation
        assert fetched.next_run_at >= before + 59


# ---------------------------------------------------------------------------
# mark_started / mark_completed / mark_failed
# ---------------------------------------------------------------------------


class TestMarkTransitions:
    def test_mark_started_transitions_to_running(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        assert engine.mark_started(item.item_id)
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_RUNNING
        assert fetched.last_run_at is not None

    def test_mark_completed_from_running(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        engine.mark_started(item.item_id)
        assert engine.mark_completed(item.item_id)
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_ACTIVE
        assert fetched.run_count == 1

    def test_mark_failed_from_running(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        engine.mark_started(item.item_id)
        assert engine.mark_failed(item.item_id, error="boom")
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_FAILED
        assert fetched.run_count == 1
        assert fetched.flow_spec.get("_last_error") == "boom"

    def test_mark_started_not_active_returns_false(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        engine.pause(item.item_id)
        assert not engine.mark_started(item.item_id)

    def test_mark_completed_not_running_returns_false(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        # item is active, not running
        assert not engine.mark_completed(item.item_id)

    def test_mark_failed_not_running_returns_false(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        assert not engine.mark_failed(item.item_id)

    def test_mark_started_nonexistent_returns_false(self):
        engine = SchedulerEngine()
        assert not engine.mark_started("ghost")

    def test_mark_completed_nonexistent_returns_false(self):
        engine = SchedulerEngine()
        assert not engine.mark_completed("ghost")

    def test_mark_failed_nonexistent_returns_false(self):
        engine = SchedulerEngine()
        assert not engine.mark_failed("ghost")


# ---------------------------------------------------------------------------
# max_runs enforcement
# ---------------------------------------------------------------------------


class TestMaxRuns:
    def test_auto_complete_after_max_runs(self):
        engine = SchedulerEngine()
        item = engine.add("once", {}, interval_seconds=1, max_runs=2)
        for _ in range(2):
            engine.mark_started(item.item_id)
            engine.mark_completed(item.item_id)
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_COMPLETED
        assert fetched.run_count == 2

    def test_does_not_complete_below_max(self):
        engine = SchedulerEngine()
        item = engine.add("multi", {}, interval_seconds=1, max_runs=3)
        engine.mark_started(item.item_id)
        engine.mark_completed(item.item_id)
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_ACTIVE
        assert fetched.run_count == 1

    def test_unlimited_runs_never_auto_complete(self):
        engine = SchedulerEngine()
        item = engine.add("inf", {}, interval_seconds=1, max_runs=None)
        for _ in range(10):
            engine.mark_started(item.item_id)
            engine.mark_completed(item.item_id)
        fetched = engine.get_item(item.item_id)
        assert fetched.status == STATUS_ACTIVE
        assert fetched.run_count == 10


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_existing_item(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        assert engine.remove(item.item_id)
        assert engine.get_item(item.item_id) is None

    def test_remove_nonexistent_returns_false(self):
        engine = SchedulerEngine()
        assert not engine.remove("does-not-exist")

    def test_remove_reduces_list(self):
        engine = SchedulerEngine()
        a = engine.add("a", {}, interval_seconds=60)
        engine.add("b", {}, interval_seconds=60)
        engine.remove(a.item_id)
        assert len(engine.list_items()) == 1


# ---------------------------------------------------------------------------
# list_items with status filter
# ---------------------------------------------------------------------------


class TestListItems:
    def test_list_all(self):
        engine = SchedulerEngine()
        engine.add("a", {}, interval_seconds=60)
        engine.add("b", {}, interval_seconds=60)
        assert len(engine.list_items()) == 2

    def test_list_filter_active(self):
        engine = SchedulerEngine()
        a = engine.add("a", {}, interval_seconds=60)
        b = engine.add("b", {}, interval_seconds=60)
        engine.pause(b.item_id)
        active = engine.list_items(status=STATUS_ACTIVE)
        assert len(active) == 1
        assert active[0].item_id == a.item_id

    def test_list_filter_paused(self):
        engine = SchedulerEngine()
        a = engine.add("a", {}, interval_seconds=60)
        engine.pause(a.item_id)
        paused = engine.list_items(status=STATUS_PAUSED)
        assert len(paused) == 1

    def test_list_returns_copies(self):
        engine = SchedulerEngine()
        item = engine.add("x", {}, interval_seconds=60)
        listed = engine.list_items()
        listed[0].status = STATUS_FAILED  # mutate the copy
        # Original should be unchanged
        assert engine.get_item(item.item_id).status == STATUS_ACTIVE

    def test_list_empty_engine(self):
        engine = SchedulerEngine()
        assert engine.list_items() == []


# ---------------------------------------------------------------------------
# parse_cron
# ---------------------------------------------------------------------------


class TestParseCron:
    def test_all_wildcards(self):
        result = parse_cron("* * * * *")
        assert result == {
            "minute": "*",
            "hour": "*",
            "day": "*",
            "month": "*",
            "weekday": "*",
        }

    def test_every_five_minutes(self):
        result = parse_cron("*/5 * * * *")
        assert result["minute"] == {"step": 5}
        assert result["hour"] == "*"

    def test_every_two_hours_at_top(self):
        result = parse_cron("0 */2 * * *")
        assert result["minute"] == 0
        assert result["hour"] == {"step": 2}

    def test_monday_midnight(self):
        result = parse_cron("0 0 * * 1")
        assert result["minute"] == 0
        assert result["hour"] == 0
        assert result["weekday"] == 1

    def test_first_of_month_midnight(self):
        result = parse_cron("0 0 1 * *")
        assert result["day"] == 1
        assert result["hour"] == 0
        assert result["minute"] == 0

    def test_january_first_midnight(self):
        result = parse_cron("0 0 1 1 *")
        assert result["month"] == 1
        assert result["day"] == 1

    def test_nine_thirty_weekdays(self):
        # "30 9 * * *" fires every day at 09:30 — weekdays need range which
        # is not supported so we just test the valid form
        result = parse_cron("30 9 * * *")
        assert result["minute"] == 30
        assert result["hour"] == 9


# ---------------------------------------------------------------------------
# Invalid cron raises ValueError
# ---------------------------------------------------------------------------


class TestParseCronInvalid:
    def test_too_few_fields(self):
        with pytest.raises(ValueError, match="5 fields"):
            parse_cron("* * * *")

    def test_too_many_fields(self):
        with pytest.raises(ValueError, match="5 fields"):
            parse_cron("* * * * * *")

    def test_range_unsupported(self):
        with pytest.raises(ValueError, match="unsupported"):
            parse_cron("1-5 * * * *")

    def test_list_unsupported(self):
        with pytest.raises(ValueError, match="unsupported"):
            parse_cron("1,3 * * * *")

    def test_minute_out_of_range(self):
        with pytest.raises(ValueError, match="out of range"):
            parse_cron("60 * * * *")

    def test_hour_out_of_range(self):
        with pytest.raises(ValueError, match="out of range"):
            parse_cron("0 24 * * *")

    def test_weekday_out_of_range(self):
        with pytest.raises(ValueError, match="out of range"):
            parse_cron("0 0 * * 7")

    def test_step_zero_raises(self):
        with pytest.raises(ValueError, match="step"):
            parse_cron("*/0 * * * *")

    def test_invalid_literal(self):
        with pytest.raises(ValueError):
            parse_cron("abc * * * *")

    def test_empty_string(self):
        with pytest.raises(ValueError):
            parse_cron("")


# ---------------------------------------------------------------------------
# next_cron_fire
# ---------------------------------------------------------------------------


class TestNextCronFire:
    def test_every_five_minutes_fires_within_five_minutes(self):
        parsed = parse_cron("*/5 * * * *")
        now = time.time()
        nxt = next_cron_fire(parsed, after=now)
        # Should be within 5 minutes ahead
        assert now < nxt <= now + 5 * 60

    def test_every_five_minutes_is_on_five_minute_boundary(self):
        parsed = parse_cron("*/5 * * * *")
        nxt = next_cron_fire(parsed, after=time.time())
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(nxt, tz=timezone.utc)
        assert dt.minute % 5 == 0
        assert dt.second == 0

    def test_specific_hour_minute_fires_correctly(self):
        parsed = parse_cron("30 3 * * *")
        # Use a known fixed timestamp: 2026-01-01 00:00:00 UTC
        # epoch: 1735689600
        after = 1735689600.0
        nxt = next_cron_fire(parsed, after=after)
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(nxt, tz=timezone.utc)
        assert dt.hour == 3
        assert dt.minute == 30
        assert dt.second == 0

    def test_next_fire_is_strictly_after_after(self):
        parsed = parse_cron("*/1 * * * *")
        now = time.time()
        nxt = next_cron_fire(parsed, after=now)
        assert nxt > now

    def test_fires_only_on_matching_weekday(self):
        # Monday = 1 in cron (0=Sun, 1=Mon)
        parsed = parse_cron("0 0 * * 1")
        # Start from 2026-01-01 Thu 00:00 UTC (epoch 1735689600)
        after = 1735689600.0
        nxt = next_cron_fire(parsed, after=after)
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(nxt, tz=timezone.utc)
        # Should land on a Monday
        assert dt.weekday() == 0  # Python Monday=0

    def test_hourly_fires_within_one_hour(self):
        parsed = parse_cron("0 * * * *")
        now = time.time()
        nxt = next_cron_fire(parsed, after=now)
        assert now < nxt <= now + 3600

    def test_next_fire_returns_float(self):
        parsed = parse_cron("0 0 * * *")
        nxt = next_cron_fire(parsed, after=time.time())
        assert isinstance(nxt, float)
