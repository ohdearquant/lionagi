# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""TOML adapter — inlined from pydapter.adapters.toml_."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import toml
from pydantic import BaseModel, ValidationError

from ._base import (
    Adapter,
    AdapterBase,
    AdapterError,
    dispatch_adapt_meth,
)

T = TypeVar("T", bound=BaseModel)


def _ensure_list(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict) and len(d) == 1 and isinstance(next(iter(d.values())), list):
        return next(iter(d.values()))
    return [d]


class TomlAdapter(AdapterBase, Adapter[T]):
    """TOML adapter for Python objects."""

    adapter_key = "toml"
    obj_key = "toml"

    parse_errors = (toml.TomlDecodeError,)

    @classmethod
    def _read_obj_to_toml(cls, obj: str | Path, **kw) -> dict | list:
        text = None
        if isinstance(obj, Path):
            try:
                text = obj.read_text()
            except Exception as e:
                cls._handle_error(e, "resource", resource=str(obj))
        else:
            text = obj

        if not text or (isinstance(text, str) and not text.strip()):
            cls._handle_error(ValueError("Empty TOML content"), "parse", source=text)

        try:
            return toml.loads(text, **kw)
        except cls.parse_errors as e:
            cls._handle_error(e, "parse", source=text)

    @classmethod
    def _validate(
        cls,
        data: dict | list,
        subj_cls: type[T],
        many: bool,
        adapt_meth: str | Callable,
        adapt_kw: dict | None,
        validation_errors: tuple[type[Exception], ...],
    ) -> T | list[T]:
        try:
            if many:
                data_list = _ensure_list(data)
                return [dispatch_adapt_meth(adapt_meth, i, adapt_kw, subj_cls) for i in data_list]
            return dispatch_adapt_meth(adapt_meth, data, adapt_kw, subj_cls)
        except validation_errors as e:
            cls._handle_error(e, "validation", data=data, errors=e.errors())

    @classmethod
    def from_obj(
        cls,
        subj_cls: type[T],
        obj: str | Path,
        /,
        *,
        many: bool = False,
        adapt_meth: str | Callable = "model_validate",
        adapt_kw: dict | None = None,
        validation_errors: tuple[type[Exception], ...] = (ValidationError,),
        **kw,
    ) -> T | list[T]:
        try:
            toml_data = cls._read_obj_to_toml(obj, **kw)
            return cls._validate(toml_data, subj_cls, many, adapt_meth, adapt_kw, validation_errors)
        except AdapterError:
            raise
        except Exception as e:
            cls._handle_error(e, "parse", unexpected=True)

    @classmethod
    def to_obj(
        cls,
        subj: T | list[T],
        /,
        *,
        many: bool = False,
        adapt_meth: str | Callable = "model_dump",
        adapt_kw: dict | None = None,
        **kw,
    ) -> str:
        items = subj if isinstance(subj, list) else [subj]
        if not items:
            return ""
        if many:
            payload = {
                "items": [dispatch_adapt_meth(adapt_meth, i, adapt_kw, type(i)) for i in items]
            }
        else:
            payload = dispatch_adapt_meth(adapt_meth, items[0], adapt_kw, type(items[0]))
        return toml.dumps(payload, **kw)


__all__ = ("TomlAdapter",)
