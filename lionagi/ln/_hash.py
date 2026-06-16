# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import copy
import hashlib
from enum import Enum
from typing import TYPE_CHECKING, Any

from ._json_dump import json_dumpb
from ._lazy_init import LazyInit

if TYPE_CHECKING:
    from collections.abc import Callable
    from hashlib import _Hash

try:
    import msgspec as _msgspec
    from msgspec import Struct as _MsgspecStruct
except ImportError:
    _MsgspecStruct = None
    _msgspec = None

__all__ = (
    "GENESIS_HASH",
    "HashAlgorithm",
    "MAX_HASH_INPUT_BYTES",
    "compute_chain_hash",
    "compute_hash",
    "hash_dict",
    "hash_obj",
)

_lazy = LazyInit()
PydanticBaseModel = None


def _do_init() -> None:
    """Lazy-initialize Pydantic BaseModel reference."""
    global PydanticBaseModel
    from pydantic import BaseModel

    PydanticBaseModel = BaseModel


# --- Canonical Representation Generator ---
_PRIMITIVE_TYPES = (str, int, float, bool, type(None))
_TYPE_MARKER_DICT = 0
_TYPE_MARKER_LIST = 1
_TYPE_MARKER_TUPLE = 2
_TYPE_MARKER_SET = 3
_TYPE_MARKER_FROZENSET = 4
_TYPE_MARKER_PYDANTIC = 5  # Distinguishes dumped Pydantic models
_TYPE_MARKER_MSGSPEC = 6  # Distinguishes msgspec Structs


def _generate_hashable_representation(item: Any) -> Any:
    """Convert object to stable, order-independent hashable representation.

    Recursively transforms dicts/sets into sorted tuples with type markers
    to ensure consistent hashing regardless of insertion order.
    """
    if isinstance(item, _PRIMITIVE_TYPES):
        return item

    # Handle msgspec Structs (optional dependency)
    if _MsgspecStruct is not None and isinstance(item, _MsgspecStruct):
        return (
            _TYPE_MARKER_MSGSPEC,
            _generate_hashable_representation(_msgspec.to_builtins(item)),
        )

    if PydanticBaseModel and isinstance(item, PydanticBaseModel):
        return (
            _TYPE_MARKER_PYDANTIC,
            _generate_hashable_representation(item.model_dump()),
        )

    if isinstance(item, dict):
        return (
            _TYPE_MARKER_DICT,
            tuple(
                (str(k), _generate_hashable_representation(v))
                for k, v in sorted(item.items(), key=lambda x: str(x[0]))
            ),
        )

    if isinstance(item, list):
        return (
            _TYPE_MARKER_LIST,
            tuple(_generate_hashable_representation(elem) for elem in item),
        )

    if isinstance(item, tuple):
        return (
            _TYPE_MARKER_TUPLE,
            tuple(_generate_hashable_representation(elem) for elem in item),
        )

    # frozenset must be checked before set
    if isinstance(item, frozenset):
        try:
            sorted_elements = sorted(item)
        except TypeError:
            sorted_elements = sorted(item, key=lambda x: (str(type(x)), str(x)))
        return (
            _TYPE_MARKER_FROZENSET,
            tuple(_generate_hashable_representation(elem) for elem in sorted_elements),
        )

    if isinstance(item, set):
        try:
            sorted_elements = sorted(item)
        except TypeError:
            sorted_elements = sorted(item, key=lambda x: (str(type(x)), str(x)))
        return (
            _TYPE_MARKER_SET,
            tuple(_generate_hashable_representation(elem) for elem in sorted_elements),
        )

    # Fallback for other types
    with contextlib.suppress(Exception):
        return str(item)
    with contextlib.suppress(Exception):
        return repr(item)

    return f"<unhashable:{type(item).__name__}:{id(item)}>"


def hash_obj(data: Any, strict: bool = False) -> int:
    """Stable int hash for set/dict membership; use compute_hash for cryptographic integrity."""
    _lazy.ensure(_do_init)

    data_to_process = data
    if strict:
        data_to_process = copy.deepcopy(data)

    hashable_repr = _generate_hashable_representation(data_to_process)

    try:
        return hash(hashable_repr)
    except TypeError as e:
        raise TypeError(
            f"The generated representation for the input data was not hashable. "
            f"Input type: {type(data).__name__}, "
            f"Representation type: {type(hashable_repr).__name__}. "
            f"Original error: {e}"
        ) from e


# Backward-compatible alias
hash_dict = hash_obj


MAX_HASH_INPUT_BYTES = 10 * 1024 * 1024
"""Max hash input (10MB). DoS prevention."""

GENESIS_HASH: str = "GENESIS"
"""Sentinel for first entry in hash chain."""


class HashAlgorithm(Enum):
    """NIST FIPS 180-4 compliant hash algorithms for cryptographic integrity."""

    SHA256 = "sha256"
    SHA384 = "sha384"
    SHA512 = "sha512"

    def get_hasher(self) -> Callable[..., _Hash]:
        """Return hashlib constructor for this algorithm."""
        return getattr(hashlib, self.value)

    @property
    def digest_size(self) -> int:
        """Digest size in bytes (32/48/64 for SHA256/384/512)."""
        return self.get_hasher()(b"").digest_size


def compute_hash(
    obj: Any,
    algorithm: HashAlgorithm = HashAlgorithm.SHA256,
    none_as_valid: bool = False,
) -> str:
    """Cryptographic hash for content integrity; use hash_obj for __hash__ protocol. Raises ValueError if payload >10MB."""
    payload: bytes
    if none_as_valid and obj is None:
        payload = b"null"
    elif isinstance(obj, bytes):
        payload = obj
    elif isinstance(obj, str):
        payload = obj.encode()
    else:
        payload = json_dumpb(obj, sort_keys=True, deterministic_sets=True)

    if len(payload) > MAX_HASH_INPUT_BYTES:
        raise ValueError(f"Payload {len(payload):,}B > {MAX_HASH_INPUT_BYTES:,}B limit")

    hasher = algorithm.get_hasher()
    return hasher(payload).hexdigest()


def compute_chain_hash(
    payload_hash: str,
    previous_hash: str | None = None,
    algorithm: HashAlgorithm = HashAlgorithm.SHA256,
) -> str:
    """Hash of ``"{payload_hash}:{previous_hash|GENESIS}"`` for tamper-evident chain linking."""
    chain_input = f"{payload_hash}:{previous_hash or GENESIS_HASH}"
    return compute_hash(chain_input, algorithm)
