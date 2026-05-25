# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CSV adapter — inlined from pydapter.adapters.csv_."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ._base import (
    Adapter,
    AdapterBase,
    AdapterError,
    dispatch_adapt_meth,
)

T = TypeVar("T", bound=BaseModel)


class CsvAdapter(AdapterBase, Adapter[T]):
    """CSV adapter for Python objects."""

    adapter_key = "csv"
    obj_key = "csv"

    parse_errors = (csv.Error,)

    @classmethod
    def _read_obj_to_csv(cls, obj: str | Path, **kw) -> tuple[list[dict], list[str]]:
        if isinstance(obj, Path):
            try:
                text = obj.read_text()
            except Exception as e:
                cls._handle_error(e, "resource", resource=str(obj))
        else:
            text = obj

        text = text.replace("\0", "")
        if not text.strip():
            cls._handle_error(ValueError("Empty CSV content"), "parse", source=text)

        try:
            reader = csv.DictReader(io.StringIO(text), **kw)
            rows = list(reader)
            fieldnames = list(reader.fieldnames) if reader.fieldnames else []
            if not fieldnames:
                cls._handle_error(ValueError("CSV has no headers"), "parse", source=text)
            return rows, fieldnames
        except cls.parse_errors as e:
            cls._handle_error(e, "parse", source=text)

    @classmethod
    def _validate(
        cls,
        rows: list[dict],
        fieldnames: list[str],
        subj_cls: type[T],
        many: bool,
        adapt_meth: str | Callable,
        adapt_kw: dict | None,
        validation_errors: tuple[type[Exception], ...],
    ) -> T | list[T]:
        required_fields = [
            field for field, info in subj_cls.model_fields.items() if info.is_required()
        ]
        missing_fields = [f for f in required_fields if f not in fieldnames]
        if missing_fields:
            cls._handle_error(
                ValueError(f"CSV missing required fields: {', '.join(missing_fields)}"),
                "parse",
                fields=missing_fields,
            )

        try:
            result = []
            for i, row in enumerate(rows):
                try:
                    result.append(dispatch_adapt_meth(adapt_meth, row, adapt_kw, subj_cls))
                except validation_errors as e:
                    cls._handle_error(
                        e,
                        "validation",
                        data=row,
                        row=i + 1,
                        errors=e.errors() if hasattr(e, "errors") else str(e),
                    )
            if len(result) == 1 and not many:
                return result[0]
            return result
        except validation_errors as e:
            cls._handle_error(e, "validation", data=rows, errors=e.errors())

    @classmethod
    def from_obj(
        cls,
        subj_cls: type[T],
        obj: str | Path,
        /,
        *,
        many: bool = True,
        adapt_meth: str | Callable = "model_validate",
        adapt_kw: dict | None = None,
        validation_errors: tuple[type[Exception], ...] = (ValidationError,),
        **kw,
    ) -> T | list[T]:
        try:
            rows, fieldnames = cls._read_obj_to_csv(obj, **kw)
            if not rows:
                return [] if many else None
            return cls._validate(
                rows, fieldnames, subj_cls, many, adapt_meth, adapt_kw, validation_errors
            )
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
        data = []
        for item in items:
            row = dispatch_adapt_meth(adapt_meth, item, adapt_kw, type(item))
            data.append(
                {k: v.replace("\0", "") if isinstance(v, str) else v for k, v in row.items()}
            )
        buf = io.StringIO()
        fieldnames = list(data[0].keys())
        writer = csv.DictWriter(buf, fieldnames=fieldnames, **kw)
        writer.writeheader()
        writer.writerows(data)
        return buf.getvalue()


__all__ = ("CsvAdapter",)
