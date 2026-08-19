# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""JSON serialization utilities built on orjson with configurable type handling and NDJSON streaming."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import decimal
import inspect
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
    "raise_if_non_finite",
]

# Types orjson serializes natively; routed through default() only when passthrough is requested.
_NATIVE = (dt.datetime, dt.date, dt.time, UUID)
_SERIALIZATION_METHODS = ("model_dump", "to_dict", "dict")
# Same methods, different priority: the projection prefers an owner's own
# to_dict, which is the more specific rendering (an Element's carries the
# lion_class that generic deserialization needs and model_dump drops).
# Derived rather than restated so the two cannot disagree on membership.
_PROJECTION_METHODS = ("to_dict", *(m for m in _SERIALIZATION_METHODS if m != "to_dict"))

# helpers

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


# default() factory


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


def _inspect_accepts_keyword(method: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        )
        for parameter in parameters
    )


@lru_cache(maxsize=256)
def _cached_accepts_keyword(method: Callable[..., Any], name: str) -> bool:
    return _inspect_accepts_keyword(method, name)


def _accepts_keyword(method: Callable[..., Any], name: str) -> bool:
    """Return whether a callable explicitly or generically accepts *name*."""
    target = getattr(method, "__func__", method)
    try:
        hash(target)
    except TypeError:
        return _inspect_accepts_keyword(method, name)
    return _cached_accepts_keyword(target, name)


def _overrides_substrate_to_dict(obj: Any, substrate_types: tuple[type[Any], ...]) -> bool:
    """Whether *obj*'s type replaced the to_dict it inherited from its substrate base."""
    for base in substrate_types:
        base_method = getattr(base, "to_dict", None)
        if base_method is not None and isinstance(obj, base):
            return getattr(type(obj), "to_dict", None) is not base_method
    return False


def _json_projection_default(obj: Any) -> Any:
    """Finish non-native leaves after the projection traversal."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            field_info.name: getattr(obj, field_info.name) for field_info in dataclasses.fields(obj)
        }

    fallback = _cached_default(False, False, False, False, False, 2048)
    return fallback(obj)


def _project_json_value(
    obj: Any,
    substrate_types: tuple[type[Any], ...],
    active: set[int],
    path: str,
) -> Any:
    """Invoke each owner adapter once before handing native leaves to orjson."""
    if isinstance(obj, float) and not math.isfinite(obj):
        _raise_non_finite(path)
    if isinstance(obj, Enum):
        return _project_json_value(obj.value, substrate_types, active, path)

    if isinstance(obj, Mapping):
        identity = _enter_json_projection(obj, active)
        try:
            return {
                key: _project_json_value(
                    value,
                    substrate_types,
                    active,
                    f"{path}.{key}",
                )
                for key, value in obj.items()
            }
        finally:
            active.remove(identity)
    if isinstance(obj, list | tuple):
        identity = _enter_json_projection(obj, active)
        try:
            return [
                _project_json_value(
                    value,
                    substrate_types,
                    active,
                    f"{path}[{index}]",
                )
                for index, value in enumerate(obj)
            ]
        finally:
            active.remove(identity)
    if isinstance(obj, set | frozenset):
        identity = _enter_json_projection(obj, active)
        try:
            return [
                _project_json_value(
                    value,
                    substrate_types,
                    active,
                    f"{path}[{index}]",
                )
                for index, value in enumerate(obj)
            ]
        finally:
            active.remove(identity)

    if isinstance(obj, substrate_types):
        identity = _enter_json_projection(obj, active)
        try:
            projection_owner: Any = obj
            to_dict = projection_owner.to_dict
            # A subclass that renders per mode only gets that rendering if the
            # mode reaches it, so pass it. Inherited implementations are skipped
            # deliberately: their json mode is a value projection this walk
            # repeats anyway, and asking for it would round-trip every nested
            # owner through the serializer instead of once at the root.
            projected = (
                to_dict(mode="json")
                if _overrides_substrate_to_dict(obj, substrate_types)
                and _accepts_keyword(to_dict, "mode")
                else to_dict()
            )
            return _project_json_value(
                projected,
                substrate_types,
                active,
                path,
            )
        finally:
            active.remove(identity)

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        identity = _enter_json_projection(obj, active)
        try:
            return {
                field_info.name: _project_json_value(
                    getattr(obj, field_info.name),
                    substrate_types,
                    active,
                    f"{path}.{field_info.name}",
                )
                for field_info in dataclasses.fields(obj)
            }
        finally:
            active.remove(identity)

    for method_name in _PROJECTION_METHODS:
        method = getattr(obj, method_name, None)
        if not callable(method):
            continue
        identity = _enter_json_projection(obj, active)
        try:
            value = (
                method(mode="json")
                if method_name != "dict" and _accepts_keyword(method, "mode")
                else method()
            )
            return _project_json_value(value, substrate_types, active, path)
        finally:
            active.remove(identity)
    return obj


def _enter_json_projection(obj: Any, active: set[int]) -> int:
    identity = id(obj)
    if identity in active:
        raise TypeError("Circular reference in JSON projection")
    active.add(identity)
    return identity


def _to_json_value(obj: Any) -> Any:
    """Return a JSON-compatible value through the internal orjson boundary."""
    from .types import DataClass, Params, Spec

    projected = _project_json_value(
        obj,
        (Params, DataClass, Spec),
        set(),
        "$",
    )
    output = _dumpb(
        projected,
        _json_projection_default,
        orjson.OPT_PASSTHROUGH_DATACLASS,
    )
    return orjson.loads(output)


# defaults & options


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


# non-finite float detection

# orjson writes inf, -inf and nan as `null`, indistinguishable from a genuine
# null on read. Detection below walks the object the way orjson does, so it
# sees every native form (not just what default() sees). See
# docs/internals/agent-runtime.md#non-finite-float-detection for the full
# coverage argument and the two forms no walk can decide (Fragment, future
# orjson-native types).


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

    Mirrors orjson's own traversal (native forms and the default() hook) so
    the reported path matches what would have been written.
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


def _locate_stdlib_non_finite(
    obj: Any, default: Callable[[Any], Any], path: str = "$"
) -> str | None:
    """Follow the standard-library JSON encoder and return a non-finite path."""
    if isinstance(obj, float):
        return None if math.isfinite(obj) else path
    if isinstance(obj, str | int | bool) or obj is None:
        return None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, float) and not math.isfinite(key):
                return f"{path}.<key>"
            found = _locate_stdlib_non_finite(value, default, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(obj, list | tuple):
        for index, value in enumerate(obj):
            found = _locate_stdlib_non_finite(value, default, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    try:
        converted = default(obj)
    except Exception:
        return None
    return _locate_stdlib_non_finite(converted, default, path)


def _raise_non_finite(found: str | None) -> None:
    if found is not None:
        raise ValueError(
            f"cannot serialize non-finite float at {found}: JSON has no "
            "representation for inf, -inf or nan, and writing it records "
            "something no reader can recover -- a null indistinguishable from a "
            "real one, or the tokens NaN and Infinity that strict parsers reject"
        )


def raise_if_non_finite(
    obj: Any,
    *,
    default: Callable[[Any], Any] | None = None,
    options: int = 0,
) -> None:
    """Raise ValueError naming the path of the first non-finite float in *obj*.

    For callers persisting through a serializer other than json_dumpb —
    notably stdlib ``json``, which writes inf/-inf/nan as ``Infinity``/``NaN``
    tokens that Python reads back but strict parsers reject downstream.

    ``default`` should be the conversion the writer itself applies (e.g.
    ``str`` for ``json.dumps(..., default=str)``); when supplied, the walk
    follows the standard-library encoder instead of orjson's.
    """
    if default is None:
        default = _cached_default(False, False, False, False, False, 2048)
        found = _locate_non_finite(obj, default, options)
    else:
        found = _locate_stdlib_non_finite(obj, default)
    _raise_non_finite(found)


def _dumpb(
    obj: Any, default: Callable[[Any], Any], opt: int, check_non_finite: bool = False
) -> bytes:
    """orjson.dumps, optionally rejecting payloads whose non-finite floats become null.

    check_non_finite is off by default: it costs a full Python traversal
    (~20x the dump itself). See docs/internals/agent-runtime.md#non-finite-float-detection
    for the measurement and why a null-prescan doesn't help.
    """
    out = orjson.dumps(obj, default=default, option=opt)
    # A null-free result is provably clean, so the traversal is skipped.
    if check_non_finite and b"null" in out:
        _raise_non_finite(_locate_non_finite(obj, default, opt))
    return out


# dump helpers


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

    Pass check_non_finite=True to raise ValueError instead of silently
    writing inf/-inf/nan as `null` — prefer it wherever the result is
    persisted or handed to another system. See
    docs/internals/agent-runtime.md#non-finite-float-detection for cost and coverage.
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


# streaming for very large outputs


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
