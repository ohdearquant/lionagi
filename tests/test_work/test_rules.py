from __future__ import annotations

from lionagi.work import ChoiceSet, NumericRange, StringPattern


def test_numeric_range():
    rule = NumericRange(min=0, max=10)
    assert rule.validate(5, {}).is_valid
    assert not rule.validate(20, {}).is_valid


def test_string_pattern():
    rule = StringPattern(pattern=r"^[a-z]+$")
    assert rule.validate("hello", {}).is_valid
    assert not rule.validate("Hello", {}).is_valid


def test_choice_set():
    rule = ChoiceSet(choices=["red", "green", "blue"])
    assert rule.validate("red", {}).is_valid
    assert not rule.validate("yellow", {}).is_valid
