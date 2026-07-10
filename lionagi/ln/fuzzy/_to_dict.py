from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum as _Enum
from typing import Any, Literal

from ._fuzzy_json import fuzzy_json


def _is_na(obj: Any) -> bool:
    if obj is None:
        return True
    # Avoid importing pydantic types; match by typename to stay lightweight
    tname = type(obj).__name__
    return tname in {
        "Undefined",
        "UndefinedType",
        "PydanticUndefined",
        "PydanticUndefinedType",
    }


def _enum_class_to_dict(enum_cls: type[_Enum], use_enum_values: bool) -> dict[str, Any]:
    members = dict(enum_cls.__members__)  # cheap, stable
    if use_enum_values:
        return {k: v.value for k, v in members.items()}
    return {k: v for k, v in members.items()}


def _parse_str(
    s: str,
    *,
    fuzzy_parse: bool,
    str_type: Literal["json", "xml"] | None,
    parser: Callable[[str], Any] | None,
    **kwargs: Any,
) -> Any:
    if parser is not None:
        return parser(s, **kwargs)

    if str_type == "xml":
        import xmltodict

        return xmltodict.parse(s, **kwargs)

    if fuzzy_parse:
        with contextlib.suppress(NameError):
            return fuzzy_json(s, **kwargs)  # type: ignore[name-defined]
    return json.loads(s, **kwargs)


def _object_to_mapping_like(
    obj: Any,
    *,
    prioritize_model_dump: bool = True,
    **kwargs: Any,
) -> Mapping | dict | Any:
    if prioritize_model_dump and hasattr(obj, "model_dump"):
        return obj.model_dump(**kwargs)

    for name in ("to_dict", "dict", "to_json", "json", "model_dump"):
        if hasattr(obj, name):
            res = getattr(obj, name)(**kwargs)
            return json.loads(res) if isinstance(res, str) else res

    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)

    if hasattr(obj, "__dict__"):
        return obj.__dict__

    return dict(obj)


def _enumerate_iterable(it: Iterable) -> dict[int, Any]:
    return {i: v for i, v in enumerate(it)}


def _preprocess_recursive(
    obj: Any,
    *,
    depth: int,
    max_depth: int,
    recursive_custom_types: bool,
    str_parse_opts: dict[str, Any],
    prioritize_model_dump: bool,
) -> Any:
    if depth >= max_depth:
        return obj

    t = type(obj)

    if t is str:
        try:
            parsed = _parse_str(obj, **str_parse_opts)
        except Exception:
            return obj
        return _preprocess_recursive(
            parsed,
            depth=depth + 1,
            max_depth=max_depth,
            recursive_custom_types=recursive_custom_types,
            str_parse_opts=str_parse_opts,
            prioritize_model_dump=prioritize_model_dump,
        )

    if isinstance(obj, Mapping):
        return {
            k: _preprocess_recursive(
                v,
                depth=depth + 1,
                max_depth=max_depth,
                recursive_custom_types=recursive_custom_types,
                str_parse_opts=str_parse_opts,
                prioritize_model_dump=prioritize_model_dump,
            )
            for k, v in obj.items()
        }

    if isinstance(obj, list | tuple | set | frozenset):
        items = [
            _preprocess_recursive(
                v,
                depth=depth + 1,
                max_depth=max_depth,
                recursive_custom_types=recursive_custom_types,
                str_parse_opts=str_parse_opts,
                prioritize_model_dump=prioritize_model_dump,
            )
            for v in obj
        ]
        if t is list:
            return items
        if t is tuple:
            return tuple(items)
        if t is set:
            return set(items)
        if t is frozenset:
            return frozenset(items)

    if isinstance(obj, type) and issubclass(obj, _Enum):
        try:
            enum_map = _enum_class_to_dict(
                obj,
                use_enum_values=str_parse_opts.get("use_enum_values", True),
            )
            return _preprocess_recursive(
                enum_map,
                depth=depth + 1,
                max_depth=max_depth,
                recursive_custom_types=recursive_custom_types,
                str_parse_opts=str_parse_opts,
                prioritize_model_dump=prioritize_model_dump,
            )
        except Exception:
            return obj

    if recursive_custom_types:
        with contextlib.suppress(Exception):
            mapped = _object_to_mapping_like(obj, prioritize_model_dump=prioritize_model_dump)
            return _preprocess_recursive(
                mapped,
                depth=depth + 1,
                max_depth=max_depth,
                recursive_custom_types=recursive_custom_types,
                str_parse_opts=str_parse_opts,
                prioritize_model_dump=prioritize_model_dump,
            )

    return obj


def _convert_top_level_to_dict(
    obj: Any,
    *,
    fuzzy_parse: bool,
    str_type: Literal["json", "xml"] | None,
    parser: Callable[[str], Any] | None,
    prioritize_model_dump: bool,
    use_enum_values: bool,
    **kwargs: Any,
) -> dict[str, Any]:
    if isinstance(obj, set):
        return {v: v for v in obj}

    if isinstance(obj, type) and issubclass(obj, _Enum):
        return _enum_class_to_dict(obj, use_enum_values)

    if isinstance(obj, Mapping):
        return dict(obj)

    if _is_na(obj):
        return {}

    if isinstance(obj, str):
        return _parse_str(
            obj,
            fuzzy_parse=fuzzy_parse,
            str_type=str_type,
            parser=parser,
            **kwargs,
        )

    try:
        if not isinstance(obj, Sequence):
            converted = _object_to_mapping_like(
                obj, prioritize_model_dump=prioritize_model_dump, **kwargs
            )
            if isinstance(converted, str):
                return _parse_str(
                    converted,
                    fuzzy_parse=fuzzy_parse,
                    str_type="json",
                    parser=None,
                )
            if isinstance(converted, Mapping):
                return dict(converted)
            if isinstance(converted, Iterable) and not isinstance(
                converted, str | bytes | bytearray
            ):
                return _enumerate_iterable(converted)
            return dict(converted)

    except Exception:  # noqa: S110  # intentional: exhausts every conversion strategy before giving up
        pass

    if isinstance(obj, Iterable) and not isinstance(obj, str | bytes | bytearray):
        return _enumerate_iterable(obj)

    with contextlib.suppress(Exception):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)

    return dict(obj)


def to_dict(
    input_: Any,
    /,
    *,
    prioritize_model_dump: bool = True,
    fuzzy_parse: bool = False,
    suppress: bool = False,
    str_type: Literal["json", "xml"] | None = "json",
    parser: Callable[[str], Any] | None = None,
    recursive: bool = False,
    max_recursive_depth: int | None = None,
    recursive_python_only: bool = True,
    use_enum_values: bool = False,
    use_model_dump: bool | None = None,  # deprecated
    **kwargs: Any,
) -> dict[str, Any]:
    """Convert various input types to a dictionary."""
    if use_model_dump is not None:
        prioritize_model_dump = use_model_dump

    try:
        if not isinstance(max_recursive_depth, int):
            max_depth = 5
        else:
            if max_recursive_depth < 0:
                raise ValueError("max_recursive_depth must be a non-negative integer")
            if max_recursive_depth > 10:
                raise ValueError("max_recursive_depth must be less than or equal to 10")
            max_depth = max_recursive_depth

        # Prepare one small dict to avoid repeated arg passing and lookups
        str_parse_opts = {
            "fuzzy_parse": fuzzy_parse,
            "str_type": str_type,
            "parser": parser,
            "use_enum_values": use_enum_values,  # threaded for enum class in recursion
            **kwargs,
        }

        obj = input_
        if recursive:
            obj = _preprocess_recursive(
                obj,
                depth=0,
                max_depth=max_depth,
                recursive_custom_types=not recursive_python_only,
                str_parse_opts=str_parse_opts,
                prioritize_model_dump=prioritize_model_dump,
            )

        return _convert_top_level_to_dict(
            obj,
            fuzzy_parse=fuzzy_parse,
            str_type=str_type,
            parser=parser,
            prioritize_model_dump=prioritize_model_dump,
            use_enum_values=use_enum_values,
            **kwargs,
        )

    except Exception as e:
        if suppress or input_ == "":
            return {}
        raise e
