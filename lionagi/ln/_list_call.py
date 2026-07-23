from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from ._to_list import to_list

R = TypeVar("R")
T = TypeVar("T")

__all__ = ("lcall",)


def lcall(
    input_: Iterable[T] | T,
    func: Callable[[T], R] | Iterable[Callable[[T], R]],
    /,
    *args: Any,
    input_flatten: bool = False,
    input_dropna: bool = False,
    input_unique: bool = False,
    input_use_values: bool = False,
    input_flatten_tuple_set: bool = False,
    output_flatten: bool = False,
    output_dropna: bool = False,
    output_unique: bool = False,
    output_flatten_tuple_set: bool = False,
    **kwargs: Any,
) -> list[R]:
    """Sync map of func over input with optional input/output flatten/dropna/unique transforms."""
    if not callable(func):
        try:
            func_list = list(func)
            if len(func_list) != 1 or not callable(func_list[0]):
                raise ValueError("func must contain exactly one callable function.")
            func = func_list[0]
        except TypeError as e:
            raise ValueError("func must be callable or iterable with one callable.") from e

    if output_unique and not (output_flatten or output_dropna):
        raise ValueError("unique_output requires flatten or dropna for post-processing.")

    if input_flatten or input_dropna or input_unique or input_use_values:
        # Route through to_list whenever any input transform is requested, not
        # only on flatten/dropna: otherwise input_use_values is ignored (a
        # mapping yields its keys) and input_unique is silently dropped instead
        # of raising the documented "unique=True requires flatten=True".
        input_ = to_list(
            input_,
            flatten=input_flatten,
            dropna=input_dropna,
            unique=input_unique,
            flatten_tuple_set=input_flatten_tuple_set,
            use_values=input_use_values,
        )
    else:
        if not isinstance(input_, list):
            try:
                input_ = list(input_)
            except TypeError:
                input_ = [input_]

    out = []
    append = out.append

    for item in input_:
        try:
            result = func(item, *args, **kwargs)
            append(result)
        except InterruptedError:
            return out
        except Exception:
            raise

    if output_flatten or output_dropna:
        out = to_list(
            out,
            flatten=output_flatten,
            dropna=output_dropna,
            unique=output_unique,
            flatten_tuple_set=output_flatten_tuple_set,
        )

    return out
