# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.operate.step — Step factory methods."""

from pydantic import BaseModel

from lionagi.ln.types import Spec
from lionagi.models import FieldModel
from lionagi.operations.operate.step import Step


def test_step_request_operative_converts_field_models_and_excludes_action_responses():
    """field_models list is converted to Spec; actions=True excludes action_responses from request."""
    fm = FieldModel(name="score", base_type=int)
    op = Step.request_operative(
        name="Check",
        field_models=[fm],
        actions=True,
        reason=True,
    )

    request_cls = op.create_request_model()
    request_fields = set(request_cls.model_fields.keys())

    # reason and action_required / action_requests should be present
    assert "reason" in request_fields
    assert "action_required" in request_fields
    assert "action_requests" in request_fields
    assert "score" in request_fields

    # action_responses is excluded from request
    assert "action_responses" not in request_fields

    # response model includes action_responses
    response_cls = op.create_response_model()
    response_fields = set(response_cls.model_fields.keys())
    assert "action_responses" in response_fields


def test_step_respond_operative_additional_fields_returns_new_operative():
    """respond_operative with additional_fields returns a new Operative including new fields."""
    base_op = Step.request_operative(name="Check")
    confidence_spec = Spec(float, name="confidence")

    new_op = Step.respond_operative(base_op, additional_fields={"confidence": confidence_spec})

    # new_op is a distinct object
    assert new_op is not base_op

    response_cls = new_op.create_response_model()
    assert "confidence" in response_cls.model_fields

    # original operative's response model is not affected
    original_response_cls = base_op.create_response_model()
    assert "confidence" not in original_response_cls.model_fields


def test_operative_model_type_cache_reuses_same_schema():
    class Payload(BaseModel):
        value: int

    first = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))
    second = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))

    assert first is not second
    assert first.request_type is second.request_type
    assert first.response_type is second.response_type


def test_operative_model_type_cache_distinguishes_same_shaped_base_classes():
    def make_first_payload_class():
        class Payload(BaseModel):
            value: int

        return Payload

    def make_second_payload_class():
        class Payload(BaseModel):
            value: int

        return Payload

    Payload1 = make_first_payload_class()
    Payload2 = make_second_payload_class()
    assert Payload1 is not Payload2

    model1 = Step.request_operative(base_type=Payload1).request_type
    model2 = Step.request_operative(base_type=Payload2).request_type

    assert model1 is not model2
    assert issubclass(model1, Payload1)
    assert issubclass(model2, Payload2)
    assert isinstance(model2.model_validate({"value": 1}), Payload2)
    assert not isinstance(model2.model_validate({"value": 1}), Payload1)


def test_operative_model_type_cache_is_sensitive_to_reason_and_actions():
    class Payload(BaseModel):
        value: int

    plain = Step.request_operative(base_type=Payload).request_type
    reason = Step.request_operative(base_type=Payload, reason=True).request_type
    actions = Step.request_operative(base_type=Payload, actions=True).request_type

    assert plain is not reason
    assert plain is not actions
    assert reason is not actions
    assert "reason" in reason.model_fields
    assert "action_requests" in actions.model_fields
    assert "action_responses" not in actions.model_fields


def test_operative_model_type_cache_preserves_cold_and_warm_validation_behavior():
    class Payload(BaseModel):
        value: int
        label: str

    cold = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))
    warm = Step.respond_operative(Step.request_operative(base_type=Payload, reason=True))
    payload = {
        "value": 7,
        "label": "complete",
        "reason": {"title": "check", "content": "validated", "confidence_score": 0.9},
    }

    assert (
        cold.request_type.model_validate(payload).model_dump()
        == warm.request_type.model_validate(payload).model_dump()
    )
    assert (
        cold.response_type.model_validate(payload).model_dump()
        == warm.response_type.model_validate(payload).model_dump()
    )
