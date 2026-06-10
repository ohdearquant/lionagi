# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.work: WorkForm, FieldSpec, Rule, RuleSet.

Covers the public API exported by lionagi.work.__init__:
  - FieldSpec declaration and type coercion
  - WorkForm lifecycle and transition logic
  - fill_form / validate_form functional helpers
  - Rule (required, type, range, pattern, custom) apply()
  - RuleSet composition (add, remove, get, apply_all)
"""

from __future__ import annotations

from typing import Any

import pytest

from lionagi.work import (
    VALID_TRANSITIONS,
    FieldSpec,
    FormStatus,
    Rule,
    RuleSet,
    WorkForm,
    fill_form,
    validate_form,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_form(
    fields: dict[str, dict[str, Any]] | None = None,
    values: dict[str, Any] | None = None,
    form_id: str = "test_form",
    status: FormStatus = "draft",
) -> WorkForm:
    """Build a WorkForm for testing from compact field dicts."""
    specs = {
        name: FieldSpec(name=name, **spec_kwargs) for name, spec_kwargs in (fields or {}).items()
    }
    return WorkForm(
        form_id=form_id,
        title="Test Form",
        fields=specs,
        values=values or {},
        status=status,
    )


# ---------------------------------------------------------------------------
# FieldSpec
# ---------------------------------------------------------------------------


class TestFieldSpec:
    def test_defaults(self):
        spec = FieldSpec(name="x")
        assert spec.type == "str"
        assert spec.required is True
        assert spec.default is None
        assert spec.description == ""

    def test_name_valid_letter_start(self):
        FieldSpec(name="my_field_1")  # should not raise

    def test_name_valid_underscore_start(self):
        FieldSpec(name="_private")  # should not raise

    def test_name_invalid_digit_start(self):
        with pytest.raises(Exception):
            FieldSpec(name="1bad")

    def test_name_invalid_spaces(self):
        with pytest.raises(Exception):
            FieldSpec(name="bad field")

    def test_name_invalid_hyphen(self):
        with pytest.raises(Exception):
            FieldSpec(name="bad-field")

    # Coerce: same type passthrough
    def test_coerce_str_passthrough(self):
        spec = FieldSpec(name="s", type="str")
        assert spec.coerce("hello") == "hello"

    def test_coerce_int_passthrough(self):
        spec = FieldSpec(name="n", type="int")
        assert spec.coerce(42) == 42

    # Coerce: numeric widening
    def test_coerce_int_to_float(self):
        spec = FieldSpec(name="f", type="float")
        result = spec.coerce(3)
        assert result == 3.0
        assert isinstance(result, float)

    def test_coerce_float_passthrough(self):
        spec = FieldSpec(name="f", type="float")
        assert spec.coerce(1.5) == 1.5

    # Coerce: str → bool
    def test_coerce_str_true_variants(self):
        spec = FieldSpec(name="b", type="bool")
        assert spec.coerce("true") is True
        assert spec.coerce("TRUE") is True
        assert spec.coerce("yes") is True
        assert spec.coerce("1") is True

    def test_coerce_str_false_variants(self):
        spec = FieldSpec(name="b", type="bool")
        assert spec.coerce("false") is False
        assert spec.coerce("FALSE") is False
        assert spec.coerce("no") is False
        assert spec.coerce("0") is False

    # Coerce: str → numeric
    def test_coerce_str_to_int(self):
        spec = FieldSpec(name="n", type="int")
        assert spec.coerce("42") == 42

    def test_coerce_str_to_float(self):
        spec = FieldSpec(name="f", type="float")
        assert spec.coerce("3.14") == pytest.approx(3.14)

    def test_coerce_type_mismatch_raises_type_error(self):
        spec = FieldSpec(name="n", type="int")
        with pytest.raises(TypeError):
            spec.coerce([1, 2, 3])

    def test_coerce_none_returns_none(self):
        spec = FieldSpec(name="x", type="str")
        assert spec.coerce(None) is None

    def test_coerce_invalid_str_to_int_raises(self):
        spec = FieldSpec(name="n", type="int")
        with pytest.raises(TypeError):
            spec.coerce("not_a_number")


# ---------------------------------------------------------------------------
# WorkForm
# ---------------------------------------------------------------------------


class TestWorkForm:
    def test_creation_defaults(self):
        form = WorkForm(form_id="f1", title="My Form")
        assert form.form_id == "f1"
        assert form.title == "My Form"
        assert form.fields == {}
        assert form.values == {}
        assert form.status == "draft"
        assert form.validation_errors == []

    def test_form_id_required(self):
        with pytest.raises(Exception):
            WorkForm()  # type: ignore[call-arg]

    def test_field_names(self):
        form = _make_form({"a": {"type": "str"}, "b": {"type": "int"}})
        assert set(form.field_names()) == {"a", "b"}

    def test_field_names_empty(self):
        form = WorkForm(form_id="f")
        assert form.field_names() == []

    def test_get_value_present(self):
        form = _make_form(values={"x": "hello"})
        assert form.get("x") == "hello"

    def test_get_value_missing_returns_default(self):
        form = _make_form()
        assert form.get("missing", "fallback") == "fallback"

    def test_get_value_missing_returns_none_default(self):
        form = _make_form()
        assert form.get("missing") is None

    def test_is_complete_false_on_draft(self):
        form = _make_form()
        assert form.is_complete() is False

    def test_is_complete_false_on_filled(self):
        form = _make_form(status="filled")
        assert form.is_complete() is False

    def test_is_complete_false_on_error(self):
        form = _make_form(status="error")
        assert form.is_complete() is False

    def test_is_complete_true_on_validated(self):
        form = _make_form(status="validated")
        assert form.is_complete() is True

    def test_is_complete_true_on_completed(self):
        form = _make_form(status="completed")
        assert form.is_complete() is True


class TestWorkFormTransitions:
    def test_draft_to_filled(self):
        form = _make_form()
        new_form = form.transition_to("filled")
        assert new_form.status == "filled"
        assert form.status == "draft"  # original unchanged

    def test_filled_to_validated(self):
        form = _make_form(status="filled")
        new_form = form.transition_to("validated")
        assert new_form.status == "validated"

    def test_filled_to_error(self):
        form = _make_form(status="filled")
        new_form = form.transition_to("error")
        assert new_form.status == "error"

    def test_error_to_draft(self):
        form = _make_form(status="error")
        new_form = form.transition_to("draft")
        assert new_form.status == "draft"

    def test_validated_to_submitted(self):
        form = _make_form(status="validated")
        new_form = form.transition_to("submitted")
        assert new_form.status == "submitted"

    def test_submitted_to_completed(self):
        form = _make_form(status="submitted")
        new_form = form.transition_to("completed")
        assert new_form.status == "completed"

    def test_invalid_transition_raises(self):
        form = _make_form(status="completed")
        with pytest.raises(ValueError, match="terminal"):
            form.transition_to("draft")

    def test_draft_cannot_skip_to_validated(self):
        form = _make_form()
        with pytest.raises(ValueError):
            form.transition_to("validated")

    def test_transition_returns_new_instance(self):
        form = _make_form()
        new_form = form.transition_to("filled")
        assert form is not new_form

    def test_valid_transitions_table_coverage(self):
        """All expected transitions from VALID_TRANSITIONS are accepted."""
        for from_status, allowed in VALID_TRANSITIONS.items():
            for to_status in allowed:
                form = _make_form(status=from_status)  # type: ignore[arg-type]
                result = form.transition_to(to_status)  # type: ignore[arg-type]
                assert result.status == to_status


# ---------------------------------------------------------------------------
# validate_form
# ---------------------------------------------------------------------------


class TestValidateForm:
    def test_validates_required_field_present(self):
        form = _make_form(
            fields={"name": {"type": "str", "required": True}},
            values={"name": "Alice"},
        )
        result = validate_form(form)
        assert result.status == "validated"
        assert result.validation_errors == []

    def test_error_on_missing_required(self):
        form = _make_form(fields={"name": {"type": "str", "required": True}})
        result = validate_form(form)
        assert result.status == "error"
        assert any("name" in e for e in result.validation_errors)

    def test_optional_field_absent_is_ok(self):
        form = _make_form(fields={"opt": {"type": "str", "required": False}})
        result = validate_form(form)
        assert result.status == "validated"
        assert result.validation_errors == []

    def test_type_mismatch_yields_error(self):
        form = _make_form(
            fields={"count": {"type": "int"}},
            values={"count": [1, 2]},
        )
        result = validate_form(form)
        assert result.status == "error"
        assert any("count" in e for e in result.validation_errors)

    def test_coerces_string_int(self):
        form = _make_form(
            fields={"n": {"type": "int"}},
            values={"n": "7"},
        )
        result = validate_form(form)
        assert result.status == "validated"
        assert result.values["n"] == 7

    def test_coerces_int_to_float(self):
        form = _make_form(
            fields={"f": {"type": "float"}},
            values={"f": 3},
        )
        result = validate_form(form)
        assert result.status == "validated"
        assert result.values["f"] == 3.0

    def test_does_not_mutate_original(self):
        form = _make_form(
            fields={"x": {"type": "str"}},
            values={"x": "hello"},
        )
        result = validate_form(form)
        assert form is not result
        assert form.status == "draft"  # original status preserved

    def test_multiple_errors_collected(self):
        form = _make_form(
            fields={
                "a": {"type": "str", "required": True},
                "b": {"type": "int", "required": True},
            }
        )
        result = validate_form(form)
        assert result.status == "error"
        assert len(result.validation_errors) == 2

    def test_no_fields_validates_ok(self):
        form = WorkForm(form_id="empty")
        result = validate_form(form)
        assert result.status == "validated"


# ---------------------------------------------------------------------------
# fill_form
# ---------------------------------------------------------------------------


class TestFillForm:
    def test_fill_and_auto_validate(self):
        form = _make_form(fields={"msg": {"type": "str"}})
        result = fill_form(form, {"msg": "hello"})
        assert result.status == "validated"
        assert result.values["msg"] == "hello"

    def test_fill_uses_default_when_absent(self):
        form = _make_form(fields={"level": {"type": "int", "required": False, "default": 1}})
        result = fill_form(form, {})
        assert result.values.get("level") == 1

    def test_fill_required_missing_yields_error(self):
        form = _make_form(fields={"required_field": {"type": "str", "required": True}})
        result = fill_form(form, {})
        assert result.status == "error"
        assert any("required_field" in e for e in result.validation_errors)

    def test_fill_extra_keys_preserved(self):
        form = _make_form(fields={"a": {"type": "str"}})
        result = fill_form(form, {"a": "x", "extra": 99})
        assert result.values.get("extra") == 99

    def test_fill_does_not_mutate_original(self):
        form = _make_form(fields={"x": {"type": "str"}})
        fill_form(form, {"x": "hello"})
        assert form.values == {}
        assert form.status == "draft"

    def test_fill_overrides_previous_values(self):
        form = _make_form(fields={"x": {"type": "str"}}, values={"x": "old"})
        result = fill_form(form, {"x": "new"})
        assert result.values["x"] == "new"

    def test_fill_coerces_during_validation(self):
        form = _make_form(fields={"n": {"type": "int"}})
        result = fill_form(form, {"n": "42"})
        assert result.status == "validated"
        assert result.values["n"] == 42

    def test_fill_multiple_fields(self):
        form = _make_form(
            fields={
                "name": {"type": "str"},
                "age": {"type": "int"},
                "active": {"type": "bool"},
            }
        )
        result = fill_form(form, {"name": "Bob", "age": 30, "active": True})
        assert result.status == "validated"
        assert result.values == {"name": "Bob", "age": 30, "active": True}


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


class TestRuleRequired:
    def test_passes_when_value_present(self):
        rule = Rule(rule_id="r1", field="name", check="required")
        form = WorkForm(form_id="f", values={"name": "Alice"})
        assert rule.apply(form) is None

    def test_fails_when_value_absent(self):
        rule = Rule(rule_id="r1", field="name", check="required")
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) is not None

    def test_fails_when_value_is_none(self):
        rule = Rule(rule_id="r1", field="name", check="required")
        form = WorkForm(form_id="f", values={"name": None})
        assert rule.apply(form) is not None

    def test_custom_message_used(self):
        rule = Rule(rule_id="r1", field="x", check="required", message="x is absolutely required")
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) == "x is absolutely required"

    def test_disabled_rule_skipped(self):
        rule = Rule(rule_id="r1", field="name", check="required", enabled=False)
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) is None


class TestRuleType:
    def test_passes_correct_type(self):
        rule = Rule(rule_id="r", field="n", check="type", params={"type": "int"})
        form = WorkForm(form_id="f", values={"n": 5})
        assert rule.apply(form) is None

    def test_passes_int_for_float(self):
        rule = Rule(rule_id="r", field="f", check="type", params={"type": "float"})
        form = WorkForm(form_id="f", values={"f": 3})
        assert rule.apply(form) is None  # int widened to float

    def test_fails_wrong_type(self):
        rule = Rule(rule_id="r", field="n", check="type", params={"type": "int"})
        form = WorkForm(form_id="f", values={"n": "not_int"})
        err = rule.apply(form)
        assert err is not None
        assert "int" in err

    def test_absent_value_passes(self):
        rule = Rule(rule_id="r", field="n", check="type", params={"type": "int"})
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) is None

    def test_unknown_type_returns_error(self):
        rule = Rule(rule_id="r", field="n", check="type", params={"type": "uuid"})
        form = WorkForm(form_id="f", values={"n": "some-uuid"})
        err = rule.apply(form)
        assert err is not None
        assert "unknown type" in err


class TestRuleRange:
    def test_passes_within_range(self):
        rule = Rule(rule_id="r", field="age", check="range", params={"min": 0, "max": 120})
        form = WorkForm(form_id="f", values={"age": 30})
        assert rule.apply(form) is None

    def test_passes_at_min_boundary(self):
        rule = Rule(rule_id="r", field="age", check="range", params={"min": 0})
        form = WorkForm(form_id="f", values={"age": 0})
        assert rule.apply(form) is None

    def test_passes_at_max_boundary(self):
        rule = Rule(rule_id="r", field="score", check="range", params={"max": 100})
        form = WorkForm(form_id="f", values={"score": 100})
        assert rule.apply(form) is None

    def test_fails_below_min(self):
        rule = Rule(rule_id="r", field="age", check="range", params={"min": 18})
        form = WorkForm(form_id="f", values={"age": 5})
        err = rule.apply(form)
        assert err is not None
        assert "minimum" in err

    def test_fails_above_max(self):
        rule = Rule(rule_id="r", field="score", check="range", params={"max": 100})
        form = WorkForm(form_id="f", values={"score": 150})
        err = rule.apply(form)
        assert err is not None
        assert "maximum" in err

    def test_absent_value_skipped(self):
        rule = Rule(rule_id="r", field="n", check="range", params={"min": 0})
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) is None

    def test_non_numeric_value_error(self):
        rule = Rule(rule_id="r", field="n", check="range", params={"min": 0})
        form = WorkForm(form_id="f", values={"n": "five"})
        err = rule.apply(form)
        assert err is not None
        assert "numeric" in err

    def test_float_in_range(self):
        rule = Rule(rule_id="r", field="x", check="range", params={"min": 0.0, "max": 1.0})
        form = WorkForm(form_id="f", values={"x": 0.5})
        assert rule.apply(form) is None

    def test_only_min_bound(self):
        rule = Rule(rule_id="r", field="x", check="range", params={"min": 10})
        form = WorkForm(form_id="f", values={"x": 100})
        assert rule.apply(form) is None

    def test_only_max_bound(self):
        rule = Rule(rule_id="r", field="x", check="range", params={"max": 10})
        form = WorkForm(form_id="f", values={"x": 0})
        assert rule.apply(form) is None


class TestRulePattern:
    def test_passes_matching_pattern(self):
        rule = Rule(rule_id="r", field="email", check="pattern", params={"pattern": r".+@.+"})
        form = WorkForm(form_id="f", values={"email": "a@b.com"})
        assert rule.apply(form) is None

    def test_fails_non_matching(self):
        rule = Rule(rule_id="r", field="email", check="pattern", params={"pattern": r".+@.+"})
        form = WorkForm(form_id="f", values={"email": "notanemail"})
        assert rule.apply(form) is not None

    def test_absent_value_skipped(self):
        rule = Rule(rule_id="r", field="email", check="pattern", params={"pattern": r".+@.+"})
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) is None

    def test_non_string_value_error(self):
        rule = Rule(rule_id="r", field="x", check="pattern", params={"pattern": r"\d+"})
        form = WorkForm(form_id="f", values={"x": 42})
        err = rule.apply(form)
        assert err is not None
        assert "str" in err

    def test_invalid_regex_returns_error(self):
        rule = Rule(rule_id="r", field="x", check="pattern", params={"pattern": r"[invalid"})
        form = WorkForm(form_id="f", values={"x": "hello"})
        err = rule.apply(form)
        assert err is not None
        assert "invalid regex" in err

    def test_input_too_long_rejected(self):
        from lionagi.work import REGEX_MAX_INPUT_LENGTH

        rule = Rule(rule_id="r", field="x", check="pattern", params={"pattern": r".*"})
        form = WorkForm(form_id="f", values={"x": "a" * (REGEX_MAX_INPUT_LENGTH + 1)})
        err = rule.apply(form)
        assert err is not None
        assert "length" in err

    def test_custom_message_on_mismatch(self):
        rule = Rule(
            rule_id="r",
            field="code",
            check="pattern",
            params={"pattern": r"^\d{4}$"},
            message="Must be 4 digits",
        )
        form = WorkForm(form_id="f", values={"code": "abc"})
        assert rule.apply(form) == "Must be 4 digits"


class TestRuleCustom:
    def test_passes_when_callable_returns_true(self):
        rule = Rule(
            rule_id="r",
            field="val",
            check="custom",
            params={"callable": lambda v: v is not None and v > 0},
        )
        form = WorkForm(form_id="f", values={"val": 5})
        assert rule.apply(form) is None

    def test_fails_when_callable_returns_false(self):
        rule = Rule(
            rule_id="r",
            field="val",
            check="custom",
            params={"callable": lambda v: v is not None and v > 0},
        )
        form = WorkForm(form_id="f", values={"val": -1})
        assert rule.apply(form) is not None

    def test_uses_params_error_message(self):
        rule = Rule(
            rule_id="r",
            field="val",
            check="custom",
            params={
                "callable": lambda v: v > 0,
                "error": "Must be positive",
            },
        )
        form = WorkForm(form_id="f", values={"val": -1})
        err = rule.apply(form)
        assert "Must be positive" in err

    def test_uses_rule_message_over_params_error(self):
        rule = Rule(
            rule_id="r",
            field="val",
            check="custom",
            params={"callable": lambda v: False, "error": "params error"},
            message="rule message",
        )
        form = WorkForm(form_id="f", values={"val": 1})
        assert rule.apply(form) == "rule message"

    def test_missing_callable_returns_error(self):
        rule = Rule(rule_id="r", field="val", check="custom", params={})
        form = WorkForm(form_id="f", values={"val": 1})
        err = rule.apply(form)
        assert err is not None
        assert "callable" in err

    def test_callable_exception_returns_error(self):
        def bad_fn(v: Any) -> bool:
            raise RuntimeError("exploded")

        rule = Rule(
            rule_id="r",
            field="val",
            check="custom",
            params={"callable": bad_fn},
        )
        form = WorkForm(form_id="f", values={"val": 1})
        err = rule.apply(form)
        assert err is not None
        assert "RuntimeError" in err


# ---------------------------------------------------------------------------
# RuleSet
# ---------------------------------------------------------------------------


class TestRuleSet:
    def test_add_and_apply_all_no_errors(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="name", check="required"))
        form = WorkForm(form_id="f", values={"name": "Bob"})
        assert rs.apply_all(form) == []

    def test_apply_all_collects_all_errors(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="a", check="required"))
        rs.add(Rule(rule_id="r2", field="b", check="required"))
        form = WorkForm(form_id="f", values={})
        errors = rs.apply_all(form)
        assert len(errors) == 2

    def test_apply_all_no_rules_returns_empty(self):
        rs = RuleSet()
        form = WorkForm(form_id="f", values={})
        assert rs.apply_all(form) == []

    def test_remove_existing_rule(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="x", check="required"))
        removed = rs.remove("r1")
        assert removed is True
        assert rs.get("r1") is None

    def test_remove_nonexistent_returns_false(self):
        rs = RuleSet()
        assert rs.remove("nope") is False

    def test_get_rule_found(self):
        rs = RuleSet()
        rule = Rule(rule_id="r1", field="x", check="required")
        rs.add(rule)
        assert rs.get("r1") is rule

    def test_get_rule_not_found(self):
        rs = RuleSet()
        assert rs.get("missing") is None

    def test_add_returns_self_for_chaining(self):
        rs = RuleSet()
        result = rs.add(Rule(rule_id="r1", field="x", check="required"))
        assert result is rs

    def test_chained_add(self):
        rs = (
            RuleSet()
            .add(Rule(rule_id="r1", field="x", check="required"))
            .add(Rule(rule_id="r2", field="y", check="required"))
        )
        assert len(rs.rules()) == 2

    def test_rules_returns_copy(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="x", check="required"))
        copy = rs.rules()
        copy.clear()
        assert len(rs.rules()) == 1  # original unaffected

    def test_disabled_rule_not_counted_in_errors(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="a", check="required"))
        rs.add(Rule(rule_id="r2", field="b", check="required", enabled=False))
        form = WorkForm(form_id="f", values={})
        errors = rs.apply_all(form)
        assert len(errors) == 1  # only r1 fires

    def test_rules_applied_in_insertion_order(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="first", field="a", check="required"))
        rs.add(Rule(rule_id="second", field="b", check="required"))
        form = WorkForm(form_id="f", values={})
        errors = rs.apply_all(form)
        assert "a" in errors[0]
        assert "b" in errors[1]

    def test_remove_and_reapply(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="x", check="required"))
        rs.remove("r1")
        form = WorkForm(form_id="f", values={})
        assert rs.apply_all(form) == []

    def test_mixed_pass_and_fail(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="age", check="range", params={"min": 0, "max": 120}))
        rs.add(Rule(rule_id="r2", field="email", check="pattern", params={"pattern": r".+@.+"}))
        form = WorkForm(form_id="f", values={"age": 30, "email": "notanemail"})
        errors = rs.apply_all(form)
        assert len(errors) == 1
        assert "email" in errors[0]


# ---------------------------------------------------------------------------
# Integration: fill_form + RuleSet
# ---------------------------------------------------------------------------


class TestFillFormWithRuleSet:
    def test_filled_validated_form_passes_ruleset(self):
        form = _make_form(fields={"age": {"type": "int"}})
        filled = fill_form(form, {"age": 25})
        assert filled.status == "validated"

        rs = RuleSet()
        rs.add(Rule(rule_id="age_range", field="age", check="range", params={"min": 0, "max": 120}))
        errors = rs.apply_all(filled)
        assert errors == []

    def test_filled_form_fails_ruleset_range(self):
        form = _make_form(fields={"age": {"type": "int"}})
        filled = fill_form(form, {"age": 200})
        assert filled.status == "validated"  # type check passes

        rs = RuleSet()
        rs.add(Rule(rule_id="age_range", field="age", check="range", params={"min": 0, "max": 120}))
        errors = rs.apply_all(filled)
        assert len(errors) == 1
        assert "maximum" in errors[0]

    def test_ruleset_on_error_form(self):
        """A form in error state can still have rules applied for diagnostics."""
        form = _make_form(fields={"name": {"type": "str", "required": True}})
        error_form = fill_form(form, {})
        assert error_form.status == "error"

        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="name", check="required"))
        errors = rs.apply_all(error_form)
        assert len(errors) == 1
