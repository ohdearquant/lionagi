"""Tests for the operative module."""

import pytest
from pydantic import BaseModel

from lionagi.ln.types import Operable, Spec
from lionagi.operations.operate.operative import Operative


# Define test model outside test class to avoid pytest collection warning
class SampleModel(BaseModel):
    """Test model for operative testing."""

    name: str
    value: int


class TestOperative:
    def test_initialization(self):
        """Test basic initialization of Operative."""
        operative = Operative(base_type=SampleModel)
        assert operative.name == "SampleModel"
        assert operative.request_type is not None
        assert issubclass(operative.request_type, BaseModel)

    def test_custom_name(self):
        """Test Operative with custom name."""
        operative = Operative(name="CustomName", base_type=SampleModel)
        assert operative.name == "CustomName"

    def test_response_type_creation(self):
        """Test creation of response type."""
        # Create operative with additional field in operable
        status_spec = Spec(str, name="status", default="success")
        operable = Operable((status_spec,), name="SampleWithStatus")

        operative = Operative(
            base_type=SampleModel,
            operable=operable,
        )

        # Access response_type property which auto-creates
        assert operative.response_type is not None
        assert "status" in operative.response_type.model_fields
        assert "name" in operative.response_type.model_fields
        assert "value" in operative.response_type.model_fields

    def test_response_model_update_with_text(self):
        """Test updating response model with text input."""
        operative = Operative(base_type=SampleModel)

        # Valid JSON text
        text = '{"name": "test", "value": 42}'
        result = operative.update_response_model(text=text)
        assert isinstance(result, BaseModel)
        assert result.name == "test"
        assert result.value == 42

        # Invalid JSON text
        operative = Operative(base_type=SampleModel)  # Fresh instance
        text = "invalid json"
        result = operative.update_response_model(text=text)
        # Should return raw text for invalid JSON
        assert isinstance(result, str | dict | list | type(None))

    def test_response_model_update_with_data(self):
        """Test updating response model with dict data."""
        operative = Operative(base_type=SampleModel)

        # First set initial model with valid JSON
        response_model = operative.update_response_model(text='{"name": "test", "value": 42}')

        # Update with new data
        data = {"name": "updated"}
        result = operative.update_response_model(data=data)
        assert isinstance(result, BaseModel)
        assert result.name == "updated"
        assert result.value == 42  # Original value should be preserved

    def test_validation_methods(self):
        """Test strict validation."""
        operative = Operative(base_type=SampleModel, strict=False)

        # Test validation with valid text
        valid_text = '{"name": "test", "value": 42}'
        result = operative.validate_response(valid_text, strict=True)
        assert result is not None
        assert result.name == "test"
        assert result.value == 42

    def test_retry_behavior(self):
        """Test auto retry behavior for validation."""
        operative = Operative(base_type=SampleModel, auto_retry_parse=True, max_retries=3)

        # Valid input should clear retry flag
        valid_text = '{"name": "test", "value": 42}'
        operative.validate_response(valid_text, strict=False)
        assert operative._should_retry is False

    def test_error_cases(self):
        """Test error handling cases."""
        operative = Operative(base_type=SampleModel)

        # Test with no input
        with pytest.raises(ValueError):
            operative.update_response_model()

    def test_exclude_fields(self):
        """Test excluding fields in request type."""
        # Create operable with fields, but exclude some from request
        spec1 = Spec(str, name="field1", default="test")
        spec2 = Spec(int, name="field2", default=42)
        operable = Operable((spec1, spec2), name="TestModel")

        operative = Operative(
            operable=operable,
            request_exclude={"field2"},  # Exclude field2 from request
        )

        # Request should not have field2
        assert "field2" not in operative.request_type.model_fields
        assert "field1" in operative.request_type.model_fields

        # Response should have both
        assert "field2" in operative.response_type.model_fields
        assert "field1" in operative.response_type.model_fields

    def test_operable_integration(self):
        """Test integration with Operable."""
        spec1 = Spec(str, name="username", nullable=False)
        spec2 = Spec(int, name="age", default=0)
        operable = Operable((spec1, spec2), name="UserModel")

        operative = Operative(operable=operable)

        # Check model creation
        assert operative.request_type is not None
        assert "username" in operative.request_type.model_fields
        assert "age" in operative.request_type.model_fields

    def test_operative_validate_response_strict_false_records_retry_state(self, monkeypatch):
        """When the adapter raises during validation with strict=False,
        response_str_dict is set to the input text and _should_retry is False."""
        from unittest.mock import patch

        operative = Operative(base_type=SampleModel, auto_retry_parse=True)
        operative.create_response_model()

        # Make the adapter always raise so we exercise the except branch
        original_get_adapter = operative._get_adapter

        class _AlwaysFailAdapter:
            @staticmethod
            def validate_response(text, cls, strict, fuzzy_parse):
                raise ValueError("forced adapter failure")

        with patch.object(operative, "_get_adapter", return_value=_AlwaysFailAdapter):
            result = operative.validate_response("not-json")

        assert result is None
        assert operative.response_str_dict == "not-json"
        assert operative._should_retry is False

    def test_operative_update_response_model_requires_text_or_data(self):
        operative = Operative(base_type=SampleModel)
        with pytest.raises(ValueError, match="Either text or data must be provided"):
            operative.update_response_model()
