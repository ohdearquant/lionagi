# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""JSON serialization utilities built on orjson with configurable type handling and NDJSON streaming."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import decimal
import math
import re
import sys
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
# JSON has no representation for these, so callers that ask for the check get a
# loud failure instead.
#
# Detection walks the object the way orjson does. That means covering every form
# orjson encodes natively, because those never reach default(); a walk that follows
# only default() sees nothing inside a dataclass, an Enum or a numpy array and
# reports the payload clean. The forms orjson encodes natively and that can carry a
# float are: float, dict, list, tuple (and their subclasses), dataclass instances,
# Enum members (written by value), and numpy arrays and scalars under
# OPT_SERIALIZE_NUMPY. The remaining native forms -- str, int, bool, None, bytes,
# datetime, date, time and UUID -- cannot contain a float.
#
# Two forms are outside what any walk can decide:
#
# * orjson.Fragment holds pre-serialized bytes that orjson copies into the output
#   verbatim, without parsing them and without calling default(). A `null` inside a
#   Fragment is indistinguishable from a `null` a non-finite float would have
#   produced, because neither exists as a Python float by the time the Fragment is
#   built. Fragment contents are the caller's to validate; the walk skips them.
# * A future orjson may encode a container type natively that this list does not
#   name, and the declared dependency floor is a minimum rather than an exact
#   version, so a newer orjson can be installed. The list is written against the
#   native types orjson documents; it is not enforced against the installed
#   version at run time.
#
# Everything else reaches orjson through default(), and the walk follows that
# conversion.


def _numpy_non_finite(obj: Any, path: str) -> str | None | Literal[False]:
    """Path of the first non-finite element if obj is a numpy float array/scalar.

    Returns False when obj is not a numpy value, distinguishing "not mine to judge"
    from "checked and clean".
    """
    np = sys.modules.get("numpy")
    # numpy cannot have produced this object if it was never imported.
    if np is None or not isinstance(obj, np.ndarray | np.generic):
        return False
    # Only float dtypes can be non-finite; int, bool and datetime64 cannot, and
    # np.isfinite raises on the object and string dtypes orjson refuses anyway.
    if obj.dtype.kind != "f":
        return None
    bad = ~np.isfinite(obj)
    if not bad.any():
        return None
    if obj.ndim == 0:
        return path
    index = np.unravel_index(int(np.argmax(bad)), obj.shape)
    return path + "".join(f"[{int(i)}]" for i in index)


def _locate_non_finite(
    obj: Any, default: Callable[[Any], Any], opt: int, path: str = "$"
) -> str | None:
    """Return the path of the first non-finite float reachable from obj, else None.

    Mirrors how orjson traverses the object, including the natively-encoded forms
    that bypass default() and the default() hook itself, so the reported path
    matches what would have been written.
    """
    typ = obj.__class__
    # Concrete types first: isinstance against the collections ABCs is an order of
    # magnitude slower, and these cover the overwhelming majority of nodes.
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
            found = _locate_non_finite(value, default, opt, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if typ in (list, tuple, set, frozenset) or (
        isinstance(obj, Sequence | set | frozenset) and not isinstance(obj, str | bytes)
    ):
        for index, value in enumerate(obj):
            found = _locate_non_finite(value, default, opt, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    # Remaining natively-encoded forms, none of which reach default().
    if isinstance(obj, float):
        # A float subclass orjson accepts, notably numpy's float64.
        return None if math.isfinite(obj) else path
    if opt & orjson.OPT_SERIALIZE_NUMPY:
        found = _numpy_non_finite(obj, path)
        if found is not False:
            return found
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if not opt & orjson.OPT_PASSTHROUGH_DATACLASS:
            for field in dataclasses.fields(obj):
                found = _locate_non_finite(
                    getattr(obj, field.name), default, opt, f"{path}.{field.name}"
                )
                if found is not None:
                    return found
            return None
    elif isinstance(obj, Enum):
        # orjson writes an Enum member by its value, never through default().
        return _locate_non_finite(obj.value, default, opt, path)
    elif isinstance(obj, orjson.Fragment):
        # Pre-serialized bytes, copied into the output unparsed. Whatever they
        # contain was decided before this call and cannot be judged from here.
        return None
    # Anything else reaches orjson through default(); follow the same conversion.
    try:
        converted = default(obj)
    except Exception:
        return None
    return _locate_non_finite(converted, default, opt, path)


def _dumpb(
    obj: Any, default: Callable[[Any], Any], opt: int, check_non_finite: bool = False
) -> bytes:
    """orjson.dumps, optionally rejecting payloads whose non-finite floats become null.

    The check is off by default because it costs a full Python-level traversal of
    the object and there is no cheaper sound trigger for it. A non-finite float and
    a `None` both emit the literal `null`, so the output cannot say which produced
    it, and orjson exposes no hook that sees floats -- they are encoded natively and
    never reach default(). Measured on a 200-item object with one legitimate null,
    the traversal costs roughly 20x the dump it guards; even scanning the output for
    `null` first costs about half a dump again, so gating on it does not help a
    payload that has any null at all, which is most of them. Callers that persist a
    value ask for the check explicitly; see json_dumpb.
    """
    out = orjson.dumps(obj, default=default, option=opt)
    # Every non-finite float produces a literal `null`, so a null-free result is
    # provably clean and the walk is skipped. This only pays off once the caller has
    # already opted into the check; as a gate on every dump it costs more than it saves.
    if check_non_finite and b"null" in out:
        found = _locate_non_finite(obj, default, opt)
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
    check_non_finite: bool = False,
    default: Callable[[Any], Any] | None = None,
    options: int | None = None,
) -> bytes:
    """Serialize to bytes via orjson (fast path); safe_fallback=True for logging only.

    orjson writes inf, -inf and nan as `null`, which a reader cannot tell apart from
    a genuine null, so those values are lost silently. Pass check_non_finite=True to
    raise ValueError instead, naming the path of the first offending value. Prefer it
    wherever the result is persisted or handed to another system, where the loss is
    durable and undetectable after the fact.

    The check costs a full traversal of the payload -- roughly 20x the dump on an
    object of a few thousand nodes -- which is why it is off by default. It covers
    floats in mappings and sequences, dataclass fields, Enum values, numpy float
    arrays and scalars when `options` carries OPT_SERIALIZE_NUMPY, and anything the
    `default` hook converts into those. It cannot cover orjson.Fragment: those bytes
    are copied into the output unparsed, so a `null` inside one is opaque to any
    caller-side check and remains the responsibility of whoever built the Fragment.
    """
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
    return _dumpb(obj, default, opt, check_non_finite)


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
    check_non_finite: bool = False,
    # options
    naive_utc: bool = False,
    utc_z: bool = False,
    allow_non_str_keys: bool = False,
    # advanced
    default: Callable[[Any], Any] | None = None,
    options: int | None = None,
) -> Iterable[bytes]:
    """Stream iterable as NDJSON bytes (one orjson-serialized object per line, always newline-terminated).

    check_non_finite=True raises ValueError on the first line holding an inf, -inf or
    nan rather than writing it as `null`; see json_dumpb for what that costs and covers.
    """
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
        yield _dumpb(item, default, opt, check_non_finite)
