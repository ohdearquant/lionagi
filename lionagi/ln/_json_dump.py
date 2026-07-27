# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""JSON serialization utilities built on orjson with configurable type handling and NDJSON streaming."""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum
from functools import lru_cache
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal, overload
from uuid import UUID

import orjson

__all__ = [
    "get_orjson_default",
    "make_options",
    "json_dumpb",
    "json_dumps",
    "json_lines_iter",
]

# Types orjson serializes natively; routed through default() only when passthrough is requested.
_NATIVE = (dt.datetime, dt.date, dt.time, UUID)
_SERIALIZATION_METHODS = ("model_dump", "to_dict", "dict")

# --------- helpers ------------------------------------------------------------

_ADDR_PAT = re.compile(r" at 0x[0-9A-Fa-f]+")


def _clip(s: str, limit: int = 2048) -> str:
    return shorten(s, width=limit, placeholder=f"...(+{len(s) - limit} chars)")  # type: ignore[arg-type]


def _normalize_for_sorting(x: Any) -> str:
    """Normalize repr/str to remove process-specific addresses."""
    s = str(x)
    return _ADDR_PAT.sub(" at 0x?", s)


def _stable_sorted_iterable(o: Iterable[Any]) -> list[Any]:
    """Deterministic ordering for sets (incl. mixed types); key=(class name,
    normalized str) avoids cross-type comparisons and address variance."""
    return sorted(o, key=lambda x: (x.__class__.__name__, _normalize_for_sorting(x)))


def _safe_exception_payload(ex: Exception) -> dict[str, str]:
    return {"type": ex.__class__.__name__, "message": str(ex)}


def _default_serializers(
    deterministic_sets: bool,
    decimal_as_float: bool,
    enum_as_name: bool,
    passthrough_datetime: bool,
) -> dict[type, Callable[[Any], Any]]:
    ser: dict[type, Callable[[Any], Any]] = {
        Path: str,
        decimal.Decimal: (float if decimal_as_float else str),
        set: (_stable_sorted_iterable if deterministic_sets else list),
        frozenset: (_stable_sorted_iterable if deterministic_sets else list),
    }
    if enum_as_name:
        ser[Enum] = lambda e: e.name
    # Only needed if you also set OPT_PASSTHROUGH_DATETIME via options.
    if passthrough_datetime:
        ser[dt.datetime] = lambda o: o.isoformat()
    return ser


# --------- default() factory --------------------------------------------------


def get_orjson_default(
    *,
    order: list[type] | None = None,
    additional: Mapping[type, Callable[[Any], Any]] | None = None,
    extend_default: bool = True,
    deterministic_sets: bool = False,
    decimal_as_float: bool = False,
    enum_as_name: bool = False,
    passthrough_datetime: bool = False,
    safe_fallback: bool = False,
    fallback_clip: int = 2048,
) -> Callable[[Any], Any]:
    """Build an extensible default= callable for orjson.dumps with set/Decimal/Enum/datetime handling."""
    ser = _default_serializers(
        deterministic_sets=deterministic_sets,
        decimal_as_float=decimal_as_float,
        enum_as_name=enum_as_name,
        passthrough_datetime=passthrough_datetime,
    )
    if additional:
        ser.update(additional)

    base_order: list[type] = [Path, decimal.Decimal, set, frozenset]
    if enum_as_name:
        base_order.insert(0, Enum)
    if passthrough_datetime:
        base_order.insert(0, dt.datetime)

    if order:
        order_ = (
            (base_order + [t for t in order if t not in base_order])
            if extend_default
            else list(order)
        )
    else:
        order_ = base_order.copy()

    if not passthrough_datetime:
        # Avoid checks for types already on the orjson native fast path.
        order_ = [t for t in order_ if t not in _NATIVE]

    order_tuple = tuple(order_)
    cache: dict[type, Callable[[Any], Any]] = {}

    def default(obj: Any) -> Any:
        typ = obj.__class__
        func = cache.get(typ)
        if func is None:
            for typ_cls in order_tuple:
                if issubclass(typ, typ_cls):
                    f = ser.get(typ_cls)
                    if f:
                        cache[typ] = f
                        func = f
                        break
            else:
                # Duck-typed support for common data holders
                for m in _SERIALIZATION_METHODS:
                    md = getattr(obj, m, None)
                    if callable(md):
                        with contextlib.suppress(Exception):
                            return md()
                if safe_fallback:
                    if isinstance(obj, Exception):
                        return _safe_exception_payload(obj)
                    return _clip(repr(obj), fallback_clip)
                raise TypeError(f"Type is not JSON serializable: {typ.__name__}")
        return func(obj)

    return default


@lru_cache(maxsize=128)
def _cached_default(
    deterministic_sets: bool,
    decimal_as_float: bool,
    enum_as_name: bool,
    passthrough_datetime: bool,
    safe_fallback: bool,
    fallback_clip: int,
):
    return get_orjson_default(
        deterministic_sets=deterministic_sets,
        decimal_as_float=decimal_as_float,
        enum_as_name=enum_as_name,
        passthrough_datetime=passthrough_datetime,
        safe_fallback=safe_fallback,
        fallback_clip=fallback_clip,
    )


# --------- defaults & options -------------------------------------------------


def make_options(
    *,
    pretty: bool = False,
    sort_keys: bool = False,
    naive_utc: bool = False,
    utc_z: bool = False,
    append_newline: bool = False,
    passthrough_datetime: bool = False,
    allow_non_str_keys: bool = False,
) -> int:
    """Compose orjson option bit flags from keyword arguments."""
    opt = 0
    if append_newline:
        opt |= orjson.OPT_APPEND_NEWLINE
    if pretty:
        opt |= orjson.OPT_INDENT_2
    if sort_keys:
        opt |= orjson.OPT_SORT_KEYS
    if naive_utc:
        opt |= orjson.OPT_NAIVE_UTC
    if utc_z:
        opt |= orjson.OPT_UTC_Z
    if passthrough_datetime:
        opt |= orjson.OPT_PASSTHROUGH_DATETIME
    if allow_non_str_keys:
        opt |= orjson.OPT_NON_STR_KEYS
    return opt


# --------- non-finite float detection -----------------------------------------

# orjson writes inf, -inf and nan as `null`, which is indistinguishable from a
# genuine null on read: the value silently changes and no consumer can detect it.
# JSON has no representation for these, so serialization fails loudly instead.


def _locate_non_finite(obj: Any, default: Callable[[Any], Any], path: str = "$") -> str | None:
    """Return the path of the first non-finite float reachable from obj, else None.

    Mirrors how orjson traverses the object, including the default() hook, so the
    reported path matches what would have been written.
    """
    typ = obj.__class__
    # Concrete types first: isinstance against the collections ABCs is an order of
    # magnitude slower, and these cover everything orjson serializes natively.
    if typ is float:
        return None if math.isfinite(obj) else path
    if typ in (str, int, bool, bytes) or obj is None:
        return None
    if typ is dict or isinstance(obj, Mapping):
        for key, value in obj.items():
            # Non-string keys are stringified by orjson, so a non-finite one is
            # written as the key "null" and lost the same way a value would be.
            if key.__class__ is float and not math.isfinite(key):
                return f"{path}.<key>"
            found = _locate_non_finite(value, default, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if typ in (list, tuple, set, frozenset) or (
        isinstance(obj, Sequence | set | frozenset) and not isinstance(obj, str | bytes)
    ):
        for index, value in enumerate(obj):
            found = _locate_non_finite(value, default, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    # Anything else reaches orjson through default(); follow the same conversion.
    try:
        converted = default(obj)
    except Exception:
        return None
    return _locate_non_finite(converted, default, path)


def _dumpb(obj: Any, default: Callable[[Any], Any], opt: int) -> bytes:
    """orjson.dumps, rejecting payloads whose non-finite floats would become null."""
    out = orjson.dumps(obj, default=default, option=opt)
    # Every non-finite float produces a literal `null`, so a null-free result is
    # provably clean and the walk below only runs on the rare candidate payload.
    if b"null" in out:
        found = _locate_non_finite(obj, default)
        if found is not None:
            raise ValueError(
                f"cannot serialize non-finite float at {found}: JSON has no "
                "representation for inf, -inf or nan, and writing it would "
                "silently record null in its place"
            )
    return out


# --------- dump helpers -------------------------------------------------------


def json_dumpb(
    obj: Any,
    *,
    pretty: bool = False,
    sort_keys: bool = False,
    naive_utc: bool = False,
    utc_z: bool = False,
    append_newline: bool = False,
    allow_non_str_keys: bool = False,
    deterministic_sets: bool = False,
    decimal_as_float: bool = False,
    enum_as_name: bool = False,
    passthrough_datetime: bool = False,
    safe_fallback: bool = False,
    fallback_clip: int = 2048,
    default: Callable[[Any], Any] | None = None,
    options: int | None = None,
) -> bytes:
    """Serialize to bytes via orjson (fast path); safe_fallback=True for logging only."""
    if default is None:
        default = _cached_default(
            deterministic_sets=deterministic_sets,
            decimal_as_float=decimal_as_float,
            enum_as_name=enum_as_name,
            passthrough_datetime=passthrough_datetime,
            safe_fallback=safe_fallback,
            fallback_clip=fallback_clip,
        )
    opt = (
        options
        if options is not None
        else make_options(
            pretty=pretty,
            sort_keys=sort_keys,
            naive_utc=naive_utc,
            utc_z=utc_z,
            append_newline=append_newline,
            passthrough_datetime=passthrough_datetime,
            allow_non_str_keys=allow_non_str_keys,
        )
    )
    return _dumpb(obj, default, opt)


@overload
def json_dumps(
    obj: Any,
    /,
    *,
    decode: Literal[True] = True,
    as_loaded: Literal[False] = False,
    **kwargs: Any,
) -> str: ...


@overload
def json_dumps(
    obj: Any,
    /,
    *,
    decode: Literal[False],
    as_loaded: Literal[False] = False,
    **kwargs: Any,
) -> bytes: ...


@overload
def json_dumps(
    obj: Any,
    /,
    *,
    decode: Literal[True] = True,
    as_loaded: Literal[True],
    **kwargs: Any,
) -> Any: ...


def json_dumps(
    obj: Any,
    /,
    *,
    decode: bool = True,
    as_loaded: bool = False,
    **kwargs: Any,
) -> str | bytes | Any:
    """Serialize to str (default), bytes, or re-parsed dict/list; raises ValueError if as_loaded without decode."""
    if as_loaded and not decode:
        raise ValueError("as_loaded=True requires decode=True")
    out = json_dumpb(obj, **kwargs)
    if not decode:
        return out
    return orjson.loads(out) if as_loaded else out.decode("utf-8")


# --------- streaming for very large outputs ----------------------------------


def json_lines_iter(
    it: Iterable[Any],
    *,
    # default() configuration for each line
    deterministic_sets: bool = False,
    decimal_as_float: bool = False,
    enum_as_name: bool = False,
    passthrough_datetime: bool = False,
    safe_fallback: bool = False,
    fallback_clip: int = 2048,
    # options
    naive_utc: bool = False,
    utc_z: bool = False,
    allow_non_str_keys: bool = False,
    # advanced
    default: Callable[[Any], Any] | None = None,
    options: int | None = None,
) -> Iterable[bytes]:
    """Stream iterable as NDJSON bytes (one orjson-serialized object per line, always newline-terminated)."""
    if default is None:
        default = _cached_default(
            deterministic_sets=deterministic_sets,
            decimal_as_float=decimal_as_float,
            enum_as_name=enum_as_name,
            passthrough_datetime=passthrough_datetime,
            safe_fallback=safe_fallback,
            fallback_clip=fallback_clip,
        )
    if options is None:
        opt = make_options(
            pretty=False,
            sort_keys=False,
            naive_utc=naive_utc,
            utc_z=utc_z,
            append_newline=True,  # enforce newline for NDJSON
            passthrough_datetime=passthrough_datetime,
            allow_non_str_keys=allow_non_str_keys,
        )
    else:
        opt = options | orjson.OPT_APPEND_NEWLINE

    for item in it:
        yield _dumpb(item, default, opt)
