# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read/write helper for the plugin-related blocks of ``~/.lionagi/settings.yaml``.

Trust records and the enable/disable flag are both user-level, never
project-level: a repository must not be able to self-trust a plugin it
carries by committing a settings line — the human on the machine approves.

Every mutator in this package (GC, trust, enable/disable) goes through
``locked_user_settings()``, a single exclusive-``flock`` critical section
held across the whole read-modify-write, so a concurrent writer's stale
snapshot can never silently clobber another's write.
"""

from __future__ import annotations

import contextlib
import copy
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from lionagi._paths import ensure_lionagi_dir

if sys.platform == "win32":
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:  # pragma: no cover - only reached by cross-platform import simulation
        _msvcrt = None
else:
    import fcntl as _fcntl

    _msvcrt = None

__all__ = (
    "locked_user_settings",
    "read_user_settings",
    "user_settings_path",
    "write_user_settings",
)


def user_settings_path() -> Path:
    return Path.home() / ".lionagi" / "settings.yaml"


def _load_yaml_dict(raw: str) -> dict[str, Any]:
    data = yaml.safe_load(raw) if raw.strip() else {}
    return data if isinstance(data, dict) else {}


def _lock_file(fp, *, exclusive: bool) -> None:
    if _fcntl is not None:
        mode = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
        _fcntl.flock(fp.fileno(), mode)
        return
    if _msvcrt is not None:
        position = fp.tell()
        fp.seek(0)
        mode = _msvcrt.LK_LOCK if exclusive else _msvcrt.LK_RLCK
        _msvcrt.locking(fp.fileno(), mode, 1)
        fp.seek(position)


def _unlock_file(fp) -> None:
    if _fcntl is not None:
        _fcntl.flock(fp.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        position = fp.tell()
        fp.seek(0)
        _msvcrt.locking(fp.fileno(), _msvcrt.LK_UNLCK, 1)
        fp.seek(position)


def read_user_settings() -> dict[str, Any]:
    """Snapshot read under a shared lock — safe against a concurrent writer's
    truncate-then-rewrite (see ``locked_user_settings``)."""
    path = user_settings_path()
    if not path.is_file():
        return {}
    with open(path) as f:
        _lock_file(f, exclusive=False)
        try:
            raw = f.read()
        finally:
            _unlock_file(f)
    return _load_yaml_dict(raw)


def write_user_settings(data: dict[str, Any]) -> None:
    """Unconditional whole-file rewrite under an exclusive lock.

    Safe as a standalone call (never tears a concurrent read), but callers
    that need to read-modify-write — GC, trust, enable/disable — must use
    ``locked_user_settings()`` instead: this function's lock only spans the
    write, not the read that preceded it, so two independent read/write
    pairs can still race each other.
    """
    path = user_settings_path()
    ensure_lionagi_dir(path.parent)
    mode = "r+" if path.is_file() else "w+"
    with open(path, mode) as fp:
        _lock_file(fp, exclusive=True)
        try:
            fp.seek(0)
            fp.truncate()
            yaml.safe_dump(data, fp, sort_keys=False, allow_unicode=True)
            fp.flush()
            os.fsync(fp.fileno())
        finally:
            _unlock_file(fp)


@contextlib.contextmanager
def locked_user_settings():
    """Read-modify-write ``~/.lionagi/settings.yaml`` under one exclusive
    POSIX lock held for the whole critical section — the choke point every
    settings mutator must go through so a concurrent pair can never
    interleave and drop one's write.

    Yields the parsed settings dict; mutate it in place. Written back only
    if it changed, so a no-op pass touches neither the file's mtime nor a
    concurrent reader.

    Opens with ``O_CREAT`` but never ``O_TRUNC``, since truncating before
    the lock is held could blow away content a racing creator already
    committed; truncation only happens below, after the lock is held.
    """
    path = user_settings_path()
    ensure_lionagi_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as fp:
        _lock_file(fp, exclusive=True)
        try:
            fp.seek(0)
            data = _load_yaml_dict(fp.read())
            before = copy.deepcopy(data)
            yield data
            if data == before:
                return
            fp.seek(0)
            fp.truncate()
            yaml.safe_dump(data, fp, sort_keys=False, allow_unicode=True)
            fp.flush()
            os.fsync(fp.fileno())
        finally:
            _unlock_file(fp)
