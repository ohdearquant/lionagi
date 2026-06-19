# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for LionAGI error classes."""

import pytest

from lionagi._errors import (
    ConfigurationError,
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


class TestLionError:
    """Tests for base LionError class."""

    def test_default_initialization(self):
        """Test error with default values."""
        error = LionError()
        assert str(error) == "LionAGI error"
        assert error.message == "LionAGI error"
        assert error.details == {}
        assert error.status_code == 500

    def test_custom_message(self):
        """Test error with custom message."""
        error = LionError("Custom error message")
        assert str(error) == "Custom error message"
        assert error.message == "Custom error message"

    def test_with_details(self):
        """Test error with details dictionary."""
        details = {"key": "value", "count": 42}
        error = LionError("Error", details=details)
        assert error.details == details

    def test_with_status_code(self):
        """Test error with custom status code."""
        error = LionError("Error", status_code=404)
        assert error.status_code == 404

    def test_with_cause(self):
        """Test error with underlying cause."""
        cause = ValueError("Original error")
        error = LionError("Wrapped error", cause=cause)
        assert error.get_cause() is cause
        assert error.__cause__ is cause

    def test_to_dict_basic(self):
        """Test serialization to dictionary."""
        error = LionError("Test error", status_code=400)
        result = error.to_dict()
        assert result == {
            "error": "LionError",
            "message": "Test error",
            "status_code": 400,
        }

    def test_to_dict_with_details(self):
        """Test serialization with details."""
        error = LionError("Error", details={"field": "value"})
        result = error.to_dict()
        assert result["details"] == {"field": "value"}

    def test_to_dict_with_cause(self):
        """Test serialization including cause."""
        cause = ValueError("Root cause")
        error = LionError("Error", cause=cause)
        result = error.to_dict(include_cause=True)
        assert "cause" in result
        assert "ValueError" in result["cause"]

    def test_to_dict_without_cause(self):
        """Test serialization excluding cause by default."""
        cause = ValueError("Root cause")
        error = LionError("Error", cause=cause)
        result = error.to_dict(include_cause=False)
        assert "cause" not in result

    def test_from_value_basic(self):
        """Test creating error from value."""
        error = LionError.from_value(42)
        assert error.details["value"] == 42
        assert error.details["type"] == "int"

    def test_from_value_with_expected(self):
        """Test creating error with expected type."""
        error = LionError.from_value(42, expected="str")
        assert error.details["expected"] == "str"
        assert error.details["value"] == 42

    def test_from_value_with_message(self):
        """Test creating error with custom message."""
        error = LionError.from_value(42, message="Invalid value")
        assert error.message == "Invalid value"

    def test_from_value_with_cause(self):
        """Test creating error with cause."""
        cause = TypeError("Type mismatch")
        error = LionError.from_value(42, cause=cause)
        assert error.get_cause() is cause

    def test_from_value_with_extra_details(self):
        """Test creating error with extra details."""
        error = LionError.from_value(42, field="age", min_value=0)
        assert error.details["field"] == "age"
        assert error.details["min_value"] == 0

    def test_get_cause_no_cause(self):
        """Test get_cause when no cause exists."""
        error = LionError("Error")
        assert error.get_cause() is None


class TestValidationError:
    """Tests for ValidationError class."""

    def test_default_message(self):
        """Test ValidationError default message."""
        error = ValidationError()
        assert error.message == "Validation failed"
        assert error.status_code == 422

    def test_custom_message(self):
        """Test ValidationError with custom message."""
        error = ValidationError("Invalid input")
        assert error.message == "Invalid input"

    def test_inheritance(self):
        """Test ValidationError inherits from LionError and ValueError."""
        error = ValidationError()
        assert isinstance(error, LionError)
        assert isinstance(error, ValueError)
        assert isinstance(error, Exception)


class TestNotFoundError:
    """Tests for NotFoundError class."""

    def test_default_message(self):
        """Test NotFoundError default message."""
        error = NotFoundError()
        assert error.message == "Item not found"
        assert error.status_code == 404

    def test_with_details(self):
        """Test NotFoundError with item details."""
        error = NotFoundError("User not found", details={"user_id": "123"})
        assert error.details["user_id"] == "123"

    def test_inheritance(self):
        """Test NotFoundError inherits from LionError."""
        error = NotFoundError()
        assert isinstance(error, LionError)


class TestExistsError:
    """Tests for ExistsError class."""

    def test_default_message(self):
        """Test ExistsError default message."""
        error = ExistsError()
        assert error.message == "Item already exists"
        assert error.status_code == 409

    def test_inheritance(self):
        """Test ExistsError inherits from LionError."""
        error = ExistsError()
        assert isinstance(error, LionError)


class TestObservationError:
    """Tests for ObservationError class."""

    def test_default_message(self):
        """Test ObservationError default message."""
        error = ObservationError()
        assert error.message == "Observation failed"
        assert error.status_code == 500

    def test_inheritance(self):
        """Test ObservationError inherits from LionError."""
        error = ObservationError()
        assert isinstance(error, LionError)


class TestResourceError:
    """Tests for ResourceError class."""

    def test_default_message(self):
        """Test ResourceError default message."""
        error = ResourceError()
        assert error.message == "Resource error"
        assert error.status_code == 429

    def test_inheritance(self):
        """Test ResourceError inherits from LionError."""
        error = ResourceError()
        assert isinstance(error, LionError)


class TestRateLimitError:
    """Tests for RateLimitError class."""

    def test_initialization(self):
        """Test RateLimitError requires retry_after."""
        error = RateLimitError(retry_after=60.0)
        assert error.retry_after == 60.0
        assert error.message == "Rate limit exceeded"
        assert error.status_code == 429

    def test_with_message(self):
        """Test RateLimitError with custom message."""
        error = RateLimitError(retry_after=30.0, message="Too many requests")
        assert error.message == "Too many requests"
        assert error.retry_after == 30.0

    def test_retry_after_value(self):
        """Test retry_after attribute stores correct value."""
        error = RateLimitError(retry_after=60.0)
        assert error.retry_after == 60.0
        # Retry after can be accessed but is set via __setattr__
        error2 = RateLimitError(retry_after=120.5)
        assert error2.retry_after == 120.5

    def test_inheritance(self):
        """Test RateLimitError inherits from LionError."""
        error = RateLimitError(retry_after=60.0)
        assert isinstance(error, LionError)


class TestRelationError:
    """Tests for RelationError class."""

    def test_initialization(self):
        """Test RelationError initialization."""
        error = RelationError("Relation failed")
        assert error.message == "Relation failed"

    def test_default_message(self):
        """Test RelationError uses base default message."""
        error = RelationError()
        assert error.message == "LionAGI error"

    def test_inheritance(self):
        """Test RelationError inherits from LionError."""
        error = RelationError()
        assert isinstance(error, LionError)


class TestOperationError:
    """Tests for OperationError class."""

    def test_initialization(self):
        """Test OperationError initialization."""
        error = OperationError("Operation failed")
        assert error.message == "Operation failed"

    def test_default_message(self):
        """Test OperationError uses base default message."""
        error = OperationError()
        assert error.message == "LionAGI error"

    def test_inheritance(self):
        """Test OperationError inherits from LionError and ValueError."""
        error = OperationError()
        assert isinstance(error, LionError)
        assert isinstance(error, ValueError)


class TestExecutionError:
    """Tests for ExecutionError class."""

    def test_initialization(self):
        """Test ExecutionError initialization."""
        error = ExecutionError("Execution failed")
        assert error.message == "Execution failed"

    def test_default_message(self):
        """Test ExecutionError uses base default message."""
        error = ExecutionError()
        assert error.message == "LionAGI error"

    def test_inheritance(self):
        """Test ExecutionError inherits from LionError and RuntimeError."""
        error = ExecutionError()
        assert isinstance(error, LionError)
        assert isinstance(error, RuntimeError)


class TestConfigurationError:
    """Tests for ConfigurationError class."""

    def test_default_message(self):
        error = ConfigurationError()
        assert error.message == "Invalid configuration"
        assert error.status_code == 500

    def test_inheritance(self):
        """Test ConfigurationError inherits from LionError and ValueError."""
        error = ConfigurationError("bad config")
        assert isinstance(error, LionError)
        assert isinstance(error, ValueError)


class TestAliases:
    """Tests for error class aliases."""

    def test_item_not_found_alias(self):
        """Test ItemNotFoundError is alias for NotFoundError."""
        assert ItemNotFoundError is NotFoundError

    def test_item_exists_alias(self):
        """Test ItemExistsError is alias for ExistsError."""
        assert ItemExistsError is ExistsError


class TestErrorChaining:
    """Tests for error chaining and cause preservation."""

    def test_chain_multiple_errors(self):
        """Test chaining multiple errors."""
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
    """Tests for __slots__ memory efficiency."""

    def test_lion_error_has_slots(self):
        """Test LionError defines slots for memory efficiency."""
        assert hasattr(LionError, "__slots__")
        assert "message" in LionError.__slots__
        assert "details" in LionError.__slots__
        assert "status_code" in LionError.__slots__

    def test_subclass_slots(self):
        """Test subclasses define empty slots."""
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
    """Test all error classes have correct default status codes."""
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
    """Test all error classes are proper exceptions."""
    if error_class == RateLimitError:
        error = error_class(retry_after=60.0)
    else:
        error = error_class()
    assert isinstance(error, Exception)
    assert isinstance(error, LionError)
