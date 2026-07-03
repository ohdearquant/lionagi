# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest

from lionagi.lndl.errors import (
    InvalidConstructorError,
    LNDLError,
    MissingFieldError,
    MissingLvarError,
    TypeMismatchError,
)


def test_lndl_error_is_exception():
    err = LNDLError("base error")
    assert isinstance(err, Exception)
    assert str(err) == "base error"


def test_missing_lvar_error_is_lndl_error():
    err = MissingLvarError("lvar 'x' not found")
    assert isinstance(err, LNDLError)
    assert "lvar 'x'" in str(err)


def test_missing_field_error_is_lndl_error():
    err = MissingFieldError("field 'name' missing")
    assert isinstance(err, LNDLError)
    assert isinstance(err, MissingFieldError)


def test_type_mismatch_error_is_lndl_error():
    err = TypeMismatchError("got str, expected int")
    assert isinstance(err, LNDLError)


def test_invalid_constructor_error_is_lndl_error():
    err = InvalidConstructorError("bad constructor")
    assert isinstance(err, LNDLError)


def test_errors_raise_correctly():
    with pytest.raises(LNDLError):
        raise LNDLError("test")

    with pytest.raises(MissingLvarError):
        raise MissingLvarError("x")

    with pytest.raises(MissingFieldError):
        raise MissingFieldError("y")

    with pytest.raises(TypeMismatchError):
        raise TypeMismatchError("z")

    with pytest.raises(InvalidConstructorError):
        raise InvalidConstructorError("c")


def test_error_hierarchy_catch_by_base():
    with pytest.raises(LNDLError):
        raise MissingLvarError("lvar")

    with pytest.raises(LNDLError):
        raise MissingFieldError("field")
