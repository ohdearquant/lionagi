# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Adapter-generated failures do not retain rejected data."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from lionagi.adapters._base import (
    AdapterParseError,
    AdapterRegistry,
    AdapterValidationError,
    _safe_adapter_data,
)
from lionagi.adapters.csv_ import CsvAdapter
from lionagi.adapters.json_ import JsonAdapter
from lionagi.adapters.toml_ import TomlAdapter

_MARKER = "synthetic-private-marker-4817"


class _Trap:
    renders = 0

    def __str__(self):
        self.renders += 1
        return _MARKER


_TRAP = _Trap()


class _Validated(BaseModel):
    amount: str

    @field_validator("amount")
    @classmethod
    def reject(cls, value):
        raise PydanticCustomError("bad_amount", "rejected {value}", {"value": _TRAP})


def _contains_marker(value, seen=None):
    if seen is None:
        seen = set()
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, str):
        return _MARKER in value
    if isinstance(value, dict):
        return any(_contains_marker(k, seen) or _contains_marker(v, seen) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_marker(v, seen) for v in value)
    if isinstance(value, BaseException):
        return any(
            _contains_marker(item, seen)
            for item in (
                str(value),
                repr(value),
                value.args,
                getattr(value, "details", None),
                value.__cause__,
                value.__context__,
            )
        )
    return False


@pytest.mark.parametrize("kind", ["json", "toml", "csv", "dataframe"])
def test_validation_failure_does_not_expose_input_or_render_context(kind):
    _TRAP.renders = 0
    if kind == "json":
        action = lambda: JsonAdapter.from_obj(_Validated, json.dumps({"amount": _MARKER}))
    elif kind == "toml":
        action = lambda: TomlAdapter.from_obj(_Validated, f'amount = "{_MARKER}"')
    elif kind == "csv":
        action = lambda: CsvAdapter.from_obj(_Validated, f"amount\n{_MARKER}\n")
    else:
        pd = pytest.importorskip("pandas")
        from lionagi.adapters.pandas_ import DataFrameAdapter

        action = lambda: DataFrameAdapter.from_obj(_Validated, pd.DataFrame([{"amount": _MARKER}]))
    with pytest.raises(AdapterValidationError) as captured:
        action()
    if _contains_marker(captured.value):
        pytest.fail("adapter error exposed rejected input or an original exception", pytrace=False)
    assert _TRAP.renders == 0


@pytest.mark.parametrize("kind", ["json", "toml", "csv"])
def test_unlisted_pydantic_validation_error_does_not_expose_input(kind):
    class Record(BaseModel):
        amount: int

    adapters = {
        "json": (JsonAdapter, json.dumps({"amount": _MARKER})),
        "toml": (TomlAdapter, f'amount = "{_MARKER}"'),
        "csv": (CsvAdapter, f"amount\n{_MARKER}\n"),
    }
    adapter, source = adapters[kind]
    with pytest.raises(AdapterParseError) as captured:
        adapter.from_obj(Record, source, validation_errors=(TypeError,))
    if _contains_marker(captured.value):
        pytest.fail("unlisted validation error exposed rejected input", pytrace=False)
    expected_data = {"item_0": "<redacted>"}
    assert captured.value.data == ([expected_data] if kind == "csv" else expected_data)
    assert captured.value.message == "Validation failed"
    assert captured.value.details["category"] == "parse"
    assert captured.value.details["unexpected"] is True
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("kind", ["json", "toml", "csv"])
def test_unexpected_non_validation_error_keeps_existing_diagnostic(kind):
    adapters = {
        "json": (JsonAdapter, '{"amount": "value"}'),
        "toml": (TomlAdapter, 'amount = "value"'),
        "csv": (CsvAdapter, "amount\nvalue\n"),
    }
    adapter, source = adapters[kind]

    def reject(_):
        raise RuntimeError("unexpected conversion")

    with pytest.raises(AdapterParseError) as captured:
        adapter.from_obj(_Validated, source, adapt_meth=reject, validation_errors=(TypeError,))
    assert captured.value.message == "unexpected conversion"
    assert captured.value.details["unexpected"] is True
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert captured.value.__context__ is captured.value.__cause__


@pytest.mark.parametrize("kind", ["json", "toml", "csv"])
def test_unlisted_model_validator_exception_keeps_message_and_cause(kind):
    class Record(BaseModel):
        amount: str

        @field_validator("amount")
        @classmethod
        def fail(cls, value):
            raise RuntimeError("validator error")

    adapters = {
        "json": (JsonAdapter, '{"amount": "value"}'),
        "toml": (TomlAdapter, 'amount = "value"'),
        "csv": (CsvAdapter, "amount\nvalue\n"),
    }
    adapter, source = adapters[kind]
    with pytest.raises(AdapterParseError) as captured:
        adapter.from_obj(Record, source)
    assert captured.value.message == "validator error"
    assert isinstance(captured.value.__cause__, RuntimeError)


def test_pydantic_diagnostic_keeps_count_but_omits_error_details():
    class Record(BaseModel):
        amount: int
        required: int

    with pytest.raises(AdapterValidationError) as captured:
        JsonAdapter.from_obj(Record, json.dumps({"amount": _MARKER}))
    error = captured.value
    assert error.errors == [{"count": 2}]
    if _contains_marker(error):
        pytest.fail("validation diagnostic exposed a rejected value", pytrace=False)


def test_custom_pydantic_error_type_does_not_expose_data_or_render_context():
    class Record(BaseModel):
        amount: str

        @field_validator("amount")
        @classmethod
        def fail(cls, value):
            raise PydanticCustomError(value, "bad {value}", {"value": _TRAP})

    _TRAP.renders = 0
    with pytest.raises(AdapterValidationError) as captured:
        JsonAdapter.from_obj(Record, json.dumps({"amount": _MARKER}))
    assert captured.value.errors == [{"count": 1}]
    if _contains_marker(captured.value):
        pytest.fail("custom error type exposed a value", pytrace=False)
    assert _TRAP.renders == 0


def test_json_many_non_array_does_not_expose_data():
    with pytest.raises(AdapterValidationError) as captured:
        JsonAdapter.from_obj(_Validated, json.dumps({_MARKER: _MARKER}), many=True)
    if _contains_marker(captured.value):
        pytest.fail("non-array error exposed rejected input", pytrace=False)


def test_json_many_non_array_does_not_expose_dynamic_type_name():
    def decode(_):
        return type(_MARKER, (dict,), {})()

    with pytest.raises(AdapterValidationError) as captured:
        JsonAdapter.from_obj(_Validated, '{"amount": "value"}', many=True, object_hook=decode)
    if _contains_marker(captured.value):
        pytest.fail("non-array error exposed a caller-defined type name", pytrace=False)


def test_opaque_value_does_not_invoke_user_defined_metaclass_methods():
    class HostileMeta(type):
        def __hash__(cls):
            raise AssertionError("must not hash an unknown type")

        def __eq__(cls, other):
            raise AssertionError("must not compare an unknown type")

    class Hostile(metaclass=HostileMeta):
        pass

    assert _safe_adapter_data(Hostile()) == "<redacted>"
    with pytest.raises(AdapterValidationError) as captured:
        JsonAdapter.from_obj(
            _Validated, '{"amount": "value"}', many=True, object_hook=lambda _: Hostile()
        )
    assert captured.value.details["data_type"] == "other"


def test_bounded_data_shapes_replace_dynamic_keys_values_and_cycles():
    cycle = []
    cycle.append(cycle)
    source = {_MARKER: [{_MARKER: (_MARKER,)}, cycle]}
    safe = _safe_adapter_data(source)
    if _contains_marker(safe):
        pytest.fail("sanitized shape retained an input key or value", pytrace=False)
    assert type(safe) is dict
    assert list(safe) == ["item_0"]
    assert type(safe["item_0"]) is list
    assert type(safe["item_0"][0]) is dict
    assert type(safe["item_0"][0]["item_0"]) is tuple
    assert safe["item_0"][1] == ["<cycle>"]


def test_bounded_data_reports_truncation_and_opaque_subclasses():
    class HostileDict(dict):
        def items(self):
            raise AssertionError("must not inspect subclasses")

        def __str__(self):
            raise AssertionError("must not render subclasses")

        def __repr__(self):
            raise AssertionError("must not render subclasses")

    assert _safe_adapter_data(HostileDict({_MARKER: _MARKER})) == "<redacted>"
    wide = _safe_adapter_data([_MARKER] * 200)
    assert "<truncated>" in wide
    if _contains_marker(wide):
        pytest.fail("bounded result retained an input value", pytrace=False)
    deep = _safe_adapter_data([[[[[_MARKER]]]]])
    assert deep == [[[[["<truncated>"]]]]]


def test_custom_validation_exception_is_not_rendered_or_introspected():
    class CustomValidationError(Exception):
        def __str__(self):
            raise AssertionError("must not render custom exceptions")

        def __repr__(self):
            raise AssertionError("must not render custom exceptions")

        def errors(self):
            raise AssertionError("must not inspect custom exceptions")

        def error_count(self):
            raise AssertionError("must not inspect custom exceptions")

    def reject(_):
        raise CustomValidationError(_MARKER)

    with pytest.raises(AdapterValidationError) as captured:
        JsonAdapter.from_obj(
            _Validated,
            json.dumps({"amount": _MARKER}),
            adapt_meth=reject,
            validation_errors=(CustomValidationError,),
        )
    if _contains_marker(captured.value):
        pytest.fail("custom validation error retained caller data", pytrace=False)


def test_csv_outer_validation_catch_does_not_render_or_retain_input():
    _TRAP.renders = 0
    original = None
    try:
        _Validated(amount=_MARKER)
    except ValidationError as error:
        original = error

    class BrokenRows(list):
        def __iter__(self):
            raise original

    with pytest.raises(AdapterValidationError) as captured:
        CsvAdapter._validate(
            BrokenRows(), ["amount"], _Validated, True, "model_validate", None, (ValidationError,)
        )
    if _contains_marker(captured.value):
        pytest.fail("outer validation catch exposed rejected input", pytrace=False)
    assert _TRAP.renders == 0


def test_direct_validation_error_passthrough_and_explicit_adapter_error_are_unchanged():
    original = None
    try:
        _Validated(amount=_MARKER)
    except ValidationError as error:
        original = error

    class CustomAdapter:
        adapter_key = "custom"

        @classmethod
        def from_obj(cls, subj_cls, obj, **kwargs):
            raise original

    registry = AdapterRegistry()
    registry.register(CustomAdapter)
    with pytest.raises(ValidationError) as caught:
        registry.adapt_from(_Validated, {}, obj_key="custom")
    assert caught.value is original
    caller_data = {_MARKER: _MARKER}
    explicit = AdapterValidationError("custom", data=caller_data)
    assert explicit.data is caller_data


def test_validation_error_builder_does_not_retain_original_cause():
    original = None
    try:
        _Validated(amount=_MARKER)
    except ValidationError as error:
        original = error
    result = JsonAdapter._validation_error(original, {"amount": _MARKER})
    if result.__cause__ is not None or result.__context__ is not None:
        pytest.fail("validation builder retained an exception chain", pytrace=False)


def test_dataframe_missing_columns_do_not_expose_names_or_data():
    pd = pytest.importorskip("pandas")
    from lionagi.adapters.pandas_ import DataFrameAdapter

    frame = pd.DataFrame([{_MARKER: "value", _MARKER + "-other": "value"}])
    with pytest.raises(AdapterValidationError) as captured:
        DataFrameAdapter.from_obj(
            _Validated,
            frame,
            required_columns=[_MARKER, _MARKER + "-absent", _MARKER + "-also-absent"],
        )
    if _contains_marker(captured.value):
        pytest.fail("missing-column error exposed column names", pytrace=False)
    assert captured.value.data == {
        "columns": ["<redacted>"] * 2,
        "required": ["<redacted>"] * 3,
    }
    assert captured.value.details["columns_count"] == 2
    assert captured.value.details["required_count"] == 3
    assert not captured.value.details.get("truncated", False)
    assert "2" in captured.value.message


def test_dataframe_missing_column_shape_without_optional_dependency(monkeypatch):
    from lionagi.adapters import pandas_ as pandas_adapter

    class Frame:
        empty = False
        columns = [_MARKER, _MARKER + "-other"]

    monkeypatch.setattr(pandas_adapter, "_require_pandas", lambda: None)
    with pytest.raises(AdapterValidationError) as captured:
        pandas_adapter.DataFrameAdapter._validate_dataframe_structure(
            Frame(), True, [_MARKER, _MARKER + "-absent"]
        )
    assert captured.value.data == {
        "columns": ["<redacted>"] * 2,
        "required": ["<redacted>"] * 2,
    }
    if _contains_marker(captured.value):
        pytest.fail("missing-column error exposed column names", pytrace=False)


def test_dataframe_missing_columns_bounded_with_exact_counts(monkeypatch):
    from lionagi.adapters import pandas_ as pandas_adapter

    class Frame:
        empty = False
        columns = [f"{_MARKER}-column-{i}" for i in range(100)]

    required = [f"{_MARKER}-required-{i}" for i in range(100)]
    monkeypatch.setattr(pandas_adapter, "_require_pandas", lambda: None)
    with pytest.raises(AdapterValidationError) as captured:
        pandas_adapter.DataFrameAdapter._validate_dataframe_structure(Frame(), True, required)
    error = captured.value
    assert set(error.data) == {"columns", "required"}
    assert type(error.data["columns"]) is list
    assert type(error.data["required"]) is list
    assert all(x == "<redacted>" for x in error.data["columns"] + error.data["required"])
    assert 1 + 2 + sum(len(v) for v in error.data.values()) <= 128
    assert error.details["columns_count"] == 100
    assert error.details["required_count"] == 100
    assert error.details["truncated"] is True
    if _contains_marker(error):
        pytest.fail("bounded error exposed column names", pytrace=False)


@pytest.mark.parametrize("required_count,truncated", [(124, False), (125, True)])
def test_dataframe_missing_columns_budget_boundary(monkeypatch, required_count, truncated):
    from lionagi.adapters import pandas_ as pandas_adapter

    class Frame:
        empty = False
        columns = ["present"]

    required = [f"absent-{i}" for i in range(required_count)]
    monkeypatch.setattr(pandas_adapter, "_require_pandas", lambda: None)
    with pytest.raises(AdapterValidationError) as captured:
        pandas_adapter.DataFrameAdapter._validate_dataframe_structure(Frame(), True, required)
    error = captured.value
    assert len(error.data["columns"]) == 1
    assert len(error.data["required"]) == min(required_count, 124)
    assert error.details["required_count"] == required_count
    assert error.details.get("truncated", False) is truncated


@pytest.mark.parametrize("stage", ["structure", "conversion"])
def test_dataframe_internal_validation_error_does_not_expose_record(stage):
    pd = pytest.importorskip("pandas")
    from lionagi.adapters.pandas_ import DataFrameAdapter

    class BadFrame(pd.DataFrame):
        @property
        def empty(self):
            if stage == "structure":
                raise RuntimeError(_MARKER)
            return False

        def to_dict(self, *args, **kwargs):
            raise RuntimeError(_MARKER)

    frame = BadFrame([{_MARKER: _MARKER}])
    with pytest.raises(AdapterValidationError) as captured:
        if stage == "structure":
            DataFrameAdapter._validate_dataframe_structure(frame, many=True)
        else:
            DataFrameAdapter._convert_dataframe_to_records(frame, many=True)
    if _contains_marker(captured.value):
        pytest.fail("internal validation error exposed rejected data", pytrace=False)


@pytest.mark.parametrize("direction", ["from", "to"])
def test_dataframe_unexpected_conversion_error_does_not_expose_record(direction):
    pd = pytest.importorskip("pandas")
    from lionagi.adapters.pandas_ import DataFrameAdapter

    class Record(BaseModel):
        amount: str

    def reject(record):
        raise RuntimeError(_MARKER)

    with pytest.raises(AdapterValidationError) as captured:
        if direction == "from":
            DataFrameAdapter.from_obj(
                Record, pd.DataFrame([{"amount": _MARKER}]), adapt_meth=reject
            )
        else:
            DataFrameAdapter.to_obj(Record(amount=_MARKER), adapt_meth=reject)
    if _contains_marker(captured.value):
        pytest.fail("conversion error exposed rejected data", pytrace=False)


@pytest.mark.parametrize("stage", ["structure", "conversion"])
def test_dataframe_failure_does_not_reinspect_hostile_object(stage):
    pytest.importorskip("pandas")
    from lionagi.adapters.pandas_ import DataFrameAdapter

    class HostileFrame:
        reinspected = False

        @property
        def empty(self):
            raise RuntimeError(_MARKER)

        @property
        def columns(self):
            self.reinspected = True
            raise RuntimeError(_MARKER)

        @property
        def shape(self):
            self.reinspected = True
            raise RuntimeError(_MARKER)

        def to_dict(self, **kwargs):
            raise RuntimeError(_MARKER)

    frame = HostileFrame()
    error = None
    try:
        if stage == "structure":
            DataFrameAdapter._validate_dataframe_structure(frame, many=True)
        else:
            DataFrameAdapter._convert_dataframe_to_records(frame, many=True)
    except Exception as captured:
        error = captured
    if not isinstance(error, AdapterValidationError) or _contains_marker(error):
        pytest.fail("failure handler exposed a caller object or its exception", pytrace=False)
    assert not frame.reinspected


def test_dataframe_public_to_obj_does_not_retain_unexpected_record_error(monkeypatch):
    pytest.importorskip("pandas")
    from lionagi.adapters.pandas_ import DataFrameAdapter

    def reject(cls, items, adapt_meth, adapt_kw, **kwargs):
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(DataFrameAdapter, "_models_to_dataframe", classmethod(reject))
    with pytest.raises(AdapterValidationError) as captured:
        DataFrameAdapter.to_obj([_MARKER])
    if _contains_marker(captured.value):
        pytest.fail("public conversion error retained caller data", pytrace=False)


def test_dataframe_validation_does_not_index_list_subclass_on_failure():
    pytest.importorskip("pandas")
    from lionagi.adapters.pandas_ import DataFrameAdapter

    class HostileRows(list):
        indexed = False

        def __getitem__(self, key):
            self.indexed = True
            raise RuntimeError(_MARKER)

    rows = HostileRows([{"amount": _MARKER}])
    error = None
    try:
        DataFrameAdapter._validate_and_convert_models(
            _Validated, rows, True, "model_validate", None, (ValidationError,)
        )
    except Exception as captured:
        error = captured
    if not isinstance(error, AdapterValidationError) or _contains_marker(error):
        pytest.fail("validation handler indexed caller-controlled rows", pytrace=False)
    assert not rows.indexed


def test_csv_row_metadata_uses_ordinal_not_input_row_id():
    class Row(BaseModel):
        amount: int
        row: str

    source = f"amount,row\n1,first\n{_MARKER},{_MARKER}\n"
    with pytest.raises(AdapterValidationError) as captured:
        CsvAdapter.from_obj(Row, source, many=True)
    assert type(captured.value.details["row"]) is int
    assert captured.value.details["row"] == 2
    if _contains_marker(captured.value):
        pytest.fail("CSV error exposed input row values", pytrace=False)


def test_validation_error_builder_does_not_inspect_custom_exception_class():
    class CustomError(Exception):
        inspected = False

        def __getattribute__(self, name):
            if name == "__class__":
                type(self).inspected = True
                raise RuntimeError(_MARKER)
            return super().__getattribute__(name)

    error = JsonAdapter._validation_error(CustomError(_MARKER), {_MARKER: _MARKER})
    assert not CustomError.inspected
    if _contains_marker(error):
        pytest.fail("error builder exposed a custom exception", pytrace=False)


@pytest.mark.parametrize("direction", ["from", "to"])
def test_dataframe_missing_dependency_preserves_existing_diagnostic(monkeypatch, direction):
    import builtins

    from lionagi.adapters.pandas_ import DataFrameAdapter

    real_import = builtins.__import__

    def without_pandas(name, *args, **kwargs):
        if name == "pandas":
            raise ModuleNotFoundError("No module named 'pandas'", name="pandas")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pandas)
    with pytest.raises(AdapterValidationError) as captured:
        if direction == "from":
            DataFrameAdapter.from_obj(_Validated, object())
        else:
            DataFrameAdapter.to_obj([])
    error = captured.value
    assert "pandas is required" in error.message
    assert "pip install pandas" in error.message
    assert error.details["unexpected"] is True
    assert isinstance(error.__cause__, ImportError)
    assert isinstance(error.__cause__.__cause__, ModuleNotFoundError)
