import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from lionagi.models import FieldModel
from lionagi.operations.operate.step import Operative, Step


class TestStepBasicFunctionality:
    def test_request_operative_basic(self):
        operative = Step.request_operative()

        assert isinstance(operative, Operative)
        assert operative.name is not None

    def test_request_operative_with_name(self):
        operative = Step.request_operative(operative_name="test_name")

        assert operative.name == "test_name"

    def test_request_operative_with_max_retries(self):
        operative = Step.request_operative(max_retries=5)

        assert operative.max_retries == 5

    def test_request_operative_with_auto_retry_parse_false(self):
        operative = Step.request_operative(auto_retry_parse=False)

        assert operative.auto_retry_parse is False

    def test_request_operative_with_auto_retry_parse_true(self):
        operative = Step.request_operative(auto_retry_parse=True)

        assert operative.auto_retry_parse is True

    def test_request_operative_with_reason_field(self):
        operative = Step.request_operative(reason=True)

        assert isinstance(operative, Operative)

    def test_request_operative_with_actions_field(self):
        operative = Step.request_operative(actions=True)

        assert isinstance(operative, Operative)

    def test_request_operative_with_reason_and_actions(self):
        operative = Step.request_operative(reason=True, actions=True)

        assert isinstance(operative, Operative)

    def test_request_operative_with_field_models(self):
        field_model = FieldModel(field="test_field", description="Test")
        operative = Step.request_operative(field_models=[field_model])

        assert isinstance(operative, Operative)

    def test_request_operative_with_exclude_fields(self):
        operative = Step.request_operative(exclude_fields=["field1", "field2"])

        assert isinstance(operative, Operative)

    def test_request_operative_with_field_descriptions(self):
        descriptions = {"field1": "Description 1"}
        operative = Step.request_operative(field_descriptions=descriptions)

        assert isinstance(operative, Operative)

    def test_request_operative_with_base_type(self):

        class CustomBase(BaseModel):
            test_field: str = "test"

        operative = Step.request_operative(base_type=CustomBase)

        assert isinstance(operative, Operative)

    def test_request_operative_with_config_dict(self):
        config = {"extra": "forbid"}
        operative = Step.request_operative(config_dict=config)

        assert isinstance(operative, Operative)

    def test_request_operative_with_doc(self):
        operative = Step.request_operative(doc="Test documentation")

        assert isinstance(operative, Operative)

    def test_request_operative_with_new_model_name(self):
        operative = Step.request_operative(new_model_name="CustomModel")

        assert isinstance(operative, Operative)

    def test_request_operative_with_parameter_fields(self):
        param_fields = {"param1": FieldInfo(description="Parameter 1")}
        operative = Step.request_operative(parameter_fields=param_fields)

        assert isinstance(operative, Operative)


class TestStepParameterProcessing:
    def test_request_operative_none_field_models(self):
        operative = Step.request_operative(field_models=None)

        assert isinstance(operative, Operative)

    def test_request_operative_none_exclude_fields(self):
        operative = Step.request_operative(exclude_fields=None)

        assert isinstance(operative, Operative)

    def test_request_operative_none_field_descriptions(self):
        operative = Step.request_operative(field_descriptions=None)

        assert isinstance(operative, Operative)

    def test_request_operative_empty_lists(self):
        operative = Step.request_operative(
            field_models=[],
            exclude_fields=[],
        )

        assert isinstance(operative, Operative)

    def test_request_operative_none_request_params(self):
        operative = Step.request_operative(request_params=None)

        assert isinstance(operative, Operative)


class TestStepUtilityMethods:
    def test_step_instantiation(self):
        step = Step()

        assert isinstance(step, Step)

    def test_step_static_methods_callable(self):
        assert callable(Step.request_operative)
        assert callable(Step.respond_operative)


class TestStepEdgeCases:
    def test_request_operative_single_field_model(self):
        field1 = FieldModel(field="unique_field", description="Single Field")

        operative = Step.request_operative(field_models=[field1])

        assert isinstance(operative, Operative)

    def test_request_operative_reason_already_in_field_models(self):
        from lionagi.operations.fields import REASON_FIELD

        operative = Step.request_operative(field_models=[REASON_FIELD], reason=True)

        assert isinstance(operative, Operative)

    def test_request_operative_actions_already_in_field_models(self):
        from lionagi.operations.fields import ACTION_REQUESTS_FIELD

        operative = Step.request_operative(field_models=[ACTION_REQUESTS_FIELD], actions=True)

        assert isinstance(operative, Operative)
