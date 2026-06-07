"""
Simple Coverage Tests for Step Class

Focuses on exercising code paths to improve coverage without
complex integrations that may be broken due to API changes.
"""

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from lionagi.models import FieldModel
from lionagi.operations.operate.step import Operative, Step


class TestStepBasicFunctionality:
    """Test basic Step functionality that should work."""

    def test_request_operative_basic(self):
        """Test basic request operative creation."""
        operative = Step.request_operative()

        assert isinstance(operative, Operative)
        assert operative.name is not None

    def test_request_operative_with_name(self):
        """Test request operative with custom name."""
        operative = Step.request_operative(operative_name="test_name")

        assert operative.name == "test_name"

    def test_request_operative_with_max_retries(self):
        """Test request operative with max retries."""
        operative = Step.request_operative(max_retries=5)

        assert operative.max_retries == 5

    def test_request_operative_with_auto_retry_parse_false(self):
        """Test request operative with auto_retry_parse disabled."""
        operative = Step.request_operative(auto_retry_parse=False)

        assert operative.auto_retry_parse is False

    def test_request_operative_with_auto_retry_parse_true(self):
        """Test request operative with auto_retry_parse enabled."""
        operative = Step.request_operative(auto_retry_parse=True)

        assert operative.auto_retry_parse is True

    def test_request_operative_with_reason_field(self):
        """Test request operative with reason field enabled."""
        operative = Step.request_operative(reason=True)

        assert isinstance(operative, Operative)

    def test_request_operative_with_actions_field(self):
        """Test request operative with actions field enabled."""
        operative = Step.request_operative(actions=True)

        assert isinstance(operative, Operative)

    def test_request_operative_with_reason_and_actions(self):
        """Test request operative with both reason and actions."""
        operative = Step.request_operative(reason=True, actions=True)

        assert isinstance(operative, Operative)

    def test_request_operative_with_field_models(self):
        """Test request operative with custom field models."""
        field_model = FieldModel(field="test_field", description="Test")
        operative = Step.request_operative(field_models=[field_model])

        assert isinstance(operative, Operative)

    def test_request_operative_with_exclude_fields(self):
        """Test request operative with exclude fields."""
        operative = Step.request_operative(exclude_fields=["field1", "field2"])

        assert isinstance(operative, Operative)

    def test_request_operative_with_field_descriptions(self):
        """Test request operative with field descriptions."""
        descriptions = {"field1": "Description 1"}
        operative = Step.request_operative(field_descriptions=descriptions)

        assert isinstance(operative, Operative)

    def test_request_operative_with_base_type(self):
        """Test request operative with custom base type."""

        class CustomBase(BaseModel):
            test_field: str = "test"

        operative = Step.request_operative(base_type=CustomBase)

        assert isinstance(operative, Operative)

    def test_request_operative_with_inherit_base_false(self):
        """Test request operative with inherit_base disabled."""
        operative = Step.request_operative(inherit_base=False)

        assert isinstance(operative, Operative)

    def test_request_operative_with_config_dict(self):
        """Test request operative with config dict."""
        config = {"extra": "forbid"}
        operative = Step.request_operative(config_dict=config)

        assert isinstance(operative, Operative)

    def test_request_operative_with_doc(self):
        """Test request operative with documentation."""
        operative = Step.request_operative(doc="Test documentation")

        assert isinstance(operative, Operative)

    def test_request_operative_with_frozen(self):
        """Test request operative with frozen model."""
        operative = Step.request_operative(frozen=True)

        assert isinstance(operative, Operative)

    def test_request_operative_with_new_model_name(self):
        """Test request operative with custom model name."""
        operative = Step.request_operative(new_model_name="CustomModel")

        assert isinstance(operative, Operative)

    def test_request_operative_with_parameter_fields(self):
        """Test request operative with parameter fields."""
        param_fields = {"param1": FieldInfo(description="Parameter 1")}
        operative = Step.request_operative(parameter_fields=param_fields)

        assert isinstance(operative, Operative)


class TestStepParameterProcessing:
    """Test parameter processing edge cases."""

    def test_request_operative_none_field_models(self):
        """Test that None field_models is handled."""
        operative = Step.request_operative(field_models=None)

        assert isinstance(operative, Operative)

    def test_request_operative_none_exclude_fields(self):
        """Test that None exclude_fields is handled."""
        operative = Step.request_operative(exclude_fields=None)

        assert isinstance(operative, Operative)

    def test_request_operative_none_field_descriptions(self):
        """Test that None field_descriptions is handled."""
        operative = Step.request_operative(field_descriptions=None)

        assert isinstance(operative, Operative)

    def test_request_operative_empty_lists(self):
        """Test request operative with empty lists."""
        operative = Step.request_operative(
            field_models=[],
            exclude_fields=[],
        )

        assert isinstance(operative, Operative)

    def test_request_operative_none_request_params(self):
        """Test that None request_params is handled."""
        operative = Step.request_operative(request_params=None)

        assert isinstance(operative, Operative)


class TestStepUtilityMethods:
    """Test Step as utility class."""

    def test_step_instantiation(self):
        """Test that Step can be instantiated."""
        step = Step()

        assert isinstance(step, Step)

    def test_step_static_methods_callable(self):
        """Test that Step static methods are callable."""
        assert callable(Step.request_operative)
        assert callable(Step.respond_operative)


class TestStepEdgeCases:
    """Test edge cases and error conditions."""

    def test_request_operative_single_field_model(self):
        """Test request operative with single field model."""
        field1 = FieldModel(field="unique_field", description="Single Field")

        operative = Step.request_operative(field_models=[field1])

        assert isinstance(operative, Operative)

    def test_request_operative_reason_already_in_field_models(self):
        """Test adding reason when it's already in field_models."""
        from lionagi.operations.fields import REASON_FIELD

        operative = Step.request_operative(field_models=[REASON_FIELD], reason=True)

        assert isinstance(operative, Operative)

    def test_request_operative_actions_already_in_field_models(self):
        """Test adding actions when already in field_models."""
        from lionagi.operations.fields import ACTION_REQUESTS_FIELD

        operative = Step.request_operative(field_models=[ACTION_REQUESTS_FIELD], actions=True)

        assert isinstance(operative, Operative)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
