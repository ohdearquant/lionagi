# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""DataFrame adapter (inlined from pydapter.extras.pandas_); pandas is an optional dependency."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from ._base import (
    Adapter,
    AdapterBase,
    AdapterError,
    AdapterResourceError,
    AdapterValidationError,
    dispatch_adapt_meth,
)

T = TypeVar("T", bound=BaseModel)


def _require_pandas():
    try:
        import pandas as pd

        return pd
    except ImportError as e:
        raise ImportError(
            "pandas is required for DataFrame/Series adapters. "
            "Install it with: pip install pandas  or  uv add pandas"
        ) from e


class DataFrameAdapter(AdapterBase, Adapter[T]):
    """Adapter for converting between Pydantic models and pandas DataFrames."""

    adapter_key = "pd.DataFrame"
    obj_key = "pd.DataFrame"

    @classmethod
    def _validate_dataframe_structure(
        cls,
        df: Any,  # pd.DataFrame
        many: bool,
        required_columns: list[str] | None = None,
    ) -> None:
        _require_pandas()
        try:
            if df.empty:
                if many:
                    return
                raise AdapterResourceError(
                    "Cannot convert empty DataFrame to single model instance (many=False)",
                    resource="DataFrame",
                )
            if required_columns:
                missing_cols = set(required_columns) - set(df.columns)
                if missing_cols:
                    raise AdapterValidationError(
                        f"DataFrame is missing required columns: {sorted(missing_cols)}",
                        data={"columns": list(df.columns), "required": required_columns},
                    )
        except (AdapterResourceError, AdapterValidationError):
            raise
        except Exception as e:
            cls._handle_error(e, "validation", data={"columns": list(df.columns)})

    @classmethod
    def _convert_dataframe_to_records(cls, df: Any, many: bool) -> list[dict] | dict:
        try:
            if many:
                return df.to_dict(orient="records")
            return df.iloc[0].to_dict()
        except IndexError as e:
            cls._handle_error(
                e,
                "resource",
                resource="DataFrame",
                message="Cannot access first row of empty DataFrame",
            )
        except Exception as e:
            cls._handle_error(e, "validation", data={"shape": df.shape})

    @classmethod
    def _validate_and_convert_models(
        cls,
        subj_cls: type[T],
        records: list[dict] | dict,
        many: bool,
        adapt_meth: str | Callable,
        adapt_kw: dict | None,
        validation_errors: tuple[type[Exception], ...],
    ) -> T | list[T]:
        try:
            if many:
                return [dispatch_adapt_meth(adapt_meth, r, adapt_kw, subj_cls) for r in records]
            return dispatch_adapt_meth(adapt_meth, records, adapt_kw, subj_cls)
        except validation_errors as e:
            cls._handle_error(
                e,
                "validation",
                data=records[0] if many and isinstance(records, list) else records,
                errors=e.errors() if hasattr(e, "errors") else None,
            )

    @classmethod
    def _models_to_dataframe(
        cls,
        items: list[T],
        adapt_meth: str | Callable,
        adapt_kw: dict | None,
        **kw,
    ) -> Any:  # pd.DataFrame
        pd = _require_pandas()
        try:
            records = [dispatch_adapt_meth(adapt_meth, i, adapt_kw, type(i)) for i in items]
            return pd.DataFrame(records, **kw)
        except Exception as e:
            cls._handle_error(e, "validation", data={"item_count": len(items)})

    @classmethod
    def from_obj(
        cls,
        subj_cls: type[T],
        obj: Any,  # pd.DataFrame
        /,
        *,
        many: bool = True,
        adapt_meth: str | Callable = "model_validate",
        adapt_kw: dict | None = None,
        validation_errors: tuple[type[Exception], ...] = None,
        required_columns: list[str] | None = None,
        **kw: Any,
    ) -> T | list[T]:
        from pydantic import ValidationError as PydanticValidationError

        if validation_errors is None:
            validation_errors = (PydanticValidationError,)

        try:
            cls._validate_dataframe_structure(obj, many, required_columns)
            if obj.empty and many:
                return []
            records = cls._convert_dataframe_to_records(obj, many)
            return cls._validate_and_convert_models(
                subj_cls, records, many, adapt_meth, adapt_kw, validation_errors
            )
        except AdapterError:
            raise
        except Exception as e:
            cls._handle_error(e, "validation", unexpected=True)

    @classmethod
    def to_obj(
        cls,
        subj: T | list[T],
        /,
        *,
        many: bool = True,
        adapt_meth: str | Callable = "model_dump",
        adapt_kw: dict | None = None,
        **kw: Any,
    ) -> Any:  # pd.DataFrame
        try:
            items = subj if isinstance(subj, list) else [subj]
            return cls._models_to_dataframe(items, adapt_meth, adapt_kw, **kw)
        except AdapterError:
            raise
        except Exception as e:
            cls._handle_error(e, "validation", unexpected=True)


__all__ = ("DataFrameAdapter",)
