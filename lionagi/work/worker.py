from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel


class Worker(BaseModel):
    name: str
    func: Callable
    retries: int = 0
    timeout: float | None = None


_WORKER_REGISTRY: dict[str, Worker] = {}


def worker(
    func: Callable | None = None,
    *,
    name: str | None = None,
    retries: int = 0,
    timeout: float | None = None,
) -> Callable | Worker:
    if retries < 0:
        raise ValueError("retries must be >= 0")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")

    def decorator(func_to_register: Callable) -> Callable:
        worker_name = name or func_to_register.__name__
        registered = Worker(
            name=worker_name,
            func=func_to_register,
            retries=retries,
            timeout=timeout,
        )
        _WORKER_REGISTRY[worker_name] = registered
        return func_to_register

    if func is None:
        return decorator
    return decorator(func)


def get_worker(name: str) -> Worker | None:
    return _WORKER_REGISTRY.get(name)


async def invoke_worker(func: Callable, args: dict[str, Any], branch: Any) -> Any:
    sig = inspect.signature(func)
    params = sig.parameters
    call_kwargs = dict(args)
    if "branch" in params and "branch" not in call_kwargs:
        call_kwargs["branch"] = branch

    if inspect.iscoroutinefunction(func):
        return await func(**call_kwargs)
    return func(**call_kwargs)


def clear_worker_registry() -> None:
    _WORKER_REGISTRY.clear()
