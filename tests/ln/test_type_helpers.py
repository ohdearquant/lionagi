"""Class and Optional predicates keep their accepted annotation domains."""

import subprocess
import sys
from typing import Annotated, Any, Optional, Union

import pytest
from pydantic import BaseModel

from lionagi.ln import is_pydantic_model_class, is_union_type, unwrap_optional


class Example(BaseModel):
    value: int = 1


class ModelFieldsOnly:
    model_fields = {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (BaseModel, True),
        (Example, True),
        (Example(), False),
        (int, False),
        (ModelFieldsOnly, False),
        (None, False),
        (42, False),
        ("Example", False),
        (list[Example], False),
        (Optional[Example], False),
    ],
)
def test_pydantic_class_check_matches_original_predicate(value, expected):
    assert is_pydantic_model_class(value) is expected
    assert is_pydantic_model_class(value) == (
        isinstance(value, type) and issubclass(value, BaseModel)
    )


def test_pydantic_class_check_handles_type_error(monkeypatch):
    import pydantic

    class RejectSubclass(type):
        def __subclasscheck__(cls, subclass):
            raise TypeError("unsupported subclass check")

    class RejectingModel(metaclass=RejectSubclass):
        pass

    monkeypatch.setattr(pydantic, "BaseModel", RejectingModel)
    assert is_pydantic_model_class(Example) is False


def test_pydantic_class_check_handles_instance_type_error():
    class UnavailableClass:
        @property
        def __class__(self):
            raise TypeError("class unavailable")

    assert is_pydantic_model_class(UnavailableClass()) is False


def test_type_helpers_preserve_lazy_pydantic_import():
    code = (
        "import sys\n"
        "from lionagi.ln import is_pydantic_model_class, unwrap_optional\n"
        "assert 'pydantic' not in sys.modules\n"
        "assert unwrap_optional(int | None) is int\n"
        "assert is_pydantic_model_class(42) is False\n"
        "assert 'pydantic' not in sys.modules\n"
        "assert is_pydantic_model_class(int) is False\n"
        "assert 'pydantic' in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (Optional[Example], Example),
        (Example | None, Example),
        (None | int, int),
        (Optional[str], str),
        (Union[int, str, None], Union[int, str, None]),
        (int | str, int | str),
        (int, int),
        (Example(), None),
        (Any, Any),
        (list[int], list[int]),
        (None, None),
        (type(None), type(None)),
        (Annotated[Optional[int], "metadata"], Annotated[Optional[int], "metadata"]),
        (Optional[Annotated[int, "metadata"]], Annotated[int, "metadata"]),
    ],
)
def test_unwrap_optional_preserves_other_annotations(annotation, expected):
    if isinstance(annotation, Example):
        expected = annotation
    assert unwrap_optional(annotation) == expected


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (Optional[int], True),
        (int | None, True),
        (int | str, True),
        (int, False),
        (42, False),
        (Annotated[Optional[int], "metadata"], False),
    ],
)
def test_union_check_distinguishes_wrappers_and_values(annotation, expected):
    assert is_union_type(annotation) is expected
