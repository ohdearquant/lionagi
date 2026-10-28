# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Placement that reuses the cache entries a branch has written (ADR-0127 Amendment 3)."""

import copy
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.providers.anthropic import _prompt_cache as planner
from lionagi.service import _prompt_cache as ledger_mod
from lionagi.service._prompt_cache import PromptCacheCall, clone_ledger, ledger_for, settle
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

MODEL = "claude-opus-5-5"
SYSTEM = "You review pull requests."
HOUR = {"type": "ephemeral", "ttl": "1h"}
FIVE = {"type": "ephemeral"}
TOOL = {"name": "lookup", "description": "Find a record.", "input_schema": {"type": "object"}}


@pytest.fixture(autouse=True)
def routes(monkeypatch):
    """Set the routes a request can take; one direct key unless a test says otherwise."""

    def use(*names):
        monkeypatch.setattr(ledger_mod, "placement_routes", lambda: frozenset(names))

    use("direct")
    return use


def _payload(last, *, tools=True, **extra):
    """A request with one block per lookback position, its last block at position ``last``.

    With the tool, position 1 is the tool, 2 the system block and message k sits at 3 + k.
    Without it, position 1 is the system block and message k sits at 2 + k.
    """
    first = 3 if tools else 2
    payload = {
        "model": MODEL,
        "max_tokens": 100,
        "system": [{"type": "text", "text": SYSTEM}],
        "messages": [
            {
                "role": "user" if k % 2 == 0 else "assistant",
                "content": [{"type": "text", "text": f"turn {k}"}],
            }
            for k in range(last - first + 1)
        ],
        **extra,
    }
    if tools:
        payload["tools"] = [copy.deepcopy(TOOL)]
    return payload


def _point(payload, position):
    (point,) = [p for p in planner.points(payload)[0] if p.position == position]
    return point


def _block(payload, position):
    point = _point(payload, position)
    if point.tier == "messages":
        return payload["messages"][point.message]["content"][point.slot]
    return payload[point.tier][point.slot]


def _row(payload, position, **row):
    return _entry(_point(payload, position), **row)


def _entry(point, *, ttl="5m", length=1000, on=("direct",), process=None, age=0.0):
    return point.cert, {
        "ttl": ttl,
        "tier": point.tier,
        "length": length,
        "marked_at": time.time() - age,
        "hit_at": None,
        "routes": {route: process or ledger_mod._PROCESS for route in on},
    }


def _ledger(*rows):
    return {"version": 1, "entries": dict(rows), "evicted_until": None}


def _place(payload, ledger, intent="continuing", anchors=()):
    call = PromptCacheCall(ledger=ledger, intent=intent, anchors=anchors)
    call.placement = planner.place(payload, call)
    return call, call.placement


def _ttls(record):
    return {mark["position"]: mark["ttl"] for mark in record["marks"]}


def _roles(record):
    return {mark["position"]: mark["roles"] for mark in record["marks"]}


def _certs(payload):
    return {p.position: p.cert for p in planner.points(payload)[0]}


def _completed(read=0, creation=0, status=EventStatus.COMPLETED):
    usage = {
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": creation,
    }
    return SimpleNamespace(status=status, response={"usage": usage})


def _attempt(call, route="direct", started=None):
    call.attempts.append((route, time.time() if started is None else started))


# --- the prefix certificate ---


def test_a_changed_system_block_keeps_the_tool_entry_and_retires_every_later_one():
    before = _certs(_payload(10))
    changed = _payload(10)
    changed["system"][0]["text"] = "You test pull requests."
    after = _certs(changed)

    assert after[1] == before[1]
    assert all(after[p] != before[p] for p in range(2, 11))


@pytest.mark.parametrize(
    "field, value, kept",
    [
        ("speed", "fast", {1}),
        ("tool_choice", {"type": "any"}, {1, 2}),
        ("output_config", {"effort": "low"}, set()),
        ("thinking", {"type": "adaptive"}, set()),
    ],
)
def test_a_setting_retires_the_tiers_whose_cache_depends_on_it(field, value, kept):
    before = _certs(_payload(10))
    after = _certs(_payload(10, **{field: value}))

    assert {p for p in before if before[p] == after[p]} == kept


def test_a_mark_on_a_block_is_not_content_and_a_mark_inside_a_tool_schema_is():
    base = _certs(_payload(5))
    marked = _payload(5)
    marked["tools"][0]["cache_control"] = HOUR
    inner = _payload(5)
    inner["tools"][0]["input_schema"] = {"type": "object", "cache_control": FIVE}

    assert _certs(marked) == base
    assert _certs(inner)[1] != base[1]


def test_a_string_turn_and_its_one_text_block_certify_alike():
    as_string = _payload(6)
    for message in as_string["messages"]:
        message["content"] = message["content"][0]["text"]

    assert _certs(as_string) == _certs(_payload(6))


def test_a_message_level_field_retires_the_entries_from_its_message_on():
    before = _certs(_payload(10))
    changed = _payload(10)
    changed["messages"][4]["output_config"] = {"effort": "low"}  # message 4 sits at position 7
    after = _certs(changed)

    assert all(after[p] == before[p] for p in range(1, 7))
    assert all(after[p] != before[p] for p in range(7, 11))


def test_an_inserted_empty_message_takes_no_position_and_retires_every_later_entry():
    before = _certs(_payload(10))
    changed = _payload(10)
    changed["messages"].insert(4, {"role": "user", "content": []})
    after = _certs(changed)

    assert sorted(after) == sorted(before)
    assert all(after[p] == before[p] for p in range(1, 7))
    assert all(after[p] != before[p] for p in range(7, 11))


def _run_payload():
    """Six positions of turns, then a tool_use run at 7, a tool_result run at 8 and text at 9."""
    payload = _payload(6)
    payload["messages"].append(
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_a", "name": "lookup", "input": {}},
                {"type": "tool_use", "id": "toolu_b", "name": "lookup", "input": {}},
            ],
        }
    )
    payload["messages"].append(
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": "a"},
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": "b"},
                {"type": "text", "text": "go on"},
            ],
        }
    )
    return payload


def _by_place(payload):
    return {(p.message, p.slot): p for p in planner.points(payload)[0] if p.message is not None}


def test_a_run_of_tool_results_is_one_position_and_each_result_its_own_endpoint():
    blocks = _by_place(_run_payload())
    changed = _run_payload()
    changed["messages"][5]["content"][1]["content"] = "changed"
    after = _by_place(changed)

    assert blocks[(4, 0)].position == blocks[(4, 1)].position == 7
    assert blocks[(5, 0)].position == blocks[(5, 1)].position == 8
    assert blocks[(5, 2)].position == 9
    assert after[(5, 0)].cert == blocks[(5, 0)].cert
    assert after[(5, 1)].cert != blocks[(5, 1)].cert


# --- reach ---


@pytest.mark.parametrize("distance, exact_read", [(19, False), (20, True), (21, True)])
def test_the_tail_serves_as_the_read_only_within_twenty_positions(distance, exact_read):
    payload = _payload(30 + distance, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 30)))

    roles = _roles(record)
    assert (30 in roles) is exact_read
    assert ("read" in roles[30 + distance]) is not exact_read
    assert record["g1"] == "met"


@pytest.mark.parametrize("message", [4, 5], ids=["tool_use run", "tool_result run"])
def test_a_caller_mark_early_in_a_run_does_not_reach_a_later_block_of_it(message):
    payload = _run_payload()
    payload["messages"][message]["content"][0]["cache_control"] = FIVE
    later = _by_place(payload)[(message, 1)]
    _, record = _place(payload, _ledger(_entry(later)), intent="final")

    assert later.index in {mark["index"] for mark in record["marks"]}
    assert record["g1"] == "met"


@pytest.mark.parametrize("message", [4, 5], ids=["tool_use run", "tool_result run"])
def test_a_caller_mark_late_in_a_run_reaches_an_earlier_block_of_it(message):
    payload = _run_payload()
    payload["messages"][message]["content"][1]["cache_control"] = FIVE
    earlier = _by_place(payload)[(message, 0)]
    _, record = _place(payload, _ledger(_entry(earlier)), intent="final")

    assert earlier.index not in {mark["index"] for mark in record["marks"]}
    assert record["g1"] == "met"


# --- allocation ---


def test_a_continuing_request_reads_marks_the_tail_and_the_stable_end_before_any_probe():
    payload = _payload(100)
    _block(payload, 1)["cache_control"] = HOUR
    ledger = _ledger(_row(payload, 20), _row(payload, 60, process="another process"))
    _, record = _place(payload, ledger)

    assert sorted(_ttls(record)) == [1, 2, 20, 100]
    assert "probe_slots" in record["degraded"]
    assert record["g1"] == "met"


def test_a_final_request_reads_the_established_entry_and_probes_the_deeper_unknown_one():
    payload = _payload(100, tools=False)
    ledger = _ledger(_row(payload, 20), _row(payload, 60, process="another process"))
    _, record = _place(payload, ledger, intent="final")

    assert sorted(_ttls(record)) == [1, 20, 60]
    assert record["g1"] == "met"


def test_an_unknown_entry_the_tail_already_reaches_takes_no_probe_mark():
    payload = _payload(100, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 90, process="another process")))

    assert sorted(_ttls(record)) == [1, 100]
    assert [row["index"] for row in record["landing"]] == [_point(payload, 90).index]


def test_after_a_resume_the_deepest_unknown_entries_are_probed_first():
    payload = _payload(100, tools=False)
    rows = [_row(payload, p, process="another process") for p in (20, 40, 60)]
    _, record = _place(payload, _ledger(*rows))

    assert sorted(_ttls(record)) == [1, 40, 60, 100]
    assert "probe_slots" in record["degraded"]
    assert record["g1"] == {"excluded": "no_established_candidate"}


def test_after_a_fold_the_read_reaches_the_entry_beyond_the_tails_window():
    payload = _payload(35, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 10)))

    assert sorted(_ttls(record)) == [1, 10, 35]
    assert record["g1"] == "met"


def test_a_final_request_marks_nothing_past_the_entry_it_reads():
    payload = _payload(35, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 10)), intent="final")

    assert sorted(_ttls(record)) == [1, 10]


def test_a_final_request_with_no_candidate_marks_only_the_stable_end():
    _, record = _place(_payload(35, tools=False), _ledger(), intent="final")

    assert sorted(_ttls(record)) == [1]


def test_an_anchor_that_reaches_the_entry_serves_as_its_read():
    payload = _payload(75, tools=False)
    anchor = (_point(payload, 52).message, 0)
    _, record = _place(payload, _ledger(_row(payload, 40)), anchors=(anchor,))

    assert sorted(_ttls(record)) == [1, 52, 75]
    assert set(_roles(record)[52]) == {"anchor", "read"}
    assert record["g1"] == "met"


def test_an_anchor_inside_the_tails_window_still_takes_its_own_mark():
    payload = _payload(30, tools=False)
    anchor = (_point(payload, 25).message, 0)
    _, record = _place(payload, _ledger(), anchors=(anchor,))

    assert sorted(_ttls(record)) == [1, 25, 30]


def test_a_final_request_places_no_anchor():
    payload = _payload(30, tools=False)
    anchor = (_point(payload, 25).message, 0)
    _, record = _place(payload, _ledger(), intent="final", anchors=(anchor,))

    assert sorted(_ttls(record)) == [1]


def test_a_side_request_places_only_the_stable_mark():
    payload = _payload(30, tools=False)
    _, record = _place(payload, None, intent="side")

    assert sorted(_ttls(record)) == [1]


def test_a_side_request_reads_and_probes_no_conversation_entry():
    payload = _payload(30, tools=False)
    ledger = _ledger(_row(payload, 5), _row(payload, 10, process="another process"))
    _, record = _place(payload, ledger, intent="side")

    assert sorted(_ttls(record)) == [1]
    assert "target" not in record


def test_a_request_built_outside_a_call_marks_the_turn_and_the_stable_end():
    record = planner.place(_payload(30, tools=False), None)

    assert _ttls(record) == {1: "1h", 30: "5m"}


def test_an_expired_entry_is_not_a_candidate():
    payload = _payload(100, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 40, age=301.0)))

    assert sorted(_ttls(record)) == [1, 100]
    assert record["g1"] == {"excluded": "no_established_candidate"}


# --- the order of lifetimes ---


def test_an_early_five_minute_caller_mark_keeps_the_one_hour_stable_mark_off():
    payload = _payload(20)
    _block(payload, 1)["cache_control"] = FIVE
    _, record = _place(payload, _ledger())

    assert _ttls(record) == {1: "5m", 20: "5m"}
    assert "ttl_order:stable" in record["degraded"]


def test_a_caller_mark_on_the_stable_end_becomes_the_one_hour_mark():
    payload = _payload(20)
    _block(payload, 2)["cache_control"] = FIVE
    handed_in = payload["system"]
    _, record = _place(payload, _ledger())

    assert _ttls(record) == {2: "1h", 20: "5m"}
    assert set(_roles(record)[2]) == {"caller", "stable"}
    assert payload["system"][0]["cache_control"] == HOUR
    assert handed_in[0]["cache_control"] == FIVE


def test_a_one_hour_entry_is_read_at_one_hour_ahead_of_a_one_hour_caller_mark():
    payload = _payload(130, tools=False)
    _block(payload, 100)["cache_control"] = HOUR
    _, record = _place(payload, _ledger(_row(payload, 40, ttl="1h")))

    assert _ttls(record) == {1: "1h", 40: "1h", 100: "1h", 130: "5m"}
    assert record["g1"] == "met"


def test_a_five_minute_entry_ahead_of_a_one_hour_caller_mark_is_not_read():
    payload = _payload(130, tools=False)
    _block(payload, 100)["cache_control"] = HOUR
    _, record = _place(payload, _ledger(_row(payload, 40, ttl="5m")))

    assert 40 not in _ttls(record)
    assert "ttl_order:read" in record["degraded"]
    assert record["g1"] == {"excluded": "ttl_order"}


def test_a_one_hour_entry_after_a_five_minute_caller_mark_is_not_read():
    payload = _payload(130, tools=False)
    _block(payload, 10)["cache_control"] = FIVE
    _, record = _place(payload, _ledger(_row(payload, 40, ttl="1h")))

    assert _ttls(record) == {1: "1h", 10: "5m", 130: "5m"}
    assert record["g1"] == {"excluded": "ttl_order"}


# --- routes ---


def test_an_entry_held_on_every_configured_account_is_read(routes):
    routes("A", "B")
    payload = _payload(100, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 40, on=("A", "B"))))

    assert 40 in _ttls(record)
    assert record["g1"] == "met"


def test_a_deeper_entry_held_on_one_account_is_probed_beside_the_guaranteed_read(routes):
    routes("A", "B")
    payload = _payload(100, tools=False)
    ledger = _ledger(_row(payload, 40, on=("A", "B")), _row(payload, 60, on=("A",)))
    _, record = _place(payload, ledger)

    assert sorted(_ttls(record)) == [1, 40, 60, 100]
    assert _roles(record)[60] == ["probe"]
    assert record["g1"] == "met"


def test_an_entry_held_on_one_of_two_accounts_is_probed_without_the_guarantee(routes):
    routes("A", "B")
    payload = _payload(100, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 40, on=("B",))))

    assert _roles(record)[40] == ["probe"]
    assert record["g1"] == {"excluded": "route_unselected"}


def test_with_one_configured_account_the_entry_held_there_is_read(routes):
    routes("B")
    payload = _payload(100, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 40, on=("B",))))

    assert _roles(record)[40] == ["read"]
    assert record["g1"] == "met"


# --- the guarantee ---


def test_four_caller_marks_out_of_reach_exclude_the_guarantee():
    payload = _payload(100, tools=False)
    for position in (60, 70, 80, 90):
        _block(payload, position)["cache_control"] = FIVE
    _, record = _place(payload, _ledger(_row(payload, 20)))

    assert sorted(_ttls(record)) == [60, 70, 80, 90]
    assert record["g1"] == {"excluded": "caller_marks"}


def test_an_entry_of_unknown_length_is_read_without_the_guarantee():
    payload = _payload(100, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 40, length=None)))

    assert 40 in _ttls(record)
    assert record["g1"] == {"excluded": "length_unknown"}


def test_a_cold_start_has_no_candidate_to_guarantee():
    _, record = _place(_payload(100, tools=False), _ledger())

    assert record["g1"] == {"excluded": "no_established_candidate"}


def test_after_an_eviction_the_guarantee_waits_until_the_evicted_rows_would_have_expired():
    payload = _payload(100, tools=False)
    ledger = _ledger(_row(payload, 40))
    ledger["evicted_until"] = time.time() + 60
    _, held = _place(payload, ledger)
    ledger["evicted_until"] = time.time() - 1
    _, after = _place(_payload(100, tools=False), ledger)

    assert held["g1"] == {"excluded": "ledger_evicted"}
    assert after["g1"] == "met"


def test_an_entry_no_mark_reaches_is_a_defect(monkeypatch):
    # An allocation bug that never counts a mark on the entry itself as reaching it.
    reaches = planner._reaches
    monkeypatch.setattr(
        planner, "_reaches", lambda mark, entry: mark.index != entry.index and reaches(mark, entry)
    )
    payload = _payload(100, tools=False)
    _, record = _place(payload, _ledger(_row(payload, 40)))

    assert record["g1"] == "defect"


# --- settling a completed request ---


def test_a_read_of_the_entry_length_identifies_the_hit_and_the_deepest_mark_gets_its_length():
    payload = _payload(100, tools=False)
    cert, row = _row(payload, 40, length=4000)
    ledger = _ledger((cert, row))
    call, _ = _place(payload, ledger)
    _attempt(call)
    record = settle(call, _completed(read=4000, creation=2500))

    assert record["read"] == "at_or_above"
    assert record["hit"] == {"identified": cert}
    entries = ledger["entries"]
    assert entries[cert]["hit_at"] == call.attempts[-1][1]
    assert entries[_point(payload, 100).cert]["length"] == 6500
    assert entries[_point(payload, 1).cert]["length"] is None
    assert entries[_point(payload, 1).cert]["ttl"] == "1h"


def test_a_short_read_while_the_entry_is_live_is_below_live():
    payload = _payload(100, tools=False)
    call, _ = _place(payload, _ledger(_row(payload, 40, length=4000)))
    _attempt(call)

    assert settle(call, _completed(read=1000, creation=5500))["read"] == "below_live"


def test_a_short_read_by_an_attempt_that_started_after_the_entry_expired_is_below_expired():
    payload = _payload(100, tools=False)
    call, record = _place(payload, _ledger(_row(payload, 40, length=4000)))
    _attempt(call, started=record["target"]["expires_at"] + 1)

    assert settle(call, _completed(read=0, creation=6500))["read"] == "below_expired"


@pytest.mark.parametrize(
    "api_call",
    [
        SimpleNamespace(status=EventStatus.FAILED, response=None),
        SimpleNamespace(status=EventStatus.COMPLETED, response={"usage": {"output_tokens": 3}}),
        _completed(read=1000, creation=10, status=EventStatus.FAILED),
    ],
    ids=["failed", "no input count", "failed with usage"],
)
def test_an_attempt_without_complete_usage_is_unevaluated_and_writes_nothing(api_call):
    payload = _payload(100, tools=False)
    ledger = _ledger(_row(payload, 40))
    before = copy.deepcopy(ledger)
    call, _ = _place(payload, ledger)
    _attempt(call)
    record = settle(call, api_call)

    assert record["read"] == "unevaluated"
    assert ledger == before


def test_a_request_with_no_attempt_or_no_placement_is_unevaluated_and_writes_nothing():
    payload = _payload(100, tools=False)
    ledger = _ledger(_row(payload, 40))
    before = copy.deepcopy(ledger)
    unsent, _ = _place(payload, ledger)
    unplaced = PromptCacheCall(ledger=ledger, intent="continuing")
    _attempt(unplaced)

    assert settle(unsent, _completed(read=1000, creation=10))["read"] == "unevaluated"
    assert settle(unplaced, _completed(read=1000, creation=10))["read"] == "unevaluated"
    assert ledger == before


def test_a_completed_request_drops_the_rows_that_have_expired():
    payload = _payload(100, tools=False)
    expired = _row(payload, 40, age=400.0)
    ledger = _ledger(expired)
    call, _ = _place(payload, ledger)
    _attempt(call)
    settle(call, _completed(creation=900))

    assert expired[0] not in ledger["entries"]


def test_an_excluded_attempt_records_its_read_size_without_a_verdict():
    payload = _payload(100, tools=False)
    call, _ = _place(payload, _ledger())
    _attempt(call)

    assert settle(call, _completed(read=500, creation=100))["read"] == {"size": 500}


def test_an_equal_read_refreshes_only_the_entry_a_mark_reaches():
    payload = _payload(100, tools=False)
    outside = _row(payload, 10, length=500)
    inside = _row(payload, 40, length=500)
    ledger = _ledger(outside, inside)
    call, _ = _place(payload, ledger)
    _attempt(call)
    record = settle(call, _completed(read=500, creation=100))

    assert record["hit"] == {"identified": inside[0]}
    assert ledger["entries"][outside[0]]["hit_at"] is None


def test_two_reached_entries_of_the_read_length_leave_the_hit_unidentified():
    payload = _payload(100, tools=False)
    shallow = _row(payload, 85, length=500)
    deep = _row(payload, 90, length=500)
    ledger = _ledger(shallow, deep)
    call, _ = _place(payload, ledger)
    _attempt(call)
    record = settle(call, _completed(read=500, creation=100))

    assert record["hit"] == "unidentified"
    assert ledger["entries"][shallow[0]]["hit_at"] is None
    assert ledger["entries"][deep[0]]["hit_at"] is None


def test_an_entry_held_on_another_account_is_never_identified_as_this_attempts_hit(routes):
    routes("A", "B")
    payload = _payload(50, tools=False)
    cert, row = _row(payload, 40, length=500, on=("A",))
    ledger = _ledger((cert, row))
    call, _ = _place(payload, ledger)
    _attempt(call, "B")
    record = settle(call, _completed(read=500, creation=100))

    assert record["hit"] == "unidentified"
    assert ledger["entries"][cert]["hit_at"] is None


def test_a_deeper_reached_entry_of_unknown_length_leaves_the_hit_unidentified():
    payload = _payload(50, tools=False)
    known = _row(payload, 40, length=500)
    unknown = _row(payload, 45, length=None)
    ledger = _ledger(known, unknown)
    call, _ = _place(payload, ledger)
    _attempt(call)
    record = settle(call, _completed(read=500, creation=100))

    assert record["hit"] == "unidentified"
    assert ledger["entries"][known[0]]["hit_at"] is None


def test_an_equal_read_outside_the_reached_entries_is_never_attributed():
    payload = _payload(50, tools=False)
    outside = _row(payload, 10, length=1000)
    reached = _row(payload, 40, length=None)
    ledger = _ledger(outside, reached)
    call, placed = _place(payload, ledger)
    _attempt(call)
    record = settle(call, _completed(read=1000, creation=200))

    assert 40 not in _ttls(placed)  # the tail at 50 reaches it, so it takes no mark of its own
    assert record["hit"] == "unidentified"
    assert ledger["entries"][outside[0]]["hit_at"] is None
    assert ledger["entries"][reached[0]]["length"] is None


def test_an_identified_hit_keeps_the_entry_live_from_the_attempts_start():
    payload = _payload(50, tools=False)
    cert, row = _row(payload, 40, length=800, age=290.0)
    ledger = _ledger((cert, row))
    call, _ = _place(payload, ledger)
    _attempt(call)
    settle(call, _completed(read=800, creation=100))

    assert ledger_mod.expires_at(ledger["entries"][cert]) == call.attempts[-1][1] + 300.0


def test_an_attempt_with_a_server_tool_infers_no_length_and_no_hit():
    payload = _payload(100)
    payload["tools"][0] = {"type": "web_search_20260209", "name": "web_search"}
    cert, row = _row(payload, 40, length=4000)
    ledger = _ledger((cert, row))
    call, _ = _place(payload, ledger)
    _attempt(call)
    record = settle(call, _completed(read=4000, creation=2500))

    assert record["hit"] == "unidentified"
    assert ledger["entries"][cert]["hit_at"] is None
    assert ledger["entries"][_point(payload, 100).cert]["length"] is None


def test_a_retry_on_another_account_records_its_evidence_under_that_account_only(routes):
    routes("A", "B")
    payload = _payload(20, tools=False)
    ledger = _ledger()
    call, _ = _place(payload, ledger)
    _attempt(call, "A")
    _attempt(call, "B")
    record = settle(call, _completed(creation=900))

    assert record["route"] == "B"
    assert ledger["entries"]
    assert all(row["routes"] == {"B": ledger_mod._PROCESS} for row in ledger["entries"].values())


def test_past_the_ceiling_the_shortest_five_minute_rows_leave_and_the_watermark_is_set():
    now = time.time()
    entries = {
        f"row-{n}": {
            "ttl": "5m",
            "tier": "messages",
            "length": n,
            "marked_at": now,
            "hit_at": None,
            "routes": {},
        }
        for n in range(ledger_mod.CEILING)
    }
    entries["hour"] = {**entries["row-0"], "ttl": "1h", "length": 0}
    ledger = {"version": 1, "entries": entries, "evicted_until": None}
    call, _ = _place(_payload(20, tools=False), ledger)
    _attempt(call)
    record = settle(call, _completed(creation=100_000))

    # 512 rows, the one-hour row, and the request's two new rows: three leave.
    assert len(ledger["entries"]) == ledger_mod.CEILING
    assert {"row-0", "row-1", "row-2"}.isdisjoint(ledger["entries"])
    assert "hour" in ledger["entries"]
    assert ledger["evicted_until"] >= now + 300.0
    assert "ledger_evicted" in record["degraded"]


# --- clone and resume ---


def test_a_cloned_ledger_keeps_its_rows_and_none_of_their_evidence():
    payload = _payload(100, tools=False)
    ledger = _ledger(_row(payload, 40))
    clone = clone_ledger(ledger)
    _, record = _place(payload, clone)

    assert clone["entries"].keys() == ledger["entries"].keys()
    assert all(row["routes"] == {} for row in clone["entries"].values())
    assert _roles(record)[40] == ["probe"]
    assert record["g1"] == {"excluded": "no_established_candidate"}
    assert ledger["entries"][_point(payload, 40).cert]["routes"] == {"direct": ledger_mod._PROCESS}


def test_a_cloned_branch_carries_the_rows_without_their_evidence():
    branch = Branch()
    payload = _payload(100, tools=False)
    cert, row = _row(payload, 40)
    ledger_for(branch.metadata)["entries"][cert] = row
    clone = branch.clone()

    assert clone.metadata["prompt_cache"]["entries"][cert]["routes"] == {}
    assert branch.metadata["prompt_cache"]["entries"][cert]["routes"] == {
        "direct": ledger_mod._PROCESS
    }


# --- end to end through a branch ---


@pytest.mark.asyncio
async def test_a_branchs_second_request_reads_the_entry_its_first_request_wrote():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model=MODEL, max_tokens=100, prompt_caching=True)
    branch = Branch(system=SYSTEM, chat_model=model)
    usages = iter(
        [
            {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 1200},
            {"cache_read_input_tokens": 1200, "cache_creation_input_tokens": 300},
        ]
    )
    records = []
    real_settle = ledger_mod.settle

    def keep(call, api_call):
        records.append(real_settle(call, api_call))
        return records[-1]

    async def respond(*_args, **_kwargs):
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [{"type": "text", "text": "an answer"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1, **next(usages)},
        }

    with (
        patch.object(model.endpoint, "call", side_effect=respond),
        patch.object(ledger_mod, "settle", side_effect=keep),
    ):
        await branch.communicate("first question")
        await branch.communicate("second question")

    first, second = records
    assert first["g1"] == {"excluded": "no_established_candidate"}
    assert second["g1"] == "met"
    assert second["read"] == "at_or_above"
    first_turn = next(mark for mark in first["marks"] if mark["roles"] == ["tail"])
    assert second["hit"] == {"identified": first_turn["cert"]}
    assert branch.metadata["prompt_cache"]["entries"][first_turn["cert"]]["length"] == 1200


@pytest.mark.asyncio
async def test_a_branch_on_a_model_without_prompt_caching_keeps_no_rows_and_settles_nothing():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model=MODEL, max_tokens=100)
    branch = Branch(system=SYSTEM, chat_model=model)

    async def respond(*_args, **_kwargs):
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [{"type": "text", "text": "an answer"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }

    with (
        patch.object(model.endpoint, "call", side_effect=respond),
        patch.object(ledger_mod, "settle") as settled,
    ):
        await branch.communicate("first question")

    assert model.endpoint.places_prompt_cache is False
    assert "prompt_cache" not in branch.metadata
    settled.assert_not_called()
