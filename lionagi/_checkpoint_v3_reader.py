# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Journal-aware checkpoint recovery and version dispatch.

The caller must not execute a refused prefix. Recovery metadata is separate
from the logical checkpoint and error messages never include checkpoint data.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, NoReturn

from lionagi._checkpoint_v3_codec import (
    CodecError,
    FrozenObject,
    apply_patch,
    decode_json,
    decode_record,
    freeze_json,
    thaw_json,
    validate_entry,
)
from lionagi._checkpoint_version import (
    _SUPPORTED_CHECKPOINT_VERSIONS,
    UnsupportedCheckpointVersionError,
)
from lionagi.ln import copy

__all__ = (
    "CheckpointInvalidUTF8Error",
    "CheckpointMalformedBaseError",
    "CheckpointRecoveryError",
    "load_checkpoint_unit",
    "load_checkpoint_v3",
)

_GENERATION = re.compile(r"[0-9a-f]{32}\Z")


class CheckpointRecoveryError(ValueError):
    """A refused checkpoint with sanitized diagnostics and an optional prefix."""

    def __init__(
        self,
        report: dict[str, Any],
        valid_prefix: dict[str, Any] | None = None,
        *,
        detail: str | None = None,
    ):
        self.checkpoint_recovery = report
        self.valid_prefix = valid_prefix
        code = report["problem"]["code"]
        message = detail or (
            "Checkpoint version is not supported." if code == "unsupported_version" else code
        )
        super().__init__(message)


class CheckpointMalformedBaseError(CheckpointRecoveryError, json.JSONDecodeError):
    """Malformed, unclassifiable base without retaining its sensitive document."""

    def __init__(self, error: json.JSONDecodeError, byte_offset: int):
        self.msg = error.msg
        self.doc = ""
        self.pos = error.pos
        self.lineno = error.lineno
        self.colno = error.colno
        self.checkpoint_recovery = _report()
        self.checkpoint_recovery["problem"] = {
            "code": "invalid_record",
            "line": error.lineno,
            "byte_offset": byte_offset,
            "expected_seq": None,
            "actual_seq": None,
        }
        self.checkpoint_recovery["disposition"] = "refused"
        self.valid_prefix = None
        # The recovery and JSON parents require incompatible initializer arguments.
        # Initialize ValueError directly after assigning both public contracts, so
        # neither parent retains the input document or overwrites its offsets.
        ValueError.__init__(
            self, f"{self.msg}: line {self.lineno} column {self.colno} (char {self.pos})"
        )


class CheckpointInvalidUTF8Error(CheckpointRecoveryError, UnicodeDecodeError):
    """Invalid base encoding without retaining input bytes in the exception."""

    def __init__(self, error: UnicodeDecodeError):
        # Offsets refer to the original file, whereas .object is deliberately
        # empty so an exception cannot retain arbitrary checkpoint contents.
        UnicodeDecodeError.__init__(self, "utf-8", b"", error.start, error.end, error.reason)
        self.checkpoint_recovery = _report()
        self.checkpoint_recovery["problem"] = {
            "code": "invalid_record",
            "line": None,
            "byte_offset": error.start,
            "expected_seq": None,
            "actual_seq": None,
        }
        self.checkpoint_recovery["disposition"] = "refused"
        self.valid_prefix = None

    def __str__(self) -> str:
        return f"Invalid UTF-8 at byte offset {self.start}"

    def __repr__(self) -> str:
        return f"CheckpointInvalidUTF8Error(start={self.start}, end={self.end})"


def _decode_base_utf8(data: bytes) -> str:
    error = None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        error = CheckpointInvalidUTF8Error(exc)
    if error is not None:
        # The original UnicodeDecodeError retains all input in .object.
        # Raise outside its except block to avoid retaining it as context.
        raise error from None
    return text


def _report() -> dict[str, Any]:
    return {
        "report_version": 1,
        "source_version": None,
        "generation": None,
        "base_sequence": None,
        "last_applied_sequence": None,
        "applied_records": 0,
        "valid_prefix_bytes": None,
        "ignored": {"torn_final_record": 0, "stale_generation_record": 0},
        "problem": None,
        "disposition": "clean",
    }


def _refuse(
    report: dict[str, Any],
    code: str,
    *,
    line: int | None = None,
    byte_offset: int | None = None,
    expected_seq: int | None = None,
    actual_seq: int | None = None,
    prefix: dict[str, Any] | None = None,
    detail: str | None = None,
) -> NoReturn:
    report["problem"] = {
        "code": code,
        "line": line,
        "byte_offset": byte_offset,
        "expected_seq": expected_seq,
        "actual_seq": actual_seq,
    }
    report["disposition"] = "refused"
    raise CheckpointRecoveryError(report, copy(prefix), detail=detail) from None


def _identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _read_bound(path: Path) -> tuple[bytes, tuple[int, int]]:
    """Read an identity-bound regular file, using nonblocking open when available."""
    nonblock = getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, os.O_RDONLY | nonblock)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _CheckpointReadRaceError()
        with os.fdopen(fd, "rb") as stream:
            fd = -1  # The stream owns the descriptor from this point.
            data = stream.read(before.st_size)
            after = os.fstat(stream.fileno())
            if len(data) != before.st_size or after.st_size < before.st_size:
                raise _CheckpointReadRaceError()
            identity = _identity(before)
            if _identity(after) != identity or _identity(path.stat()) != identity:
                raise _CheckpointReadRaceError()
        return data, identity
    finally:
        if fd >= 0:
            os.close(fd)


class _CheckpointReadRaceError(Exception):
    pass


def _base(data: bytes, report: dict[str, Any]) -> dict[str, Any]:
    # A replaced base can differ from the dispatch snapshot. Preserve the
    # dual JSON/recovery error contract for this independently captured base.
    text = _decode_base_utf8(data)
    error = None
    root = None
    try:
        root = json.loads(text, object_pairs_hook=_ObjectPairs)
    except json.JSONDecodeError as exc:
        error = CheckpointMalformedBaseError(exc, len(text[: exc.pos].encode("utf-8")))
    except (ValueError, RecursionError):
        # Let the strict decoder classify non-syntax decoder failures.
        pass
    if error is not None:
        raise error from None
    # Attribute the version only to these captured bytes. The dispatch
    # snapshot may have been replaced while the coherent pair was captured.
    if isinstance(root, _ObjectPairs):
        versions = [value for key, value in root if key == "version"]
        if len(versions) == 1 and type(versions[0]) is int:
            report["source_version"] = versions[0]
    error_reason = None
    try:
        frozen = decode_json(data)
        state = thaw_json(frozen)
        if not isinstance(state, dict):
            raise CodecError()
        version = state.get("version")
        if type(version) is int:
            report["source_version"] = version
        if type(version) is not int or version != 3:
            raise CodecError("unsupported_version")
        report["source_version"] = 3
        generation = state.get("generation")
        if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
            raise CodecError()
        if type(state.get("journal_seq")) is not int or state["journal_seq"] != 0:
            raise CodecError("invalid_sequence")
        if (
            not isinstance(state.get("session_id"), str)
            or not isinstance(state.get("prompt"), str)
            or not isinstance(state.get("plan"), list)
            or any(not isinstance(item, dict) for item in state["plan"])
            or not isinstance(state.get("config"), dict)
            or not isinstance(state.get("flow_context"), dict)
            or not isinstance(state.get("ops"), dict)
            or not isinstance(state.get("spawned"), list)
            or ("max_spawn" in state and type(state["max_spawn"]) is not int)
        ):
            raise CodecError()
        for identity, entry in state["ops"].items():
            frozen_entry = freeze_json(entry)
            validate_entry("op", frozen_entry)
            if identity != entry["agent_id"]:
                raise CodecError()
        seen = set()
        for entry in state["spawned"]:
            frozen_entry = freeze_json(entry)
            validate_entry("spawned", frozen_entry)
            if entry["node_id"] in seen:
                raise CodecError()
            seen.add(entry["node_id"])
    except CodecError as exc:
        error_reason = exc.reason
    if error_reason is not None:
        # Raising outside the handler avoids retaining codec/parser exceptions
        # that can contain the original checkpoint data.
        _refuse(report, error_reason)
    report.update(
        generation=generation, base_sequence=0, last_applied_sequence=0, valid_prefix_bytes=0
    )
    return state


def _capture(path: Path, retries: int, report: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    journal = path.with_name(path.name + ".journal")
    reason = "incoherent_view"
    for _ in range(retries):
        report.clear()
        report.update(_report())
        try:
            base_bytes, base_id = _read_bound(path)
            # Validate only after a second binding/content read to avoid reporting
            # an obsolete malformed base during replacement.
            again, again_id = _read_bound(path)
            if base_id != again_id or base_bytes != again:
                raise _CheckpointReadRaceError()
            state = _base(base_bytes, report)
            try:
                journal_bytes, journal_id = _read_bound(journal)
            except FileNotFoundError:
                reason = "missing_journal"
                continue
            final_bytes, final_id = _read_bound(path)
            if (
                base_id != final_id
                or base_bytes != final_bytes
                or _identity(journal.stat()) != journal_id
            ):
                raise _CheckpointReadRaceError()
            return state, journal_bytes
        except CheckpointRecoveryError:
            raise
        except (OSError, _CheckpointReadRaceError):
            reason = "incoherent_view"
    report["valid_prefix_bytes"] = None
    _refuse(report, reason)


def load_checkpoint_v3(
    path: str | Path, *, retries: int = 4
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture and replay a v3 pair; this function performs no legacy dispatch.

    On refusal, ``CheckpointRecoveryError`` contains ``checkpoint_recovery``
    and, when a base was valid, a diagnostic-only ``valid_prefix`` state.
    """
    if type(retries) is not int or retries < 1:
        raise ValueError("retries must be a positive integer")
    return _load_checkpoint_v3(Path(path), retries)


def _load_checkpoint_v3(path: Path, retries: int) -> tuple[dict[str, Any], dict[str, Any]]:
    report = _report()
    state, journal = _capture(path, retries, report)
    error_reason = None
    try:
        context = freeze_json(state["flow_context"])
        if not isinstance(context, FrozenObject):
            raise CodecError()
    except CodecError as exc:
        error_reason = exc.reason
    if error_reason is not None:
        _refuse(report, error_reason, prefix=state)

    def valid_prefix() -> dict[str, Any]:
        return {**state, "flow_context": thaw_json(context)}

    offset = 0
    parts = journal.split(b"\n")
    for number, part in enumerate(parts, start=1):
        if number == len(parts):
            if not part:
                break
            report["ignored"]["torn_final_record"] = 1
            report["problem"] = {
                "code": "torn_final_record",
                "line": number,
                "byte_offset": offset,
                "expected_seq": None,
                "actual_seq": None,
            }
            report["disposition"] = "recovered_prefix"
            break
        line = part + b"\n"
        error_reason = None
        try:
            record = decode_record(line)
        except CodecError as exc:
            error_reason = exc.reason
        if error_reason is not None:
            _refuse(report, error_reason, line=number, byte_offset=offset, prefix=valid_prefix())
        if record.generation != report["generation"]:
            report["ignored"]["stale_generation_record"] += 1
            report["disposition"] = "recovered_prefix"
        else:
            expected = report["last_applied_sequence"] + 1
            if record.seq != expected:
                _refuse(
                    report,
                    "invalid_sequence",
                    line=number,
                    byte_offset=offset,
                    expected_seq=expected,
                    actual_seq=record.seq,
                    prefix=valid_prefix(),
                )
            error_reason = None
            try:
                candidate_context = context
                if record.patch is not None:
                    candidate_context = apply_patch(context, record.patch)
                entry = thaw_json(record.entry)
            except CodecError as exc:
                error_reason = exc.reason
            if error_reason is not None:
                _refuse(
                    report, error_reason, line=number, byte_offset=offset, prefix=valid_prefix()
                )
            # All validation is complete before outcome and context are published.
            if record.kind == "op":
                state["ops"][entry["agent_id"]] = entry
            else:
                for index, prior in enumerate(state["spawned"]):
                    if prior["node_id"] == entry["node_id"]:
                        state["spawned"][index] = entry
                        break
                else:
                    state["spawned"].append(entry)
            context = candidate_context
            report["last_applied_sequence"] = record.seq
            report["applied_records"] += 1
        offset += len(line)
        report["valid_prefix_bytes"] = offset
    state["flow_context"] = thaw_json(context)
    return state, report


class _ObjectPairs(list):
    """Mark objects during dispatch, without changing legacy duplicate semantics."""


def _legacy_from_pairs(root: Any) -> Any:
    """Convert the already-decoded tree to legacy last-key-wins objects iteratively."""

    def container(value: Any) -> Any:
        if isinstance(value, _ObjectPairs):
            return {}
        if isinstance(value, list):
            return []
        return value

    result = container(root)
    if isinstance(root, (_ObjectPairs, list)):
        stack = [(root, result)]
        while stack:
            source, target = stack.pop()
            if isinstance(source, _ObjectPairs):
                for key, value in source:
                    converted = container(value)
                    target[key] = converted
                    if isinstance(value, (_ObjectPairs, list)):
                        stack.append((value, converted))
            else:
                for value in source:
                    converted = container(value)
                    target.append(converted)
                    if isinstance(value, (_ObjectPairs, list)):
                        stack.append((value, converted))
    return result


def load_checkpoint_unit(path: str | Path, *, retries: int = 4) -> tuple[Any, dict[str, Any]]:
    """Explicitly dispatch a checkpoint; v3 always requires coherent replay.

    A base that cannot be bound after bounded retries, including an inaccessible
    path or directory in place of the file, is refused as ``incoherent_view``.
    """
    if type(retries) is not int or retries < 1:
        raise ValueError("retries must be a positive integer")
    report = _report()
    checkpoint_path = Path(path)
    for _ in range(retries):
        try:
            data, _ = _read_bound(checkpoint_path)
            break
        except (OSError, _CheckpointReadRaceError):
            pass
    else:
        _refuse(report, "incoherent_view")
    # A malformed document cannot reliably declare its version. Preserve the
    # legacy JSON and Unicode exception types rather than guessing v3.
    text = _decode_base_utf8(data)
    error = None
    resource_failure = None
    try:
        root = json.loads(text, object_pairs_hook=_ObjectPairs)
    except json.JSONDecodeError as exc:
        error = CheckpointMalformedBaseError(exc, len(text[: exc.pos].encode("utf-8")))
    except UnicodeError:
        raise
    except RecursionError:
        resource_failure = "nesting depth"
    except ValueError:
        resource_failure = "integer digit limit"
    if error is not None:
        # Raise outside the except block so the original exception, including
        # its sensitive .doc, is not retained in the exception context.
        raise error from None
    if resource_failure is not None:
        # Likewise, do not retain a decoder exception with sensitive input.
        _refuse(report, "invalid_record", detail=resource_failure)
    if isinstance(root, _ObjectPairs):
        versions = [value for key, value in root if key == "version"]
        if len(versions) > 1:
            raise UnsupportedCheckpointVersionError("Checkpoint version is ambiguous.")
        version = versions[0] if versions else None
        if versions and type(version) is int:
            report["source_version"] = version
        if versions and type(version) is int and version == 3:
            return _load_checkpoint_v3(checkpoint_path, retries)
        if versions and (type(version) is not int or version not in _SUPPORTED_CHECKPOINT_VERSIONS):
            raise UnsupportedCheckpointVersionError("Checkpoint version is not supported.")
    return _legacy_from_pairs(root), report
