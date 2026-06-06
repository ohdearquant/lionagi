from __future__ import annotations

"""JSON serialization utilities built on orjson.

Provides flexible serialization with:
- Configurable type handling (Decimal, Enum, datetime, sets)
- Safe fallback mode for logging non-serializable objects
- NDJSON streaming for iterables
- Caching of default handlers for performance
"""

import contextlib  # noqa: E402
import datetime as dt  # noqa: E402
import decimal  # noqa: E402
import re  # noqa: E402
from collections.abc import Callable, Iterable, Mapping  # noqa: E402
from enum import Enum  # noqa: E402
from functools import lru_cache  # noqa: E402
from pathlib import Path  # noqa: E402
from textwrap import shorten  # noqa: E402
from typing import Any  # noqa: E402
from uuid import UUID  # noqa: E402

import orjson  # noqa: E402

__all__ = [
    "get_orjson_default",
    "make_options",
    "json_dumpb",
    "json_dumps",
    "json_lines_iter",
]

# Types orjson already serializes natively at C/Rust speed.
# (We only route them through default() when passthrough is requested.)
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
    """
    Deterministic ordering for sets (including mixed types).
    Key: (class name, normalized str) avoids comparisons across unlike types
    and removes memory address variance in default reprs.
    """
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
    """
    Build a fast, extensible `default=` callable for orjson.dumps.

    - deterministic_sets: sort set/frozenset deterministically (slower).
    - decimal_as_float: serialize Decimal as float (faster/smaller; precision loss).
    - enum_as_name: serialize Enum as .name (else orjson uses .value by default).
    - passthrough_datetime: if True, also pass OPT_PASSTHROUGH_DATETIME in options.
    - safe_fallback: if True, unknown objects never raise (for logs);
      Exceptions become a tiny dict; all else becomes clipped repr(str).

    'order' and 'additional' preserve your override semantics.
    """
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
            for T in order_tuple:  # noqa: N806
                if issubclass(typ, T):
                    f = ser.get(T)
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
    """
    Compose orjson 'option' bit flags succinctly.
    """
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
    """
    Serialize to **bytes** (fast path). Prefer this in hot code.

    Notes:
      - If you set passthrough_datetime=True, you likely also want it in options.
      - safe_fallback=True is recommended for LOGGING ONLY.
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
    return orjson.dumps(obj, default=default, option=opt)


def json_dumps(
    obj: Any,
    /,
    *,
    decode: bool = True,
    as_loaded: bool = False,
    **kwargs: Any,
) -> str | bytes | Any:
    """Serialize to JSON with flexible output format.

    Args:
        obj: Object to serialize.
        decode: Return str instead of bytes (default True).
        as_loaded: Parse output back to dict/list (requires decode=True).
        **kwargs: Passed to json_dumpb.

    Returns:
        str (default), bytes (decode=False), or dict/list (as_loaded=True).

    Raises:
        ValueError: If as_loaded=True without decode=True.
    """
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
    """
    Stream an iterable as **NDJSON** (one JSON object per line) in **bytes**.

    Always ensures a trailing newline per line (OPT_APPEND_NEWLINE).
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
        yield orjson.dumps(item, default=default, option=opt)
