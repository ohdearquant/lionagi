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

import threading
import time

from lionagi.plugins._user_settings import locked_user_settings, read_user_settings


def test_locked_user_settings_serializes_concurrent_writers(plugin_home):
    """Two concurrent read-modify-write sections -- one shaped like GC pruning a
    trust record, one shaped like `li plugin disable` setting `enabled: false` --
    must both land. Neither may be lost to the other's stale read, which is
    exactly what happened before GC's mutation was locked (finding #2)."""
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


def test_locked_user_settings_no_op_writes_nothing(plugin_home):
    """A pass that reads but doesn't mutate the yielded dict must not touch the
    file -- GC's "nothing stale" path must stay a true no-op, not a rewrite
    that just happens to write back identical content."""
    from lionagi.plugins._user_settings import user_settings_path

    with locked_user_settings() as settings:
        settings.setdefault("trusted_plugins", {})["seed"] = {"manifest": "x"}

    mtime_before = user_settings_path().stat().st_mtime_ns

    with locked_user_settings() as settings:
        _ = settings.get("trusted_plugins", {})  # read-only pass

    mtime_after = user_settings_path().stat().st_mtime_ns
    assert mtime_after == mtime_before
