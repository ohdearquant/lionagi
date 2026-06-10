# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi/ln/concurrency/_compat.py compatibility helpers."""

from lionagi.ln.concurrency._compat import (
    BaseExceptionGroup,
    ExceptionGroup,
    get_exception_group_exceptions,
    is_exception_group,
)

# ---------------------------------------------------------------------------
# D9 – ExceptionGroup helpers work for both groups and plain exceptions
# ---------------------------------------------------------------------------


class TestIsExceptionGroup:
    def test_true_for_exception_group(self):
        eg = ExceptionGroup("group", [ValueError("a"), TypeError("b")])
        assert is_exception_group(eg) is True

    def test_true_for_base_exception_group(self):
        beg = BaseExceptionGroup("base", [KeyboardInterrupt()])
        assert is_exception_group(beg) is True

    def test_false_for_plain_exception(self):
        assert is_exception_group(ValueError("plain")) is False

    def test_false_for_runtime_error(self):
        assert is_exception_group(RuntimeError("oops")) is False


class TestGetExceptionGroupExceptions:
    def test_returns_contained_exceptions_for_group(self):
        inner = [ValueError("v"), TypeError("t")]
        eg = ExceptionGroup("grp", inner)
        result = get_exception_group_exceptions(eg)
        assert len(result) == 2
        assert any(isinstance(e, ValueError) for e in result)
        assert any(isinstance(e, TypeError) for e in result)

    def test_returns_single_exception_in_sequence_for_plain(self):
        exc = RuntimeError("lone")
        result = get_exception_group_exceptions(exc)
        assert len(result) == 1
        assert result[0] is exc

    def test_exception_group_exceptions_are_iterable(self):
        eg = ExceptionGroup("eg", [ValueError("x")])
        result = get_exception_group_exceptions(eg)
        # Must be iterable
        items = list(result)
        assert len(items) == 1
