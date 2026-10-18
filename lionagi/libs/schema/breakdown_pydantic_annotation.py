from typing import Any, get_args, get_origin

from pydantic import BaseModel

from lionagi.ln import is_pydantic_model_class


def breakdown_pydantic_annotation(
    model: type[BaseModel],
    max_depth: int | None = None,
    current_depth: int = 0,
) -> dict[str, Any]:
    if not is_pydantic_model_class(model):
        raise TypeError("Input must be a Pydantic model")

    if max_depth is not None and current_depth >= max_depth:
        raise RecursionError("Maximum recursion depth reached")

    out: dict[str, Any] = {}
    for k, v in model.__annotations__.items():
        origin = get_origin(v)
        if is_pydantic_model_class(v):
            out[k] = breakdown_pydantic_annotation(v, max_depth, current_depth + 1)
        elif origin is list:
            args = get_args(v)
            if args and is_pydantic_model_class(args[0]):
                out[k] = [breakdown_pydantic_annotation(args[0], max_depth, current_depth + 1)]
            else:
                out[k] = [args[0] if args else Any]
        else:
            out[k] = v

    return out
