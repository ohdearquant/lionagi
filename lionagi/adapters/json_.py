# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""JSON adapter — inlined from pydapter.adapters.json_."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ._base import (
    Adapter,
    AdapterBase,
    AdapterError,
    AdapterValidationError,
    dispatch_adapt_meth,
)

T = TypeVar("T", bound=BaseModel)


class JsonAdapter(AdapterBase, Adapter[T]):
    """JSON adapter for Python objects (files, strings, bytes)."""

    adapter_key = "json"
    obj_key = "json"

    parse_errors = (json.JSONDecodeError,)

    @classmethod
    def _read_obj_to_json(cls, obj: str | bytes | Path, **kw) -> dict | list:
        text = None
        if isinstance(obj, Path):
            try:
                text = obj.read_text()
            except Exception as e:
                cls._handle_error(e, "resource", resource=str(obj))
        else:
            text = obj.decode("utf-8") if isinstance(obj, bytes) else obj

        if not text or (isinstance(text, str) and not text.strip()):
            cls._handle_error(ValueError("Empty JSON content"), "parse", source=text)

        try:
            return json.loads(text, **kw)
        except cls.parse_errors as e:
            cls._handle_error(
                e,
                "parse",
                source=text,
                position=e.pos,
                line=e.lineno,
                column=e.colno,
            )

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
                return [dispatch_adapt_meth(adapt_meth, i, adapt_kw, subj_cls) for i in data]
            return dispatch_adapt_meth(adapt_meth, data, adapt_kw, subj_cls)
        except validation_errors as e:
            cls._handle_error(e, "validation", data=data, errors=e.errors())

    @classmethod
    def from_obj(
        cls,
        subj_cls: type[T],
        obj: str | bytes | Path,
        /,
        *,
        many: bool = False,
        adapt_meth: str | Callable = "model_validate",
        adapt_kw: dict | None = None,
        validation_errors: tuple[type[Exception], ...] = (ValidationError,),
        **kw,
    ) -> T | list[T]:
        try:
            json_data = cls._read_obj_to_json(obj, **kw)
            if many and not isinstance(json_data, list):
                raise AdapterValidationError(
                    "Expected JSON array for many=True",
                    adapter="json",
                    data=json_data,
                    details={"data_type": type(json_data).__name__, "expected": "list"},
                )
            return cls._validate(json_data, subj_cls, many, adapt_meth, adapt_kw, validation_errors)
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
            return "[]" if many else "{}"
        json_kwargs = {"indent": 2, "sort_keys": True, "ensure_ascii": False, **kw}
        if many:
            payload = [dispatch_adapt_meth(adapt_meth, i, adapt_kw, type(i)) for i in items]
        else:
            payload = dispatch_adapt_meth(adapt_meth, items[0], adapt_kw, type(items[0]))
        return json.dumps(payload, **json_kwargs)


__all__ = ("JsonAdapter",)
