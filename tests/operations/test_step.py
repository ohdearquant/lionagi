# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.operate.step — Step factory methods."""

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
