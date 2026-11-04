# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""DataFrame adapter (inlined from pydapter.extras.pandas_); pandas is an optional dependency."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ._base import (
    _ADAPTER_DATA_NODE_BUDGET,
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
                columns = df.columns
                missing_cols = set(required_columns) - set(columns)
                if missing_cols:
                    column_count = len(columns)
                    required_count = len(required_columns)
                    # The data dict and its two lists consume three nodes.
                    available = _ADAPTER_DATA_NODE_BUDGET - 3
                    shown_columns = min(column_count, available)
                    shown_required = min(required_count, available - shown_columns)
                    details = {"columns_count": column_count, "required_count": required_count}
                    if shown_columns < column_count or shown_required < required_count:
                        details["truncated"] = True
                    raise AdapterValidationError(
                        f"DataFrame is missing {len(missing_cols)} required column(s)",
                        data={
                            "columns": ["<redacted>"] * shown_columns,
                            "required": ["<redacted>"] * shown_required,
                        },
                        details=details,
                    )
        except (AdapterResourceError, AdapterValidationError):
            raise
        except Exception:
            error = cls._record_validation_error()
        else:
            return
        raise error from None

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
        except Exception:
            error = cls._record_validation_error()
        raise error from None

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
        def convert(record):
            try:
                return dispatch_adapt_meth(adapt_meth, record, adapt_kw, subj_cls)
            except validation_errors as e:
                validation_error = e
            except ValidationError as e:
                validation_error = e
            except AdapterError:
                raise
            except Exception as e:
                cls._handle_error(e, "validation", unexpected=True)
            error = cls._validation_error(validation_error, record)
            del validation_error
            raise error from None

        if many:
            return [convert(record) for record in cls._safe_iterator(records, validation_errors)]
        return convert(records)

    @classmethod
    def _safe_iterator(cls, items, validation_errors=None):
        """Wrap iteration failures without intercepting item callbacks."""
        caught = (Exception,) if validation_errors is None else validation_errors
        try:
            yield from items
        except caught as exc:
            if validation_errors is None:
                error = cls._record_validation_error(unexpected=True)
            else:
                error = cls._validation_error(exc, items)
        except ValidationError as exc:
            error = cls._validation_error(exc, items)
        else:
            return
        raise error from None

    @classmethod
    def _models_to_dataframe(
        cls,
        records: list[Any],
        **kw,
    ) -> Any:  # pd.DataFrame
        pd = _require_pandas()

        try:
            return pd.DataFrame(records, **kw)
        except Exception:
            error = cls._record_validation_error()
        raise error from None

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
        validation_errors: tuple[type[Exception], ...] | None = None,
        required_columns: list[str] | None = None,
        **kw: Any,
    ) -> T | list[T]:
        from pydantic import ValidationError as PydanticValidationError

        if validation_errors is None:
            validation_errors = (PydanticValidationError,)

        try:
            try:
                cls._validate_dataframe_structure(obj, many, required_columns)
            except ImportError as e:
                # The optional dependency check precedes record processing.
                cls._handle_error(e, "validation", unexpected=True)
            if obj.empty and many:
                return []
            records = cls._convert_dataframe_to_records(obj, many)
            return cls._validate_and_convert_models(
                subj_cls, records, many, adapt_meth, adapt_kw, validation_errors
            )
        except AdapterError:
            raise
        except Exception:
            error = cls._record_validation_error(unexpected=True)
        raise error from None

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
            try:
                _require_pandas()
            except ImportError as e:
                cls._handle_error(e, "validation", unexpected=True)
        except AdapterError:
            raise
        except Exception:
            error = cls._record_validation_error(unexpected=True)
        else:
            error = None
        if error is not None:
            raise error from None

        records = [
            dispatch_adapt_meth(adapt_meth, item, adapt_kw, type(item))
            for item in cls._safe_iterator(items)
        ]
        try:
            return cls._models_to_dataframe(records, **kw)
        except AdapterError:
            raise
        except Exception:
            error = cls._record_validation_error(unexpected=True)
        raise error from None


__all__ = ("DataFrameAdapter",)
