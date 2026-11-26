# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared row-bound process identity and quarantine rules."""

from __future__ import annotations

import json
import math
import socket
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

UNVERIFIABLE_PID_QUARANTINE_THRESHOLD = 3


class QuarantineProbeError(RuntimeError):
    """A required quarantine identity probe failed."""


def quarantine_identity(row: dict[str, Any]) -> tuple[int, float | None, str | None] | None:
    """Return the metadata-backed process identity, or None if it cannot be bound."""
    if not isinstance(row, dict):
        return None
    try:
        metadata = row.get("node_metadata")
        if not isinstance(metadata, dict):
            return None
        raw_pid = metadata.get("pid")
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (int, str)):
            return None
        try:
            pid = int(raw_pid)
        except (ValueError, TypeError, OverflowError):
            return None
        if pid <= 0:
            return None
        raw_created = metadata.get("pid_create_time")
        created = None
        if raw_created is not None and not isinstance(raw_created, bool):
            try:
                candidate = float(raw_created)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if math.isfinite(candidate) and candidate > 0:
                    created = candidate
        # A tuple that changes and reverts between sweeps is unobservable.
        generation = row.get("cli_worker_generation")
        if generation is not None and not isinstance(generation, str):
            return None
        return pid, created, generation
    except Exception:
        # Metadata is untrusted; unexpected conversion or mapping failures
        # must not make a row appear safely identified.
        return None


def encode_identity(pid: Any, create_time: Any, generation: Any) -> str:
    """Encode a canonical identity, rejecting an invalid PID or generation."""
    identity = quarantine_identity(
        {
            "node_metadata": {"pid": pid, "pid_create_time": create_time},
            "cli_worker_generation": generation,
        }
    )
    if identity is None:
        raise ValueError("invalid quarantine identity")
    return json.dumps(identity, separators=(",", ":"), allow_nan=False)


def is_current_quarantine(row: dict[str, Any], *, raise_on_probe_error: bool = False) -> bool:
    """Whether a running row's canonical marker names its current local identity."""
    if not isinstance(row, dict):
        return False
    try:
        metadata = row.get("node_metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError, RecursionError):
                return False
        if not isinstance(metadata, dict):
            return False
        if row.get("status") != "running" or metadata.get("unverifiable_quarantined") is not True:
            return False
        count = metadata.get("unverifiable_count")
        epoch = metadata.get("unverifiable_epoch")
        if (
            type(count) is not int
            or count < UNVERIFIABLE_PID_QUARANTINE_THRESHOLD
            or type(epoch) is not int
            or epoch < 0
        ):
            return False
        if "process_identity_mode" in metadata and metadata["process_identity_mode"] != "local":
            return False
        host = metadata.get("pid_host")
        # A present host that is not non-empty text cannot be scoped; null reads as absent.
        if host is not None and (not isinstance(host, str) or not host):
            return False
        if isinstance(host, str) and host:
            probed, current_host = run_probe(socket.gethostname)
            if not probed:
                if raise_on_probe_error:
                    raise QuarantineProbeError("quarantine host identity probe failed")
                return False
            if host != current_host:
                return False
        identity = quarantine_identity({**row, "node_metadata": metadata})
        if identity is None:
            return False
        return metadata.get("unverifiable_identity") == encode_identity(*identity)
    except QuarantineProbeError:
        raise
    except Exception:
        # A malformed row or an unavailable host identity must not authorize
        # a disposition or display an unverified current quarantine.
        return False


def run_probe(
    probe: Callable[[], T],
    *,
    catch: type[BaseException] | tuple[type[BaseException], ...] = Exception,
) -> tuple[bool, T | None]:
    """Run ``probe`` and report ``(True, value)``, or ``(False, None)`` when it raised ``catch``.

    A caller that turns a failed probe into :class:`QuarantineProbeError` raises it at its own
    call site, which has no ``except`` block, so the public error never carries the probe's
    exception (and its private detail) as ``__context__``.
    """
    try:
        return True, probe()
    except catch:
        pass
    return False, None


def _quarantine_record_status(row: dict[str, Any]) -> str | None:
    """Classify quarantine metadata for display and disposition safeguards."""
    if not isinstance(row, dict) or row.get("status") != "running":
        return None
    metadata = row.get("node_metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError, RecursionError):
            return None
    if not isinstance(metadata, dict) or metadata.get("unverifiable_quarantined") is not True:
        return None
    host = metadata.get("pid_host")
    if host is not None and (not isinstance(host, str) or not host):
        return "unreadable"

    # A valid record can name this host or a different one. Omit only the host
    # while checking the complete canonical identity and history; a foreign
    # host does not excuse an invalid token, count, epoch, or process mode.
    hostless = {key: value for key, value in metadata.items() if key != "pid_host"}
    identity = quarantine_identity({**row, "node_metadata": hostless})
    if identity is None:
        return "unreadable"
    token = metadata.get("unverifiable_identity")
    try:
        decoded_token = json.loads(token) if isinstance(token, str) else None
        canonical_token = json.dumps(decoded_token, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return "unreadable"
    if not isinstance(decoded_token, list) or len(decoded_token) != 3 or canonical_token != token:
        return "unreadable"
    try:
        if encode_identity(*decoded_token) != token:
            return "unreadable"
    except (TypeError, ValueError):
        return "unreadable"
    if token != encode_identity(*identity):
        return "stale"
    if not is_current_quarantine({**row, "node_metadata": hostless}):
        return "unreadable"
    host = metadata.get("pid_host")
    if isinstance(host, str) and host:
        try:
            socket.gethostname()
        except Exception:
            # If local scope cannot be determined, retain the existing warning.
            return "probe_failed"
    # A canonical record whose identity no longer matches is stale, not corrupt.
    return None


_STATUS_LABELS = {
    "unreadable": "quarantine record unreadable",
    "probe_failed": "quarantine host probe failed",
    "stale": "quarantine record stale",
}


def _quarantine_status_label(row: dict[str, Any]) -> str | None:
    """The display label for a running quarantine record that is not current, else None."""
    return _STATUS_LABELS.get(_quarantine_record_status(row))
