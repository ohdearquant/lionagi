# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.work: forms, rules, definitions, and engine."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest

from lionagi.work import (
    FieldSpec,
    Rule,
    RuleSet,
    WorkEngine,
    WorkerDefinition,
    WorkForm,
    WorkResult,
    WorkTask,
    fill_form,
    load_definition,
    validate_form,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_form(fields: dict | None = None, values: dict | None = None) -> WorkForm:
    """Build a simple WorkForm for testing."""
    specs = {
        name: FieldSpec(name=name, **spec_kwargs) for name, spec_kwargs in (fields or {}).items()
    }
    return WorkForm(form_id="test_form", title="Test Form", fields=specs, values=values or {})


def _echo_handler(form: WorkForm) -> dict[str, Any]:
    """Simple handler that echoes back form values."""
    return {"echoed": form.values}


def _failing_handler(form: WorkForm) -> None:
    raise ValueError("handler failed on purpose")


def _make_defn(handler_path: str = "tests.work.test_work_system._echo_handler") -> WorkerDefinition:
    return WorkerDefinition(
        definition_id="echo",
        name="Echo Worker",
        input_form="test_form",
        output_form="test_form",
        handler=handler_path,
    )


# ---------------------------------------------------------------------------
# FieldSpec tests
# ---------------------------------------------------------------------------


class TestFieldSpec:
    def test_defaults(self):
        spec = FieldSpec(name="x")
        assert spec.type == "str"
        assert spec.required is True
        assert spec.default is None
        assert spec.description == ""

    def test_name_validation_valid(self):
        # alphanumeric + underscore starting with letter
        FieldSpec(name="my_field_1")

    def test_name_validation_invalid_starts_with_digit(self):
        with pytest.raises(Exception):
            FieldSpec(name="1bad")

    def test_name_validation_invalid_spaces(self):
        with pytest.raises(Exception):
            FieldSpec(name="bad field")

    def test_coerce_str_to_int(self):
        spec = FieldSpec(name="n", type="int")
        assert spec.coerce("42") == 42

    def test_coerce_int_to_float(self):
        spec = FieldSpec(name="n", type="float")
        assert spec.coerce(3) == 3.0
        assert isinstance(spec.coerce(3), float)

    def test_coerce_str_to_bool_true(self):
        spec = FieldSpec(name="flag", type="bool")
        assert spec.coerce("yes") is True
        assert spec.coerce("TRUE") is True
        assert spec.coerce("1") is True

    def test_coerce_str_to_bool_false(self):
        spec = FieldSpec(name="flag", type="bool")
        assert spec.coerce("no") is False
        assert spec.coerce("FALSE") is False
        assert spec.coerce("0") is False

    def test_coerce_type_mismatch_raises(self):
        spec = FieldSpec(name="n", type="int")
        with pytest.raises(TypeError):
            spec.coerce([1, 2, 3])

    def test_coerce_none_returns_none(self):
        spec = FieldSpec(name="x", type="str")
        assert spec.coerce(None) is None


# ---------------------------------------------------------------------------
# WorkForm tests
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

    def test_field_names(self):
        form = _make_form({"a": {"type": "str"}, "b": {"type": "int"}})
        assert set(form.field_names()) == {"a", "b"}

    def test_get_value(self):
        form = _make_form(values={"x": "hello"})
        assert form.get("x") == "hello"
        assert form.get("missing", "default") == "default"

    def test_is_complete_false_on_draft(self):
        form = _make_form()
        assert form.is_complete() is False

    def test_is_complete_true_on_validated(self):
        form = WorkForm(form_id="f", status="validated")
        assert form.is_complete() is True

    def test_is_complete_true_on_completed(self):
        form = WorkForm(form_id="f", status="completed")
        assert form.is_complete() is True


# ---------------------------------------------------------------------------
# validate_form tests
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
        form = _make_form(
            fields={"opt": {"type": "str", "required": False}},
        )
        result = validate_form(form)
        assert result.status == "validated"

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

    def test_does_not_mutate_original(self):
        form = _make_form(
            fields={"x": {"type": "str"}},
            values={"x": "hello"},
        )
        result = validate_form(form)
        assert form is not result


# ---------------------------------------------------------------------------
# fill_form tests
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

    def test_fill_extra_keys_preserved(self):
        form = _make_form(fields={"a": {"type": "str"}})
        result = fill_form(form, {"a": "x", "extra": 99})
        assert result.values.get("extra") == 99


# ---------------------------------------------------------------------------
# Rule tests
# ---------------------------------------------------------------------------


class TestRule:
    def test_required_passes(self):
        rule = Rule(rule_id="r1", field="name", check="required")
        form = WorkForm(form_id="f", values={"name": "Alice"})
        assert rule.apply(form) is None

    def test_required_fails(self):
        rule = Rule(rule_id="r1", field="name", check="required")
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) is not None

    def test_range_passes(self):
        rule = Rule(rule_id="r2", field="age", check="range", params={"min": 0, "max": 120})
        form = WorkForm(form_id="f", values={"age": 30})
        assert rule.apply(form) is None

    def test_range_below_min(self):
        rule = Rule(rule_id="r2", field="age", check="range", params={"min": 18})
        form = WorkForm(form_id="f", values={"age": 5})
        err = rule.apply(form)
        assert err is not None
        assert "minimum" in err

    def test_range_above_max(self):
        rule = Rule(rule_id="r2", field="score", check="range", params={"max": 100})
        form = WorkForm(form_id="f", values={"score": 150})
        err = rule.apply(form)
        assert err is not None
        assert "maximum" in err

    def test_pattern_passes(self):
        rule = Rule(rule_id="r3", field="email", check="pattern", params={"pattern": r".+@.+"})
        form = WorkForm(form_id="f", values={"email": "a@b.com"})
        assert rule.apply(form) is None

    def test_pattern_fails(self):
        rule = Rule(rule_id="r3", field="email", check="pattern", params={"pattern": r".+@.+"})
        form = WorkForm(form_id="f", values={"email": "notanemail"})
        assert rule.apply(form) is not None

    def test_custom_passes(self):
        rule = Rule(
            rule_id="r4",
            field="val",
            check="custom",
            params={"callable": lambda v: v is not None and v > 0},
        )
        form = WorkForm(form_id="f", values={"val": 5})
        assert rule.apply(form) is None

    def test_custom_fails(self):
        rule = Rule(
            rule_id="r4",
            field="val",
            check="custom",
            params={"callable": lambda v: v is not None and v > 0, "error": "Must be positive"},
        )
        form = WorkForm(form_id="f", values={"val": -1})
        err = rule.apply(form)
        assert err is not None

    def test_disabled_rule_skipped(self):
        rule = Rule(rule_id="r5", field="name", check="required", enabled=False)
        form = WorkForm(form_id="f", values={})
        assert rule.apply(form) is None

    def test_custom_message_used(self):
        rule = Rule(rule_id="r6", field="x", check="required", message="x is absolutely required")
        form = WorkForm(form_id="f", values={})
        err = rule.apply(form)
        assert err == "x is absolutely required"


# ---------------------------------------------------------------------------
# RuleSet tests
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

    def test_remove_rule(self):
        rs = RuleSet()
        rs.add(Rule(rule_id="r1", field="x", check="required"))
        removed = rs.remove("r1")
        assert removed is True
        assert rs.get("r1") is None

    def test_remove_nonexistent_returns_false(self):
        rs = RuleSet()
        assert rs.remove("nope") is False

    def test_get_rule(self):
        rs = RuleSet()
        rule = Rule(rule_id="r1", field="x", check="required")
        rs.add(rule)
        assert rs.get("r1") is rule

    def test_chaining(self):
        rs = RuleSet()
        result = rs.add(Rule(rule_id="r1", field="x", check="required"))
        assert result is rs


# ---------------------------------------------------------------------------
# WorkerDefinition tests
# ---------------------------------------------------------------------------


class TestWorkerDefinition:
    def test_creation(self):
        defn = WorkerDefinition(
            definition_id="w1",
            name="Worker One",
            input_form="input_f",
            output_form="output_f",
            handler="tests.work.test_work_system._echo_handler",
        )
        assert defn.definition_id == "w1"
        assert defn.max_concurrent == 1
        assert defn.timeout_seconds == 0
        assert defn.tags == []

    def test_handler_must_have_module(self):
        with pytest.raises(Exception):
            WorkerDefinition(
                definition_id="w",
                name="W",
                input_form="f",
                output_form="f",
                handler="no_module",  # no dot — invalid
            )

    def test_resolve_handler(self):
        defn = _make_defn()
        fn = defn.resolve_handler()
        assert callable(fn)

    def test_load_definition_from_dict(self):
        data = {
            "definition_id": "d1",
            "name": "D1 Worker",
            "input_form": "in",
            "output_form": "out",
            "handler": "tests.work.test_work_system._echo_handler",
            "max_concurrent": 2,
            "timeout_seconds": 30,
        }
        defn = load_definition(data)
        assert defn.definition_id == "d1"
        assert defn.max_concurrent == 2
        assert defn.timeout_seconds == 30

    def test_load_definition_from_json_file(self, tmp_path):
        data = {
            "definition_id": "json_worker",
            "name": "JSON Worker",
            "input_form": "f",
            "output_form": "f",
            "handler": "tests.work.test_work_system._echo_handler",
        }
        path = tmp_path / "worker.json"
        path.write_text(json.dumps(data))
        defn = load_definition(str(path))
        assert defn.definition_id == "json_worker"

    def test_load_definition_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_definition("/nonexistent/path/worker.json")

    def test_load_definition_unsupported_extension(self, tmp_path):
        path = tmp_path / "worker.txt"
        path.write_text("{}")
        with pytest.raises(ValueError, match="Unsupported"):
            load_definition(str(path))


# ---------------------------------------------------------------------------
# WorkEngine tests
# ---------------------------------------------------------------------------


class TestWorkEngine:
    def test_register_and_submit(self):
        engine = WorkEngine()
        defn = _make_defn()
        engine.register_worker(defn, _echo_handler)
        form = fill_form(_make_form({"msg": {"type": "str"}}), {"msg": "hello"})
        task_id = engine.submit(form, worker_id="echo")
        assert isinstance(task_id, str)

    def test_get_result_after_submit(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        form = fill_form(_make_form({"msg": {"type": "str"}}), {"msg": "world"})
        task_id = engine.submit(form)
        result = engine.get_result(task_id)
        assert result is not None
        assert result.success is True
        assert result.value == {"echoed": {"msg": "world"}}

    def test_failed_task_stores_error(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _failing_handler)
        form = fill_form(_make_form({"msg": {"type": "str"}}), {"msg": "x"})
        task_id = engine.submit(form)
        result = engine.get_result(task_id)
        assert result is not None
        assert result.success is False
        assert "ValueError" in result.error

    def test_list_tasks_empty(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        assert engine.list_tasks() == []

    def test_list_tasks_after_submit(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        form = fill_form(_make_form({"x": {"type": "str"}}), {"x": "a"})
        engine.submit(form)
        tasks = engine.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == "completed"

    def test_list_tasks_filtered(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        form = fill_form(_make_form({"x": {"type": "str"}}), {"x": "a"})
        engine.submit(form)
        completed = engine.list_tasks(status="completed")
        queued = engine.list_tasks(status="queued")
        assert len(completed) == 1
        assert len(queued) == 0

    def test_no_workers_raises(self):
        engine = WorkEngine()
        form = fill_form(_make_form({"x": {"type": "str"}}), {"x": "a"})
        with pytest.raises(ValueError, match="No workers"):
            engine.submit(form)

    def test_unknown_worker_id_raises(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        form = fill_form(_make_form({"x": {"type": "str"}}), {"x": "a"})
        with pytest.raises(ValueError, match="No worker registered"):
            engine.submit(form, worker_id="nonexistent")

    def test_worker_ids(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        assert "echo" in engine.worker_ids()

    def test_unregister_worker(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        removed = engine.unregister_worker("echo")
        assert removed is True
        assert "echo" not in engine.worker_ids()

    def test_clear_completed(self):
        engine = WorkEngine()
        engine.register_worker(_make_defn(), _echo_handler)
        form = fill_form(_make_form({"x": {"type": "str"}}), {"x": "a"})
        engine.submit(form)
        cleared = engine.clear_completed()
        assert cleared == 1
        assert engine.list_tasks() == []

    def test_work_result_success_property(self):
        r = WorkResult(task_id="t1", value=42, error=None)
        assert r.success is True

    def test_work_result_failure_property(self):
        r = WorkResult(task_id="t1", value=None, error="something failed")
        assert r.success is False

    def test_work_task_is_terminal(self):
        t = WorkTask(form_id="f", worker_id="w", status="completed")
        assert t.is_terminal is True
        t2 = WorkTask(form_id="f", worker_id="w", status="running")
        assert t2.is_terminal is False

    def test_work_task_duration(self):
        t = WorkTask(form_id="f", worker_id="w")
        t.submitted_at = 1000.0
        t.completed_at = 1005.0
        assert t.duration == pytest.approx(5.0)

    def test_concurrent_submissions_thread_safe(self):
        """Multiple threads can submit tasks concurrently without data corruption."""
        engine = WorkEngine(name="thread-test")

        defn = WorkerDefinition(
            definition_id="fast",
            name="Fast",
            input_form="f",
            output_form="f",
            handler="tests.work.test_work_system._echo_handler",
            max_concurrent=0,  # unlimited
        )
        engine.register_worker(defn, _echo_handler)

        task_ids: list[str] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def submit_one() -> None:
            try:
                form = fill_form(_make_form({"n": {"type": "int"}}), {"n": 1})
                tid = engine.submit(form, worker_id="fast")
                with lock:
                    task_ids.append(tid)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=submit_one) for _ in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == [], f"Thread errors: {errors}"
        assert len(task_ids) == 10
        assert len(set(task_ids)) == 10  # all unique


# ---------------------------------------------------------------------------
# Async engine test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_async_coroutine_handler():
    """submit_async awaits coroutine handlers correctly."""

    async def async_handler(form: WorkForm) -> str:
        await asyncio.sleep(0)
        return "async_result"

    engine = WorkEngine()
    defn = WorkerDefinition(
        definition_id="async_worker",
        name="Async Worker",
        input_form="f",
        output_form="f",
        handler="tests.work.test_work_system._echo_handler",
    )
    engine.register_worker(defn, async_handler)
    form = fill_form(_make_form({"x": {"type": "str"}}), {"x": "test"})
    task_id = await engine.submit_async(form, worker_id="async_worker")
    result = engine.get_result(task_id)
    assert result is not None
    assert result.success is True
    assert result.value == "async_result"
