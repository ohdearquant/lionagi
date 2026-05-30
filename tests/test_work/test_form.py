from __future__ import annotations

from lionagi.protocols.generic import Element
from lionagi.work.form import FieldSpec, Form


def test_form_inherits_element():
    form = Form(
        assignment="bind data",
        input_fields={
            "prompt": FieldSpec(name="prompt", required=True),
        },
        output_fields={
            "summary": FieldSpec(name="summary"),
        },
    )
    assert isinstance(form, Element)


def test_form_is_ready_with_required_inputs():
    form = Form(
        assignment="ready",
        input_fields={
            "prompt": FieldSpec(name="prompt", required=True),
            "tone": FieldSpec(name="tone", required=False),
        },
        output_fields={},
        inputs={
            "prompt": "write this",
            "tone": "casual",
        },
    )
    assert form.is_ready()


def test_form_not_ready_when_missing_required_input():
    form = Form(
        assignment="ready",
        input_fields={"prompt": FieldSpec(name="prompt", required=True)},
        output_fields={},
    )
    assert not form.is_ready()


def test_form_validate_outputs():
    form = Form(
        assignment="validate",
        input_fields={},
        output_fields={
            "result": FieldSpec(name="result", required=True, data_type="str"),
            "score": FieldSpec(name="score", required=False, data_type="number"),
        },
        outputs={
            "result": "ok",
            "score": "bad",
        },
    )
    errors = form.validate_outputs()
    assert "Field 'score' expected number" in errors
