# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Immutable checkpoint values and the pure journal record codec.

This module does not read or write checkpoint files. Its caller owns record
framing, admission order, durability, and the recovery of a coherent file pair.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = (
    "CodecError",
    "FrozenArray",
    "FrozenObject",
    "JournalRecord",
    "PatchOp",
    "apply_patch",
    "capture_context",
    "decode_json",
    "decode_record",
    "diff_context",
    "encode_record",
    "freeze_json",
    "thaw_json",
    "validate_entry",
)


class CodecError(ValueError):
    """A sanitized codec failure with a stable recovery reason."""

    def __init__(self, reason: str = "invalid_record") -> None:
        self.reason = reason
        super().__init__(reason)


_WIDTH = 32
_BITS = 5
_MASK = _WIDTH - 1
_MISSING = object()
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_INDEX = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_MAX_SEQUENCE = 2**63 - 1


@dataclass(frozen=True, slots=True)
class _VectorNode:
    children: tuple[Any, ...]
    _height: int = field(init=False, repr=False)
    _size: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.children) is not tuple or len(self.children) > _WIDTH:
            raise CodecError()
        if any(
            type(child) is not _VectorNode and not _immutable_json(child) for child in self.children
        ):
            raise CodecError()
        branches = [child for child in self.children if type(child) is _VectorNode]
        if branches:
            if len(branches) != len(self.children):
                raise CodecError()
            height = branches[0]._height
            if any(child._height != height for child in branches):
                raise CodecError()
            capacity = _WIDTH ** (height + 1)
            if any(child._size != capacity for child in branches[:-1]) or branches[-1]._size == 0:
                raise CodecError()
            object.__setattr__(self, "_height", height + 1)
            object.__setattr__(self, "_size", sum(child._size for child in branches))
        else:
            object.__setattr__(self, "_height", 0)
            object.__setattr__(self, "_size", len(self.children))


@dataclass(frozen=True, slots=True, eq=False)
class FrozenArray:
    """A persistent 32-way vector; updates share unaffected trie branches."""

    _root: _VectorNode
    _depth: int
    _length: int

    def __post_init__(self) -> None:
        if (
            type(self._root) is not _VectorNode
            or type(self._depth) is not int
            or type(self._length) is not int
        ):
            raise CodecError()
        if (
            self._depth < 0
            or self._length < 0
            or (self._length - 1).bit_length() > _BITS * (self._depth + 1)
        ):
            raise CodecError()
        if (self._length == 0) != (len(self._root.children) == 0):
            raise CodecError()
        if self._depth != self._root._height or self._length != self._root._size:
            raise CodecError()

    def __eq__(self, other: object) -> bool:
        return _frozen_equal(self, other)

    __hash__ = None

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, index: int) -> Any:
        if not isinstance(index, int) or index < 0 or index >= self._length:
            raise IndexError(index)
        node = self._root
        for level in range(self._depth, 0, -1):
            node = node.children[(index >> (_BITS * level)) & _MASK]
        return node.children[index & _MASK]

    def __iter__(self):
        for index in range(self._length):
            yield self[index]


@dataclass(frozen=True, slots=True, eq=False)
class FrozenObject:
    """Hold JSON members in captured order.

    A pure member reorder emits no patch, so replay retains the base order.
    """

    _items: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        if type(self._items) is not tuple:
            raise CodecError()
        names = set()
        for pair in self._items:
            if type(pair) is not tuple or len(pair) != 2:
                raise CodecError()
            key, value = pair
            if type(key) is not str or key in names or not _immutable_json(value):
                raise CodecError()
            names.add(key)

    def __eq__(self, other: object) -> bool:
        return _frozen_equal(self, other)

    __hash__ = None

    def __getitem__(self, key: str) -> Any:
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (name for name, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True, slots=True, eq=False)
class PatchOp:
    op: str
    path: str
    value: Any = None

    def __post_init__(self) -> None:
        if (
            type(self.op) is not str
            or type(self.path) is not str
            or not _immutable_json(self.value)
        ):
            raise CodecError()

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is PatchOp
            and self.op == other.op
            and self.path == other.path
            and _frozen_equal(self.value, other.value)
        )

    __hash__ = None


@dataclass(frozen=True, slots=True, eq=False)
class JournalRecord:
    generation: str
    seq: int
    kind: str
    entry: FrozenObject
    patch: tuple[PatchOp, ...] | None = None

    def __post_init__(self) -> None:
        if not all(
            _immutable_json(value) for value in (self.generation, self.seq, self.kind, self.entry)
        ):
            raise CodecError()
        if self.patch is not None and (
            type(self.patch) is not tuple or any(type(op) is not PatchOp for op in self.patch)
        ):
            raise CodecError()

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is JournalRecord
            and _frozen_equal(self.generation, other.generation)
            and _frozen_equal(self.seq, other.seq)
            and _frozen_equal(self.kind, other.kind)
            and _frozen_equal(self.entry, other.entry)
            and self.patch == other.patch
        )

    __hash__ = None


def _immutable_json(value: Any) -> bool:
    if type(value) in (FrozenObject, FrozenArray):
        return True
    if value is None or type(value) in (bool, int, str):
        return True
    return type(value) is float and math.isfinite(value)


def _frozen_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is FrozenObject:
        before, after = dict(left._items), dict(right._items)
        return before.keys() == after.keys() and all(
            _frozen_equal(value, after[key]) for key, value in before.items()
        )
    if type(left) is FrozenArray:
        return len(left) == len(right) and all(
            _frozen_equal(first, second) for first, second in zip(left, right, strict=True)
        )
    return _same_scalar(left, right)


def _empty_array() -> FrozenArray:
    return FrozenArray(_VectorNode(()), 0, 0)


def _put(node: _VectorNode, depth: int, index: int, value: Any) -> _VectorNode:
    slot = (index >> (_BITS * depth)) & _MASK
    children = list(node.children)
    if depth == 0:
        if slot == len(children):
            children.append(value)
        else:
            children[slot] = value
    elif slot == len(children):
        children.append(_put(_VectorNode(()), depth - 1, index, value))
    else:
        children[slot] = _put(children[slot], depth - 1, index, value)
    return _VectorNode(tuple(children))


def _array_put(array: FrozenArray, index: int, value: Any) -> FrozenArray:
    if index < 0 or index > len(array):
        raise CodecError()
    root, depth = array._root, array._depth
    if index == len(array) and index == _WIDTH ** (depth + 1):
        root, depth = _VectorNode((root,)), depth + 1
    root = _put(root, depth, index, value)
    return FrozenArray(root, depth, max(len(array), index + 1))


def _trim(node: _VectorNode, depth: int, count: int) -> _VectorNode:
    if count == 0:
        return _VectorNode(())
    if depth == 0:
        return _VectorNode(node.children[:count])
    capacity = _WIDTH**depth
    whole, remaining = divmod(count, capacity)
    children = list(node.children[:whole])
    if remaining:
        children.append(_trim(node.children[whole], depth - 1, remaining))
    return _VectorNode(tuple(children))


def _array_take(array: FrozenArray, count: int) -> FrozenArray:
    if count == len(array):
        return array
    if count == 0:
        return _empty_array()
    return FrozenArray(_trim(array._root, array._depth, count), array._depth, count)


def _same_scalar(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, float) and left == 0.0 and right == 0.0:
        return math.copysign(1.0, left) == math.copysign(1.0, right)
    return left == right


def _json_key(key: Any) -> str:
    if isinstance(key, str):
        return str.__str__(key)
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, int):
        return int.__repr__(key)
    if isinstance(key, float):
        value = float.__float__(key)
        if not math.isfinite(value):
            raise CodecError()
        return json.dumps(value, allow_nan=False)
    raise CodecError()


def _freeze(value: Any, previous: Any, active: set[int]) -> Any:
    if value is None or isinstance(value, (bool, str, int, float)):
        if value is None:
            result = None
        elif isinstance(value, bool):
            result = bool(value)
        elif isinstance(value, str):
            result = str.__str__(value)
        elif isinstance(value, int):
            result = int.__int__(value)
        else:
            result = float.__float__(value)
            if not math.isfinite(result):
                raise CodecError()
        return previous if previous is not _MISSING and _same_scalar(result, previous) else result

    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise CodecError()
        active.add(identity)
        try:
            old = dict(previous._items) if isinstance(previous, FrozenObject) else {}
            items = []
            names = set()
            for key, item in value.items():
                name = _json_key(key)
                if name in names:
                    raise CodecError()
                names.add(name)
                items.append((name, _freeze(item, old.get(name, _MISSING), active)))
            if isinstance(previous, FrozenObject) and len(items) == len(previous._items):
                if all(name in old and item is old[name] for name, item in items):
                    return previous
            return FrozenObject(tuple(items))
        finally:
            active.remove(identity)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise CodecError()
        active.add(identity)
        try:
            old = previous if isinstance(previous, FrozenArray) else _empty_array()
            result = _array_take(old, min(len(old), len(value)))
            for index, item in enumerate(value):
                before = old[index] if index < len(old) else _MISSING
                captured = _freeze(item, before, active)
                if index >= len(result) or captured is not before:
                    result = _array_put(result, index, captured)
            return result
        finally:
            active.remove(identity)

    try:
        converted = str(value)
    except Exception:
        raise CodecError() from None
    return previous if previous is not _MISSING and _same_scalar(converted, previous) else converted


def freeze_json(value: Any, previous: Any = _MISSING) -> Any:
    """Capture normalized JSON, reusing unchanged immutable nodes.

    The caller must prevent concurrent mutation by another thread. Unsupported
    non-container values use the legacy string fallback. Cycles and colliding
    normalized keys are refused. No arbitrary depth limit is imposed.
    """
    try:
        return _freeze(value, previous, set())
    except Exception:
        # User-defined container hooks can raise CodecError with arbitrary
        # payload, so never trust an exception message from normalization.
        raise CodecError() from None


def thaw_json(value: Any) -> Any:
    """Return detached JSON data suitable for encoding or application."""
    try:
        if isinstance(value, FrozenObject):
            return {key: thaw_json(item) for key, item in value._items}
        if isinstance(value, FrozenArray):
            return [thaw_json(item) for item in value]
        if value is None or type(value) in (bool, int, float, str):
            if isinstance(value, float) and not math.isfinite(value):
                raise CodecError()
            return value
    except RecursionError:
        raise CodecError() from None
    raise CodecError()


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _diff(old: Any, new: Any, path: str, operations: list[PatchOp]) -> None:
    if old is new:
        return
    if isinstance(old, FrozenObject) and isinstance(new, FrozenObject):
        before, after = dict(old._items), dict(new._items)
        for key in before:
            if key not in after:
                operations.append(PatchOp("remove", f"{path}/{_escape(key)}"))
        for key, value in after.items():
            child_path = f"{path}/{_escape(key)}"
            if key not in before:
                operations.append(PatchOp("add", child_path, value))
            else:
                _diff(before[key], value, child_path, operations)
        return
    if isinstance(old, FrozenArray) and isinstance(new, FrozenArray):
        for index in range(min(len(old), len(new))):
            _diff(old[index], new[index], f"{path}/{index}", operations)
        for index in range(len(old) - 1, len(new) - 1, -1):
            operations.append(PatchOp("remove", f"{path}/{index}"))
        for index in range(len(old), len(new)):
            operations.append(PatchOp("add", f"{path}/-", new[index]))
        return
    if type(old) in (str, int, float, bool, type(None)) and _same_scalar(old, new):
        return
    operations.append(PatchOp("replace", path, new))


def diff_context(old: FrozenObject, new: FrozenObject) -> tuple[PatchOp, ...]:
    if not isinstance(old, FrozenObject) or not isinstance(new, FrozenObject):
        raise CodecError()
    operations: list[PatchOp] = []
    try:
        _diff(old, new, "", operations)
    except RecursionError:
        raise CodecError() from None
    return tuple(operations)


def capture_context(
    live: dict[str, Any], previous: FrozenObject | None = None
) -> tuple[FrozenObject, tuple[PatchOp, ...]]:
    """Synchronously capture a context and its immutable delta."""
    if not isinstance(live, dict) or (
        previous is not None and not isinstance(previous, FrozenObject)
    ):
        raise CodecError()
    baseline = previous if previous is not None else FrozenObject(())
    current = freeze_json(live, baseline)
    return current, diff_context(baseline, current)


def _pointer(path: str) -> tuple[str, ...]:
    if not isinstance(path, str):
        raise CodecError()
    if path == "":
        return ()
    if not path.startswith("/"):
        raise CodecError()
    tokens = []
    for token in path[1:].split("/"):
        output = []
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in "01":
                    raise CodecError()
                output.append("~" if token[index + 1] == "0" else "/")
                index += 2
            else:
                output.append(token[index])
                index += 1
        tokens.append("".join(output))
    return tuple(tokens)


def _array_index(token: str, length: int, *, add: bool) -> int:
    if token == "-" and add:  # noqa: S105
        return length
    if not _INDEX.fullmatch(token):
        raise CodecError()
    try:
        index = int(token)
    except ValueError:
        raise CodecError() from None
    if (add and index > length) or (not add and index >= length):
        raise CodecError()
    return index


def _array_from_values(values: list[Any]) -> FrozenArray:
    result = _empty_array()
    for value in values:
        result = _array_put(result, len(result), value)
    return result


def _apply(value: Any, tokens: tuple[str, ...], op: PatchOp) -> Any:
    if not tokens:
        if op.op != "replace" or not isinstance(op.value, FrozenObject):
            raise CodecError()
        return op.value
    token, remaining = tokens[0], tokens[1:]
    if isinstance(value, FrozenObject):
        items = dict(value._items)
        if remaining:
            if token not in items:
                raise CodecError()
            items[token] = _apply(items[token], remaining, op)
        elif op.op == "add":
            if token in items:
                raise CodecError()
            items[token] = op.value
        elif op.op == "remove":
            if token not in items:
                raise CodecError()
            del items[token]
        else:
            if token not in items:
                raise CodecError()
            items[token] = op.value
        return FrozenObject(tuple(items.items()))
    if isinstance(value, FrozenArray):
        index = _array_index(token, len(value), add=not remaining and op.op == "add")
        if remaining:
            return _array_put(value, index, _apply(value[index], remaining, op))
        if op.op == "replace":
            return _array_put(value, index, op.value)
        if op.op == "add" and index == len(value):
            return _array_put(value, index, op.value)
        values = list(value)
        if op.op == "add":
            values.insert(index, op.value)
        else:
            del values[index]
        return _array_from_values(values)
    raise CodecError()


def _validate_op(op: PatchOp) -> None:
    if not isinstance(op, PatchOp) or op.op not in ("add", "remove", "replace"):
        raise CodecError()
    tokens = _pointer(op.path)
    if not tokens and (op.op != "replace" or not isinstance(op.value, FrozenObject)):
        raise CodecError()
    if op.op != "remove":
        thaw_json(op.value)
    elif op.value is not None:
        raise CodecError()


def apply_patch(context: FrozenObject, patch: tuple[PatchOp, ...]) -> FrozenObject:
    """Apply a complete patch privately; on error the input is unchanged."""
    if not isinstance(context, FrozenObject) or not isinstance(patch, tuple):
        raise CodecError()
    result: Any = context
    try:
        for op in patch:
            _validate_op(op)
            result = _apply(result, _pointer(op.path), op)
    except RecursionError:
        raise CodecError() from None
    return result


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CodecError()
        result[key] = value
    return result


def _bad_constant(_value: str) -> None:
    raise CodecError()


def decode_json(data: bytes | str) -> Any:
    """Decode strict v3 JSON, rejecting duplicate names at any depth."""
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        value = json.loads(data, object_pairs_hook=_object_pairs, parse_constant=_bad_constant)
        return freeze_json(value)
    except CodecError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CodecError() from None


def validate_entry(kind: str, entry: FrozenObject) -> None:
    """Validate an outcome shared by base snapshots and journal records."""
    if kind not in ("op", "spawned"):
        raise CodecError("unsupported_kind")
    if not isinstance(entry, FrozenObject):
        raise CodecError()
    fields = dict(entry._items)
    identity = "agent_id" if kind == "op" else "node_id"
    if not isinstance(fields.get(identity), str) or not fields[identity]:
        raise CodecError()
    if fields.get("status") not in ("completed", "failed") or "response" not in fields:
        raise CodecError()
    if kind == "spawned":
        for name in (
            "operation",
            "assignee",
            "instruction",
            "parent_id",
            "spawn_id",
            "emitter_workload",
        ):
            if name in fields and fields[name] is not None and not isinstance(fields[name], str):
                raise CodecError()
        if (
            "emitter_contains_user_settings" in fields
            and type(fields["emitter_contains_user_settings"]) is not bool
        ):
            raise CodecError()
    thaw_json(entry)


def _validate_record(record: JournalRecord) -> None:
    if not isinstance(record.generation, str) or not _GENERATION.fullmatch(record.generation):
        raise CodecError()
    if type(record.seq) is not int or not 1 <= record.seq <= _MAX_SEQUENCE:
        raise CodecError("invalid_sequence")
    if record.kind not in ("op", "spawned"):
        raise CodecError("unsupported_kind")
    validate_entry(record.kind, record.entry)
    if record.patch is not None:
        if not isinstance(record.patch, tuple):
            raise CodecError()
        for op in record.patch:
            _validate_op(op)


def encode_record(
    generation: str,
    seq: int,
    kind: str,
    entry: FrozenObject,
    patch: tuple[PatchOp, ...] | None = None,
) -> bytes:
    """Encode one validated immutable journal transition including its LF."""
    record = JournalRecord(generation, seq, kind, entry, patch)
    _validate_record(record)
    obj: dict[str, Any] = {
        "version": 3,
        "generation": generation,
        "seq": seq,
        "kind": kind,
        "entry": thaw_json(entry),
    }
    if patch is not None:
        operations = []
        for op in patch:
            operation = {"op": op.op, "path": op.path}
            if op.op != "remove":
                operation["value"] = thaw_json(op.value)
            operations.append(operation)
        obj["flow_context"] = {"patch": operations}
    try:
        return (
            json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CodecError() from None


def decode_record(line: bytes | str) -> JournalRecord:
    """Decode one complete journal line; incomplete-tail policy belongs to the reader."""
    if not isinstance(line, (bytes, str)) or not line.endswith(
        b"\n" if isinstance(line, bytes) else "\n"
    ):
        raise CodecError()
    if line.count(b"\n" if isinstance(line, bytes) else "\n") != 1:
        raise CodecError()
    decoded = decode_json(line)
    if not isinstance(decoded, FrozenObject):
        raise CodecError()
    obj = dict(decoded._items)
    # A future version may introduce different members, so classify a
    # declared unsupported version before applying the v3-only schema.
    if "version" in obj and (type(obj["version"]) is not int or obj["version"] != 3):
        raise CodecError("unsupported_version")
    if set(obj) not in (
        {"version", "generation", "seq", "kind", "entry"},
        {"version", "generation", "seq", "kind", "entry", "flow_context"},
    ):
        raise CodecError()
    patch = None
    if "flow_context" in obj:
        container = obj["flow_context"]
        if not isinstance(container, FrozenObject) or set(container) != {"patch"}:
            raise CodecError()
        raw_patch = container["patch"]
        if not isinstance(raw_patch, FrozenArray):
            raise CodecError()
        operations = []
        for value in raw_patch:
            if not isinstance(value, FrozenObject):
                raise CodecError()
            fields = dict(value._items)
            operation = fields.get("op")
            expected = {"op", "path"} if operation == "remove" else {"op", "path", "value"}
            if set(fields) != expected:
                raise CodecError()
            operations.append(PatchOp(operation, fields["path"], fields.get("value")))
        patch = tuple(operations)
    record = JournalRecord(obj["generation"], obj["seq"], obj["kind"], obj["entry"], patch)
    _validate_record(record)
    return record
