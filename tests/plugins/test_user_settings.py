# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``~/.lionagi/settings.yaml`` read-modify-write lock (``locked_user_settings``).

The regression this guards: `li plugin list` runs a GC pass that used to do an
unlocked read of the whole settings document, mutate only `trusted_plugins`,
then write the original snapshot back. A concurrent `li plugin disable` (or
`trust`) write landing in that window would be silently overwritten by the
GC pass's stale copy of the rest of the document. `locked_user_settings()`
closes that window by holding one exclusive flock across the entire
read-modify-write, so two concurrent critical sections serialize instead of
interleaving.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from lionagi.plugins._user_settings import (
    locked_user_settings,
    read_user_settings,
    user_settings_path,
)


def test_locked_user_settings_serializes_concurrent_writers(plugin_home):
    """Two concurrent read-modify-write sections -- one shaped like GC pruning a
    trust record, one shaped like `li plugin disable` setting `enabled: false` --
    must both land. Neither may be lost to the other's stale read, which is
    exactly what happened before GC's mutation was locked."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def gc_like() -> None:
        try:
            barrier.wait(timeout=5)
            with locked_user_settings() as settings:
                # Hold the lock long enough that, absent the fix, the other
                # thread's unlocked write would have already landed and be
                # about to get clobbered by this section's write-back.
                time.sleep(0.05)
                trusted = settings.setdefault("trusted_plugins", {})
                trusted["web-research"] = {"manifest": "deadbeef", "targets": {}}
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors, not swallowed
            errors.append(exc)

    def disable_like() -> None:
        try:
            barrier.wait(timeout=5)
            with locked_user_settings() as settings:
                plugins_block = settings.setdefault("plugins", {})
                plugins_block["other-plugin"] = {"enabled": False}
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_gc = threading.Thread(target=gc_like)
    t_disable = threading.Thread(target=disable_like)
    t_gc.start()
    t_disable.start()
    t_gc.join(timeout=5)
    t_disable.join(timeout=5)

    assert not errors
    settings = read_user_settings()
    assert settings["trusted_plugins"]["web-research"]["manifest"] == "deadbeef"
    assert settings["plugins"]["other-plugin"]["enabled"] is False


def test_plugins_imports_on_win32_without_fcntl():
    script = """
import importlib
import sys
import lionagi.plugins

for module_name in (
    "lionagi.plugins",
    "lionagi.plugins.registry",
    "lionagi.plugins.trust",
    "lionagi.plugins._user_settings",
):
    sys.modules.pop(module_name, None)

sys.platform = "win32"
sys.modules["fcntl"] = None
importlib.import_module("lionagi.plugins")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_locked_user_settings_no_op_writes_nothing(plugin_home):
    """A pass that reads but doesn't mutate the yielded dict must not touch the
    file -- GC's "nothing stale" path must stay a true no-op, not a rewrite
    that just happens to write back identical content."""
    with locked_user_settings() as settings:
        settings.setdefault("trusted_plugins", {})["seed"] = {"manifest": "x"}

    mtime_before = user_settings_path().stat().st_mtime_ns

    with locked_user_settings() as settings:
        _ = settings.get("trusted_plugins", {})  # read-only pass

    mtime_after = user_settings_path().stat().st_mtime_ns
    assert mtime_after == mtime_before


def test_locked_user_settings_first_creation_race_does_not_lose_writes(plugin_home, monkeypatch):
    """The file-doesn't-exist-yet branch is its own race: the pre-fix code
    picked its open mode from a ``path.is_file()`` snapshot taken *before*
    the lock, and opened with ``w+`` (truncate-on-open) whenever that
    snapshot said "absent". A second writer that also observed "absent" --
    including one that only gets to open the file *after* the first writer
    already committed and unlocked -- would truncate that already-landed
    content the instant it opened, before ever touching the lock.

    We pin both writers onto that "absent" branch for the whole test by
    forcing ``Path.is_file`` to always return False, which reproduces the
    clobber deterministically regardless of real thread scheduling. The
    fixed implementation never branches on a pre-lock existence check --
    it always opens with ``O_CREAT`` and no ``O_TRUNC`` -- so this patch is
    inert against it."""
    assert not user_settings_path().is_file()

    a_committed = threading.Event()
    errors: list[BaseException] = []

    def writer_a() -> None:
        try:
            with locked_user_settings() as settings:
                trusted = settings.setdefault("trusted_plugins", {})
                trusted["writer-a"] = {"manifest": "aaaaaaaa", "targets": {}}
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            a_committed.set()

    def writer_b() -> None:
        try:
            # Wait for A's write to be fully committed and unlocked before
            # opening -- this is the exact window in which the pre-fix
            # open()-time truncate destroys A's content.
            assert a_committed.wait(timeout=5)
            with locked_user_settings() as settings:
                trusted = settings.setdefault("trusted_plugins", {})
                trusted["writer-b"] = {"manifest": "bbbbbbbb", "targets": {}}
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with monkeypatch.context() as m:
        m.setattr(Path, "is_file", lambda self: False)
        t_a = threading.Thread(target=writer_a)
        t_b = threading.Thread(target=writer_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

    assert not errors
    settings = read_user_settings()
    assert settings["trusted_plugins"]["writer-a"]["manifest"] == "aaaaaaaa"
    assert settings["trusted_plugins"]["writer-b"]["manifest"] == "bbbbbbbb"
