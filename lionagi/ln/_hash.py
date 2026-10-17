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
from ._structural import _is_circular_reference

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


# Canonical Representation Generator
_PRIMITIVE_TYPES = (str, int, float, bool, type(None))
_TYPE_MARKER_DICT = 0
_TYPE_MARKER_LIST = 1
_TYPE_MARKER_TUPLE = 2
_TYPE_MARKER_SET = 3
_TYPE_MARKER_FROZENSET = 4
_TYPE_MARKER_PYDANTIC = 5
_TYPE_MARKER_MSGSPEC = 6
_TYPE_MARKER_MEMORYVIEW = 7


def _through_container(item: Any, active: set[int], build: Callable[[], Any]) -> Any:
    """Build *item*'s representation while it is on the path, refusing a cycle.

    A container that is reached again before its own representation is built holds
    itself, so no finite representation exists. Recording the ids on the way down and
    raising ``TypeError`` on the repeat turns an unbounded descent into the same
    failure a value reports when it cannot be keyed, instead of reaching the recursion
    limit. The id is dropped on the way back up, so the same object may legitimately
    appear twice in a value that is not cyclic.
    """
    identity = id(item)
    if identity in active:
        raise TypeError(
            f"cyclic {type(item).__name__} has no content hash; its representation "
            "would not terminate"
        )
    active.add(identity)
    try:
        return build()
    finally:
        active.discard(identity)


def _generate_hashable_representation(item: Any) -> Any:
    """Convert object to a stable, order-independent hashable representation
    (dicts/sets become sorted tuples tagged with a type marker).

    A cyclic container raises ``TypeError``: no finite representation exists for it, so
    it reports the same failure a value reports when it cannot be keyed rather than
    descending until the recursion limit. A model whose own serializer cannot build a
    value -- a cycle it detects, or nesting past the recursion limit -- raises
    ``TypeError`` for the same reason.
    """
    return _hashable_representation(item, set())


def _builtins_of_model(item: Any, build: Callable[[], Any]) -> Any:
    """Return *build()*'s mapping, or raise ``TypeError`` when it cannot be built.

    ``msgspec.to_builtins`` reaches the recursion limit on a value that holds itself or
    nests past it, and Pydantic's ``model_dump`` refuses a model that holds itself with
    ``ValueError``, before this module can walk it, so the failure is reported as the
    same ``TypeError`` a value that cannot be keyed reports, and the equality fallback
    takes it. Any other error from the model's own serializer, a ``ValueError``
    included, propagates.
    """
    try:
        return build()
    except RecursionError as exc:
        raise TypeError(f"cannot key a cyclic or too deeply nested {type(item).__name__}") from exc
    except ValueError as exc:
        if not _is_circular_reference(exc):
            raise
        raise TypeError(f"cannot key a cyclic or too deeply nested {type(item).__name__}") from exc


def _hashable_representation(item: Any, active: set[int]) -> Any:
    if isinstance(item, _PRIMITIVE_TYPES):
        return item

    # Handle msgspec Structs (optional dependency)
    if _MsgspecStruct is not None and isinstance(item, _MsgspecStruct):
        return _through_container(
            item,
            active,
            lambda: (
                _TYPE_MARKER_MSGSPEC,
                _hashable_representation(
                    _builtins_of_model(item, lambda: _msgspec.to_builtins(item)), active
                ),
            ),
        )

    if PydanticBaseModel and isinstance(item, PydanticBaseModel):
        return _through_container(
            item,
            active,
            lambda: (
                _TYPE_MARKER_PYDANTIC,
                _hashable_representation(
                    _builtins_of_model(item, lambda: item.model_dump()), active
                ),
            ),
        )

    if isinstance(item, dict):
        return _through_container(
            item,
            active,
            lambda: (
                _TYPE_MARKER_DICT,
                tuple(
                    (str(k), _hashable_representation(v, active))
                    for k, v in sorted(item.items(), key=lambda x: str(x[0]))
                ),
            ),
        )

    if isinstance(item, list):
        return _through_container(
            item,
            active,
            lambda: (
                _TYPE_MARKER_LIST,
                tuple(_hashable_representation(elem, active) for elem in item),
            ),
        )

    if isinstance(item, tuple):
        return _through_container(
            item,
            active,
            lambda: (
                _TYPE_MARKER_TUPLE,
                tuple(_hashable_representation(elem, active) for elem in item),
            ),
        )

    # frozenset must be checked before set
    if isinstance(item, frozenset):
        return _through_container(
            item, active, lambda: _set_representation(item, active, _TYPE_MARKER_FROZENSET)
        )

    if isinstance(item, set):
        return _through_container(
            item, active, lambda: _set_representation(item, active, _TYPE_MARKER_SET)
        )

    if isinstance(item, memoryview):
        # A memoryview's string is its address, but it compares equal to any view or
        # bytes object of the same shape holding equal values, whatever the format, so
        # it is represented by those values: a one-dimensional view of byte values as the
        # bytes it equals, any other view by its shape and values. A released view, or
        # one whose format cannot be unpacked, equals only itself and is represented by
        # its string.
        with contextlib.suppress(ValueError, NotImplementedError):
            values = item.tolist()
            if item.ndim == 1 and all(
                isinstance(value, int | float) and 0 <= value <= 255 and value == int(value)
                for value in values
            ):
                return _hashable_representation(bytes(int(value) for value in values), active)
            return (_TYPE_MARKER_MEMORYVIEW, item.shape, _hashable_representation(values, active))

    with contextlib.suppress(Exception):
        return str(item)
    with contextlib.suppress(Exception):
        return repr(item)

    return f"<unhashable:{type(item).__name__}:{id(item)}>"


def _set_representation(item: Any, active: set[int], marker: int) -> Any:
    try:
        sorted_elements = sorted(item)
    except TypeError:
        sorted_elements = sorted(item, key=lambda x: (str(type(x)), str(x)))
    return (
        marker,
        tuple(_hashable_representation(elem, active) for elem in sorted_elements),
    )


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
