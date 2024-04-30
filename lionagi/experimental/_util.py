from collections.abc import Mapping, Generator
from collections import deque

from .abc import BaseRecord

def _to_list_type(value):
    if isinstance(value, (tuple, list, set, Generator, deque)):
        return list(value)
    if isinstance(value, Mapping, BaseRecord):
        return list(value.values())
    return [value]