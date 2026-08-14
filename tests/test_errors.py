# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for LionAGI error classes."""

import importlib

import pytest

from lionagi._errors import (
    ConfigurationError,
    EmptyOutgoingContentError,
    ExecutionError,
    ExistsError,
    ItemExistsError,
    ItemNotFoundError,
    LionError,
    NotFoundError,
    ObservationError,
    OperationError,
    RateLimitError,
    RelationError,
    ResourceError,
    ValidationError,
)

_EXPECTED_ALL = (
    "LionError",
    "ValidationError",
    "NotFoundError",
    "ExistsError",
    "ObservationError",
    "ResourceError",
    "RateLimitError",
    "RelationError",
    "OperationError",
    "ExecutionError",
    "ConfigurationError",
    "TimeoutError",
    "EmptyOutgoingContentError",
    "ItemNotFoundError",
    "ItemExistsError",
)


class TestLionError:
    def test_default_initialization(self):
        error = LionError()
        assert str(error) == "LionAGI error"
        assert error.message == "LionAGI error"
        assert error.details == {}
        assert error.status_code == 500

    def test_custom_message(self):
        error = LionError("Custom error message")
        assert str(error) == "Custom error message"
        assert error.message == "Custom error message"

    def test_with_details(self):
        details = {"key": "value", "count": 42}
        error = LionError("Error", details=details)
        assert error.details == details

    def test_with_status_code(self):
        error = LionError("Error", status_code=404)
        assert error.status_code == 404

    def test_with_cause(self):
        cause = ValueError("Original error")
        error = LionError("Wrapped error", cause=cause)
        assert error.get_cause() is cause
        assert error.__cause__ is cause

    def test_to_dict_basic(self):
        error = LionError("Test error", status_code=400)
        result = error.to_dict()
        assert result == {
            "error": "LionError",
            "message": "Test error",
            "status_code": 400,
        }

    def test_to_dict_with_details(self):
        error = LionError("Error", details={"field": "value"})
        result = error.to_dict()
        assert result["details"] == {"field": "value"}

    def test_to_dict_with_cause(self):
        cause = ValueError("Root cause")
        error = LionError("Error", cause=cause)
        result = error.to_dict(include_cause=True)
        assert "cause" in result
        assert "ValueError" in result["cause"]

    def test_to_dict_without_cause(self):
        cause = ValueError("Root cause")
        error = LionError("Error", cause=cause)
        result = error.to_dict(include_cause=False)
        assert "cause" not in result

    def test_from_value_basic(self):
        error = LionError.from_value(42)
        assert error.details["value"] == 42
        assert error.details["type"] == "int"

    def test_from_value_with_expected(self):
        error = LionError.from_value(42, expected="str")
        assert error.details["expected"] == "str"
        assert error.details["value"] == 42

    def test_from_value_with_message(self):
        error = LionError.from_value(42, message="Invalid value")
        assert error.message == "Invalid value"

    def test_from_value_with_cause(self):
        cause = TypeError("Type mismatch")
        error = LionError.from_value(42, cause=cause)
        assert error.get_cause() is cause

    def test_from_value_with_extra_details(self):
        error = LionError.from_value(42, field="age", min_value=0)
        assert error.details["field"] == "age"
        assert error.details["min_value"] == 0

    def test_get_cause_no_cause(self):
        error = LionError("Error")
        assert error.get_cause() is None


class TestValidationError:
    def test_default_message(self):
        error = ValidationError()
        assert error.message == "Validation failed"
        assert error.status_code == 422

    def test_custom_message(self):
        error = ValidationError("Invalid input")
        assert error.message == "Invalid input"

    def test_inheritance(self):
        error = ValidationError()
        assert isinstance(error, LionError)
        assert isinstance(error, ValueError)
        assert isinstance(error, Exception)


class TestNotFoundError:
    def test_default_message(self):
        error = NotFoundError()
        assert error.message == "Item not found"
        assert error.status_code == 404

    def test_with_details(self):
        error = NotFoundError("User not found", details={"user_id": "123"})
        assert error.details["user_id"] == "123"

    def test_inheritance(self):
        error = NotFoundError()
        assert isinstance(error, LionError)


class TestExistsError:
    def test_default_message(self):
        error = ExistsError()
        assert error.message == "Item already exists"
        assert error.status_code == 409

    def test_inheritance(self):
        error = ExistsError()
        assert isinstance(error, LionError)


class TestObservationError:
    def test_default_message(self):
        error = ObservationError()
        assert error.message == "Observation failed"
        assert error.status_code == 500

    def test_inheritance(self):
        error = ObservationError()
        assert isinstance(error, LionError)


class TestResourceError:
    def test_default_message(self):
        error = ResourceError()
        assert error.message == "Resource error"
        assert error.status_code == 429

    def test_inheritance(self):
        error = ResourceError()
        assert isinstance(error, LionError)


class TestRateLimitError:
    def test_initialization(self):
        error = RateLimitError(retry_after=60.0)
        assert error.retry_after == 60.0
        assert error.message == "Rate limit exceeded"
        assert error.status_code == 429

    def test_with_message(self):
        error = RateLimitError(retry_after=30.0, message="Too many requests")
        assert error.message == "Too many requests"
        assert error.retry_after == 30.0

    def test_retry_after_value(self):
        error = RateLimitError(retry_after=60.0)
        assert error.retry_after == 60.0
        # Retry after can be accessed but is set via __setattr__
        error2 = RateLimitError(retry_after=120.5)
        assert error2.retry_after == 120.5

    def test_inheritance(self):
        error = RateLimitError(retry_after=60.0)
        assert isinstance(error, LionError)


class TestRelationError:
    def test_initialization(self):
        error = RelationError("Relation failed")
        assert error.message == "Relation failed"

    def test_default_message(self):
        error = RelationError()
        assert error.message == "LionAGI error"

    def test_inheritance(self):
        error = RelationError()
        assert isinstance(error, LionError)


class TestOperationError:
    def test_initialization(self):
        error = OperationError("Operation failed")
        assert error.message == "Operation failed"

    def test_default_message(self):
        error = OperationError()
        assert error.message == "LionAGI error"

    def test_inheritance(self):
        error = OperationError()
        assert isinstance(error, LionError)
        assert isinstance(error, ValueError)


class TestExecutionError:
    def test_initialization(self):
        error = ExecutionError("Execution failed")
        assert error.message == "Execution failed"

    def test_default_message(self):
        error = ExecutionError()
        assert error.message == "LionAGI error"

    def test_inheritance(self):
        error = ExecutionError()
        assert isinstance(error, LionError)
        assert isinstance(error, RuntimeError)


class TestConfigurationError:
    def test_default_message(self):
        error = ConfigurationError()
        assert error.message == "Invalid configuration"
        assert error.status_code == 500

    def test_inheritance(self):
        error = ConfigurationError("bad config")
        assert isinstance(error, LionError)
        assert isinstance(error, ValueError)


class TestPublicSurface:
    def test_all_matches_expected(self):
        mod = importlib.import_module("lionagi._errors")
        declared = set(mod.__all__)
        expected = set(_EXPECTED_ALL)
        missing = expected - declared
        extra = declared - expected
        assert not missing, f"Names missing from __all__: {sorted(missing)}"
        assert not extra, f"Undocumented names in __all__: {sorted(extra)}"

    def test_all_entries_importable(self):
        mod = importlib.import_module("lionagi._errors")
        for name in mod.__all__:
            assert hasattr(mod, name), f"{name!r} declared in __all__ but not defined"

    def test_empty_outgoing_content_error_in_all(self):
        mod = importlib.import_module("lionagi._errors")
        assert "EmptyOutgoingContentError" in mod.__all__

    def test_empty_outgoing_content_error_inheritance(self):
        error = EmptyOutgoingContentError()
        assert isinstance(error, LionError)
        assert isinstance(error, ValueError)


class TestAliases:
    def test_item_not_found_alias(self):
        assert ItemNotFoundError is NotFoundError

    def test_item_exists_alias(self):
        assert ItemExistsError is ExistsError


class TestErrorChaining:
    def test_chain_multiple_errors(self):
        original = ValueError("Original")
        wrapped = ValidationError("Validation", cause=original)
        final = OperationError("Operation", cause=wrapped)

        assert final.get_cause() is wrapped
        assert wrapped.get_cause() is original

    def test_traceback_preservation(self):
        """Test that cause preserves traceback."""
        try:
            raise ValueError("Original error")
        except ValueError as e:
            error = LionError("Wrapped", cause=e)
            assert error.__cause__ is e


class TestErrorSlots:
    def test_lion_error_has_slots(self):
        assert hasattr(LionError, "__slots__")
        assert "message" in LionError.__slots__
        assert "details" in LionError.__slots__
        assert "status_code" in LionError.__slots__

    def test_subclass_slots(self):
        assert hasattr(ValidationError, "__slots__")
        assert ValidationError.__slots__ == ()
        assert hasattr(RateLimitError, "__slots__")
        assert "retry_after" in RateLimitError.__slots__


@pytest.mark.parametrize(
    "error_class,expected_status",
    [
        (LionError, 500),
        (ValidationError, 422),
        (NotFoundError, 404),
        (ExistsError, 409),
        (ObservationError, 500),
        (ResourceError, 429),
        (RateLimitError, 429),
        (RelationError, 500),
        (OperationError, 500),
        (ExecutionError, 500),
    ],
)
def test_error_status_codes(error_class, expected_status):
    if error_class == RateLimitError:
        error = error_class(retry_after=60.0)
    else:
        error = error_class()
    assert error.status_code == expected_status


@pytest.mark.parametrize(
    "error_class",
    [
        LionError,
        ValidationError,
        NotFoundError,
        ExistsError,
        ObservationError,
        ResourceError,
        RateLimitError,
        RelationError,
        OperationError,
        ExecutionError,
    ],
)
def test_all_errors_are_exceptions(error_class):
    if error_class == RateLimitError:
        error = error_class(retry_after=60.0)
    else:
        error = error_class()
    assert isinstance(error, Exception)
    assert isinstance(error, LionError)
