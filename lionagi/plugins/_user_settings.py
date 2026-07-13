# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read/write helper for the plugin-related blocks of ``~/.lionagi/settings.yaml``.

Trust records (D5) and the enable/disable flag (D7) are both user-level, never
project-level: a repository must not be able to self-trust a plugin it
carries by committing a settings line — the human on the machine approves.
This mirrors ``lionagi.agent.settings.load_settings`` (which merges global
and project settings for *reading* hooks/config) but is scoped to writing the
one file a plugin operator actually controls.

Every mutator in this package (GC, trust, enable/disable) goes through
``locked_user_settings()`` — a single exclusive-``flock`` critical section
held across the whole read-modify-write. Two independent read-then-write
calls (the old shape: ``read_user_settings()`` ... mutate ... ``write_user_
settings()``) leave a window where a concurrent CLI invocation's write can
land in between and get silently clobbered by the first process's stale
snapshot; holding one lock across the full cycle closes that window (mirrors
``lionagi.cli.team._locked_team``).
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import os
from pathlib import Path
from typing import Any

import yaml

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


def read_user_settings() -> dict[str, Any]:
    """Snapshot read under a shared lock — safe against a concurrent writer's
    truncate-then-rewrite (see ``locked_user_settings``)."""
    path = user_settings_path()
    if not path.is_file():
        return {}
    with open(path) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            raw = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "r+" if path.is_file() else "w+"
    with open(path, mode) as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            fp.seek(0)
            fp.truncate()
            yaml.safe_dump(data, fp, sort_keys=False, allow_unicode=True)
            fp.flush()
            os.fsync(fp.fileno())
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def locked_user_settings():
    """Read-modify-write ``~/.lionagi/settings.yaml`` under one exclusive
    POSIX lock held for the whole critical section — the choke point every
    settings mutator (GC, trust, enable/disable) must go through so a
    concurrent pair of these can never interleave and drop one's write.

    Yields the parsed settings dict; mutate it in place. Written back only
    if it changed (compared against a snapshot taken before the yield), so a
    no-op pass (e.g. GC finding nothing stale) touches neither the file's
    mtime nor a concurrent reader.

    Opens with ``O_CREAT`` but never ``O_TRUNC``: on first creation, two
    concurrent callers racing to create the file must not truncate it before
    either holds the lock, or the loser's truncate can blow away content the
    winner already committed and unlocked. Truncation only happens below,
    after the lock is held and the (possibly just-written) content has been
    read.
    """
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
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
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
