"""Experimental operation registry."""

from collections.abc import Callable

from lionagi.protocols._concepts import Manager
from lionagi.utils import is_coro_func


class OperationManager(Manager):
    def __init__(self):
        super().__init__()
        self.registry: dict[str, Callable] = {}

    def register(self, operation: str, func: Callable, update: bool = False):
        if operation in self.registry and not update:
            raise ValueError(f"Operation '{operation}' is already registered.")
        if not is_coro_func(func):
            raise ValueError(f"Operation '{operation}' must be an async function.")
        self.registry[operation] = func
