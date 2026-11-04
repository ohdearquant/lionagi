"""DataFrame conversion errors report the failed input accurately."""

from typing import Any

import pytest
from pydantic import BaseModel

from lionagi.adapters._base import AdapterParseError, AdapterValidationError
from lionagi.adapters.pandas_ import DataFrameAdapter


def test_batch_error_describes_the_record_that_failed():
    pd = pytest.importorskip("pandas")

    class Record(BaseModel):
        amount: int
        payload: Any

    frame = pd.DataFrame(
        [
            {"amount": 1, "payload": {"a": 1}},
            {"amount": "bad", "payload": [1, 2]},
            {"amount": 3, "payload": [3]},
        ]
    )
    with pytest.raises(AdapterValidationError) as captured:
        DataFrameAdapter.from_obj(Record, frame, many=True)

    assert captured.value.details["data"] == {
        "item_0": "<redacted>",
        "item_1": ["<redacted>", "<redacted>"],
    }


def test_small_frame_conversion_failure_has_no_invented_shape_or_truncation():
    pd = pytest.importorskip("pandas")

    class BadFrame(pd.DataFrame):
        def to_dict(self, *args, **kwargs):
            raise RuntimeError("conversion failed")

    class Record(BaseModel):
        amount: int

    with pytest.raises(AdapterValidationError) as captured:
        DataFrameAdapter.from_obj(Record, BadFrame([{"amount": 1}]), many=True)

    assert not captured.value.details.get("truncated", False)
    assert "data" not in captured.value.details


def test_record_shape_uses_failing_record_without_pandas():
    class Record(BaseModel):
        amount: int
        payload: Any

    records = [
        {"amount": 1, "payload": {"a": 1}},
        {"amount": "bad", "payload": [1, 2]},
        {"amount": 3, "payload": [3]},
    ]
    with pytest.raises(AdapterValidationError) as captured:
        DataFrameAdapter._validate_and_convert_models(
            Record, records, True, "model_validate", None, ()
        )
    assert captured.value.details["data"]["item_1"] == ["<redacted>", "<redacted>"]


def test_unlisted_iterator_exception_is_not_inspected_or_replaced():
    class HostileError(AdapterParseError):
        def __getattribute__(self, name):
            if name == "__class__":
                raise RuntimeError("exception was inspected")
            return super().__getattribute__(name)

    original = HostileError("iterator failure")

    class Interrupted(list):
        def __iter__(self):
            raise original

    class Record(BaseModel):
        amount: int

    result = None
    try:
        DataFrameAdapter._validate_and_convert_models(
            Record, Interrupted(), True, "model_validate", None, (TypeError,)
        )
    except AdapterParseError as captured:
        result = captured
    except RuntimeError:
        pass
    assert result is original


@pytest.mark.parametrize("many", [True, False], ids=["valid-batch", "invalid-single"])
def test_validation_error_types_tuple_is_not_iterated(many):
    pd = pytest.importorskip("pandas")

    class Record(BaseModel):
        amount: int

    class ErrorTypes(tuple):
        calls = 0

        def __iter__(self):
            type(self).calls += 1
            raise RuntimeError("exception types were iterated")

    errors = ErrorTypes((ValueError,))
    frame = pd.DataFrame([{"amount": 1 if many else "invalid"}])
    if many:
        assert DataFrameAdapter.from_obj(Record, frame, many=True, validation_errors=errors) == [
            Record(amount=1)
        ]
    else:
        with pytest.raises(AdapterValidationError) as captured:
            DataFrameAdapter.from_obj(Record, frame, many=False, validation_errors=errors)
        assert captured.value.details["original_exception"] == "validation"
        assert captured.value.details["errors"] == [{"count": 1}]
        assert captured.value.details["data"] == {"item_0": "<redacted>"}
    assert ErrorTypes.calls == 0


@pytest.mark.parametrize("many", [True, False], ids=["valid-batch", "invalid-single"])
def test_validation_error_types_tuple_without_pandas(many):
    class Record(BaseModel):
        amount: int

    class ErrorTypes(tuple):
        calls = 0

        def __iter__(self):
            type(self).calls += 1
            raise RuntimeError("exception types were iterated")

    errors = ErrorTypes((ValueError,))
    record = {"amount": 1 if many else "invalid"}
    if many:
        assert DataFrameAdapter._validate_and_convert_models(
            Record, [record], True, "model_validate", None, errors
        ) == [Record(amount=1)]
    else:
        with pytest.raises(AdapterValidationError) as captured:
            DataFrameAdapter._validate_and_convert_models(
                Record, record, False, "model_validate", None, errors
            )
        assert captured.value.details["errors"] == [{"count": 1}]
    assert ErrorTypes.calls == 0


@pytest.mark.parametrize("stage", ["structure", "records", "constructor"])
def test_conversion_failure_has_no_invented_data_or_truncation(monkeypatch, stage):
    from lionagi.adapters import pandas_ as adapter

    class BadFrame:
        @property
        def empty(self):
            raise RuntimeError("private value")

        def to_dict(self, **kwargs):
            raise RuntimeError("private value")

    class Pandas:
        @staticmethod
        def DataFrame(*args, **kwargs):
            raise RuntimeError("private value")

    monkeypatch.setattr(adapter, "_require_pandas", lambda: Pandas)
    with pytest.raises(AdapterValidationError) as captured:
        if stage == "structure":
            DataFrameAdapter._validate_dataframe_structure(BadFrame(), many=True)
        elif stage == "records":
            DataFrameAdapter._convert_dataframe_to_records(BadFrame(), many=True)
        else:
            DataFrameAdapter._models_to_dataframe([{"amount": 1}])
    assert "data" not in captured.value.details
    assert not captured.value.details.get("truncated", False)
    assert "private value" not in str(captured.value)
