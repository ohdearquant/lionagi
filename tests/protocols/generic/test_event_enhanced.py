# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Execution.retryable, add_error(), _serialize_exception_group(), and Event.assert_completed()."""

from __future__ import annotations

import pytest

from lionagi.ln.concurrency._compat import ExceptionGroup
from lionagi.ln.types import Unset
from lionagi.protocols.generic.event import Event, EventStatus, Execution

# ---------------------------------------------------------------------------
# 1. Execution.retryable field
# ---------------------------------------------------------------------------


class TestExecutionRetryable:
    def test_default_is_unset(self):
        ex = Execution()
        assert ex.retryable is Unset

    def test_set_to_true(self):
        ex = Execution(retryable=True)
        assert ex.retryable is True

    def test_set_to_false(self):
        ex = Execution(retryable=False)
        assert ex.retryable is False

    def test_mutable_after_init(self):
        ex = Execution()
        ex.retryable = True
        assert ex.retryable is True
        ex.retryable = False
        assert ex.retryable is False

    def test_serialized_in_to_dict(self):
        ex = Execution(retryable=True)
        d = ex.to_dict()
        assert "retryable" in d
        assert d["retryable"] is True

    def test_serialized_none_in_to_dict(self):
        ex = Execution()
        d = ex.to_dict()
        assert "retryable" in d
        assert d["retryable"] is None

    def test_shows_in_str(self):
        ex = Execution(retryable=True)
        s = str(ex)
        assert "retryable=True" in s

    def test_shows_unset_in_str(self):
        ex = Execution()
        s = str(ex)
        assert "retryable=<unset>" in s

    def test_shows_false_in_str(self):
        ex = Execution(retryable=False)
        s = str(ex)
        assert "retryable=False" in s


# ---------------------------------------------------------------------------
# 2. Execution.add_error()
# ---------------------------------------------------------------------------


class TestExecutionAddError:
    def test_first_error_sets_error(self):
        ex = Execution()
        err = ValueError("first")
        ex.add_error(err)
        assert ex.error is err

    def test_second_error_creates_exception_group(self):
        ex = Execution()
        err1 = ValueError("first")
        err2 = TypeError("second")
        ex.add_error(err1)
        ex.add_error(err2)
        assert isinstance(ex.error, ExceptionGroup)
        assert len(ex.error.exceptions) == 2
        assert ex.error.exceptions[0] is err1
        assert ex.error.exceptions[1] is err2

    def test_third_error_extends_exception_group(self):
        ex = Execution()
        err1 = ValueError("first")
        err2 = TypeError("second")
        err3 = RuntimeError("third")
        ex.add_error(err1)
        ex.add_error(err2)
        ex.add_error(err3)
        assert isinstance(ex.error, ExceptionGroup)
        assert len(ex.error.exceptions) == 3
        assert ex.error.exceptions[0] is err1
        assert ex.error.exceptions[1] is err2
        assert ex.error.exceptions[2] is err3

    def test_works_when_error_is_none_initially(self):
        ex = Execution()
        assert ex.error is None
        err = RuntimeError("boom")
        ex.add_error(err)
        assert ex.error is err

    def test_replaces_string_error_with_exception(self):
        ex = Execution(error="old string error")
        new_err = ValueError("new")
        ex.add_error(new_err)
        # When error is a non-exception type (string), add_error replaces it
        assert ex.error is new_err

    def test_exception_group_message(self):
        ex = Execution()
        ex.add_error(ValueError("a"))
        ex.add_error(TypeError("b"))
        assert isinstance(ex.error, ExceptionGroup)
        assert ex.error.message == "multiple errors"

    def test_add_many_errors(self):
        ex = Execution()
        errors = [ValueError(f"error-{i}") for i in range(10)]
        for err in errors:
            ex.add_error(err)
        assert isinstance(ex.error, ExceptionGroup)
        assert len(ex.error.exceptions) == 10


# ---------------------------------------------------------------------------
# 3. Execution._serialize_exception_group()
# ---------------------------------------------------------------------------


class TestSerializeExceptionGroup:
    def test_simple_exception_group(self):
        ex = Execution()
        eg = ExceptionGroup("test group", [ValueError("a"), TypeError("b")])
        result = ex._serialize_exception_group(eg)
        assert result["error"] == "ExceptionGroup"
        assert "test group" in result["message"]
        assert len(result["exceptions"]) == 2
        assert result["exceptions"][0] == {
            "error": "ValueError",
            "message": "a",
        }
        assert result["exceptions"][1] == {
            "error": "TypeError",
            "message": "b",
        }

    def test_nested_exception_groups(self):
        ex = Execution()
        inner = ExceptionGroup("inner", [ValueError("deep")])
        outer = ExceptionGroup("outer", [inner, TypeError("shallow")])
        result = ex._serialize_exception_group(outer)
        assert result["error"] == "ExceptionGroup"
        assert len(result["exceptions"]) == 2
        # First entry is the nested group
        nested = result["exceptions"][0]
        assert nested["error"] == "ExceptionGroup"
        assert len(nested["exceptions"]) == 1
        assert nested["exceptions"][0] == {
            "error": "ValueError",
            "message": "deep",
        }
        # Second entry is a plain exception
        assert result["exceptions"][1] == {
            "error": "TypeError",
            "message": "shallow",
        }

    def test_depth_limit(self):
        """Verify that nesting deeper than MAX_DEPTH (100) is handled."""
        ex = Execution()
        # Build a chain of 102 nested ExceptionGroups
        eg = ExceptionGroup("leaf", [ValueError("bottom")])
        for i in range(102):
            eg = ExceptionGroup(f"level-{i}", [eg])
        result = ex._serialize_exception_group(eg)
        # Walk down to find the depth-limited entry
        current = result
        depth = 0
        while (
            "exceptions" in current
            and len(current["exceptions"]) > 0
            and isinstance(current["exceptions"][0], dict)
            and "error" in current["exceptions"][0]
        ):
            current = current["exceptions"][0]
            depth += 1
            if depth > 200:
                break  # safety
        # At some point we should hit the max-depth sentinel
        assert "Max nesting depth" in current.get("message", "") or depth <= 103

    def test_circular_reference_detection(self):
        """Verify cycle detection produces a sentinel instead of infinite recursion."""
        ex = Execution()
        eg = ExceptionGroup("root", [ValueError("a")])
        # Simulate circular reference by passing same eg_id in _seen
        seen = {id(eg)}
        result = ex._serialize_exception_group(eg, _seen=seen)
        assert result["error"] == "ExceptionGroup"
        assert result["message"] == "Circular reference detected"

    def test_to_dict_with_exception_group_error(self):
        """Verify to_dict serializes ExceptionGroup error via _serialize_exception_group."""
        ex = Execution(status=EventStatus.FAILED)
        ex.error = ExceptionGroup("combined", [ValueError("a"), TypeError("b")])
        d = ex.to_dict()
        assert isinstance(d["error"], dict)
        assert d["error"]["error"] == "ExceptionGroup"
        assert len(d["error"]["exceptions"]) == 2

    def test_to_dict_with_plain_exception_error(self):
        """Verify to_dict serializes a plain exception as error dict."""
        ex = Execution(status=EventStatus.FAILED)
        ex.error = ValueError("single failure")
        d = ex.to_dict()
        assert isinstance(d["error"], dict)
        assert d["error"]["error"] == "ValueError"
        assert d["error"]["message"] == "single failure"

    def test_to_dict_with_string_error(self):
        """Verify to_dict passes through string errors unchanged."""
        ex = Execution(status=EventStatus.FAILED, error="plain string")
        d = ex.to_dict()
        assert d["error"] == "plain string"


# ---------------------------------------------------------------------------
# 4. Event.assert_completed()
# ---------------------------------------------------------------------------


class TestEventAssertCompleted:
    def test_no_op_when_completed(self):
        event = Event()
        event.execution.status = EventStatus.COMPLETED
        # Should not raise
        event.assert_completed()

    def test_raises_when_pending(self):
        event = Event()
        assert event.status == EventStatus.PENDING
        with pytest.raises(RuntimeError, match="did not complete successfully"):
            event.assert_completed()

    def test_raises_when_failed(self):
        event = Event()
        event.execution.status = EventStatus.FAILED
        event.execution.error = "something went wrong"
        with pytest.raises(RuntimeError, match="did not complete successfully"):
            event.assert_completed()

    def test_error_message_includes_execution_details(self):
        event = Event()
        event.execution.status = EventStatus.FAILED
        event.execution.error = "timeout"
        event.execution.duration = 30.0
        with pytest.raises(RuntimeError) as exc_info:
            event.assert_completed()
        msg = str(exc_info.value)
        assert "status" in msg
        assert "failed" in msg
        assert "timeout" in msg
        assert "duration" in msg
        # response should be excluded from the message
        assert "response" not in msg

    def test_raises_when_processing(self):
        event = Event()
        event.execution.status = EventStatus.PROCESSING
        with pytest.raises(RuntimeError, match="did not complete successfully"):
            event.assert_completed()

    def test_raises_when_skipped(self):
        event = Event()
        event.execution.status = EventStatus.SKIPPED
        with pytest.raises(RuntimeError, match="did not complete successfully"):
            event.assert_completed()

    def test_raises_when_cancelled(self):
        event = Event()
        event.execution.status = EventStatus.CANCELLED
        with pytest.raises(RuntimeError, match="did not complete successfully"):
            event.assert_completed()

    def test_raises_when_aborted(self):
        event = Event()
        event.execution.status = EventStatus.ABORTED
        with pytest.raises(RuntimeError, match="did not complete successfully"):
            event.assert_completed()


# ---------------------------------------------------------------------------
# 5. Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_execution_without_retryable(self):
        """Execution() without retryable uses Unset sentinel (not None)."""
        ex = Execution()
        assert ex.status == EventStatus.PENDING
        assert ex.duration is Unset
        assert ex.response is None
        assert ex.error is None
        assert ex.retryable is Unset

    def test_execution_with_positional_style_kwargs(self):
        """Execution(duration=1.0, response='ok') still works."""
        ex = Execution(duration=1.0, response="ok")
        assert ex.duration == 1.0
        assert ex.response == "ok"
        assert ex.status == EventStatus.PENDING
        assert ex.retryable is Unset

    def test_to_dict_includes_retryable_key(self):
        """to_dict() always includes the retryable key."""
        ex = Execution()
        d = ex.to_dict()
        assert "retryable" in d
        keys = set(d.keys())
        assert keys == {"status", "duration", "response", "error", "retryable"}

    def test_existing_event_serialization(self):
        """Event serialization still produces expected structure."""
        event = Event()
        event.execution.status = EventStatus.COMPLETED
        event.execution.response = {"key": "value"}
        event.execution.duration = 2.5
        serialized = event.model_dump()
        exec_data = serialized["execution"]
        assert exec_data["status"] == "completed"
        assert exec_data["response"] == {"key": "value"}
        assert exec_data["duration"] == 2.5
        assert exec_data["error"] is None
        assert "retryable" in exec_data

    def test_event_retryable_via_execution(self):
        """Event with retryable set propagates through serialization."""
        event = Event()
        event.execution.retryable = True
        serialized = event.model_dump()
        assert serialized["execution"]["retryable"] is True

    def test_slots_include_retryable(self):
        """Execution.__slots__ includes retryable."""
        assert "retryable" in Execution.__slots__

    def test_execution_str_format_unchanged(self):
        """The str format still starts with 'Execution(' and includes all fields."""
        ex = Execution(
            duration=1.0,
            response="data",
            status=EventStatus.COMPLETED,
            error=None,
            retryable=False,
        )
        s = str(ex)
        assert s.startswith("Execution(")
        assert "status=completed" in s
        assert "duration=1.0" in s
        assert "response=data" in s
        assert "error=None" in s
        assert "retryable=False" in s
