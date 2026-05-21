# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Event.as_fresh_event().

Previously used ``to_dict()`` which unconditionally touched excluded
keys (raising KeyError on ``metadata`` in some subclasses) and
shallow-copied excluded fields so retry clones shared mutable state
with the original.
"""

from typing import Any

from pydantic import Field

from lionagi.protocols.generic.event import Event, EventStatus


class _EventWithExcluded(Event):
    """Event subclass with an excluded field (mirrors Operation.parameters)."""

    parameters: dict[str, Any] = Field(default_factory=dict, exclude=True)


def test_as_fresh_event_returns_new_id_and_pending_execution():
    orig = _EventWithExcluded()
    orig.execution.status = EventStatus.COMPLETED
    orig.execution.response = "done"

    fresh = orig.as_fresh_event()

    assert fresh.id != orig.id
    assert fresh.execution.status == EventStatus.PENDING
    assert fresh.execution.response is None


def test_as_fresh_event_preserves_excluded_fields():
    orig = _EventWithExcluded(parameters={"input": "x", "nested": [1, 2]})

    fresh = orig.as_fresh_event()

    assert fresh.parameters == {"input": "x", "nested": [1, 2]}


def test_as_fresh_event_deepcopies_excluded_fields():
    nested = {"a": [1, 2, 3]}
    orig = _EventWithExcluded(parameters={"nested": nested})

    fresh = orig.as_fresh_event()

    fresh.parameters["nested"]["a"].append(99)
    assert orig.parameters["nested"]["a"] == [1, 2, 3]


def test_as_fresh_event_deepcopies_metadata_when_copy_meta():
    orig = _EventWithExcluded()
    orig.metadata["ctx"] = {"tag": "alpha"}

    fresh = orig.as_fresh_event(copy_meta=True)

    fresh.metadata["ctx"]["tag"] = "beta"
    assert orig.metadata["ctx"]["tag"] == "alpha"


def test_as_fresh_event_records_original_reference():
    orig = _EventWithExcluded()

    fresh = orig.as_fresh_event()

    assert fresh.metadata["original"]["id"] == str(orig.id)
    assert fresh.metadata["original"]["created_at"] == orig.created_at


def test_as_fresh_event_falls_back_for_uncopyable_values():
    # A closure captures state that copy.deepcopy cannot pickle cleanly;
    # the helper should still succeed and retain a reference.
    def _closure():
        return 1

    orig = _EventWithExcluded(parameters={"fn": _closure})

    fresh = orig.as_fresh_event()

    # The closure comes back intact (either copied or referenced).
    assert fresh.parameters["fn"]() == 1


def test_as_fresh_event_does_not_raise_on_event_without_metadata_subfield():
    # Base Event has no extra excluded fields; prove the path does not
    # require them to exist.
    orig = Event()

    fresh = orig.as_fresh_event()

    assert fresh.id != orig.id
